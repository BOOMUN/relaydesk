from __future__ import annotations

from urllib.parse import urlsplit

from opencc import OpenCC
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..models import KnowledgeSource, Product
from .product_price_query import product_search_aliases
from .web_crawler import CrawledPage, UnsafeUrlError, normalize_url


PRODUCT_KNOWLEDGE_CATEGORIES = frozenset(
    {
        "wifi_4g",
        "wifi_5g",
        "travel_gadget",
        "eshop",
    }
)

_CATEGORY_LABELS = {
    "wifi_4g": "4G WiFi 蛋租借方案",
    "wifi_5g": "5G WiFi 蛋租借方案",
    "travel_gadget": "旅行設備租借",
    "eshop": "網店商品",
}

_s2t = OpenCC("s2t.json")


def _site_key(value: str) -> str:
    return value.casefold().strip().rstrip(".").removeprefix("www.")


def _traditional(value: str | None) -> str:
    return _s2t.convert(str(value or "").strip())


def _localized_name(product: Product) -> str:
    translations = product.name_translations or {}
    return _traditional(translations.get("zh-TW") or product.name)


def _catalog_specifications(product: Product) -> tuple[str, ...]:
    specifications: list[str] = []
    for offer in product.offers:
        if not offer.is_active:
            continue
        parts: list[str] = []
        if offer.label and offer.label not in {
            "標準價格",
            "标准价格",
            "標準規格",
            "标准规格",
            "每日租用",
        }:
            parts.append(_traditional(offer.label))
        if offer.data_label:
            parts.append(_traditional(offer.data_label))
        if offer.duration_days:
            parts.append(f"{offer.duration_days}日")
        specification = " / ".join(dict.fromkeys(part for part in parts if part))
        if specification:
            specifications.append(specification)
    return tuple(dict.fromkeys(specifications))


def _catalog_aliases(product: Product) -> tuple[str, ...]:
    name = _localized_name(product)
    aliases = tuple(
        dict.fromkeys(
            value.strip()
            for value in product_search_aliases(product)
            if value and value.strip() != name
        )
    )
    return aliases[:60]


def _catalog_page_content(
    product: Product,
    aliases: tuple[str, ...] | None = None,
) -> str:
    name = _localized_name(product)
    aliases = list(
        aliases
        if aliases is not None
        else _catalog_aliases(product)
    )
    lines = [
        "官方產品資料",
        f"產品名稱：{name}",
        f"產品類型：{_CATEGORY_LABELS.get(product.category, '商品')}",
    ]
    if product.destination:
        lines.append(f"適用目的地：{_traditional(product.destination)}")
    if product.network:
        lines.append(f"網絡類型：{product.network}")
    if aliases:
        lines.append(f"常用名稱及型號：{'、'.join(aliases)}")
    specifications = _catalog_specifications(product)
    if specifications:
        lines.append(f"可選規格：{'；'.join(specifications)}")
    description = _traditional(product.description)
    if description:
        lines.append(f"產品說明：{description[:6000]}")
    lines.extend(
        (
            "價格及供應狀態：請優先查詢結構化產品目錄，以最新同步資料及最終訂單為準。",
            f"官方產品頁：{product.canonical_url}",
        )
    )
    return "\n".join(lines)


def catalog_product_knowledge_pages(
    db: Session,
    source: KnowledgeSource,
) -> list[CrawledPage]:
    """Build indexable product-page text for SongWiFi's client-rendered routes.

    The public product pages are Nuxt SPA routes whose initial HTML contains no
    useful main text. The already synchronized structured catalogue is the
    authoritative representation of those same public pages, so it supplies
    stable product identity and descriptive text while live price/stock values
    deliberately remain in the relational catalogue.
    """

    products = db.scalars(
        select(Product)
        .where(
            Product.tenant_id == source.tenant_id,
            Product.is_active.is_(True),
            Product.category.in_(PRODUCT_KNOWLEDGE_CATEGORIES),
        )
        .options(
            selectinload(Product.source),
            selectinload(Product.offers),
        )
        .order_by(Product.category, Product.name)
    ).all()
    pages: list[CrawledPage] = []
    source_site = _site_key(source.domain)
    for product in products:
        if _site_key(product.source.domain) != source_site:
            continue
        try:
            url = normalize_url(product.canonical_url)
        except UnsafeUrlError:
            continue
        if _site_key(urlsplit(url).hostname or "") != source_site:
            continue
        search_aliases = _catalog_aliases(product)
        pages.append(
            CrawledPage(
                url=url,
                title=_localized_name(product)[:255],
                content=_catalog_page_content(product, search_aliases),
                content_type="html",
                language="zh-TW",
                metadata={
                    "http_content_type": "text/html",
                    "extraction_mode": "structured_product_catalog",
                    "product_id": product.id,
                    "product_external_key": product.external_key,
                    "product_category": product.category,
                    "product_type": product.product_type,
                    "catalog_source_id": product.source_id,
                    "live_price_lookup_required": 1,
                    "search_aliases": list(search_aliases),
                },
            )
        )
    return pages


__all__ = [
    "PRODUCT_KNOWLEDGE_CATEGORIES",
    "catalog_product_knowledge_pages",
]
