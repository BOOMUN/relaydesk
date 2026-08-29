from __future__ import annotations

import html as html_module
import ipaddress
import re
import socket
import time
from collections import deque
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader


USER_AGENT = "AgentDeskKnowledgeBot/0.1"
MAX_HTML_BYTES = 5 * 1024 * 1024
MAX_PDF_BYTES = 15 * 1024 * 1024
MAX_PDF_PAGES = 200
MAX_EXTRACTED_CHARS = 2_000_000
MAX_REDIRECTS = 5
TRACKING_PARAMETERS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
}
# Common non-semantic containers used by the source site for navigation and
# subscription chrome.  The crawler already removes semantic nav/footer
# elements, but these wrappers are ordinary divs in several templates.
_BOILERPLATE_CLASS_TOKENS = frozenset(
    {
        "header",
        "footer",
        "siteheader",
        "sitefooter",
        "mainmenu",
        "navbar",
        "navigation",
        "navbuttons",
        "subscribeform",
        "pmenubar",
    }
)
SKIPPED_EXTENSIONS = {
    ".7z",
    ".avi",
    ".css",
    ".csv",
    ".doc",
    ".docx",
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".json",
    ".mov",
    ".mp3",
    ".mp4",
    ".png",
    ".ppt",
    ".pptx",
    ".rar",
    ".svg",
    ".tar",
    ".webp",
    ".xls",
    ".xlsx",
    ".xml",
    ".zip",
}


class CrawlError(RuntimeError):
    pass


class UnsafeUrlError(CrawlError):
    pass


class FetchError(CrawlError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


@dataclass(slots=True)
class FetchResult:
    url: str
    content_type: str
    body: bytes
    encoding: str
    last_modified: str | None = None


@dataclass(slots=True)
class CrawledPage:
    url: str
    title: str
    content: str
    content_type: str
    language: str
    metadata: dict[str, Any]


def normalize_public_root_url(value: str) -> str:
    normalized = normalize_url(value)
    validate_public_url(normalized)
    return normalized


def normalize_url(value: str, *, base_url: str | None = None) -> str:
    candidate = urljoin(base_url, value.strip()) if base_url else value.strip()
    parsed = urlsplit(candidate)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise UnsafeUrlError("只允许使用 http 或 https 公开网址")
    if parsed.username or parsed.password:
        raise UnsafeUrlError("网址不能包含用户名或密码")
    host = parsed.hostname
    if not host:
        raise UnsafeUrlError("网址缺少有效域名")
    try:
        host = host.encode("idna").decode("ascii").lower()
        port = parsed.port
    except (UnicodeError, ValueError) as exc:
        raise UnsafeUrlError("网址域名或端口无效") from exc
    if port not in {None, 80, 443}:
        raise UnsafeUrlError("公开网站采集仅允许 80 或 443 端口")

    netloc = f"[{host}]" if ":" in host else host
    default_port = 80 if parsed.scheme.lower() == "http" else 443
    if port and port != default_port:
        netloc = f"{netloc}:{port}"
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    query_items = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_PARAMETERS
    ]
    return urlunsplit(
        (parsed.scheme.lower(), netloc, path, urlencode(sorted(query_items)), "")
    )


def validate_public_url(url: str, *, allowed_site: str | None = None) -> None:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if allowed_site and _site_key(host) != allowed_site:
        raise UnsafeUrlError("采集链接超出输入网址所属域名")
    try:
        literal = ipaddress.ip_address(host)
        addresses = {literal}
    except ValueError:
        try:
            records = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise UnsafeUrlError("无法解析网址域名") from exc
        addresses = {ipaddress.ip_address(record[4][0]) for record in records}
    if not addresses or any(not address.is_global for address in addresses):
        raise UnsafeUrlError("为防止访问内网资源，只允许解析到公网 IP 的网址")


def _site_key(host: str) -> str:
    normalized = host.lower().rstrip(".")
    return normalized.removeprefix("www.")


def _is_crawlable_url(url: str, allowed_site: str) -> bool:
    try:
        normalized = normalize_url(url)
    except UnsafeUrlError:
        return False
    parsed = urlsplit(normalized)
    if _site_key(parsed.hostname or "") != allowed_site:
        return False
    suffix = PurePosixPath(parsed.path.lower()).suffix
    return suffix == ".pdf" or suffix not in SKIPPED_EXTENSIONS


class SafeHttpClient:
    def __init__(self, allowed_site: str) -> None:
        self.allowed_site = allowed_site
        self.client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/pdf;q=0.9,*/*;q=0.1"},
            follow_redirects=False,
            timeout=httpx.Timeout(15.0, connect=8.0),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            trust_env=False,
        )

    def close(self) -> None:
        self.client.close()

    def get(self, url: str, *, max_bytes: int) -> FetchResult:
        current = normalize_url(url)
        for _ in range(MAX_REDIRECTS + 1):
            validate_public_url(current, allowed_site=self.allowed_site)
            try:
                with self.client.stream("GET", current) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise FetchError("网站返回了没有目标地址的跳转")
                        current = normalize_url(location, base_url=current)
                        continue
                    if response.status_code >= 400:
                        raise FetchError(
                            f"网站返回 HTTP {response.status_code}",
                            status_code=response.status_code,
                        )
                    content_length = response.headers.get("content-length")
                    if content_length and int(content_length) > max_bytes:
                        raise FetchError("页面文件超过允许大小")
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise FetchError("页面文件超过允许大小")
                        chunks.append(chunk)
                    media_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                    return FetchResult(
                        url=normalize_url(str(response.url)),
                        content_type=media_type,
                        body=b"".join(chunks),
                        encoding=response.encoding or "utf-8",
                        last_modified=response.headers.get("last-modified"),
                    )
            except (httpx.HTTPError, ValueError) as exc:
                raise FetchError("网站连接失败") from exc
        raise FetchError("网站跳转次数过多")


class WebsiteCrawler:
    def __init__(self, root_url: str, *, max_pages: int = 500, max_depth: int = 5) -> None:
        self.root_url = normalize_public_root_url(root_url)
        self.max_pages = max_pages
        self.max_depth = max_depth
        self.allowed_site = _site_key(urlsplit(self.root_url).hostname or "")
        self.http = SafeHttpClient(self.allowed_site)
        self.discovered_count = 0
        self.failed_count = 0
        self.skipped_count = 0
        self.processed_count = 0
        self.limit_reached = False
        self.errors: list[str] = []

    def close(self) -> None:
        self.http.close()

    def crawl(self) -> Iterator[CrawledPage]:
        robots = self._robots_parser()
        queue: deque[tuple[str, int]] = deque([(self.root_url, 0)])
        scheduled = {self.root_url}
        for sitemap_url in self._sitemap_pages(robots):
            if len(scheduled) >= self.max_pages * 3:
                break
            if sitemap_url not in scheduled:
                scheduled.add(sitemap_url)
                queue.append((sitemap_url, 1))
        self.discovered_count = len(scheduled)
        processed = 0
        crawl_delay = robots.crawl_delay(USER_AGENT) or robots.crawl_delay("*") or 0
        last_request_at = 0.0

        try:
            while queue and processed < self.max_pages:
                url, depth = queue.popleft()
                if not robots.can_fetch(USER_AGENT, url):
                    self.skipped_count += 1
                    continue
                elapsed = time.monotonic() - last_request_at
                if crawl_delay > elapsed:
                    time.sleep(crawl_delay - elapsed)
                try:
                    result = self.http.get(
                        url,
                        max_bytes=MAX_PDF_BYTES if urlsplit(url).path.lower().endswith(".pdf") else MAX_HTML_BYTES,
                    )
                    last_request_at = time.monotonic()
                    page, links = extract_page(result)
                except CrawlError as exc:
                    self.failed_count += 1
                    self._remember_error(url, exc)
                    continue
                processed += 1
                self.processed_count = processed
                if page is not None:
                    yield page
                if depth >= self.max_depth:
                    continue
                for link in links:
                    try:
                        normalized = normalize_url(link, base_url=result.url)
                    except UnsafeUrlError:
                        continue
                    if normalized in scheduled or not _is_crawlable_url(normalized, self.allowed_site):
                        continue
                    scheduled.add(normalized)
                    queue.append((normalized, depth + 1))
                self.discovered_count = len(scheduled)
        finally:
            self.processed_count = processed
            self.limit_reached = bool(queue and processed >= self.max_pages)
            self.close()

    def _robots_parser(self) -> RobotFileParser:
        parsed = urlsplit(self.root_url)
        robots_url = urlunsplit((parsed.scheme, parsed.netloc, "/robots.txt", "", ""))
        parser = RobotFileParser(robots_url)
        try:
            result = self.http.get(robots_url, max_bytes=512 * 1024)
            parser.parse(result.body.decode(result.encoding, errors="replace").splitlines())
        except FetchError as exc:
            if exc.status_code == 404:
                parser.parse(["User-agent: *", "Disallow:"])
            else:
                parser.parse(["User-agent: *", "Disallow: /"])
                self._remember_error(robots_url, exc)
        return parser

    def _sitemap_pages(self, robots: RobotFileParser) -> list[str]:
        parsed = urlsplit(self.root_url)
        defaults = [urlunsplit((parsed.scheme, parsed.netloc, "/sitemap.xml", "", ""))]
        sitemap_queue = deque([*(robots.site_maps() or []), *defaults])
        visited: set[str] = set()
        pages: list[str] = []
        while sitemap_queue and len(visited) < 10 and len(pages) < self.max_pages * 2:
            raw_url = sitemap_queue.popleft()
            try:
                sitemap_url = normalize_url(raw_url, base_url=self.root_url)
                if _site_key(urlsplit(sitemap_url).hostname or "") != self.allowed_site:
                    continue
                if sitemap_url in visited:
                    continue
                visited.add(sitemap_url)
                result = self.http.get(sitemap_url, max_bytes=2 * 1024 * 1024)
                text = result.body.decode(result.encoding, errors="replace")
            except CrawlError:
                continue
            locations = [
                html_module.unescape(item.strip())
                for item in re.findall(
                    r"<(?:[A-Za-z0-9_-]+:)?loc\b[^>]*>(.*?)</(?:[A-Za-z0-9_-]+:)?loc>",
                    text,
                    flags=re.I | re.S,
                )
            ]
            is_index = bool(re.search(r"<(?:[A-Za-z0-9_-]+:)?sitemapindex\b", text, re.I))
            if is_index:
                sitemap_queue.extend(locations)
            else:
                for location in locations:
                    try:
                        normalized = normalize_url(location, base_url=sitemap_url)
                    except UnsafeUrlError:
                        continue
                    if _is_crawlable_url(normalized, self.allowed_site):
                        pages.append(normalized)
        return list(dict.fromkeys(pages))

    def _remember_error(self, url: str, exc: Exception) -> None:
        if len(self.errors) < 10:
            self.errors.append(f"{url}: {exc}")


def extract_page(result: FetchResult) -> tuple[CrawledPage | None, list[str]]:
    is_pdf = result.content_type == "application/pdf" or urlsplit(result.url).path.lower().endswith(".pdf")
    if is_pdf:
        return extract_pdf(result), []
    if result.content_type and result.content_type not in {
        "text/html",
        "application/xhtml+xml",
    }:
        return None, []
    return extract_html(result)


def extract_html(result: FetchResult) -> tuple[CrawledPage | None, list[str]]:
    markup = result.body.decode(result.encoding, errors="replace")
    soup = BeautifulSoup(markup, "html.parser")
    links = [str(anchor.get("href")) for anchor in soup.find_all("a", href=True)]
    language = str(soup.html.get("lang", "")).strip() if soup.html else ""
    title_node = soup.find("title")
    heading = soup.find("h1")
    title = _clean_line(
        (title_node.get_text(" ", strip=True) if title_node else "")
        or (heading.get_text(" ", strip=True) if heading else "")
        or result.url
    )[:255]

    for node in soup.find_all(
        ["script", "style", "noscript", "template", "svg", "canvas", "form", "nav", "footer", "aside"]
    ):
        node.decompose()
    for node in soup.find_all(_is_boilerplate_container):
        node.decompose()
    root = soup.find("main") or soup.find("article") or soup.body or soup
    lines: list[str] = []
    for node in root.find_all(["h1", "h2", "h3", "h4", "p", "li", "dt", "dd", "th", "td"]):
        value = _clean_line(node.get_text(" ", strip=True))
        if value and node.name in {"h1", "h2", "h3", "h4"}:
            value = f"{'#' * int(node.name[1])} {value}"
        if value and (not lines or lines[-1] != value):
            lines.append(value)
    if not lines:
        value = _clean_line(root.get_text(" ", strip=True))
        if value:
            lines.append(value)
    content = "\n".join(lines)[:MAX_EXTRACTED_CHARS].strip()
    if len(content) < 20:
        return None, links
    return (
        CrawledPage(
            url=result.url,
            title=title,
            content=content,
            content_type="html",
            language=normalize_language(language, content),
            metadata={
                "http_content_type": result.content_type or "text/html",
                "source_updated_at": result.last_modified,
            },
        ),
        links,
    )


def extract_pdf(result: FetchResult) -> CrawledPage | None:
    try:
        reader = PdfReader(BytesIO(result.body), strict=False)
        if reader.is_encrypted and reader.decrypt("") == 0:
            raise CrawlError("PDF 已加密，无法提取正文")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise CrawlError(f"PDF 超过 {MAX_PDF_PAGES} 页限制")
        parts: list[str] = []
        size = 0
        for page_number, page in enumerate(reader.pages, start=1):
            value = (page.extract_text() or "").strip()
            if not value:
                continue
            remaining = MAX_EXTRACTED_CHARS - size
            if remaining <= 0:
                break
            page_text = f"## Page {page_number}\n\n{value}"
            parts.append(page_text[:remaining])
            size += len(parts[-1])
    except CrawlError:
        raise
    except Exception as exc:
        raise CrawlError("PDF 正文提取失败") from exc
    content = "\n\n".join(parts).strip()
    if len(content) < 20:
        return None
    metadata_title = getattr(reader.metadata, "title", None) if reader.metadata else None
    fallback = PurePosixPath(urlsplit(result.url).path).name or result.url
    return CrawledPage(
        url=result.url,
        title=_clean_line(str(metadata_title or fallback))[:255],
        content=content,
        content_type="pdf",
        language=normalize_language("", content),
        metadata={
            "page_count": len(reader.pages),
            "http_content_type": result.content_type,
            "source_updated_at": result.last_modified,
        },
    )


def normalize_language(value: str, content: str) -> str:
    lowered = value.lower().replace("_", "-")
    if lowered.startswith("zh-hant") or lowered in {"zh-tw", "zh-hk", "zh-mo"}:
        return "zh-TW"
    if lowered.startswith("zh"):
        return "zh-CN"
    if lowered.startswith("en"):
        return "en"
    traditional_hints = set("這麼請問轉與為務時間產換貨訂號謝後價開關")
    if any(character in traditional_hints for character in content[:5000]):
        return "zh-TW"
    if re.search(r"[\u3400-\u9fff]", content):
        return "zh-CN"
    return "en" if re.search(r"[A-Za-z]", content) else "unknown"


def _clean_line(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _is_boilerplate_container(node: Any) -> bool:
    """Identify ordinary divs that contain site-wide chrome."""
    attributes = [node.get("id", ""), *node.get("class", [])]
    for value in attributes:
        normalized = re.sub(r"[^a-z0-9]+", "", str(value).lower())
        if normalized in _BOILERPLATE_CLASS_TOKENS:
            return True
        # Templates commonly append a role (for example, site-header-wrap).
        if any(
            normalized.startswith(prefix)
            for prefix in ("siteheader", "sitefooter", "mainmenu", "navbuttons", "subscribeform")
        ):
            return True
    return False
