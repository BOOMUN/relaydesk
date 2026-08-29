from __future__ import annotations

import socket
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.database import SessionLocal
from backend.app.models import (
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgePageRevision,
    KnowledgeSource,
    KnowledgeSyncRun,
    KnowledgeWebPage,
)
from backend.app.services.knowledge import retrieve_knowledge
from backend.app.services.knowledge_ingestion import (
    categorize_content,
    persist_crawled_page,
    reconcile_missing_pages,
)
from backend.app.services.web_crawler import (
    CrawledPage,
    FetchError,
    FetchResult,
    UnsafeUrlError,
    WebsiteCrawler,
    extract_html,
    normalize_public_root_url,
)
from backend.app.services.knowledge_tasks import daily_schedule_due, next_daily_sync


def test_public_url_validation_blocks_private_network(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        ],
    )
    with pytest.raises(UnsafeUrlError, match="公网"):
        normalize_public_root_url("https://internal.example/")
    with pytest.raises(UnsafeUrlError, match="80 或 443"):
        normalize_public_root_url("https://example.com:8080/")


def test_daily_sync_uses_beijing_time_at_0310():
    before = datetime(2026, 8, 21, 18, 0, tzinfo=timezone.utc)  # 02:00 in Shanghai
    next_run = next_daily_sync(before)
    assert next_run.isoformat() == "2026-08-22T03:10:00+08:00"
    assert daily_schedule_due(before) == (False, datetime(2026, 8, 22).date())

    after = datetime(2026, 8, 21, 20, 0, tzinfo=timezone.utc)  # 04:00 in Shanghai
    assert daily_schedule_due(after) == (True, datetime(2026, 8, 22).date())


def test_html_extraction_and_automatic_category():
    result = FetchResult(
        url="https://example.com/support/returns",
        content_type="text/html",
        encoding="utf-8",
        body=(
            "<html lang='zh-CN'><head><title>退换货帮助</title><script>secret()</script></head>"
            "<body><nav>重复导航</nav><main><h1>退货政策</h1>"
            "<p>客户签收商品后七天内可以申请退货和退款。</p>"
            "<a href='/support/warranty'>保修说明</a></main></body></html>"
        ).encode(),
    )
    page, links = extract_html(result)
    assert page is not None
    assert page.title == "退换货帮助"
    assert "七天内" in page.content
    assert "重复导航" not in page.content
    assert "secret" not in page.content
    assert page.language == "zh-CN"
    assert links == ["/support/warranty"]
    assert categorize_content(page.title, page.content, page.url) == "after_sales"


def test_html_extraction_removes_generic_site_chrome_containers():
    result = FetchResult(
        url="https://example.com/esim",
        content_type="text/html",
        encoding="utf-8",
        body=(
            "<html><head><title>eSIM</title></head><body>"
            "<div class='site-header'><p>重复导航</p><div class='main-menu'>菜单</div></div>"
            "<div id='p-menubar'><p>产品导航</p></div>"
            "<main><h1>eSIM 方案</h1><p>这是正文内容，包含套餐和使用说明。</p>"
            "<div class='nav-buttons'><p>上一页 下一页</p></div></main>"
            "<div class='subscribe-form'><p>订阅优惠</p></div>"
            "<div class='site-footer'><p>页脚链接</p></div>"
            "</body></html>"
        ).encode(),
    )
    page, _ = extract_html(result)
    assert page is not None
    assert "这是正文内容" in page.content
    for noise in ("重复导航", "菜单", "产品导航", "上一页", "订阅优惠", "页脚链接"):
        assert noise not in page.content


def test_crawler_follows_same_site_links_and_respects_page_limit(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "backend.app.services.web_crawler.validate_public_url",
        lambda *_args, **_kwargs: None,
    )

    class FakeHttp:
        def get(self, url: str, *, max_bytes: int):
            del max_bytes
            if url.endswith("/robots.txt"):
                return FetchResult(url, "text/plain", b"User-agent: *\nDisallow:\n", "utf-8")
            if url.endswith("/sitemap.xml"):
                raise FetchError("missing", status_code=404)
            if url.endswith("/returns"):
                return FetchResult(
                    url,
                    "text/html",
                    "<html><title>Returns</title><main><p>Return policy details for customers.</p></main></html>".encode(),
                    "utf-8",
                )
            return FetchResult(
                url,
                "text/html",
                "<html><title>Home</title><main><p>Public company knowledge home page.</p>"
                "<a href='/returns'>Returns</a><a href='https://outside.example/help'>Outside</a>"
                "</main></html>".encode(),
                "utf-8",
            )

        def close(self):
            return None

    crawler = WebsiteCrawler("https://example.com/", max_pages=2, max_depth=2)
    crawler.http.close()
    crawler.http = FakeHttp()  # type: ignore[assignment]
    pages = list(crawler.crawl())
    assert [page.title for page in pages] == ["Home", "Returns"]
    assert crawler.failed_count == 0
    assert crawler.discovered_count == 2


def test_crawled_documents_require_review_before_rag(authenticated_client: TestClient):
    with SessionLocal() as db:
        source = KnowledgeSource(
            tenant_id=1,
            created_by_user_id=1,
            root_url="https://example.com/",
            domain="example.com",
            status="completed",
            max_pages=10,
            max_depth=2,
            discovered_pages=1,
            imported_pages=1,
        )
        db.add(source)
        db.commit()
        db.refresh(source)
        created = persist_crawled_page(
            db,
            source,
            CrawledPage(
                url="https://example.com/returns",
                title="退换货政策",
                content="客户签收商品后七天内可以申请退货，商品必须保持完整。",
                content_type="html",
                language="zh-CN",
                metadata={"http_content_type": "text/html"},
            ),
        )
        assert created.change == "new"
        document = db.scalar(select(KnowledgeDocument).where(KnowledgeDocument.source == "https://example.com/returns"))
        assert document is not None
        assert document.is_active is False
        assert db.scalar(select(KnowledgeWebPage).where(KnowledgeWebPage.document_id == document.id)).review_status == "draft"
        assert len(db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id)).all()) >= 1
        assert retrieve_knowledge(db, 1, "商品退货期限是多久？") == []
        source_id = source.id

    response = authenticated_client.post(f"/api/knowledge/sources/{source_id}/publish")
    assert response.status_code == 200
    assert response.json()["published_count"] == 1
    detail = authenticated_client.get("/api/knowledge").json()
    crawled = next(item for item in detail if item["source_url"] == "https://example.com/returns")
    assert crawled["review_status"] == "published"
    assert crawled["source_type"] == "html"
    with SessionLocal() as db:
        matches = retrieve_knowledge(db, 1, "商品退货期限是多久？")
        assert matches
        assert matches[0].metadata["title"] == "退换货政策"


def test_website_change_is_staged_while_published_version_remains_live(
    authenticated_client: TestClient,
):
    original = "客户签收后七天内可以申请退货，商品必须保持完整。"
    changed = "客户签收后三十天内可以申请退货，商品必须保持完整并保留包装。"
    with SessionLocal() as db:
        source = KnowledgeSource(
            tenant_id=1,
            created_by_user_id=1,
            root_url="https://policy.example.com/",
            domain="policy.example.com",
            status="completed",
            max_pages=10,
            max_depth=2,
        )
        db.add(source)
        db.commit()
        db.refresh(source)
        persist_crawled_page(
            db,
            source,
            CrawledPage(
                url="https://policy.example.com/returns",
                title="退货政策",
                content=original,
                content_type="html",
                language="zh-CN",
                metadata={},
            ),
        )
        source_id = source.id

    assert authenticated_client.post(f"/api/knowledge/sources/{source_id}/publish").status_code == 200

    with SessionLocal() as db:
        source = db.get(KnowledgeSource, source_id)
        result = persist_crawled_page(
            db,
            source,
            CrawledPage(
                url="https://policy.example.com/returns",
                title="新退货政策",
                content=changed,
                content_type="html",
                language="zh-CN",
                metadata={},
            ),
        )
        assert result.change == "changed"
        document = db.scalar(
            select(KnowledgeDocument).where(
                KnowledgeDocument.source == "https://policy.example.com/returns"
            )
        )
        assert document.content == original
        assert document.is_active is True
        revision = db.scalar(
            select(KnowledgePageRevision).where(KnowledgePageRevision.status == "draft")
        )
        assert revision is not None
        assert revision.content == changed

    listed = authenticated_client.get("/api/knowledge").json()
    item = next(entry for entry in listed if entry["source_id"] == source_id)
    assert item["pending_update"] is True
    assert item["pending_content"] == changed
    assert item["content"] == original

    response = authenticated_client.post(f"/api/knowledge/sources/{source_id}/publish")
    assert response.status_code == 200
    assert response.json()["published_update_count"] == 1
    with SessionLocal() as db:
        document = db.scalar(
            select(KnowledgeDocument).where(
                KnowledgeDocument.source == "https://policy.example.com/returns"
            )
        )
        assert document.content == changed
        assert document.is_active is True


def test_missing_page_requires_two_authoritative_scans(authenticated_client: TestClient):
    del authenticated_client
    with SessionLocal() as db:
        source = KnowledgeSource(
            tenant_id=1,
            created_by_user_id=1,
            root_url="https://missing.example.com/",
            domain="missing.example.com",
            status="completed",
            max_pages=10,
            max_depth=2,
        )
        db.add(source)
        db.commit()
        db.refresh(source)
        created = persist_crawled_page(
            db,
            source,
            CrawledPage(
                url="https://missing.example.com/faq",
                title="FAQ",
                content="This is a sufficiently detailed FAQ page for customers.",
                content_type="html",
                language="en",
                metadata={},
            ),
        )
        assert reconcile_missing_pages(db, source, set(), authoritative=True) == 0
        assert reconcile_missing_pages(db, source, set(), authoritative=True) == 1
        page = db.get(KnowledgeWebPage, created.web_page_id)
        assert page.sync_state.availability_status == "suspected_missing"
        assert page.document.is_active is False


def test_source_api_runs_background_crawl(
    authenticated_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "backend.app.api.knowledge.normalize_public_root_url",
        lambda value: "https://docs.example.com/" if value else value,
    )

    def fake_crawl(source_id: int, **kwargs):
        with SessionLocal() as db:
            source = db.get(KnowledgeSource, source_id)
            assert source is not None
            source.status = "completed"
            source.discovered_pages = 1
            source.imported_pages = 1
            persist_crawled_page(
                db,
                source,
                CrawledPage(
                    url="https://docs.example.com/faq",
                    title="常见问题",
                    content="这里记录公司产品的常见问题与使用方法。",
                    content_type="html",
                    language="zh-CN",
                    metadata={},
                ),
            )
            sync_run = db.get(KnowledgeSyncRun, kwargs.get("sync_run_id"))
            if sync_run is not None:
                sync_run.status = "completed"
            db.commit()

    monkeypatch.setattr("backend.app.api.knowledge.run_crawl_job", fake_crawl)
    response = authenticated_client.post(
        "/api/knowledge/sources",
        json={"root_url": "https://docs.example.com", "max_pages": 20, "max_depth": 3},
    )
    assert response.status_code == 202
    sources = authenticated_client.get("/api/knowledge/sources").json()
    assert sources[0]["status"] == "completed"
    assert sources[0]["draft_pages"] == 1

    immediate = authenticated_client.post(f"/api/knowledge/sources/{sources[0]['id']}/sync")
    assert immediate.status_code == 202

    duplicate = authenticated_client.post(
        "/api/knowledge/sources",
        json={"root_url": "https://docs.example.com", "max_pages": 20, "max_depth": 3},
    )
    assert duplicate.status_code == 409


def test_source_api_exposes_last_success_and_failed_task_summary(
    authenticated_client: TestClient,
):
    success_at = datetime(2026, 8, 24, 19, 15, tzinfo=timezone.utc)
    failed_at = datetime(2026, 8, 25, 1, 20, tzinfo=timezone.utc)
    with SessionLocal() as db:
        source = KnowledgeSource(
            tenant_id=1,
            created_by_user_id=1,
            root_url="https://status.example.com/",
            domain="status.example.com",
            status="failed",
            max_pages=20,
            max_depth=2,
        )
        db.add(source)
        db.flush()
        db.add_all(
            (
                KnowledgeSyncRun(
                    tenant_id=1,
                    source_id=source.id,
                    trigger="scheduled",
                    status="completed",
                    queued_at=success_at,
                    completed_at=success_at,
                ),
                KnowledgeSyncRun(
                    tenant_id=1,
                    source_id=source.id,
                    trigger="scheduled",
                    status="failed",
                    queued_at=failed_at,
                    completed_at=failed_at,
                    error_message="crawler timeout",
                ),
            )
        )
        source_id = source.id
        db.commit()

    response = authenticated_client.get("/api/knowledge/sources")
    assert response.status_code == 200
    source_payload = next(item for item in response.json() if item["id"] == source_id)
    assert source_payload["last_successful_sync_at"].startswith("2026-08-24T19:15")
    assert source_payload["failed_task_count"] == 1
    assert source_payload["partial_task_count"] == 0
    assert source_payload["last_failed_task_at"].startswith("2026-08-25T01:20")
    assert source_payload["last_failure_message"] == "crawler timeout"
