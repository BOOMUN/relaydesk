from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import httpx

from ..config import settings
from .rest_actions import RestActionSecurityError, validate_public_origin


BRAVE_SEARCH_ORIGIN = "https://api.search.brave.com"
BRAVE_SEARCH_PATH = "/res/v1/web/search"


def _domain_allowed(hostname: str, allowed_domains: list[str]) -> bool:
    host = hostname.casefold().rstrip(".")
    return not allowed_domains or any(
        host == domain or host.endswith(f".{domain}")
        for domain in allowed_domains
    )


def search_public_web(
    query: str,
    *,
    allowed_domains: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Search only through a fixed provider endpoint and return cited snippets."""

    if settings.web_search_provider != "brave" or not settings.web_search_api_key:
        return []
    normalized_query = " ".join(str(query).split()).strip()[:500]
    if not normalized_query:
        return []
    domains = [str(item).casefold().rstrip(".") for item in allowed_domains or [] if str(item).strip()]
    # This fixed-origin validation also fails closed if local DNS has been
    # poisoned to resolve the search provider to an internal address.
    try:
        origin = validate_public_origin(BRAVE_SEARCH_ORIGIN, resolve_dns=True)
        with httpx.Client(
            timeout=httpx.Timeout(float(settings.web_search_timeout_seconds), connect=5.0),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = client.get(
                f"{origin.url}{BRAVE_SEARCH_PATH}",
                params={
                    "q": normalized_query,
                    "count": settings.web_search_max_results,
                    "safesearch": "strict",
                    "result_filter": "web",
                },
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": settings.web_search_api_key,
                },
            )
    except (httpx.HTTPError, RestActionSecurityError):
        return []
    if response.status_code != 200 or 300 <= response.status_code < 400:
        return []
    try:
        payload = response.json()
    except ValueError:
        return []
    rows: list[dict[str, Any]] = []
    for item in ((payload.get("web") or {}).get("results") or []):
        url = str(item.get("url") or "").strip()
        title = " ".join(str(item.get("title") or "").split()).strip()
        description = " ".join(str(item.get("description") or "").split()).strip()
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or not title or not description:
            continue
        if not _domain_allowed(parsed.hostname, domains):
            continue
        rows.append(
            {
                "title": title[:300],
                "content": description[:1500],
                "source": url,
                "source_url": url,
                "page_title": title[:300],
                "section_path": "",
                "source_type": "web_search",
                "retrieval_score": None,
                "bm25_score": None,
                "semantic_score": None,
            }
        )
        if len(rows) >= settings.web_search_max_results:
            break
    return rows


__all__ = ["search_public_web"]
