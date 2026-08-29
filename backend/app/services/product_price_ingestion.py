from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterator
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup
from opencc import OpenCC
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import SessionLocal
from ..models import (
    Product,
    ProductPriceHistory,
    ProductPriceOffer,
    ProductPriceSource,
    ProductPriceSyncRun,
    utcnow,
)
from .web_crawler import (
    MAX_HTML_BYTES,
    USER_AGENT,
    CrawlError,
    FetchError,
    SafeHttpClient,
    WebsiteCrawler,
    normalize_url,
)


SONGWIFI_SITE = "songwifi.com.hk"
_t2s = OpenCC("t2s.json")
PRODUCT_PATH_HINTS = (
    "/product",
    "/products",
    "/shop",
    "/store",
    "/item",
    "/plan",
    "/plans",
    "/pricing",
    "/esim",
    "/wifi",
)


@dataclass(slots=True)
class ScrapedOffer:
    external_key: str
    label: str
    currency: str
    price_amount: Decimal
    original_amount: Decimal | None = None
    unit: str = "item"
    duration_days: int | None = None
    data_label: str | None = None
    promo_label: str | None = None
    availability: str = "in_stock"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ScrapedProduct:
    external_key: str
    canonical_url: str
    name: str
    name_translations: dict[str, str]
    aliases: list[str]
    category: str
    product_type: str
    destination: str | None
    network: str | None
    description: str
    metadata: dict[str, Any]
    offers: list[ScrapedOffer]


@dataclass(slots=True)
class ProductSyncResult:
    status: str
    new_products: int = 0
    new_offers: int = 0
    changed_offers: int = 0
    unchanged_offers: int = 0
    missing_products: int = 0
    failed_pages: int = 0
    error_message: str | None = None


class NuxtDataError(CrawlError):
    pass


class _NuxtResolver:
    """Resolve the compact reference format used by Nuxt's devalue payload."""

    _WRAPPERS = {"Reactive", "ShallowReactive", "Ref", "EmptyRef"}

    def __init__(self, values: list[Any]) -> None:
        self.values = values
        self.memo: dict[int, Any] = {}

    def resolve(self, reference: int) -> Any:
        if reference < 0:
            return None
        if reference in self.memo:
            return self.memo[reference]
        try:
            value = self.values[reference]
        except IndexError as exc:
            raise NuxtDataError("网页商品数据引用无效") from exc
        if isinstance(value, dict):
            hydrated: dict[str, Any] = {}
            self.memo[reference] = hydrated
            for key, item in value.items():
                hydrated[key] = self.resolve(item) if isinstance(item, int) else item
            return hydrated
        if isinstance(value, list):
            if value and isinstance(value[0], str) and value[0] in self._WRAPPERS:
                hydrated = (
                    self.resolve(value[1])
                    if len(value) > 1 and isinstance(value[1], int)
                    else None
                )
                self.memo[reference] = hydrated
                return hydrated
            if value and value[0] == "Set":
                hydrated = [self.resolve(item) for item in value[1:] if isinstance(item, int)]
                self.memo[reference] = hydrated
                return hydrated
            hydrated_list: list[Any] = []
            self.memo[reference] = hydrated_list
            hydrated_list.extend(
                self.resolve(item) if isinstance(item, int) else item for item in value
            )
            return hydrated_list
        self.memo[reference] = value
        return value


def _nuxt_collections(markup: str, *keys: str) -> dict[str, Any]:
    soup = BeautifulSoup(markup, "html.parser")
    node = soup.select_one("#__NUXT_DATA__")
    if node is None:
        raise NuxtDataError("网页没有可读取的 Nuxt 商品数据")
    try:
        values = json.loads(node.get_text())
    except (TypeError, json.JSONDecodeError) as exc:
        raise NuxtDataError("网页商品数据格式无效") from exc
    if not isinstance(values, list):
        raise NuxtDataError("网页商品数据不是预期格式")
    mapping = next(
        (
            item
            for item in values
            if isinstance(item, dict) and all(key in item for key in keys)
        ),
        None,
    )
    if mapping is None:
        raise NuxtDataError("网页中没有找到商品价格集合")
    resolver = _NuxtResolver(values)
    return {
        key: resolver.resolve(mapping[key])
        for key in keys
        if isinstance(mapping.get(key), int)
    }


def _money(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        amount = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    if amount < 0:
        return None
    return amount.quantize(Decimal("0.01"))


def _language_value(langs: Any, language: str, key: str) -> str:
    if not isinstance(langs, list):
        return ""
    selected = next(
        (item for item in langs if isinstance(item, dict) and item.get("lang") == language),
        None,
    )
    if selected is None and langs:
        selected = langs[0] if isinstance(langs[0], dict) else None
    return str((selected or {}).get(key) or "").strip()


def _origin(root_url: str) -> str:
    parsed = urlsplit(root_url)
    return urlunsplit((parsed.scheme, parsed.netloc, "/", "", ""))


def _robots_parser(http: SafeHttpClient, root_url: str) -> RobotFileParser:
    parsed = urlsplit(root_url)
    robots_url = urlunsplit((parsed.scheme, parsed.netloc, "/robots.txt", "", ""))
    parser = RobotFileParser(robots_url)
    try:
        result = http.get(robots_url, max_bytes=512 * 1024)
        parser.parse(result.body.decode(result.encoding, errors="replace").splitlines())
    except FetchError as exc:
        if exc.status_code == 404:
            parser.parse(["User-agent: *", "Disallow:"])
        else:
            raise CrawlError("无法确认网站的 robots.txt 采集许可") from exc
    return parser


def _decode_markup(body: bytes, encoding: str) -> str:
    return body.decode(encoding or "utf-8", errors="replace")


def _plain_text(value: Any) -> str:
    if value in (None, ""):
        return ""
    return BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True)


def _shop_image_url(value: Any, *, base_url: str) -> str | None:
    if isinstance(value, str) and value.strip():
        try:
            return normalize_url(value, base_url=base_url)
        except CrawlError:
            return value.strip()
    if isinstance(value, dict):
        for key in ("source", "url", "path", "original", "image"):
            found = _shop_image_url(value.get(key), base_url=base_url)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = _shop_image_url(item, base_url=base_url)
            if found:
                return found
    return None


def _shop_offer_availability(variant: dict[str, Any]) -> str:
    unlimited = str(variant.get("unlimited_inventory") or "").strip().casefold()
    if unlimited in {"1", "true", "yes"}:
        return "in_stock"
    raw_quantity = variant.get("total_available")
    if raw_quantity in (None, "") and "unlimited_inventory" not in variant:
        return "in_stock"
    try:
        quantity = Decimal(str(raw_quantity or 0))
    except (InvalidOperation, ValueError):
        quantity = Decimal(0)
    return "in_stock" if quantity > 0 else "out_of_stock"


def _songwifi_shop_catalog(payload: Any, origin: str) -> list[ScrapedProduct]:
    """Convert SongWiFi's public eShop response into the common price model."""

    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise NuxtDataError("SongWiFi 商城商品接口返回格式无效")
    products: list[ScrapedProduct] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        product_id = str(item.get("product_id") or "").strip()
        name_tw = _plain_text(item.get("name"))
        if not product_id or not name_tw:
            continue
        raw_variants = item.get("variants")
        variants = raw_variants if isinstance(raw_variants, list) and raw_variants else [item]
        offers: list[ScrapedOffer] = []
        aliases = [
            name_tw,
            _plain_text(item.get("brand_name")),
            _plain_text(item.get("category_name")),
            str(item.get("code") or "").strip(),
        ]
        for index, variant in enumerate(variants):
            if not isinstance(variant, dict):
                continue
            regular = _money(variant.get("price")) or _money(item.get("price"))
            sale = _money(variant.get("onsale"))
            if sale is None:
                sale = _money(item.get("onsale"))
            current = sale if sale is not None and sale > 0 else regular
            if current is None:
                continue
            variant_id = str(variant.get("variant_id") or f"base-{index + 1}").strip()
            label_tw = _plain_text(variant.get("option_name")) or "標準規格"
            sku = str(variant.get("sku") or "").strip()
            aliases.extend(value for value in (label_tw, sku) if value)
            offers.append(
                ScrapedOffer(
                    external_key=f"variant:{variant_id}",
                    label=label_tw,
                    currency="HKD",
                    price_amount=current,
                    original_amount=(regular if regular is not None and regular != current else None),
                    unit="item",
                    promo_label=("優惠價" if regular is not None and regular != current else None),
                    availability=_shop_offer_availability(variant),
                    metadata={
                        "variant_id": variant_id,
                        "sku": sku or None,
                        "label_zh_cn": _t2s.convert(label_tw),
                        "total_available": variant.get("total_available"),
                        "unlimited_inventory": variant.get("unlimited_inventory"),
                    },
                )
            )
        if not offers:
            continue
        description = _plain_text(item.get("description") or item.get("content"))
        image_url = _shop_image_url(
            item.get("cover") or item.get("images") or item.get("image"),
            base_url=origin,
        )
        products.append(
            ScrapedProduct(
                external_key=f"eshop-product:{product_id}",
                canonical_url=normalize_url(f"/eshop/product/{product_id}", base_url=origin),
                name=name_tw,
                name_translations={"zh-CN": _t2s.convert(name_tw), "zh-TW": name_tw},
                aliases=list(dict.fromkeys(value for value in aliases if value)),
                category="eshop",
                product_type="eshop_product",
                destination=None,
                network=None,
                description=description,
                metadata={
                    "product_id": product_id,
                    "brand_name": _plain_text(item.get("brand_name")) or None,
                    "category_name": _plain_text(item.get("category_name")) or None,
                    "image_url": image_url,
                },
                offers=offers,
            )
        )
    return products


def _songwifi_public_app_id(
    http: SafeHttpClient,
    robots: RobotFileParser,
    origin: str,
    root_markup: str,
) -> str:
    pattern = re.compile(r'app_id\s*:\s*["\']([A-Za-z0-9_-]{16,128})["\']')
    direct = pattern.search(root_markup)
    if direct:
        return direct.group(1)
    soup = BeautifulSoup(root_markup, "html.parser")
    script_urls: list[str] = []
    for node in soup.find_all("script", src=True):
        try:
            script_url = normalize_url(str(node.get("src")), base_url=origin)
        except CrawlError:
            continue
        if (urlsplit(script_url).hostname or "").lower().removeprefix("www.") != SONGWIFI_SITE:
            continue
        if not script_url.lower().endswith(".js") or script_url in script_urls:
            continue
        script_urls.append(script_url)
    for script_url in script_urls[:8]:
        if not robots.can_fetch(USER_AGENT, script_url):
            continue
        result = http.get(script_url, max_bytes=8 * 1024 * 1024)
        match = pattern.search(_decode_markup(result.body, result.encoding))
        if match:
            return match.group(1)
    raise NuxtDataError("无法从 SongWiFi 网页取得商城公开接口标识")


def _fetch_songwifi_shop_catalog(
    http: SafeHttpClient,
    robots: RobotFileParser,
    origin: str,
    root_markup: str,
) -> list[ScrapedProduct]:
    app_id = _songwifi_public_app_id(http, robots, origin, root_markup)
    page = 1
    page_size = 100
    total = 1
    rows: list[Any] = []
    while len(rows) < total:
        query = urlencode(
            {"app_id": app_id, "lang": "zh-Hant", "page": page, "limit": page_size}
        )
        api_url = normalize_url(f"/api/v1/shop-products?{query}", base_url=origin)
        if not robots.can_fetch(USER_AGENT, api_url):
            raise CrawlError(f"robots.txt 不允许采集：{api_url}")
        result = http.get(api_url, max_bytes=8 * 1024 * 1024)
        try:
            payload = json.loads(_decode_markup(result.body, result.encoding))
        except json.JSONDecodeError as exc:
            raise NuxtDataError("SongWiFi 商城商品接口没有返回有效 JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise NuxtDataError("SongWiFi 商城商品接口返回格式无效")
        meta = payload.get("meta")
        if isinstance(meta, dict) and str(meta.get("code") or "200") != "200":
            raise NuxtDataError("SongWiFi 商城商品接口返回失败状态")
        page_rows = payload["data"]
        rows.extend(page_rows)
        try:
            total = max(0, int(payload.get("total", len(rows))))
        except (TypeError, ValueError):
            total = len(rows)
        if not page_rows or len(rows) >= total:
            break
        page += 1
        if page > 100:
            raise NuxtDataError("SongWiFi 商城商品分页数量异常")
    products = _songwifi_shop_catalog({"data": rows}, origin)
    if total and not products:
        raise NuxtDataError("SongWiFi 商城接口未解析到任何可报价商品")
    return products


def _songwifi_products(source: ProductPriceSource) -> tuple[list[ScrapedProduct], int, int, list[str]]:
    origin = _origin(source.root_url)
    allowed_site = (urlsplit(origin).hostname or "").lower().removeprefix("www.")
    http = SafeHttpClient(allowed_site)
    try:
        robots = _robots_parser(http, origin)
        root_url = origin
        esim_url = normalize_url("/esim", base_url=origin)
        for url in (root_url, esim_url):
            if not robots.can_fetch(USER_AGENT, url):
                raise CrawlError(f"robots.txt 不允许采集：{url}")
        root_result = http.get(root_url, max_bytes=8 * 1024 * 1024)
        esim_result = http.get(esim_url, max_bytes=8 * 1024 * 1024)
        root_markup = _decode_markup(root_result.body, root_result.encoding)
        shop_products = _fetch_songwifi_shop_catalog(http, robots, origin, root_markup)
    finally:
        http.close()

    wifi_data = _nuxt_collections(
        root_markup,
        "wifi_destinations",
        "wifi_products",
    )
    esim_data = _nuxt_collections(
        _decode_markup(esim_result.body, esim_result.encoding),
        "esim_destinations",
        "esim_plans",
    )
    products: list[ScrapedProduct] = list(shop_products)

    for item in wifi_data.get("wifi_destinations", []):
        if not isinstance(item, dict):
            continue
        destination_id = str(item.get("destination_id") or "").strip()
        name_tw = _language_value(item.get("langs"), "zh-Hant", "name")
        name_en = _language_value(item.get("langs"), "en", "name")
        current = _money(item.get("discount")) or _money(item.get("price"))
        regular = _money(item.get("price"))
        if not destination_id or not name_tw or current is None:
            continue
        network_match = re.match(r"\s*([45]G)\b", name_tw, re.I)
        network = network_match.group(1).upper() if network_match else None
        is_gadget = network is None
        category = "travel_gadget" if is_gadget else f"wifi_{network.lower()}"
        product_type = "travel_gadget" if is_gadget else "wifi_rental"
        external_url = str(item.get("external_url") or "").strip()
        canonical_url = (
            normalize_url(external_url, base_url=origin)
            if external_url
            else f"{origin.rstrip('/')}/order/song-wifi?{urlencode({'destination_id': destination_id})}"
        )
        description_parts = [
            _language_value(item.get("langs"), "zh-Hant", field)
            for field in ("description", "data_limit", "coverage")
        ]
        covers = item.get("wifi_destinations_cover") or []
        cover = next(
            (
                str(image.get("source"))
                for image in covers
                if isinstance(image, dict) and image.get("source")
            ),
            None,
        )
        products.append(
            ScrapedProduct(
                external_key=f"wifi-destination:{destination_id}",
                canonical_url=canonical_url,
                name=name_tw,
                name_translations={"zh-CN": _t2s.convert(name_tw), "zh-TW": name_tw, "en": name_en},
                aliases=[value for value in (name_tw, name_en) if value],
                category=category,
                product_type=product_type,
                destination=(re.sub(r"^\s*[45]G\s*(?:WiFi\s*)?", "", name_tw).strip() if network else None),
                network=network,
                description=" ".join(dict.fromkeys(part for part in description_parts if part)),
                metadata={
                    "destination_id": destination_id,
                    "product_ids": item.get("product_id") or [],
                    "image_url": cover,
                },
                offers=[
                    ScrapedOffer(
                        external_key="daily",
                        label="每日租用",
                        currency="HKD",
                        price_amount=current,
                        original_amount=(regular if regular and regular != current else None),
                        unit="day",
                        promo_label=("优惠价" if regular and regular != current else None),
                        metadata={"label_zh_cn": "每日租用"},
                    )
                ],
            )
        )

    destinations = {
        str(item.get("destination_id")): item
        for item in esim_data.get("esim_destinations", [])
        if isinstance(item, dict) and item.get("destination_id")
    }
    plans_by_destination: dict[str, list[dict[str, Any]]] = {}
    for plan in esim_data.get("esim_plans", []):
        if isinstance(plan, dict):
            plans_by_destination.setdefault(str(plan.get("destination_id")), []).append(plan)
    for destination_id, item in destinations.items():
        code = str(item.get("code") or destination_id).strip()
        name_tw = _language_value(item.get("langs"), "zh-Hant", "name")
        name_en = _language_value(item.get("langs"), "en", "name")
        description = _language_value(item.get("langs"), "zh-Hant", "description")
        coverage = _language_value(item.get("langs"), "zh-Hant", "coverage")
        offers: list[ScrapedOffer] = []
        for plan in plans_by_destination.get(destination_id, []):
            plan_id = str(plan.get("plan_id") or "").strip()
            label = _language_value(plan.get("langs"), "zh-Hant", "name")
            current = _money(plan.get("discount")) or _money(plan.get("price"))
            regular = _money(plan.get("price"))
            if not plan_id or current is None:
                continue
            duration = plan.get("days")
            duration_days = int(duration) if str(duration).isdigit() else None
            offers.append(
                ScrapedOffer(
                    external_key=plan_id,
                    label=label or plan_id,
                    currency="HKD",
                    price_amount=current,
                    original_amount=(regular if regular and regular != current else None),
                    unit="plan",
                    duration_days=duration_days,
                    data_label=str(plan.get("data_label") or "").strip() or None,
                    promo_label=("优惠价" if regular and regular != current else None),
                    metadata={
                        "order_url": f"{origin.rstrip('/')}/order/esim?{urlencode({'plan': plan_id})}",
                        "label_zh_cn": _t2s.convert(label or plan_id),
                        "average_per_day": (
                            str((current / duration_days).quantize(Decimal('0.01')))
                            if duration_days
                            else None
                        ),
                    },
                )
            )
        if not name_tw or not offers:
            continue
        products.append(
            ScrapedProduct(
                external_key=f"esim-destination:{code}",
                canonical_url=normalize_url(f"/esim/{code}", base_url=origin),
                name=f"{name_tw} eSIM",
                name_translations={
                    "zh-CN": f"{_t2s.convert(name_tw)} eSIM",
                    "zh-TW": f"{name_tw} eSIM",
                    "en": f"{name_en} eSIM",
                },
                aliases=[value for value in (name_tw, name_en, code, f"{name_tw} eSIM") if value],
                category="esim",
                product_type="esim",
                destination=name_tw,
                network=("4G/5G" if "5G" in coverage else "4G" if "4G" in coverage else None),
                description=" ".join(part for part in (description, coverage) if part),
                metadata={"destination_id": destination_id, "code": code, "image_url": item.get("cover")},
                offers=offers,
            )
        )
    if not products:
        raise NuxtDataError("SongWiFi 页面未解析到任何可报价商品")
    return products, len(products), 0, []


def _jsonld_nodes(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        if str(value.get("@type", "")).lower() == "product":
            yield value
        graph = value.get("@graph")
        if graph is not None:
            yield from _jsonld_nodes(graph)
        for item in value.values():
            if isinstance(item, (dict, list)):
                yield from _jsonld_nodes(item)
    elif isinstance(value, list):
        for item in value:
            yield from _jsonld_nodes(item)


def _generic_page_products(markup: str, page_url: str) -> list[ScrapedProduct]:
    soup = BeautifulSoup(markup, "html.parser")
    nodes: list[dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            payload = json.loads(script.get_text(strip=True))
        except (TypeError, json.JSONDecodeError):
            continue
        nodes.extend(_jsonld_nodes(payload))
    products: list[ScrapedProduct] = []
    for product_index, node in enumerate(nodes):
        name = str(node.get("name") or "").strip()
        if not name:
            continue
        raw_offers = node.get("offers") or []
        offer_nodes = raw_offers if isinstance(raw_offers, list) else [raw_offers]
        offers: list[ScrapedOffer] = []
        for offer_index, offer in enumerate(offer_nodes):
            if not isinstance(offer, dict):
                continue
            price = _money(offer.get("price") or offer.get("lowPrice"))
            if price is None:
                continue
            offer_url = str(offer.get("url") or "").strip()
            offer_key = str(offer.get("sku") or offer_url or offer.get("name") or offer_index)
            offers.append(
                ScrapedOffer(
                    external_key=hashlib.sha256(offer_key.encode("utf-8")).hexdigest()[:32],
                    label=str(offer.get("name") or "标准价格").strip(),
                    currency=str(offer.get("priceCurrency") or "HKD").upper(),
                    price_amount=price,
                    unit="item",
                    availability=(
                        "out_of_stock"
                        if "OutOfStock" in str(offer.get("availability"))
                        else "in_stock"
                    ),
                    metadata={"offer_url": offer_url or None},
                )
            )
        if not offers:
            continue
        canonical_url = str(node.get("url") or page_url).strip()
        try:
            canonical_url = normalize_url(canonical_url, base_url=page_url)
        except CrawlError:
            canonical_url = page_url
        raw_key = str(node.get("sku") or node.get("productID") or canonical_url or name)
        products.append(
            ScrapedProduct(
                external_key=f"schema:{hashlib.sha256(raw_key.encode('utf-8')).hexdigest()[:32]}",
                canonical_url=canonical_url,
                name=name,
                name_translations={},
                aliases=[name],
                category=str(node.get("category") or "other")[:80],
                product_type="product",
                destination=None,
                network=None,
                description=str(node.get("description") or "").strip(),
                metadata={"schema": "Product", "page_url": page_url},
                offers=offers,
            )
        )
    return products


def _generic_products(source: ProductPriceSource) -> tuple[list[ScrapedProduct], int, int, list[str]]:
    crawler = WebsiteCrawler(source.root_url, max_pages=source.max_pages, max_depth=1)
    products: dict[str, ScrapedProduct] = {}
    failed = 0
    errors: list[str] = []
    processed = 0
    try:
        robots = crawler._robots_parser()
        sitemap_urls = crawler._sitemap_pages(robots)
        hinted = [
            url
            for url in sitemap_urls
            if any(hint in urlsplit(url).path.lower() for hint in PRODUCT_PATH_HINTS)
        ]
        candidates = list(dict.fromkeys([source.root_url, *(hinted or sitemap_urls)]))[
            : source.max_pages
        ]
        crawl_delay = robots.crawl_delay(USER_AGENT) or robots.crawl_delay("*") or 0
        last_request_at = 0.0
        for url in candidates:
            if not robots.can_fetch(USER_AGENT, url):
                continue
            elapsed = time.monotonic() - last_request_at
            if crawl_delay > elapsed:
                time.sleep(crawl_delay - elapsed)
            try:
                result = crawler.http.get(url, max_bytes=MAX_HTML_BYTES)
                last_request_at = time.monotonic()
                processed += 1
                if result.content_type not in {"text/html", "application/xhtml+xml", ""}:
                    continue
                markup = _decode_markup(result.body, result.encoding)
                for item in _generic_page_products(markup, result.url):
                    products[item.external_key] = item
            except CrawlError as exc:
                failed += 1
                if len(errors) < 10:
                    errors.append(f"{url}: {exc}")
    finally:
        crawler.close()
    return list(products.values()), processed, failed, errors


def scrape_product_source(
    source: ProductPriceSource,
) -> tuple[list[ScrapedProduct], int, int, list[str]]:
    host = (urlsplit(source.root_url).hostname or "").lower().removeprefix("www.")
    if host == SONGWIFI_SITE:
        source.adapter = "songwifi"
        return _songwifi_products(source)
    source.adapter = "schema_org"
    return _generic_products(source)


def _offer_hash(offer: ScrapedOffer) -> str:
    payload = {
        "label": offer.label,
        "currency": offer.currency,
        "price": str(offer.price_amount),
        "original": str(offer.original_amount) if offer.original_amount is not None else None,
        "unit": offer.unit,
        "duration_days": offer.duration_days,
        "data_label": offer.data_label,
        "promo_label": offer.promo_label,
        "availability": offer.availability,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _record_history(db: Session, offer: ProductPriceOffer, change_type: str) -> None:
    db.add(
        ProductPriceHistory(
            tenant_id=offer.tenant_id,
            offer_id=offer.id,
            change_type=change_type,
            currency=offer.currency,
            price_amount=offer.price_amount,
            original_amount=offer.original_amount,
            unit=offer.unit,
            content_hash=offer.content_hash,
        )
    )


def persist_product_catalog(
    db: Session,
    source: ProductPriceSource,
    scraped_products: list[ScrapedProduct],
) -> ProductSyncResult:
    now = utcnow()
    existing_products = {
        item.external_key: item
        for item in db.scalars(select(Product).where(Product.source_id == source.id)).all()
    }
    seen_products: set[str] = set()
    result = ProductSyncResult(status="completed")
    for scraped in scraped_products:
        if scraped.external_key in seen_products:
            continue
        seen_products.add(scraped.external_key)
        product = existing_products.get(scraped.external_key)
        if product is None:
            product = Product(
                tenant_id=source.tenant_id,
                source_id=source.id,
                external_key=scraped.external_key,
                canonical_url=scraped.canonical_url,
                name=scraped.name,
            )
            db.add(product)
            db.flush()
            existing_products[scraped.external_key] = product
            result.new_products += 1
        product.canonical_url = scraped.canonical_url
        product.name = scraped.name
        product.name_translations = scraped.name_translations
        product.aliases = list(dict.fromkeys(scraped.aliases))
        product.category = scraped.category
        product.product_type = scraped.product_type
        product.destination = scraped.destination
        product.network = scraped.network
        product.description = scraped.description
        product.metadata_json = scraped.metadata
        product.is_active = True
        product.consecutive_missing = 0
        product.last_seen_at = now
        product.updated_at = now

        existing_offers = {item.external_key: item for item in product.offers}
        seen_offers: set[str] = set()
        for scraped_offer in scraped.offers:
            if scraped_offer.external_key in seen_offers:
                continue
            seen_offers.add(scraped_offer.external_key)
            content_hash = _offer_hash(scraped_offer)
            offer = existing_offers.get(scraped_offer.external_key)
            if offer is None:
                offer = ProductPriceOffer(
                    tenant_id=source.tenant_id,
                    source_id=source.id,
                    product_id=product.id,
                    external_key=scraped_offer.external_key,
                    label=scraped_offer.label,
                    currency=scraped_offer.currency,
                    price_amount=scraped_offer.price_amount,
                    original_amount=scraped_offer.original_amount,
                    unit=scraped_offer.unit,
                    duration_days=scraped_offer.duration_days,
                    data_label=scraped_offer.data_label,
                    promo_label=scraped_offer.promo_label,
                    availability=scraped_offer.availability,
                    metadata_json=scraped_offer.metadata,
                    content_hash=content_hash,
                    last_seen_at=now,
                )
                db.add(offer)
                db.flush()
                _record_history(db, offer, "created")
                result.new_offers += 1
                existing_offers[offer.external_key] = offer
                continue
            changed = offer.content_hash != content_hash or not offer.is_active
            offer.label = scraped_offer.label
            offer.currency = scraped_offer.currency
            offer.price_amount = scraped_offer.price_amount
            offer.original_amount = scraped_offer.original_amount
            offer.unit = scraped_offer.unit
            offer.duration_days = scraped_offer.duration_days
            offer.data_label = scraped_offer.data_label
            offer.promo_label = scraped_offer.promo_label
            offer.availability = scraped_offer.availability
            offer.metadata_json = scraped_offer.metadata
            offer.content_hash = content_hash
            offer.is_active = True
            offer.consecutive_missing = 0
            offer.last_seen_at = now
            offer.updated_at = now
            if changed:
                _record_history(db, offer, "changed")
                result.changed_offers += 1
            else:
                result.unchanged_offers += 1

        for key, offer in existing_offers.items():
            if key in seen_offers:
                continue
            offer.consecutive_missing += 1
            if offer.consecutive_missing >= 2 and offer.is_active:
                offer.is_active = False
                offer.updated_at = now
                _record_history(db, offer, "deactivated")

    for key, product in existing_products.items():
        if key in seen_products:
            continue
        product.consecutive_missing += 1
        if product.consecutive_missing >= 2 and product.is_active:
            product.is_active = False
            product.updated_at = now
            result.missing_products += 1
            for offer in product.offers:
                if offer.is_active:
                    offer.is_active = False
                    offer.updated_at = now
                    _record_history(db, offer, "deactivated")
    db.flush()
    return result


def _finish_failed_run(source_id: int, sync_run_id: int | None, message: str) -> ProductSyncResult:
    with SessionLocal() as db:
        source = db.get(ProductPriceSource, source_id)
        if source is not None:
            source.status = "failed"
            source.error_message = message[:2000]
            source.completed_at = utcnow()
            source.updated_at = utcnow()
        run = db.get(ProductPriceSyncRun, sync_run_id) if sync_run_id else None
        if run is not None:
            run.status = "failed"
            run.error_message = message[:2000]
            run.completed_at = utcnow()
        db.commit()
    return ProductSyncResult(status="failed", error_message=message[:2000])


def run_product_price_sync(
    source_id: int,
    *,
    sync_run_id: int | None = None,
    trigger: str = "manual",
    attempt: int = 0,
) -> ProductSyncResult:
    del trigger, attempt
    try:
        with SessionLocal() as db:
            source = db.get(ProductPriceSource, source_id)
            if source is None:
                raise CrawlError("商品价格来源不存在")
            run = db.get(ProductPriceSyncRun, sync_run_id) if sync_run_id else None
            now = utcnow()
            source.status = "running"
            source.started_at = now
            source.error_message = None
            source.updated_at = now
            if run is not None:
                run.status = "running"
                run.started_at = now
            db.commit()
            db.refresh(source)

            scraped, discovered, failed_pages, errors = scrape_product_source(source)
            if source.adapter == "songwifi" and not scraped:
                raise CrawlError("SongWiFi 没有返回可用商品价格")
            result = persist_product_catalog(db, source, scraped)
            result.failed_pages = failed_pages
            result.status = "partial" if failed_pages and scraped else "completed"
            warning = "; ".join(errors[:5]) or None
            if not scraped and source.adapter != "songwifi":
                warning = "未检测到 Schema.org Product 结构化价格；该网址可能需要专用采集适配器"
            result.error_message = warning
            source.discovered_products = discovered
            source.failed_pages = failed_pages
            source.status = result.status
            source.error_message = warning
            source.completed_at = utcnow()
            source.updated_at = utcnow()
            db.flush()
            source.imported_products = int(
                db.scalar(
                    select(func.count(Product.id)).where(
                        Product.source_id == source.id,
                        Product.is_active.is_(True),
                    )
                )
                or 0
            )
            source.imported_offers = int(
                db.scalar(
                    select(func.count(ProductPriceOffer.id)).where(
                        ProductPriceOffer.source_id == source.id,
                        ProductPriceOffer.is_active.is_(True),
                    )
                )
                or 0
            )
            if run is not None:
                run.status = result.status
                run.new_products = result.new_products
                run.new_offers = result.new_offers
                run.changed_offers = result.changed_offers
                run.unchanged_offers = result.unchanged_offers
                run.missing_products = result.missing_products
                run.failed_pages = failed_pages
                run.error_message = warning
                run.completed_at = utcnow()
            db.commit()
            return result
    except Exception as exc:
        message = str(exc) or "商品价格同步失败"
        return _finish_failed_run(source_id, sync_run_id, message)
