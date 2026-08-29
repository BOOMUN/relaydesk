from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from langchain_core.documents import Document
from langchain_core.tools import tool
from opencc import OpenCC
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..config import settings
from ..models import Product, ProductPriceOffer, ProductPriceSource


FULL_CATALOG_TERMS = (
    "完整价目表",
    "完整價格表",
    "完整價目表",
    "全部价格",
    "全部價格",
    "所有价格",
    "所有價格",
    "所有产品价格",
    "所有產品價格",
    "full price list",
    "complete price list",
    "all prices",
)
PRICE_TERMS = (
    "价格",
    "價格",
    "价钱",
    "價錢",
    "报价",
    "報價",
    "收费",
    "收費",
    "多少钱",
    "多少錢",
    "几钱",
    "幾錢",
    "价目表",
    "價目表",
    "划算",
    "便宜",
    "最平",
    "优惠",
    "優惠",
    "price",
    "pricing",
    "cost",
    "rate",
    "how much",
)
PRODUCT_INTENT_TERMS = (
    "有吗",
    "有嗎",
    "有没有",
    "有沒有",
    "有冇",
    "提供",
    "出售",
    "购买",
    "購買",
    "想买",
    "想買",
    "我要买",
    "我要買",
    "想租",
    "我要租",
    "租借",
    "租用",
    "出租",
    "推荐",
    "推薦",
    "哪个好",
    "哪個好",
    # Customers often omit the noun and ask only which option is better
    # (for example, “去日本租哪个比较好”).  These terms are intentionally
    # kept separate from price words so a recommendation is still routed to
    # the product catalogue/knowledge path instead of being escalated.
    "哪个",
    "哪個",
    "哪一個",
    "哪一个",
    "哪款",
    "哪一款",
    "比较好",
    "比較好",
    "怎么选",
    "怎麼選",
    "如何选",
    "如何選",
    "套餐",
    "方案",
    "产品",
    "產品",
    "商品",
    "wifi",
    "wifi蛋",
    "随身wifi",
    "隨身wifi",
    "esim",
    "相机",
    "相機",
    "摄影机",
    "攝影機",
    "摄像机",
    "攝像機",
    "翻译机",
    "翻譯機",
    "数据线",
    "數據線",
    "充电线",
    "充電線",
    "风扇",
    "風扇",
    "保温杯",
    "保溫杯",
    "洗漱",
    "洗护",
    "洗護",
    "type-c",
    "usb-c",
    "插头",
    "插頭",
    "行李秤",
    "camera",
    "rent",
    "rental",
    "buy",
    "available",
    "availability",
    "have",
    "offer",
    "provide",
    "recommend",
    "recommendation",
    "best",
    "which",
    "option",
    "sell",
)
CATEGORY_LABELS = {
    "wifi_5g": ("5G WiFi 蛋", "5G WiFi 蛋"),
    "wifi_4g": ("4G WiFi 蛋", "4G WiFi 蛋"),
    "esim": ("eSIM 套餐", "eSIM 套餐"),
    "travel_gadget": ("旅行设备", "旅行設備"),
    "eshop": ("商城商品", "網店商品"),
    "other": ("其他商品", "其他商品"),
}
PRODUCT_TYPE_LABELS = {
    "wifi_rental": "WiFi 蛋租借",
    "esim": "eSIM 套餐",
    "travel_gadget": "旅行設備租借",
    "eshop_product": "網店商品",
    "product": "商品",
}

ENGLISH_CATEGORY_LABELS = {
    "wifi_5g": "5G WiFi rental",
    "wifi_4g": "4G WiFi rental",
    "esim": "eSIM plans",
    "travel_gadget": "Travel equipment",
    "eshop": "Shop products",
    "other": "Other products",
}

# A destination is still matched against the authoritative structured field;
# these aliases only let an English customer name that same destination.  They
# are deliberately phrase based so a greeting such as ``Hi`` cannot match the
# middle of ``China`` or ``Philippines``.
DESTINATION_ENGLISH_ALIASES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("日本",), ("japan", "japanese")),
    (("韩国", "南韩"), ("korea", "south korea", "korean")),
    (("泰国",), ("thailand", "thai")),
    (("中国", "中国大陆", "中国内地"), ("china", "mainland china", "chinese")),
    (("台湾",), ("taiwan", "taiwanese")),
    (("香港",), ("hong kong",)),
    (("澳门",), ("macau", "macao")),
    (("新加坡",), ("singapore",)),
    (("马来西亚",), ("malaysia",)),
    (("印度尼西亚", "印尼"), ("indonesia",)),
    (("菲律宾",), ("philippines",)),
    (("越南",), ("vietnam",)),
    (("澳大利亚", "澳洲"), ("australia",)),
    (("新西兰", "纽西兰"), ("new zealand",)),
    (("美国",), ("united states", "usa", "america")),
    (("加拿大",), ("canada",)),
    (("欧洲",), ("europe",)),
    (("阿联酋",), ("united arab emirates", "uae")),
    (("马尔代夫",), ("maldives",)),
    (("关岛",), ("guam",)),
    (("塞班",), ("saipan",)),
    (("东南亚",), ("southeast asia",)),
    (("全球",), ("global", "worldwide")),
)

# These are common recommendation words for a product/destination query.
# Keep the list narrow: a bare “适合” or “哪个” can also be used for
# sightseeing, restaurants, weather, and other topics outside SongWiFi.
_PRODUCT_RECOMMENDATION_TERMS = (
    "哪个",
    "哪個",
    "哪款",
    "哪一款",
    "哪一个",
    "哪一個",
    "哪个好",
    "哪個好",
    "怎么选",
    "怎麼選",
    "如何选",
    "如何選",
    "推荐",
    "推薦",
)
_NON_PRODUCT_RECOMMENDATION_TERMS = (
    "天气",
    "天氣",
    "景点",
    "景點",
    "景区",
    "景區",
    "餐厅",
    "餐廳",
    "酒店",
    "住宿",
    "机票",
    "機票",
    "航班",
    "导游",
    "導遊",
    "weather",
    "restaurant",
    "hotel",
    "accommodation",
    "flight",
    "tourist attraction",
    "sightseeing",
)
_EXPLICIT_PRODUCT_CONTEXT_TERMS = (
    "wifi",
    "wifi蛋",
    "随身wifi",
    "esim",
    "sim卡",
    "上网",
    "网络",
    "流量",
    "套餐",
    "方案",
    "产品",
    "商品",
    "设备",
    "相机",
    "摄影机",
    "攝影機",
    "摄像机",
    "攝像機",
    "翻译机",
    "翻譯機",
    "数据线",
    "數據線",
    "充电线",
    "充電線",
    "旅行设备",
    "旅行設備",
    "internet",
    "hotspot",
    "data plan",
    "device",
    "camera",
    "cable",
    "translator",
)


_t2s = OpenCC("t2s.json")
_s2t = OpenCC("s2t.json")


def _simplified(value: str) -> str:
    return re.sub(r"\s+", " ", _t2s.convert(value).casefold()).strip()


def _script_aliases(*values: str) -> set[str]:
    """Expand customer terms into explicit Simplified/Traditional variants."""

    aliases: set[str] = set()
    for value in values:
        normalized = str(value or "").strip()
        if not normalized:
            continue
        aliases.update(
            item
            for item in (
                normalized,
                _t2s.convert(normalized),
                _s2t.convert(normalized),
            )
            if item
        )
    return aliases


def _money(value: Decimal) -> str:
    normalized = value.quantize(Decimal("0.01"))
    return f"{normalized:.2f}".rstrip("0").rstrip(".")


@dataclass(frozen=True, slots=True)
class RentalPeriod:
    """A customer-requested rental period.

    ``days`` is inclusive for a date range: a request from 8/1 through 8/5 is
    five rental days.  Keeping this as a small value object makes the parsing
    and deterministic quote formatting independent from the language model.
    """

    days: int
    start_date: date | None = None
    end_date: date | None = None
    source: str = "duration"


@dataclass(frozen=True, slots=True)
class _DateToken:
    start: int
    end: int
    year: int | None
    month: int | None
    day: int


_CN_NUMBER_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "兩": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CN_NUMBER_UNITS = {
    "十": 10,
    "百": 100,
    "千": 1000,
    "万": 10000,
    "萬": 10000,
    "廿": 20,
    "卅": 30,
}


def _parse_number_token(value: str) -> int | None:
    """Parse a small Arabic/Chinese integer used in a rental query."""

    token = str(value or "").strip().replace(",", "")
    if not token:
        return None
    if token.isdigit():
        return int(token)
    token = token.replace("兩", "两")
    if not all(char in _CN_NUMBER_DIGITS or char in _CN_NUMBER_UNITS for char in token):
        return None
    total = 0
    section = 0
    number = 0
    for char in token:
        if char in _CN_NUMBER_DIGITS:
            number = _CN_NUMBER_DIGITS[char]
            continue
        unit = _CN_NUMBER_UNITS[char]
        if unit >= 10000:
            section = (section + number) * unit
            total += section
            section = 0
            number = 0
        else:
            section += (number or 1) * unit
            number = 0
    return total + section + number


_EN_NUMBER_VALUES = {
    "zero": 0,
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}


def _parse_english_number(value: str) -> int | None:
    words = re.sub(r"[-]+", " ", str(value or "").casefold()).split()
    if not words:
        return None
    if len(words) == 1 and words[0].isdigit():
        return int(words[0])
    if any(word not in _EN_NUMBER_VALUES for word in words):
        return None
    # Rental questions generally use values below 100.  This also handles
    # phrases such as "twenty five" without introducing a general NLP parser.
    total = 0
    for word in words:
        total += _EN_NUMBER_VALUES[word]
    return total


def _period_today(today: date | None = None) -> date:
    if today is not None:
        return today
    try:
        return datetime.now(ZoneInfo(settings.knowledge_sync_timezone)).date()
    except Exception:
        return date.today()


def _date_from_token(token: _DateToken, *, fallback_month: int, fallback_year: int) -> date | None:
    month = token.month if token.month is not None else fallback_month
    year = token.year if token.year is not None else fallback_year
    if year < 100:
        year += 2000
    try:
        return date(year, month, token.day)
    except (TypeError, ValueError):
        return None


def _extract_date_tokens(query: str) -> list[_DateToken]:
    """Extract common Chinese, ISO, numeric and English date spellings."""

    text = str(query or "")
    tokens: list[_DateToken] = []

    # Full ISO/year-first forms are collected first so a later month/day
    # pattern cannot split 2026-08-01 into several tokens.
    patterns: tuple[tuple[str, str], ...] = (
        (
            "iso",
            r"(?P<year>20\d{2})[-/.年](?P<month>\d{1,2})[-/.月](?P<day>\d{1,2})(?:日|号)?",
        ),
        (
            "cn",
            r"(?:(?P<year>20\d{2})年\s*)?(?P<month>[0-9零〇一二两兩三四五六七八九十百千万萬廿卅]+)月\s*(?P<day>[0-9零〇一二两兩三四五六七八九十百千万萬廿卅]+)(?:日|号|號)?",
        ),
        (
            "english",
            r"(?:(?P<year_prefix>20\d{2})\s+)?(?P<month_name>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s*(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(?P<year>20\d{2}|\d{2}))?",
        ),
        (
            "english_reverse",
            r"(?P<day>\d{1,2})(?:st|nd|rd|th)?\s*(?P<month_name>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s*(?:,?\s*(?P<year>20\d{2}|\d{2}))?",
        ),
        (
            "slash",
            r"(?P<month>\d{1,2})[./-](?P<day>\d{1,2})(?:[./-](?P<year>20\d{2}|\d{2}))?",
        ),
        (
            "bare_day",
            r"(?P<day>[0-9零〇一二两兩三四五六七八九十百千万萬廿卅]+)\s*(?:日|号|號)",
        ),
        (
            "english_bare_day",
            r"(?<![A-Za-z0-9])(?P<day>\d{1,2})(?:st|nd|rd|th)?(?![A-Za-z0-9])",
        ),
    )
    month_names = {
        "jan": 1,
        "january": 1,
        "feb": 2,
        "february": 2,
        "mar": 3,
        "march": 3,
        "apr": 4,
        "april": 4,
        "may": 5,
        "jun": 6,
        "june": 6,
        "jul": 7,
        "july": 7,
        "aug": 8,
        "august": 8,
        "sep": 9,
        "sept": 9,
        "september": 9,
        "oct": 10,
        "october": 10,
        "nov": 11,
        "november": 11,
        "dec": 12,
        "december": 12,
    }
    occupied: list[tuple[int, int]] = []
    has_named_month = bool(
        re.search(
            r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
            r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|"
            r"nov(?:ember)?|dec(?:ember)?)\b",
            text,
            re.I,
        )
    )
    for kind, pattern in patterns:
        # A bare English number is only a date component when a named month
        # is present (e.g. ``Aug 1-5``).  Otherwise product model tokens such
        # as ``3-in-1`` or ``4G/5G`` could be mistaken for a date range.
        if kind == "english_bare_day" and not has_named_month:
            continue
        for match in re.finditer(pattern, text, re.I):
            if any(match.start() < end and match.end() > start for start, end in occupied):
                continue
            groups = match.groupdict()
            year_value = groups.get("year") or groups.get("year_prefix")
            year = _parse_number_token(year_value or "") if year_value else None
            day = _parse_number_token(groups.get("day") or "")
            if day is None:
                continue
            if groups.get("month_name"):
                month = month_names.get(groups["month_name"].casefold())
            elif groups.get("month"):
                month = _parse_number_token(groups["month"])
            else:
                month = None
            if month is not None and not 1 <= month <= 12:
                continue
            if not 1 <= day <= 31:
                continue
            token = _DateToken(match.start(), match.end(), year, month, day)
            tokens.append(token)
            occupied.append((match.start(), match.end()))
    return sorted(tokens, key=lambda item: (item.start, item.end))


def _has_date_range_connector(query: str, first: _DateToken, second: _DateToken) -> bool:
    between = str(query or "")[first.end : second.start]
    return bool(
        re.search(
            r"(?:从|從|到|至|至到|开始|開始|结束|結束|入住|退房|起|止|\b(?:from|to|until|through|between)\b|[-–—~～])",
            between,
            re.I,
        )
    )


def _looks_like_date_range(query: str) -> bool:
    """Whether the message explicitly has the shape of a calendar range."""

    tokens = _extract_date_tokens(query)
    return any(
        _has_date_range_connector(query, first, second)
        for first, second in zip(tokens, tokens[1:])
    )


def _parse_rental_date_range(query: str, today: date | None = None) -> RentalPeriod | None:
    tokens = _extract_date_tokens(query)
    if len(tokens) < 2:
        return None
    reference = _period_today(today)
    for first, second in zip(tokens, tokens[1:]):
        if not _has_date_range_connector(query, first, second):
            continue
        first_month = first.month or second.month or reference.month
        first_year = first.year or second.year or reference.year
        start = _date_from_token(first, fallback_month=first_month, fallback_year=first_year)
        second_month = second.month or first_month
        second_year = second.year or first_year
        end = _date_from_token(second, fallback_month=second_month, fallback_year=second_year)
        if start is None or end is None:
            continue
        # A missing year on the end side means a range such as 12/30–1/2
        # naturally crosses into the following year.  Conversely, when only
        # the end side names a year, the start may belong to the preceding
        # year. Explicitly reversed years remain invalid instead of silently
        # producing a positive quote.
        if end < start and second.year is None:
            try:
                end = end.replace(year=end.year + 1)
            except ValueError:
                continue
        elif end < start and first.year is None:
            try:
                start = start.replace(year=start.year - 1)
            except ValueError:
                continue
        if end < start:
            continue
        days = (end - start).days + 1
        if 1 <= days <= 3660:
            return RentalPeriod(days=days, start_date=start, end_date=end, source="date_range")
    return None


_CN_DURATION_NUMBER = r"[0-9]{1,4}|[零〇一二两兩三四五六七八九十百千萬万廿卅]+"
_EN_DURATION_NUMBER = (
    r"\d{1,4}|(?:a|an|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|"
    r"nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)"
    r"(?:[-\s]+(?:one|two|three|four|five|six|seven|eight|nine|ten|"
    r"twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)){0,2}"
)

# Follow-up messages often adjust the duration that was quoted in the
# previous turn (for example, "多加两天" or "add two more days").  Keep this
# detection deterministic and deliberately narrow so a normal product query
# containing a number is not treated as an adjustment accidentally.
_RENTAL_DURATION_ADDITION_RE = re.compile(
    r"(?:"
    r"多\s*(?:加|租)|多(?=[0-9零〇一二两兩三四五六七八九十百千萬万廿卅])|"
    r"加\s*多|加\s*(?=[0-9零〇一二两兩三四五六七八九十百千萬万廿卅])|"
    r"\+\s*(?=[0-9零〇一二两兩三四五六七八九十百千萬万廿卅])|"
    r"再\s*(?:加|租|多)|另\s*加|"
    r"加上|增加|追加|延长|续租|延续|"
    r"\b(?:add|additional|extra|more|another|increase|plus|extend(?:ed|ing)?|extension)\b"
    r")",
    re.I,
)
_RENTAL_DURATION_TARGET_RE = re.compile(
    r"(?:"
    r"改\s*(?:成|为|到|至)|换\s*成|变\s*成|调整\s*(?:为|到|至)|"
    r"增加\s*(?:到|至)|加\s*(?:到|至)|延长\s*(?:到|至|为)|续租\s*(?:到|至|为)|"
    r"\b(?:change|switch|make)\b.{0,20}\bto\b|"
    r"\bextend(?:ed|ing)?\b.{0,20}\bto\b|\bup\s+to\b"
    r")",
    re.I,
)


def is_rental_duration_addition(query: str) -> bool:
    """Return whether *query* asks to add days to an existing rental.

    The function only identifies the adjustment wording; callers still need
    to parse a concrete duration before applying it.  Traditional Chinese is
    normalized to Simplified Chinese so both scripts follow the same path.
    """

    return bool(_RENTAL_DURATION_ADDITION_RE.search(_simplified(str(query or ""))))


def is_rental_duration_target(query: str) -> bool:
    """Return whether a duration is stated as a new target total."""

    return bool(_RENTAL_DURATION_TARGET_RE.search(_simplified(str(query or ""))))


def _rental_duration_tokens(
    query: str,
    today: date | None = None,
) -> list[tuple[int, int, int]]:
    """Return ``(start, end, days)`` tokens for explicit durations."""

    text = str(query or "")
    # A lone ``5日`` is normally a duration (for example an eSIM validity),
    # not a calendar date.  Only mask date tokens after a *valid* range has
    # been recognized; this prevents the date parser from swallowing package
    # durations in otherwise ordinary price questions.
    date_range = _parse_rental_date_range(text, today=today)
    if date_range is None and _looks_like_date_range(text):
        return []
    date_tokens = _extract_date_tokens(text) if date_range is not None else []
    if date_tokens:
        chars = list(text)
        range_token_spans: set[tuple[int, int]] = set()
        for first, second in zip(date_tokens, date_tokens[1:]):
            if _has_date_range_connector(text, first, second):
                range_token_spans.update(
                    {(first.start, first.end), (second.start, second.end)}
                )
        duration_suffix = re.compile(
            r"^\s*(?:天|日|晚|夜|周|週|days?|nights?|weeks?)",
            re.I,
        )
        for token in date_tokens:
            # The English bare-day extractor also sees the number in “add 2
            # days” when a date range is present.  Preserve that number for
            # duration parsing; only mask genuine range components.
            if (
                (token.start, token.end) not in range_token_spans
                and duration_suffix.match(text[token.end :])
            ):
                continue
            for index in range(token.start, token.end):
                chars[index] = " "
        text = "".join(chars)
    tokens: list[tuple[int, int, int]] = []
    cn_pattern = rf"(?<![0-9.\-])(?P<number>{_CN_DURATION_NUMBER})\s*(?:天|日|晚|夜|周|週)"
    for match in re.finditer(cn_pattern, text, re.I):
        number = _parse_number_token(match.group("number"))
        unit = match.group(0)[-1]
        if number is not None:
            if unit in {"周", "週"}:
                number *= 7
            if 1 <= number <= 3660:
                tokens.append((match.start(), match.end(), number))
    en_pattern = (
        rf"(?<![0-9.\-])(?P<number>{_EN_DURATION_NUMBER})[-\s]*"
        r"(?:(?:more|extra|additional)[-\s]+)?"
        r"(?P<unit>days?|nights?|weeks?)\b"
    )
    for match in re.finditer(en_pattern, text, re.I):
        number = _parse_english_number(match.group("number"))
        if number is not None:
            if match.group("unit").casefold().startswith("week"):
                number *= 7
            if 1 <= number <= 3660:
                tokens.append((match.start(), match.end(), number))
    return sorted(tokens, key=lambda item: (item[0], item[1]))


def _parse_rental_duration(query: str) -> int | None:
    """Parse explicit day/night/week wording, excluding date components."""

    tokens = _rental_duration_tokens(query)
    return tokens[0][2] if tokens else None


def parse_rental_period(query: str, today: date | None = None) -> RentalPeriod | None:
    """Return the deterministic rental period requested by a customer.

    Date ranges take precedence over an explicit day count and are inclusive
    at both ends.  The parser intentionally returns ``None`` for ambiguous or
    invalid dates so the agent can show the regular current-price table rather
    than inventing a total.
    """

    date_range = _parse_rental_date_range(query, today=today)
    if date_range is not None:
        return date_range
    if _looks_like_date_range(query):
        return None
    days = _parse_rental_duration(query)
    return RentalPeriod(days=days, source="duration") if days is not None else None


def _resolve_embedded_duration_adjustments(
    query: str,
    *,
    today: date | None = None,
) -> RentalPeriod | None:
    """Resolve additive duration tokens already present in one text blob.

    This covers callers that pass a standalone concatenation of prior and
    current messages instead of supplying the prior period separately.  Only
    tokens joined by explicit adjustment wording are combined; unrelated
    numbers retain the normal parser's first-match behavior.
    """

    date_range = _parse_rental_date_range(query, today=today)
    tokens = _rental_duration_tokens(query, today=today)
    if len(tokens) < 2 and not (date_range is not None and tokens):
        return None
    period: RentalPeriod | None = date_range
    recognized_adjustment = False
    if period is None:
        period = RentalPeriod(days=tokens[0][2], source="duration")
        previous_end = tokens[0][1]
        duration_tokens = tokens[1:]
    else:
        date_tokens = _extract_date_tokens(str(query or ""))
        range_ends = [
            second.end
            for first, second in zip(date_tokens, date_tokens[1:])
            if _has_date_range_connector(str(query or ""), first, second)
        ]
        previous_end = max(range_ends, default=0)
        duration_tokens = tokens
    # A valid date range occupies the date tokens that were masked above; the
    # first explicit duration after it is an adjustment only when its connector
    # says so.  For ordinary duration text, the first token is the base.
    for start, end, days in duration_tokens:
        connector = str(query or "")[max(0, previous_end) : start]
        addition = is_rental_duration_addition(connector)
        target = is_rental_duration_target(connector)
        if target:
            # “改成10天” / “延长至10天” states a replacement total.
            recognized_adjustment = True
            period = RentalPeriod(days=days, source="duration")
        elif addition:
            recognized_adjustment = True
            total_days = (period.days if period is not None else 0) + days
            if 1 <= total_days <= 3660:
                if period is not None and period.start_date and period.end_date:
                    period = RentalPeriod(
                        days=total_days,
                        start_date=period.start_date,
                        end_date=period.end_date + timedelta(days=days),
                        source="date_range_adjusted",
                    )
                else:
                    period = RentalPeriod(days=total_days, source="duration_adjusted")
        else:
            period = RentalPeriod(days=days, source="duration")
        previous_end = end
    return period if recognized_adjustment else None


def _is_current_duration_addition(query: str) -> bool:
    """Check that adjustment wording belongs to the current duration token."""

    tokens = _rental_duration_tokens(query)
    if len(tokens) <= 1:
        return is_rental_duration_addition(query)
    previous_end = tokens[-2][1]
    connector = str(query or "")[previous_end : tokens[-1][0]]
    return is_rental_duration_addition(connector) and not is_rental_duration_target(
        connector
    )


def resolve_rental_period(
    query: str,
    *,
    previous: RentalPeriod | None = None,
    today: date | None = None,
) -> RentalPeriod | None:
    """Resolve a current duration against an optional previous quote.

    An explicit duration in the current message normally replaces the old
    one (``改成5天`` / ``five days``).  When the customer uses adjustment
    wording such as ``多加两天`` or ``add two more days``, the new duration is
    added to the previous quote.  Calendar ranges retain their original
    start date and extend their end date when days are added.

    This is intentionally separate from :func:`parse_rental_period`: parsing
    a standalone message must remain unambiguous, while conversation state
    is the only place where an additive interpretation is valid.
    """

    current = parse_rental_period(query, today=today)
    # If the current text itself contains a complete base duration plus an
    # adjustment (for example “7天，多加2天”), it is already self-contained;
    # do not add the caller's previous period a second time.
    embedded = _resolve_embedded_duration_adjustments(query, today=today)
    if embedded is not None:
        return embedded
    if current is None:
        # A plain follow-up such as “多少钱” keeps the previously quoted
        # period.  An adjustment without a concrete number (for example,
        # “再加几天”) is left unresolved so the caller can request the exact
        # number instead of silently re-quoting the old total.
        if (
            previous is not None
            and not is_rental_duration_addition(query)
            and not _looks_like_date_range(query)
        ):
            return previous
        return None
    if (
        previous is None
        or current.source != "duration"
        or not _is_current_duration_addition(query)
        or is_rental_duration_target(query)
    ):
        return current

    total_days = previous.days + current.days
    if not 1 <= total_days <= 3660:
        return current
    if previous.start_date is not None and previous.end_date is not None:
        return RentalPeriod(
            days=total_days,
            start_date=previous.start_date,
            end_date=previous.end_date + timedelta(days=current.days),
            source="date_range_adjusted",
        )
    return RentalPeriod(days=total_days, source="duration_adjusted")


def _has_unspecified_rental_period(query: str) -> bool:
    """Detect a request for a duration without a concrete number of days."""

    normalized = _simplified(query)
    return bool(
        re.search(r"(?:几|多少)\s*(?:天|日|晚|夜|周|週)", normalized)
        or re.search(
            r"\b(?:how many|number of)\s+(?:rental\s+)?(?:days?|nights?|weeks?)\b",
            normalized,
            re.I,
        )
    )


def _rental_period_payload(period: RentalPeriod | None) -> dict[str, Any] | None:
    if period is None:
        return None
    return {
        "days": period.days,
        "start_date": period.start_date.isoformat() if period.start_date else None,
        "end_date": period.end_date.isoformat() if period.end_date else None,
        "source": period.source,
    }


def rental_period_from_payload(value: object) -> RentalPeriod | None:
    """Safely restore a rental period stored in message metadata."""

    if not isinstance(value, dict):
        return None
    raw_days = value.get("days")
    if isinstance(raw_days, bool):
        return None
    try:
        days = int(raw_days)
    except (TypeError, ValueError):
        return None
    if not 1 <= days <= 3660:
        return None

    def parse_date(raw: object) -> date | None:
        if not isinstance(raw, str) or not raw.strip():
            return None
        try:
            return date.fromisoformat(raw.strip())
        except ValueError:
            return None

    start_date = parse_date(value.get("start_date"))
    end_date = parse_date(value.get("end_date"))
    if (start_date is None) != (end_date is None):
        # A partial range is not safe to extend; keep the day count as a
        # duration instead.
        start_date = None
        end_date = None
    source = str(value.get("source") or "duration")
    return RentalPeriod(
        days=days,
        start_date=start_date,
        end_date=end_date,
        source=source,
    )


def _contains_any(value: str, terms: tuple[str, ...]) -> bool:
    normalized = _simplified(value)
    return any(_simplified(term) in normalized for term in terms)


def _has_explicit_product_context(query: str) -> bool:
    return _contains_any(query, _EXPLICIT_PRODUCT_CONTEXT_TERMS)


def _has_non_product_recommendation_context(query: str) -> bool:
    return _contains_any(query, _NON_PRODUCT_RECOMMENDATION_TERMS)


def is_product_recommendation_query(query: str) -> bool:
    """Return whether *query* is a product-choice/recommendation request.

    Destination-only questions such as ``去日本哪个好`` are common shorthand
    for choosing a SongWiFi product.  The product matcher calls this helper
    only after it has found a real catalogue destination/product, so generic
    out-of-scope recommendations (restaurants, hotels, weather, etc.) are not
    pulled into the catalogue route.
    """

    normalized = _simplified(query)
    if _has_non_product_recommendation_context(query) and not _has_explicit_product_context(query):
        return False
    # Explicit alternatives are a guide/comparison question.  Keep those on
    # the semantic knowledge path rather than forcing one catalogue item to
    # win merely because the wording contains “which/better”.
    if re.search(r"(?:还是|或者|或)", normalized) or re.search(
        r"(?<![a-z0-9])(?:or|vs|versus)(?![a-z0-9])",
        normalized,
    ):
        return False
    if any(_simplified(term) in normalized for term in _PRODUCT_RECOMMENDATION_TERMS):
        return True
    # English customers commonly ask “which is best/better for …” without a
    # product noun.  Keep the phrase boundary so “switch” cannot match “which”.
    return bool(
        re.search(r"\bwhich\b", normalized)
        and re.search(r"\b(?:best|better|recommend|recommendation)\b", normalized)
    )


def _is_product_rental_query(query: str) -> bool:
    """Detect rental wording, including the terse Chinese ``租哪个`` form."""

    normalized = _simplified(query)
    explicit_terms = (
        "想租",
        "我要租",
        "租借",
        "租用",
        "出租",
        "租赁",
        "rent",
        "rental",
    )
    if any(_simplified(term) in normalized for term in explicit_terms):
        return True
    # A bare “租” is deliberately accepted only with a product/recommendation
    # cue.  This prevents unrelated phrases such as “去日本租车” from being
    # interpreted as a WiFi rental request merely because Japan is a catalogue
    # destination.
    return "租" in normalized and (
        is_product_recommendation_query(query)
        or _has_explicit_product_context(query)
        # A concrete rental period is itself a product-rental cue.  Without
        # this branch, “去日本租三天多少钱” was treated as a destination-only
        # price query and could incorrectly include eSIM plans.
        or parse_rental_period(query) is not None
        or _has_unspecified_rental_period(query)
    )


def _query_contains_alias(query: str, alias: str) -> bool:
    """Match CJK substrings and Latin aliases without unsafe partial words."""

    normalized_query = _simplified(query)
    normalized_alias = _simplified(alias)
    if not normalized_alias:
        return False
    if re.search(r"[a-z]", normalized_alias) and not re.search(
        r"[\u3400-\u9fff]", normalized_alias
    ):
        phrase = re.escape(normalized_alias).replace(r"\ ", r"[\s_-]+")
        return bool(
            re.search(
                rf"(?<![a-z0-9]){phrase}(?![a-z0-9])",
                normalized_query,
            )
        )
    compact_query = re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", normalized_query)
    compact_alias = re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", normalized_alias)
    return bool(compact_alias and compact_alias in compact_query)


def _safe_reverse_subject_match(subject: str, value: str) -> bool:
    """Allow shortened product phrases while rejecting tiny Latin fragments."""

    compact_subject = re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", _simplified(subject))
    compact_value = re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", _simplified(value))
    if not compact_subject or not compact_value:
        return False
    if re.fullmatch(r"[0-9a-z]+", compact_subject):
        if len(compact_subject) < 3:
            return False
        # Product acronyms and model tokens are matched as complete words by
        # the forward-alias branch; reverse substring matching is for ordinary
        # customer nouns such as camera, cable, fan, or thermos.
        if compact_subject in {"4g", "5g"}:
            return False
    elif len(compact_subject) < 2:
        return False
    return compact_subject in compact_value


def is_full_catalog_request(query: str) -> bool:
    return _contains_any(query, FULL_CATALOG_TERMS)


def _product_haystack(product: Product) -> str:
    values = [
        product.name,
        product.destination,
        product.network,
        product.category,
        product.product_type,
        product.source.name,
        product.source.domain,
        *_all_product_aliases(product),
        *(product.name_translations or {}).values(),
    ]
    return _simplified(" ".join(str(value) for value in values if value))


def _derived_product_aliases(product: Product) -> set[str]:
    """Return customer-facing aliases missing from the upstream catalogue.

    SongWiFi exposes official names and model numbers, while customers commonly
    ask with a generic Simplified-Chinese noun. These aliases are derived at
    query time so a fresh catalogue sync cannot erase them.
    """

    name = _simplified(
        " ".join(
            value
            for value in (
                product.name,
                *(product.name_translations or {}).values(),
            )
            if value
        )
    )
    aliases: set[str] = set()
    if "gopro" in name:
        aliases.update(
            _script_aliases(
                "GoPro",
                "Go Pro",
                "运动相机",
                "运动摄影机",
                "运动摄像机",
                "动作相机",
                "Action Camera",
                "Action Cameras",
                "Sports Camera",
                "Sports Cameras",
                "Video Camera",
                "Camera Rental",
                "Camera Rentals",
                "Camera",
                "Cameras",
                "相机",
            )
        )
    if "insta 360" in name or "insta360" in name:
        aliases.update(
            _script_aliases(
                "Insta360",
                "Insta 360",
                "全景相机",
                "360相机",
                "360度相机",
                "360度全景相机",
                "全景摄影机",
                "全景摄像机",
                "运动相机",
                "运动摄影机",
                "360 Camera",
                "360-degree Camera",
                "Panoramic Camera",
                "Action Camera",
                "Action Cameras",
                "Sports Camera",
                "Sports Cameras",
                "Camera Rental",
                "Camera Rentals",
                "Camera",
                "Cameras",
                "相机",
            )
        )
    if "osmo pocket" in name or "pocket 3" in name:
        aliases.update(
            _script_aliases(
                "Osmo Pocket",
                "Pocket 3",
                "口袋相机",
                "手持云台相机",
                "摄影机",
                "摄像机",
                "手持摄影机",
                "云台摄影机",
                "Vlog相机",
                "Vlog摄影机",
                "Pocket Camera",
                "Video Camera",
                "Vlog Camera",
                "Camera Rental",
                "Camera Rentals",
                "Camera",
                "Cameras",
                "相机",
            )
        )
    if "翻译机" in name:
        aliases.update(
            (
                "翻译机",
                "翻譯機",
                "翻译器",
                "翻譯器",
                "translator",
                "translation device",
                "translator device",
            )
        )
    if "儿童" in name and "相机" in name:
        aliases.update(
            _script_aliases(
                "儿童相机",
                "儿童摄影相机",
                "儿童数码相机",
                "儿童照相机",
                "小童相机",
                "小朋友相机",
                "children's camera",
                "child camera",
                "kids camera",
                "kid's camera",
                "相机",
            )
        )
    if "洗漱" in name or "洗护" in name:
        aliases.update(
            _script_aliases(
                "旅行洗漱套装",
                "洗漱套装",
                "洗漱杯套装",
                "洗护套装",
                "旅行洗护套装",
                "分装洗漱套装",
                "旅行分装瓶",
                "旅行洗漱杯",
                "wash kit",
                "travel wash kit",
                "toiletry kit",
                "toiletry set",
                "travel toiletry kit",
            )
        )
    if "保温杯" in name:
        aliases.update(
            (
                "保温杯",
                "保溫杯",
                "智能保温杯",
                "智能保溫杯",
                "thermos",
                "smart thermos",
                "insulated bottle",
                "smart bottle",
            )
        )
    if "lightning" in name or "rc-134i" in name:
        aliases.update(
            (
                "Lightning数据线",
                "Lightning數據線",
                "苹果数据线",
                "蘋果數據線",
                "苹果Lightning数据线",
                "蘋果Lightning數據線",
                "lightning cable",
                "iphone cable",
                "apple cable",
            )
        )
    if "type-c" in name or "type c" in name or "rc-134a" in name:
        aliases.update(
            _script_aliases(
                "Type-C数据线",
                "Type-C 数据线",
                "Type C数据线",
                "USB-C数据线",
                "USB C数据线",
                "Type-C充电线",
                "Type C充电线",
                "USB-C充电线",
                "C口数据线",
                "type-c cable",
                "type c cable",
                "usb-c cable",
                "usb c cable",
                "type-c data cable",
                "usb-c data cable",
                "charging cable",
                "data cable",
            )
        )
    if "一拖三" in name:
        aliases.update(
            (
                "一拖三充电线",
                "一拖三充電線",
                "三合一充电线",
                "三合一充電線",
                "3-in-1 charging cable",
                "three-in-one charging cable",
            )
        )
    if "折叠双头" in name:
        aliases.update(("折叠双头风扇", "折疊雙頭風扇", "双头风扇", "雙頭風扇"))
    if "迷你折叠" in name:
        aliases.update(("迷你折叠风扇", "迷你折疊風扇", "迷你风扇", "迷你風扇"))
    if "风扇" in name:
        aliases.update(("风扇", "風扇", "fan", "travel fan", "portable fan"))
    if "行李秤" in name:
        aliases.update(("行李秤", "行李磅", "手提行李秤", "luggage scale", "baggage scale"))
    if "万用插头" in name or "旅行充电器" in name:
        aliases.update(
            (
                "万用插头",
                "萬用插頭",
                "旅行插头",
                "旅行插頭",
                "旅行充电器",
                "旅行充電器",
                "travel adapter",
                "universal adapter",
                "universal plug",
            )
        )
    # Upstream option metadata can contain a stale SKU. Model codes embedded in
    # the official product name (for example RT-IG02 or RC-134a) remain stable
    # customer-facing identifiers and must be searchable independently.
    aliases.update(
        re.findall(
            r"(?<![a-z0-9])(?=[a-z0-9-]*[a-z])(?=[a-z0-9-]*\d)"
            r"[a-z0-9]+(?:-[a-z0-9]+)+(?![a-z0-9])",
            name,
        )
    )
    return aliases


def _all_product_aliases(product: Product) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            value.strip()
            for value in (*list(product.aliases or []), *_derived_product_aliases(product))
            if value and value.strip()
        )
    )


def _derived_english_name(product: Product) -> str | None:
    """Provide stable English display names for common untranslated shop items."""

    name = _simplified(product.name)
    codes = re.findall(
        r"(?i)(?<![a-z0-9])(?=[a-z0-9-]*[a-z])(?=[a-z0-9-]*\d)"
        r"[a-z0-9]+(?:-[a-z0-9]+)+(?![a-z0-9])",
        product.name,
    )
    code = f" {codes[0]}" if codes else ""
    if "儿童" in name and "相机" in name:
        return f"Children's Camera{code}"
    if "洗漱" in name or "洗护" in name:
        return f"Portable Travel Toiletry Kit{code}"
    if "保温杯" in name:
        return f"REMAX Smart Thermos{code}"
    if "lightning" in name or "rc-134i" in name:
        return f"REMAX Lightning Data Cable{code}"
    if "type-c" in name or "type c" in name or "rc-134a" in name:
        return f"REMAX Type-C Data Cable{code}"
    if "一拖三" in name:
        return f"REMAX 3-in-1 Charging Cable{code}"
    if "折叠双头" in name:
        return f"REMAX Portable Foldable Dual-head Fan{code}"
    if "迷你折叠" in name:
        return f"REMAX Portable Mini Foldable Fan{code}"
    if "风扇" in name:
        return f"REMAX Portable Fan{code}"
    if "行李秤" in name:
        return f"Portable Luggage Scale{code}"
    if "万用插头" in name or "旅行充电器" in name:
        return f"Universal Travel Adapter{code}"
    if "翻译机" in name:
        return f"AI Translation Device{code}"
    return None


def _english_offer_detail(value: str) -> str:
    """Translate common option labels and omit unknown CJK-only fragments."""

    source = str(value or "").strip()
    normalized = _simplified(source)
    exact = {
        "黑": "Black",
        "黑色": "Black",
        "白": "White",
        "白色": "White",
        "蓝": "Blue",
        "蓝色": "Blue",
        "粉": "Pink",
        "粉色": "Pink",
        "粉红": "Pink",
        "粉红色": "Pink",
        "标准价格": "Standard price",
        "标准规格": "Standard option",
        "每日租用": "Daily rental",
    }
    if normalized in exact:
        return exact[normalized]
    daily = re.fullmatch(r"每日\s*([0-9.]+)\s*(gb|mb)", normalized, re.I)
    if daily:
        return f"{daily.group(1)}{daily.group(2).upper()}/day"
    if "无限" in normalized or "不限量" in normalized:
        return "Unlimited data"
    if not re.search(r"[\u3400-\u9fff]", source):
        return source
    return ""


def product_search_aliases(product: Product) -> tuple[str, ...]:
    """Return upstream and derived aliases shared by catalogue and RAG search."""

    # Put focused customer vocabulary before upstream colour/SKU labels so the
    # most useful terms survive the bounded alias list embedded in RAG pages.
    derived = sorted(
        _derived_product_aliases(product),
        key=lambda value: (-len(value), _simplified(value), value),
    )
    values = (*derived, *list(product.aliases or []))
    return tuple(
        dict.fromkeys(
            alias
            for value in values
            for alias in (
                str(value or "").strip(),
                _t2s.convert(str(value or "").strip()),
                _s2t.convert(str(value or "").strip()),
            )
            if alias
        )
    )


def _query_subject(query: str) -> str:
    subject = _simplified(query)
    noise_terms = (
        *PRICE_TERMS,
        "请问",
        "麻烦",
        "我想要",
        "我想租",
        "我要租",
        "想租",
        "租借",
        "租用",
        "我要买",
        "我想买",
        "想买",
        "有没有",
        "有吗",
        "有冇",
        "提供吗",
        "给我",
        "发我",
        "最新",
    )
    for term in sorted({_simplified(item) for item in noise_terms}, key=len, reverse=True):
        subject = subject.replace(term, " ")
    subject = re.sub(r"(?:吗|呢|吧)$", "", subject.strip())
    subject = re.sub(r"^(?:有|提供|出售)\s*", "", subject)
    for term in ("商品", "产品", "商城", "网店", "eshop"):
        subject = subject.replace(term, " ")
    return re.sub(r"\s+", " ", subject).strip()


def _destination_aliases(value: str) -> set[str]:
    """Return exact and base aliases for a structured destination label."""

    normalized = _simplified(value)
    aliases: set[str] = set()
    for item in (normalized, *re.split(r"[-+&＆、,/]+", normalized)):
        compact = re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", item)
        compact = re.sub(
            r"(?:升级)?自动翻墙$|城市适用$|商业用$|(?:\d+国|多国)$|内地$",
            "",
            compact,
        )
        if len(compact) >= 2:
            aliases.add(compact)
    for canonical_terms, english_aliases in DESTINATION_ENGLISH_ALIASES:
        if any(_simplified(term) in normalized for term in canonical_terms):
            aliases.update(english_aliases)
    return aliases


def _product_score(product: Product, query: str) -> tuple[int, bool]:
    normalized_query = _simplified(query)
    haystack = _product_haystack(product)
    score = 0
    specific = False

    destination_aliases = _destination_aliases(product.destination or "")
    if any(_query_contains_alias(query, alias) for alias in destination_aliases):
        score += 100
        specific = True
    for alias in product.aliases or []:
        if not alias:
            continue
        normalized_alias = _simplified(alias)
        # Do not count the same country signal twice. eSIM feeds often expose
        # a bare alias such as "Japan" while WiFi feeds expose "5G Japan";
        # double-counting the bare alias made the eSIM record suppress every
        # other valid product for that destination.
        if any(
            normalized_alias == _simplified(destination_alias)
            for destination_alias in destination_aliases
        ):
            continue
        if len(normalized_alias) >= 3 and _query_contains_alias(query, normalized_alias):
            score += 80
            specific = True
            break
    broad_camera_aliases = {
        _simplified(value)
        for value in (
            "相机",
            "相機",
            "摄影机",
            "攝影機",
            "摄像机",
            "攝像機",
            "camera",
            "camera rental",
            "video camera",
        )
    }
    derived_scores = [
        90 if normalized_alias in broad_camera_aliases else 130
        for alias in _derived_product_aliases(product)
        for normalized_alias in [_simplified(alias)]
        if len(normalized_alias) >= 2 and _query_contains_alias(query, normalized_alias)
    ]
    if derived_scores:
        score += max(derived_scores)
        specific = True
    compact_name = re.sub(r"\b(?:4g|5g|wifi|esim)\b", "", _simplified(product.name)).strip()
    if compact_name and len(compact_name) >= 2 and _query_contains_alias(query, compact_name):
        score += 70
        specific = True

    # eShop names are often longer than the phrase a customer types. Strip
    # conversational price words, then match a meaningful product phrase in
    # the other direction (for example “风扇价格” -> a named fan product).
    subject = _query_subject(query)
    searchable_values = (
        product.name,
        product.description,
        *(product.name_translations or {}).values(),
        *_all_product_aliases(product),
    )
    if any(
        _safe_reverse_subject_match(subject, value)
        for value in searchable_values
        if value
    ):
        score += 70
        specific = True

    asks_4g = "4g" in normalized_query
    asks_5g = "5g" in normalized_query
    if asks_4g or asks_5g:
        requested = "4G" if asks_4g else "5G"
        if requested not in (product.network or "").upper():
            return 0, True
        score += 35
        specific = True
    asks_esim = "esim" in normalized_query
    asks_wifi = any(term in normalized_query for term in ("wifi", "wifi蛋", "随身wifi", "wifi egg"))
    asks_eshop = any(term in normalized_query for term in ("商城", "网店", "eshop"))
    asks_rental = _is_product_rental_query(query)
    if asks_rental:
        if product.product_type not in {"wifi_rental", "travel_gadget"}:
            return 0, True
        score += 25
    if asks_esim:
        if product.product_type != "esim":
            return 0, True
        score += 30
    elif asks_wifi:
        if product.product_type != "wifi_rental":
            return 0, True
        score += 20
    elif asks_eshop:
        if product.product_type != "eshop_product":
            return 0, True
        score += 30
        specific = True
    for gadget in ("gopro", "insta 360", "insta360", "翻译机", "翻譯機", "pocket 3"):
        if _simplified(gadget) in normalized_query:
            if _simplified(gadget) not in haystack:
                return 0, True
            score += 100
            specific = True
    source_terms = (_simplified(product.source.name), _simplified(product.source.domain))
    if any(term and _query_contains_alias(query, term) for term in source_terms):
        score += 20
        specific = True
    return score, specific


def _load_catalog_products(
    db: Session,
    tenant_id: int,
    *,
    include_offers: bool = True,
) -> list[Product]:
    options = [selectinload(Product.source)]
    if include_offers:
        options.append(selectinload(Product.offers))
    return list(
        db.scalars(
            select(Product)
            .join(ProductPriceSource, ProductPriceSource.id == Product.source_id)
            .where(
                Product.tenant_id == tenant_id,
                Product.is_active.is_(True),
            )
            .options(*options)
            .order_by(ProductPriceSource.name, Product.category, Product.name)
        ).unique().all()
    )


def _focus_scored_products(
    scored: list[tuple[int, Product]],
) -> list[tuple[int, Product]]:
    if not scored:
        return []
    strongest = max(score for score, _ in scored)
    if strongest >= 70:
        # An exact model/name is normally much stronger than a shared brand
        # alias. Keep equally strong broad matches, but discard weak brand
        # collisions such as an exact REMAX cup query returning every cable.
        minimum = max(70, strongest - 30)
        scored = [(score, product) for score, product in scored if score >= minimum]
    return sorted(
        scored,
        key=lambda item: (
            -item[0],
            item[1].source.name,
            item[1].category,
            item[1].name,
        ),
    )


def _scored_catalog_products(
    db: Session,
    tenant_id: int,
    query: str,
    *,
    include_offers: bool = False,
) -> list[tuple[int, Product]]:
    """Load and score the catalogue once for a retrieval decision.

    Keeping the shared score pass avoids loading the full product/offer graph
    twice when the RAG path checks intent and then asks for matching IDs.
    Structured catalogue rows remain authoritative for price and availability.
    """

    products = _load_catalog_products(
        db,
        tenant_id,
        include_offers=include_offers,
    )
    scored = [
        (score, product)
        for product in products
        for score, specific in [_product_score(product, query)]
        if score > 0 and specific
    ]
    if not scored:
        return []
    if (
        not include_offers
        and _has_non_product_recommendation_context(query)
        and not _has_explicit_product_context(query)
    ):
        return []
    return scored


def _matched_query_terms(product: Product, query: str) -> tuple[str, ...]:
    terms: set[str] = set()
    for value in (
        product.name,
        *(product.name_translations or {}).values(),
        *_all_product_aliases(product),
    ):
        compact = re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", _simplified(value))
        if len(compact) >= 2 and _query_contains_alias(query, str(value)):
            terms.add(compact)
    for alias in _destination_aliases(product.destination or ""):
        if _query_contains_alias(query, alias):
            terms.add(re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", _simplified(alias)))
    return tuple(sorted(terms, key=lambda value: (-len(value), value)))


def _has_named_product_match(product: Product, query: str) -> bool:
    """Distinguish a product/model mention from a destination-only mention."""

    subject = _query_subject(query)
    values = (
        product.name,
        *(product.name_translations or {}).values(),
        *_derived_product_aliases(product),
    )
    for value in values:
        compact = re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", _simplified(value))
        if len(compact) < 2:
            continue
        if _query_contains_alias(query, str(value)) or _safe_reverse_subject_match(
            subject, str(value)
        ):
            return True
    return False


def is_product_catalog_query(db: Session, tenant_id: int, query: str) -> bool:
    """Return whether a customer message should consult the product catalogue first."""

    scored = _scored_catalog_products(db, tenant_id, query)
    if not scored:
        return False
    # A known destination can also appear in an unrelated recommendation
    # (for example, “去日本哪个景点好”).  Only treat that wording as a
    # catalogue request when it contains an explicit product cue, or when it
    # is the domain-specific destination-choice shorthand handled below.
    if _has_non_product_recommendation_context(query) and not _has_explicit_product_context(
        query
    ):
        return False
    if _contains_any(query, PRODUCT_INTENT_TERMS) or is_product_recommendation_query(query):
        return True
    return any(_has_named_product_match(product, query) for _, product in scored)


def matching_product_catalog_ids(
    db: Session,
    tenant_id: int,
    query: str,
) -> tuple[int, ...]:
    """Return focused structured product IDs for retrieval metadata matching."""

    scored = _scored_catalog_products(db, tenant_id, query)
    if not scored:
        return ()
    if not (
        _contains_any(query, PRODUCT_INTENT_TERMS)
        or is_product_recommendation_query(query)
        or any(_has_named_product_match(product, query) for _, product in scored)
    ):
        return ()
    return tuple(product.id for _, product in _focus_scored_products(scored))


def product_price_subject_score(db: Session, tenant_id: int, query: str) -> int:
    """Return the strongest structured product/destination match score."""

    products = db.scalars(
        select(Product)
        .where(Product.tenant_id == tenant_id, Product.is_active.is_(True))
        .options(selectinload(Product.source))
    ).all()
    return max(
        (
            score
            for product in products
            for score, specific in [_product_score(product, query)]
            if score > 0 and specific
        ),
        default=0,
    )


def matches_product_price_subject(db: Session, tenant_id: int, query: str) -> bool:
    """Return whether a short follow-up names a known destination or product."""

    return product_price_subject_score(db, tenant_id, query) > 0


def _localized_name(product: Product, language: str) -> str:
    translations = product.name_translations or {}
    if language == "en":
        return translations.get("en") or _derived_english_name(product) or product.name
    if language == "zh-TW":
        return translations.get("zh-TW") or _s2t.convert(product.name)
    return translations.get("zh-CN") or _t2s.convert(product.name)


def _category_label(category: str, language: str) -> str:
    if language == "en":
        return ENGLISH_CATEGORY_LABELS.get(category, ENGLISH_CATEGORY_LABELS["other"])
    labels = CATEGORY_LABELS.get(category, CATEGORY_LABELS["other"])
    return labels[1] if language == "zh-TW" else labels[0]


def _currency_amount(offer: ProductPriceOffer, amount: Decimal | None = None) -> str:
    currency = "HK$" if offer.currency.upper() == "HKD" else f"{offer.currency.upper()} "
    value = offer.price_amount if amount is None else amount
    return f"{currency}{_money(Decimal(value))}"


def _offer_line(product: Product, offer: ProductPriceOffer, language: str) -> str:
    name = _localized_name(product, language)
    currency = "HK$" if offer.currency.upper() == "HKD" else f"{offer.currency.upper()} "
    current = f"{currency}{_money(offer.price_amount)}"
    if offer.unit == "day":
        current += "/day" if language == "en" else "/日"
    details: list[str] = []
    if offer.data_label:
        detail = (
            _english_offer_detail(offer.data_label)
            if language == "en"
            else _t2s.convert(offer.data_label)
            if language == "zh-CN"
            else offer.data_label
        )
        if detail:
            details.append(detail)
    if offer.duration_days:
        details.append(
            f"{offer.duration_days} days" if language == "en" else f"{offer.duration_days}日"
        )
    if offer.unit not in {"day"} and offer.label not in {"标准价格", "標準價格", "标准规格", "標準規格", "每日租用"}:
        label = (
            _english_offer_detail(offer.label)
            if language == "en"
            else _t2s.convert(offer.label)
            if language == "zh-CN"
            else offer.label
        )
        # eSIM labels usually repeat the destination; structured fields are clearer.
        if label and not details and label not in name:
            details.append(label)
    parts = [f"• {name}"]
    if details:
        parts.append(" / ".join(details))
    parts.append(current)
    line = "｜".join(parts)
    if offer.original_amount is not None:
        original = f"{currency}{_money(offer.original_amount)}"
        if offer.unit == "day":
            original += "/day" if language == "en" else "/日"
        original_label = (
            "original price"
            if language == "en"
            else "原價"
            if language == "zh-TW"
            else "原价"
        )
        line += f" ({original_label} {original})" if language == "en" else f"（{original_label} {original}）"
    return line


_FIXED_PLAN_UNITS = {"plan", "package", "套餐", "方案", "item"}


def _offer_duration_days(offer: ProductPriceOffer) -> int | None:
    """Return a valid advertised package duration, if one is available."""

    raw_duration = offer.duration_days
    if raw_duration is None or isinstance(raw_duration, bool):
        return None
    try:
        duration = int(raw_duration)
    except (TypeError, ValueError):
        return None
    return duration if duration > 0 else None


def _is_fixed_plan_offer(offer: ProductPriceOffer) -> bool:
    unit = str(offer.unit or "").strip().casefold()
    return unit in _FIXED_PLAN_UNITS and _offer_duration_days(offer) is not None


def _selected_esim_quote_ids(
    entries: list[tuple[Product, ProductPriceOffer]],
    period: RentalPeriod | None,
) -> set[int]:
    """Choose eSIM plans that can cover the requested period.

    eSIM prices are package prices, not daily rental rates.  Prefer every
    exact-duration option; when no exact option exists, use the shortest
    available duration that is at least as long as the request.  Keeping all
    options at that duration lets customers compare data allowances without
    inventing a prorated price.
    """

    if period is None:
        return set()
    grouped: dict[int, list[ProductPriceOffer]] = {}
    for product, offer in entries:
        if product.product_type != "esim" or not _is_fixed_plan_offer(offer):
            continue
        if product.id is None or offer.id is None:
            continue
        grouped.setdefault(int(product.id), []).append(offer)

    selected: set[int] = set()
    for offers in grouped.values():
        exact = [
            offer
            for offer in offers
            if _offer_duration_days(offer) == period.days
        ]
        candidates = exact
        if not candidates:
            covering = [
                offer
                for offer in offers
                if (_offer_duration_days(offer) or 0) >= period.days
            ]
            if covering:
                shortest = min(_offer_duration_days(offer) for offer in covering)
                candidates = [
                    offer
                    for offer in covering
                    if _offer_duration_days(offer) == shortest
                ]
        selected.update(int(offer.id) for offer in candidates if offer.id is not None)
    return selected


def _estimate_offer_amount(
    offer: ProductPriceOffer,
    period: RentalPeriod | None,
    *,
    allow_coverage: bool = False,
) -> tuple[Decimal, str] | None:
    """Return a quote only when the offer's billing unit is unambiguous.

    Daily rental offers are multiplied by the requested inclusive day count.
    Fixed plans (eSIM, etc.) are quoted at their package price.  Exact validity
    is preferred; callers may explicitly allow a longer plan that covers the
    request, but a fixed plan is never prorated.
    """

    if period is None or not offer.is_active or offer.availability == "out_of_stock":
        return None
    unit = str(offer.unit or "").strip().casefold()
    if unit in {"day", "daily", "per_day", "per-day"}:
        amount = (Decimal(offer.price_amount) * period.days).quantize(Decimal("0.01"))
        return amount, "daily"
    duration_days = _offer_duration_days(offer)
    if duration_days is not None and unit in _FIXED_PLAN_UNITS:
        if duration_days == period.days or (allow_coverage and duration_days > period.days):
            return Decimal(offer.price_amount).quantize(Decimal("0.01")), "package"
    return None


def _rental_estimate_line(
    product: Product,
    offer: ProductPriceOffer,
    period: RentalPeriod | None,
    language: str,
    *,
    allow_coverage: bool = False,
) -> str | None:
    estimate = _estimate_offer_amount(offer, period, allow_coverage=allow_coverage)
    if estimate is None or period is None:
        return None
    amount, kind = estimate
    amount_text = _currency_amount(offer, amount)
    rate_text = _currency_amount(offer)
    plan_duration = _offer_duration_days(offer)
    is_covering_plan = (
        kind == "package"
        and plan_duration is not None
        and plan_duration > period.days
    )
    coverage_label = "覆蓋" if language == "zh-TW" else "覆盖"
    if language == "en":
        if period.start_date and period.end_date:
            period_text = (
                f"{period.start_date:%Y-%m-%d} to {period.end_date:%Y-%m-%d} "
                f"({period.days} days, inclusive)"
            )
        else:
            period_text = f"{period.days} days"
        if kind == "daily":
            prefix = "Estimated total"
        elif is_covering_plan:
            prefix = "Covering plan total"
        else:
            prefix = "Matching plan total"
        disclaimer = "(Final price is subject to checkout on the website.)"
        calculation = (
            f"{rate_text}/day × {period.days} days = {amount_text}"
            if kind == "daily"
            else amount_text
        )
        if is_covering_plan:
            calculation = (
                f"{amount_text} ({plan_duration}-day plan covering "
                f"{period.days} requested days)"
            )
        return f"  {prefix} for {period_text}: {calculation} {disclaimer}"

    if period.start_date and period.end_date:
        if language == "zh-TW":
            period_text = (
                f"{period.start_date:%Y-%m-%d} 至 {period.end_date:%Y-%m-%d}"
                f"，共{period.days}日，含首尾"
            )
            if kind == "daily":
                prefix = "預計租借費用"
            elif is_covering_plan:
                prefix = "可覆蓋方案費用"
            else:
                prefix = "相符方案費用"
            disclaimer = "（實際以網址結算為準）"
        else:
            period_text = (
                f"{period.start_date:%Y-%m-%d} 至 {period.end_date:%Y-%m-%d}"
                f"，共{period.days}天，含首尾"
            )
            if kind == "daily":
                prefix = "预计租借费用"
            elif is_covering_plan:
                prefix = "可覆盖方案费用"
            else:
                prefix = "匹配套餐费用"
            disclaimer = "（实际以网址结算为准）"
        if kind == "daily":
            calculation = f"{rate_text}/日 × {period.days}日 = {amount_text}"
        elif is_covering_plan:
            calculation = f"{amount_text}（{plan_duration}日套餐，{coverage_label}{period.days}日需求）"
        else:
            calculation = amount_text
        return f"  {prefix}（{period_text}）：{calculation}{disclaimer}"

    if language == "zh-TW":
        unit_label = "日"
        if kind == "daily":
            prefix = "預計租借費用"
        elif is_covering_plan:
            prefix = "可覆蓋方案費用"
        else:
            prefix = "相符方案費用"
        disclaimer = "（實際以網址結算為準）"
    else:
        unit_label = "天"
        if kind == "daily":
            prefix = "预计租借费用"
        elif is_covering_plan:
            prefix = "可覆盖方案费用"
        else:
            prefix = "匹配套餐费用"
        disclaimer = "（实际以网址结算为准）"
    if kind == "daily":
        calculation = (
            f"{rate_text}/{unit_label} × {period.days}{unit_label} = {amount_text}"
        )
    elif is_covering_plan:
        calculation = (
            f"{amount_text}（{plan_duration}{unit_label}套餐，{coverage_label}{period.days}"
            f"{unit_label}需求）"
        )
    else:
        calculation = amount_text
    return f"  {prefix}（{period.days}{unit_label}）：{calculation}{disclaimer}"


def _customer_offer_url(product: Product, offer: ProductPriceOffer) -> str:
    """Return the most specific safe HTTP(S) URL available for an offer."""

    metadata = offer.metadata_json or {}
    candidates = (
        metadata.get("order_url"),
        metadata.get("offer_url"),
        product.canonical_url,
        product.source.root_url,
    )
    base_url = product.canonical_url or product.source.root_url
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        absolute = urljoin(base_url, candidate.strip())
        parsed = urlsplit(absolute)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            continue
        if parsed.username or parsed.password:
            continue
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))
    # Product price sources are validated as public HTTP(S) roots when they are
    # created, so this branch is only a defensive fallback for legacy rows.
    return product.source.root_url


def _offer_block(
    product: Product,
    offer: ProductPriceOffer,
    language: str,
    *,
    rental_period: RentalPeriod | None = None,
    allow_fixed_coverage: bool = False,
) -> str:
    labels = {
        "en": "  Purchase link: ",
        "zh-TW": "  購買連結：",
        "zh-CN": "  购买链接：",
    }
    label = labels.get(language, labels["zh-CN"])
    parts = [_offer_line(product, offer, language)]
    estimate_line = _rental_estimate_line(
        product,
        offer,
        rental_period,
        language,
        allow_coverage=allow_fixed_coverage,
    )
    if estimate_line:
        parts.append(estimate_line)
    parts.append(f"{label}{_customer_offer_url(product, offer)}")
    return "\n".join(parts)


def _product_type_label(product: Product, language: str = "zh-TW") -> str:
    if language == "en":
        labels = {
            "wifi_rental": "WiFi rental",
            "esim": "eSIM plan",
            "travel_gadget": "Travel equipment rental",
            "eshop_product": "Shop product",
            "product": "Product",
        }
        return labels.get(product.product_type, _category_label(product.category, language))
    return PRODUCT_TYPE_LABELS.get(
        product.product_type,
        _category_label(product.category, language),
    )


def _catalog_product_document(
    product: Product,
    query: str,
    language: str = "zh-TW",
) -> Document:
    active_offers = [offer for offer in product.offers if offer.is_active]
    available_offers = [
        offer for offer in active_offers if offer.availability != "out_of_stock"
    ]
    if available_offers:
        availability_code = "in_stock"
        availability = (
            "Product exists; currently available"
            if language == "en"
            else "產品存在，目前可供應"
        )
    elif active_offers:
        availability_code = "out_of_stock"
        availability = (
            "Product exists; currently out of stock"
            if language == "en"
            else "產品存在，目前缺貨"
        )
    else:
        availability_code = "unavailable"
        availability = (
            "Product exists; no verified supply data is currently available"
            if language == "en"
            else "產品存在，暫無有效供應資料"
        )

    name = _localized_name(product, language)
    if language == "en":
        lines = [
            "STRUCTURED PRODUCT CATALOG (authoritative)",
            f"Product: {name}",
            f"Type: {_product_type_label(product, language)}",
            f"Availability: {availability}",
        ]
    else:
        lines = [
            "結構化產品目錄（優先資料）",
            f"產品：{name}",
            f"類型：{_product_type_label(product, language)}",
            f"供應狀態：{availability}",
        ]
    if active_offers:
        lines.append("Price options:" if language == "en" else "價格方案：")
        for offer in active_offers[:20]:
            # Keep each checkout URL adjacent to its own option.  A product
            # can expose several eSIM plans (for example 3GB/5日 and
            # unlimited/10日); putting only the first URL at the bottom of the
            # document lets a generation model attach the wrong link to a
            # requested variant.
            line = _offer_block(product, offer, language)
            if offer.availability == "out_of_stock":
                line += " (out of stock)" if language == "en" else "（缺貨）"
            lines.append(line)
    if product.description.strip():
        description = product.description.strip()[:1000]
        if language == "en":
            if not re.search(r"[\u3400-\u9fff]", description):
                lines.append(f"Official description: {description}")
        else:
            lines.append(f"產品說明：{_s2t.convert(description)}")
    offer = active_offers[0] if active_offers else None
    url = _customer_offer_url(product, offer) if offer is not None else product.canonical_url
    lines.append(f"Product link: {url}" if language == "en" else f"產品連結：{url}")
    return Document(
        page_content="\n".join(lines),
        metadata={
            "product_id": product.id,
            "title": name,
            "source": url,
            "source_url": url,
            "page_title": name,
            "section_path": "产品目录",
            "source_updated_at": product.updated_at.isoformat() if product.updated_at else None,
            "category": product.category,
            "source_type": "structured_product",
            "retrieval_mode": "structured_product_first",
            "product_name": name,
            "destination": _simplified(product.destination or ""),
            "match_terms": list(_matched_query_terms(product, query)),
            "availability": availability,
            "availability_code": availability_code,
        },
    )


def query_product_catalog_documents(
    db: Session,
    tenant_id: int,
    query: str,
    *,
    limit: int = 8,
    language: str = "zh-TW",
) -> list[Document]:
    """Return authoritative catalogue records before supplemental RAG evidence."""

    scored = _scored_catalog_products(
        db,
        tenant_id,
        query,
        include_offers=True,
    )
    focused = _focus_scored_products(scored)
    return [
        _catalog_product_document(product, query, language)
        for _, product in focused[: max(1, min(limit, 20))]
    ]


def _out_of_stock_block(product: Product, language: str) -> str:
    name = _localized_name(product, language)
    messages = {
        "en": f"• {name} | Product exists; currently out of stock",
        "zh-TW": f"• {name}｜產品存在，目前缺貨",
        "zh-CN": f"• {name}｜产品存在，目前缺货",
    }
    link_labels = {
        "en": "  Product link: ",
        "zh-TW": "  商品連結：",
        "zh-CN": "  商品链接：",
    }
    active_offer = next((offer for offer in product.offers if offer.is_active), None)
    url = (
        _customer_offer_url(product, active_offer)
        if active_offer is not None
        else product.canonical_url
    )
    selected_language = language if language in {"en", "zh-TW"} else "zh-CN"
    return f"{messages[selected_language]}\n{link_labels[selected_language]}{url}"


def _out_of_stock_segment(products: list[Product], language: str) -> str:
    headings = {
        "en": "*Product availability*",
        "zh-TW": "*商品供應狀態*",
        "zh-CN": "*商品供应状态*",
    }
    selected_language = language if language in {"en", "zh-TW"} else "zh-CN"
    return "\n".join(
        [headings[selected_language]]
        + [_out_of_stock_block(product, language) for product in products]
    )


def _clarification(language: str) -> str:
    if language == "en":
        return "Please tell me the destination or product name (for example, Japan 5G or Korea eSIM), and I will send the matching current prices."
    if language == "zh-TW":
        return "請告訴我目的地或商品名稱（例如日本 5G、韓國 eSIM），我會直接傳送相符的最新價格。"
    return "请告诉我目的地或商品名称（例如日本 5G、韩国 eSIM），我会直接发送匹配的最新价格。"


def _rental_period_clarification(language: str) -> str:
    if language == "en":
        return (
            "Please provide the number of rental days or the start and end dates so I can calculate "
            "an estimate (final price is subject to checkout on the website)."
        )
    if language == "zh-TW":
        return "請提供具體租借日數，或開始／結束日期，我可以為你計算預計總價（實際以網址結算為準）。"
    return "请提供具体租借天数，或开始／结束日期，我可以为你计算预计总价（实际以网址结算为准）。"


def query_product_price_catalog(
    db: Session,
    tenant_id: int,
    query: str,
    *,
    language: str = "zh-CN",
    full_catalog: bool | None = None,
    page_size: int = 20,
    rental_period_override: RentalPeriod | None = None,
) -> dict[str, Any]:
    full = is_full_catalog_request(query) if full_catalog is None else full_catalog
    rental_period = (
        rental_period_override
        if rental_period_override is not None
        else resolve_rental_period(query)
    )
    period_payload = _rental_period_payload(rental_period)
    unspecified_period = _has_unspecified_rental_period(query)
    products = _load_catalog_products(db, tenant_id)
    scored: list[tuple[int, Product]] = []
    has_specific_filter = False
    for product in products:
        score, specific = _product_score(product, query)
        has_specific_filter = has_specific_filter or specific
        if full or score > 0:
            scored.append((score, product))
    if not full and not has_specific_filter:
        return {
            "found": False,
            "needs_clarification": True,
            "segments": [_clarification(language)],
            "sources": [],
            "count": 0,
            "full_catalog": False,
            "rental_period": period_payload,
            "estimates": [],
            "rental_period_requested": unspecified_period,
        }
    if not scored:
        no_match = {
            "en": "I could not find a verified current price for that destination or product. Please check the name or ask for a human agent.",
            "zh-TW": "暫時找不到該目的地或商品的已核實價格，請檢查名稱，或要求轉接人工客服。",
            "zh-CN": "暂时找不到该目的地或商品的已核实价格，请检查名称，或要求转接人工客服。",
        }[language if language in {"en", "zh-TW"} else "zh-CN"]
        return {
            "found": False,
            "needs_clarification": False,
            "segments": [no_match],
            "sources": [],
            "count": 0,
            "full_catalog": full,
            "rental_period": period_payload,
            "estimates": [],
            "rental_period_requested": unspecified_period,
        }
    if not full:
        scored = _focus_scored_products(scored)
    else:
        scored.sort(
            key=lambda item: (
                item[1].source.name,
                item[1].category,
                item[1].name,
            )
        )
    entries: list[tuple[Product, ProductPriceOffer]] = []
    for _, product in scored:
        for offer in product.offers:
            if offer.is_active and offer.availability != "out_of_stock":
                entries.append((product, offer))

    unavailable_products = [
        product
        for _, product in scored
        if any(offer.is_active for offer in product.offers)
        and not any(
            offer.is_active and offer.availability != "out_of_stock"
            for offer in product.offers
        )
    ]
    if not entries and unavailable_products:
        source_map = {
            product.source_id: {
                "source_id": product.source_id,
                "title": product.source.name,
                "source": product.source.root_url,
                "source_type": "structured_product",
            }
            for product in unavailable_products
        }
        return {
            "found": True,
            "needs_clarification": False,
            "segments": [_out_of_stock_segment(unavailable_products, language)],
            "sources": list(source_map.values()),
            "count": 0,
            "out_of_stock_count": len(unavailable_products),
            "full_catalog": full,
            "rental_period": period_payload,
            "estimates": [],
            "rental_period_requested": unspecified_period,
        }
    if not entries:
        return {
            "found": False,
            "needs_clarification": False,
            "segments": [_clarification(language)],
            "sources": [],
            "count": 0,
            "full_catalog": full,
            "rental_period": period_payload,
            "estimates": [],
            "rental_period_requested": unspecified_period,
        }
    page_size = max(1, min(page_size, 20))
    pages = [entries[index : index + page_size] for index in range(0, len(entries), page_size)]
    segments: list[str] = []
    estimates: list[dict[str, Any]] = []
    selected_esim_quote_ids = _selected_esim_quote_ids(entries, rental_period)
    total_pages = len(pages)
    for page_index, page in enumerate(pages, start=1):
        if language == "en":
            title = "*Current product prices*"
        elif language == "zh-TW":
            title = "*最新商品價格*"
        else:
            title = "*最新商品价格*"
        if total_pages > 1:
            title += f"（{page_index}/{total_pages}）"
        lines = [title]
        last_group: tuple[int, str] | None = None
        for product, offer in page:
            group = (product.source_id, product.category)
            if group != last_group:
                lines.append(f"\n*{product.source.name} · {_category_label(product.category, language)}*")
                last_group = group
            allow_fixed_coverage = offer.id in selected_esim_quote_ids
            lines.append(
                _offer_block(
                    product,
                    offer,
                    language,
                    rental_period=rental_period,
                    allow_fixed_coverage=allow_fixed_coverage,
                )
            )
            estimate = _estimate_offer_amount(
                offer,
                rental_period,
                allow_coverage=allow_fixed_coverage,
            )
            if estimate is not None and rental_period is not None:
                amount, kind = estimate
                estimates.append(
                    {
                        "product_id": product.id,
                        "offer_id": offer.id,
                        "currency": offer.currency.upper(),
                        "amount": str(amount),
                        "days": rental_period.days,
                        "kind": kind,
                    }
                )
        if page_index == total_pages:
            latest = max(offer.last_seen_at for _, offer in entries)
            if latest.tzinfo is None:
                # SQLite returns timezone-aware columns as naive UTC values.
                latest = latest.replace(tzinfo=timezone.utc)
            local = latest.astimezone(ZoneInfo(settings.knowledge_sync_timezone))
            if language == "en":
                lines.append(f"\nSynced: {local:%Y-%m-%d %H:%M} ({settings.knowledge_sync_timezone}). Final checkout price prevails.")
            elif language == "zh-TW":
                lines.append(f"\n同步時間：{local:%Y-%m-%d %H:%M}（{settings.knowledge_sync_timezone}）。最終以訂單確認價格為準。")
            else:
                lines.append(f"\n同步时间：{local:%Y-%m-%d %H:%M}（{settings.knowledge_sync_timezone}）。最终以订单确认价格为准。")
        segments.append("\n".join(lines))
    if unavailable_products:
        segments.append(_out_of_stock_segment(unavailable_products, language))
    source_map: dict[int, dict[str, str | int]] = {}
    for product, _ in entries:
        source_map[product.source_id] = {
            "source_id": product.source_id,
            "title": product.source.name,
            "source": product.source.root_url,
            "source_type": "structured_product",
        }
    for product in unavailable_products:
        source_map[product.source_id] = {
            "source_id": product.source_id,
            "title": product.source.name,
            "source": product.source.root_url,
            "source_type": "structured_product",
        }
    if rental_period is None and unspecified_period and segments:
        segments[-1] = f"{segments[-1]}\n\n{_rental_period_clarification(language)}"
    return {
        "found": True,
        "needs_clarification": False,
        "segments": segments,
        "sources": list(source_map.values()),
        "count": len(entries),
        "out_of_stock_count": len(unavailable_products),
        "full_catalog": full,
        "rental_period": period_payload,
        "estimates": estimates,
        "rental_period_requested": unspecified_period,
    }


def build_product_price_tool(
    db: Session,
    tenant_id: int,
    language: str,
    *,
    rental_period_override: RentalPeriod | None = None,
):
    @tool
    def search_product_prices(query: str, full_catalog: bool = False) -> dict[str, Any]:
        """Read current verified product prices from the catalogue database.

        Use full_catalog only when the customer explicitly asks for every product or a
        complete price list. Otherwise pass the customer's destination/product query.
        """

        query_kwargs: dict[str, Any] = {
            "language": language,
            "full_catalog": full_catalog or is_full_catalog_request(query),
        }
        if rental_period_override is not None:
            query_kwargs["rental_period_override"] = rental_period_override
        return query_product_price_catalog(db, tenant_id, query, **query_kwargs)

    return search_product_prices
