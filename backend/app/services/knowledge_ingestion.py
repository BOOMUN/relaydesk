from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..config import settings
from ..database import SessionLocal
from ..models import (
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgePageRevision,
    KnowledgePageSyncState,
    KnowledgeSource,
    KnowledgeSyncRun,
    KnowledgeWebPage,
    utcnow,
)
from .embeddings import embed_documents
from ..vector_types import validate_vector
from .product_knowledge import catalog_product_knowledge_pages
from .web_crawler import CrawledPage, WebsiteCrawler


CATEGORY_RULES: dict[str, tuple[str, ...]] = {
    "product": (
        "产品",
        "產品",
        "商品",
        "功能",
        "规格",
        "規格",
        "型号",
        "型號",
        "product",
        "feature",
        "pricing",
    ),
    "faq": ("常见问题", "常見問題", "faq", "帮助中心", "幫助中心", "question", "how to"),
    "policy": (
        "政策",
        "条款",
        "條款",
        "隐私",
        "隱私",
        "协议",
        "協議",
        "policy",
        "terms",
        "privacy",
    ),
    "orders": (
        "订单",
        "訂單",
        "物流",
        "配送",
        "发货",
        "發貨",
        "快递",
        "order",
        "shipping",
        "delivery",
        "tracking",
    ),
    "after_sales": (
        "售后",
        "售後",
        "退货",
        "退貨",
        "换货",
        "換貨",
        "退款",
        "保修",
        "维修",
        "維修",
        "return",
        "refund",
        "warranty",
        "support",
    ),
    "service": (
        "客服",
        "服務時間",
        "服务时间",
        "營業時間",
        "营业时间",
        "工作时间",
        "工作時間",
        "customer service",
        "business hours",
    ),
    "company": (
        "关于我们",
        "關於我們",
        "公司简介",
        "公司簡介",
        "联系我们",
        "聯絡我們",
        "contact",
        "about us",
        "company",
        "careers",
    ),
}


@dataclass(slots=True)
class PagePersistResult:
    change: str
    web_page_id: int


@dataclass(slots=True)
class CrawlJobResult:
    status: str
    source_id: int
    run_id: int
    error_message: str | None = None


@dataclass(slots=True)
class ChunkDraft:
    content: str
    section_path: str
    token_count: int


def categorize_content(title: str, content: str, url: str) -> str:
    value = f"{url}\n{title}\n{content[:8000]}".lower()
    scores = {
        category: sum(value.count(term.lower()) for term in terms)
        for category, terms in CATEGORY_RULES.items()
    }
    category, score = max(scores.items(), key=lambda item: item[1])
    return category if score > 0 else "other"


_TOKEN_UNIT_RE = re.compile(
    r"[A-Za-z0-9]+(?:[-_'][A-Za-z0-9]+)*|[\u3400-\u9fff]|[^\s]"
)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def _token_spans(value: str) -> list[tuple[int, int]]:
    """Return deterministic multilingual token-unit spans without model I/O."""

    spans: list[tuple[int, int]] = []
    for match in _TOKEN_UNIT_RE.finditer(value or ""):
        token = match.group(0)
        if token.isascii() and re.search(r"[A-Za-z0-9]", token):
            # WordPiece/BPE tokenizers use more than one token for long model
            # names and URLs. Four characters per unit is a conservative,
            # deterministic approximation suitable for chunk budgets.
            for offset in range(0, len(token), 4):
                spans.append(
                    (
                        match.start() + offset,
                        min(match.start() + offset + 4, match.end()),
                    )
                )
        else:
            spans.append((match.start(), match.end()))
    return spans


def estimate_token_count(value: str) -> int:
    return len(_token_spans(value))


def _token_windows(value: str, *, maximum: int, overlap: int) -> list[str]:
    spans = _token_spans(value)
    if not spans:
        return []
    if len(spans) <= maximum:
        return [value.strip()]
    windows: list[str] = []
    start = 0
    step = max(1, maximum - overlap)
    while start < len(spans):
        end = min(len(spans), start + maximum)
        begin_character = spans[start][0]
        end_character = spans[end - 1][1]
        piece = value[begin_character:end_character].strip()
        if piece:
            windows.append(piece)
        if end >= len(spans):
            break
        start += step
    return windows


def _content_sections(content: str, title: str) -> list[tuple[str, str]]:
    default_path = [title.strip()] if title.strip() else []
    heading_path = list(default_path)
    section_path = " > ".join(heading_path)
    lines: list[str] = []
    sections: list[tuple[str, str]] = []

    def flush() -> None:
        body = "\n".join(item for item in lines if item).strip()
        if body:
            sections.append((section_path, body))

    for raw_line in (content or "").splitlines():
        line = raw_line.strip()
        if not line:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        heading = _HEADING_RE.match(line)
        if heading is None:
            lines.append(line)
            continue
        flush()
        lines = []
        level = len(heading.group(1))
        value = heading.group(2).strip()
        if level == 1:
            heading_path = [value]
        else:
            if not heading_path:
                heading_path = list(default_path)
            base = heading_path[: max(1, level - 1)]
            heading_path = [*base, value]
        section_path = " > ".join(dict.fromkeys(heading_path))
    flush()
    if not sections and content.strip():
        sections.append((" > ".join(default_path), content.strip()))
    return sections


def split_content_sections(
    content: str,
    *,
    title: str = "",
    minimum_tokens: int = 400,
    maximum_tokens: int = 800,
    overlap_tokens: int = 80,
) -> list[ChunkDraft]:
    """Split by heading boundaries, then pack adjacent short sections."""

    maximum_tokens = max(100, maximum_tokens)
    minimum_tokens = min(maximum_tokens, max(1, minimum_tokens))
    overlap_tokens = min(max(0, overlap_tokens), maximum_tokens // 3)
    atomic: list[ChunkDraft] = []
    for path, body in _content_sections(content, title):
        prefix = f"{path}\n" if path else ""
        available = max(100, maximum_tokens - estimate_token_count(prefix))
        pieces = _token_windows(body, maximum=available, overlap=overlap_tokens)
        for piece in pieces:
            rendered = f"{prefix}{piece}".strip()
            atomic.append(
                ChunkDraft(
                    content=rendered,
                    section_path=path,
                    token_count=estimate_token_count(rendered),
                )
            )

    packed: list[ChunkDraft] = []
    buffer_content = ""
    buffer_paths: list[str] = []

    def flush_buffer() -> None:
        nonlocal buffer_content, buffer_paths
        if not buffer_content:
            return
        packed.append(
            ChunkDraft(
                content=buffer_content,
                section_path=" | ".join(dict.fromkeys(item for item in buffer_paths if item)),
                token_count=estimate_token_count(buffer_content),
            )
        )
        buffer_content = ""
        buffer_paths = []

    for item in atomic:
        if item.token_count >= minimum_tokens:
            flush_buffer()
            packed.append(item)
            continue
        candidate = f"{buffer_content}\n\n{item.content}".strip()
        if buffer_content and estimate_token_count(candidate) > maximum_tokens:
            flush_buffer()
            candidate = item.content
        buffer_content = candidate
        buffer_paths.append(item.section_path)
        if estimate_token_count(buffer_content) >= minimum_tokens:
            flush_buffer()
    flush_buffer()
    return [item for item in packed if item.content]


def split_content(
    content: str,
    *,
    max_chars: int | None = None,
    overlap: int | None = None,
) -> list[str]:
    """Compatibility wrapper; chunk sizing is now token based."""

    del max_chars, overlap
    return [item.content for item in split_content_sections(content)]


def _source_updated_at(metadata: dict[str, object], fallback: datetime) -> datetime:
    value = metadata.get("source_updated_at")
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            parsed = parsedate_to_datetime(value.strip())
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError, OverflowError):
            pass
    return fallback


def rebuild_document_chunks(
    db: Session,
    document: KnowledgeDocument,
    *,
    prefer_local: bool = False,
    model_name: str | None = None,
    batch_size: int | None = None,
) -> int:
    return rebuild_documents_chunks(
        db,
        [document],
        prefer_local=prefer_local,
        model_name=model_name,
        batch_size=batch_size,
    )


def rebuild_documents_chunks(
    db: Session,
    documents: list[KnowledgeDocument],
    *,
    prefer_local: bool = False,
    model_name: str | None = None,
    batch_size: int | None = None,
) -> int:
    """Rebuild several documents using shared, bounded embedding batches.

    Crawls can publish many pages at once.  Collecting their chunk metadata and
    embedding in configured batches avoids one model/tokenizer invocation per
    document while keeping the operation in the caller's transaction.
    """

    if not documents:
        return 0
    # A caller may have loaded an active-document snapshot before adding or
    # editing one of these documents in the same Session.  Drop the lightweight
    # retrieval caches so the next query cannot reuse stale eligibility data.
    for cache_key in (
        "agentdesk_active_documents",
        "agentdesk_chunks_verified_tenants",
        "agentdesk_catalog_destinations",
    ):
        db.info.pop(cache_key, None)
    document_ids = [document.id for document in documents if document.id is not None]
    if document_ids:
        db.execute(
            delete(KnowledgeChunk).where(KnowledgeChunk.document_id.in_(document_ids))
        )

    effective_batch_size = max(1, int(batch_size or settings.embedding_batch_size))
    resolved_model_name: str | None = None
    pending: list[tuple[KnowledgeDocument, int, ChunkDraft, str]] = []
    processed = 0

    def _flush_batch(batch: list[tuple[KnowledgeDocument, int, ChunkDraft, str]]) -> None:
        nonlocal resolved_model_name, processed
        if not batch:
            return
        vectors, batch_model_name = embed_documents(
            [item[2].content for item in batch],
            prefer_local=prefer_local,
            model_name=model_name,
            batch_size=effective_batch_size,
        )
        if resolved_model_name is None:
            resolved_model_name = batch_model_name
        elif resolved_model_name != batch_model_name:
            raise RuntimeError(
                "Embedding provider changed model during a batch rebuild: "
                f"{resolved_model_name!r} -> {batch_model_name!r}"
            )
        db.add_all(
            [
                KnowledgeChunk(
                    tenant_id=document.tenant_id,
                    document_id=document.id,
                    chunk_index=index,
                    content=draft.content,
                    content_hash=content_hash,
                    page_title=document.title,
                    source_url=document.source,
                    section_path=draft.section_path,
                    source_updated_at=_source_updated_at(
                        dict(document.web_page.metadata_json or {})
                        if document.web_page is not None
                        else {},
                        document.web_page.updated_at
                        if document.web_page is not None
                        else document.updated_at,
                    ),
                    token_count=draft.token_count,
                    metadata_json=(
                        dict(document.web_page.metadata_json or {})
                        if document.web_page is not None
                        else {}
                    ),
                    embedding=validate_vector(vector),
                    embedding_model=batch_model_name,
                )
                for (document, index, draft, content_hash), vector in zip(
                    batch, vectors, strict=True
                )
            ]
        )
        # Flush each embedding batch so a large crawl does not retain every
        # pending ORM object until the final document.  The caller still owns
        # the transaction and can roll it back if a later batch fails.
        db.flush()
        processed += len(batch)

    for document in documents:
        for index, draft in enumerate(
            split_content_sections(document.content, title=document.title)
        ):
            pending.append(
                (
                    document,
                    index,
                    draft,
                    hashlib.sha256(draft.content.encode("utf-8")).hexdigest(),
                )
            )
            if len(pending) >= effective_batch_size:
                _flush_batch(pending)
                pending = []
    _flush_batch(pending)
    return processed


def persist_crawled_page(
    db: Session,
    source: KnowledgeSource,
    page: CrawledPage,
) -> PagePersistResult:
    content_hash = hashlib.sha256(page.content.encode("utf-8")).hexdigest()
    web_page = db.scalar(
        select(KnowledgeWebPage).where(
            KnowledgeWebPage.source_id == source.id,
            KnowledgeWebPage.url == page.url,
        )
    )
    category = categorize_content(page.title, page.content, page.url)
    if web_page is None:
        document = KnowledgeDocument(
            tenant_id=source.tenant_id,
            title=page.title,
            content=page.content,
            source=page.url,
            category=category,
            is_active=False,
        )
        db.add(document)
        db.flush()
        web_page = KnowledgeWebPage(
            tenant_id=source.tenant_id,
            source_id=source.id,
            document_id=document.id,
            url=page.url,
            content_hash=content_hash,
            content_type=page.content_type,
            language=page.language,
            review_status="draft",
            word_count=count_words(page.content),
            metadata_json=page.metadata,
        )
        db.add(web_page)
        db.flush()
        db.add(
            KnowledgePageSyncState(
                web_page_id=web_page.id,
                last_seen_at=utcnow(),
            )
        )
        change = "new"
    else:
        document = db.get(KnowledgeDocument, web_page.document_id)
        if document is None:
            raise RuntimeError(f"网页 {page.url} 缺少知识文档")
        sync_state = db.scalar(
            select(KnowledgePageSyncState).where(
                KnowledgePageSyncState.web_page_id == web_page.id
            )
        )
        if sync_state is None:
            sync_state = KnowledgePageSyncState(web_page_id=web_page.id)
            db.add(sync_state)
        sync_state.last_seen_at = utcnow()
        sync_state.consecutive_missing = 0
        sync_state.availability_status = "active"
        sync_state.suspected_missing_at = None
        if web_page.content_hash == content_hash:
            db.query(KnowledgePageRevision).filter(
                KnowledgePageRevision.web_page_id == web_page.id,
                KnowledgePageRevision.status == "draft",
            ).update({"status": "superseded"}, synchronize_session=False)
            db.commit()
            return PagePersistResult(change="unchanged", web_page_id=web_page.id)

        matching_revision = db.scalar(
            select(KnowledgePageRevision).where(
                KnowledgePageRevision.web_page_id == web_page.id,
                KnowledgePageRevision.content_hash == content_hash,
            )
        )
        if matching_revision is not None:
            if matching_revision.status == "draft":
                db.commit()
                return PagePersistResult(change="unchanged", web_page_id=web_page.id)
            if document.is_active and web_page.review_status == "published":
                db.query(KnowledgePageRevision).filter(
                    KnowledgePageRevision.web_page_id == web_page.id,
                    KnowledgePageRevision.status == "draft",
                ).update({"status": "superseded"}, synchronize_session=False)
                matching_revision.title = page.title
                matching_revision.content = page.content
                matching_revision.category = category
                matching_revision.content_type = page.content_type
                matching_revision.language = page.language
                matching_revision.word_count = count_words(page.content)
                matching_revision.metadata_json = page.metadata
                matching_revision.status = "draft"
                matching_revision.reviewed_at = None
                db.commit()
                return PagePersistResult(change="changed", web_page_id=web_page.id)

        if document.is_active and web_page.review_status == "published":
            db.query(KnowledgePageRevision).filter(
                KnowledgePageRevision.web_page_id == web_page.id,
                KnowledgePageRevision.status == "draft",
            ).update({"status": "superseded"}, synchronize_session=False)
            db.add(
                KnowledgePageRevision(
                    tenant_id=source.tenant_id,
                    source_id=source.id,
                    web_page_id=web_page.id,
                    title=page.title,
                    content=page.content,
                    content_hash=content_hash,
                    category=category,
                    content_type=page.content_type,
                    language=page.language,
                    word_count=count_words(page.content),
                    metadata_json=page.metadata,
                    status="draft",
                )
            )
            db.commit()
            return PagePersistResult(change="changed", web_page_id=web_page.id)

        document.title = page.title
        document.content = page.content
        document.source = page.url
        document.category = category
        document.is_active = False
        web_page.content_hash = content_hash
        web_page.content_type = page.content_type
        web_page.language = page.language
        web_page.review_status = "draft"
        web_page.word_count = count_words(page.content)
        web_page.metadata_json = page.metadata
        change = "changed"
    db.flush()
    rebuild_document_chunks(db, document)
    db.commit()
    return PagePersistResult(change=change, web_page_id=web_page.id)


def persist_catalog_product_page(
    db: Session,
    source: KnowledgeSource,
    page: CrawledPage,
) -> PagePersistResult:
    """Persist and publish a price-free page derived from the trusted catalogue."""

    result = persist_crawled_page(db, source, page)
    web_page = db.get(KnowledgeWebPage, result.web_page_id)
    if web_page is None:
        raise RuntimeError(f"产品知识页 {page.url} 入库失败")
    document = db.get(KnowledgeDocument, web_page.document_id)
    if document is None:
        raise RuntimeError(f"产品知识页 {page.url} 缺少知识文档")

    # Alias metadata can evolve without changing the rendered product text.
    # Keep trusted catalogue metadata synchronized even on an unchanged page.
    web_page.content_type = page.content_type
    web_page.language = page.language
    web_page.word_count = count_words(page.content)
    web_page.metadata_json = page.metadata

    content_hash = hashlib.sha256(page.content.encode("utf-8")).hexdigest()
    revision = db.scalar(
        select(KnowledgePageRevision).where(
            KnowledgePageRevision.web_page_id == web_page.id,
            KnowledgePageRevision.content_hash == content_hash,
            KnowledgePageRevision.status == "draft",
        )
    )
    if revision is not None:
        apply_revision(db, revision)
    else:
        web_page.review_status = "published"
        document.is_active = True
    db.commit()
    return result


def reconcile_missing_pages(
    db: Session,
    source: KnowledgeSource,
    seen_urls: set[str],
    *,
    authoritative: bool,
) -> int:
    """Mark a page as suspected missing only after two authoritative daily scans."""

    if not authoritative:
        return 0
    suspected_count = 0
    pages = db.scalars(
        select(KnowledgeWebPage).where(KnowledgeWebPage.source_id == source.id)
    ).all()
    for web_page in pages:
        state = db.scalar(
            select(KnowledgePageSyncState).where(
                KnowledgePageSyncState.web_page_id == web_page.id
            )
        )
        if state is None:
            state = KnowledgePageSyncState(web_page_id=web_page.id)
            db.add(state)
        if web_page.url in seen_urls:
            state.last_seen_at = utcnow()
            state.consecutive_missing = 0
            state.availability_status = "active"
            state.suspected_missing_at = None
            continue
        state.consecutive_missing += 1
        if state.consecutive_missing >= 2:
            state.availability_status = "suspected_missing"
            state.suspected_missing_at = state.suspected_missing_at or utcnow()
            suspected_count += 1
        else:
            state.availability_status = "missing_once"
    db.commit()
    return suspected_count


def apply_revision(
    db: Session,
    revision: KnowledgePageRevision,
) -> KnowledgeDocument:
    web_page = db.get(KnowledgeWebPage, revision.web_page_id)
    if web_page is None:
        raise RuntimeError("待审核修订对应的网页不存在")
    document = db.get(KnowledgeDocument, web_page.document_id)
    if document is None:
        raise RuntimeError("待审核修订对应的知识文档不存在")
    document.title = revision.title
    document.content = revision.content
    document.source = web_page.url
    document.category = revision.category
    document.is_active = True
    web_page.content_hash = revision.content_hash
    web_page.content_type = revision.content_type
    web_page.language = revision.language
    web_page.word_count = revision.word_count
    web_page.metadata_json = revision.metadata_json
    web_page.review_status = "published"
    revision.status = "published"
    revision.reviewed_at = utcnow()
    db.query(KnowledgePageRevision).filter(
        KnowledgePageRevision.web_page_id == web_page.id,
        KnowledgePageRevision.status == "draft",
        KnowledgePageRevision.id != revision.id,
    ).update({"status": "superseded"}, synchronize_session=False)
    rebuild_document_chunks(db, document)
    return document


def publish_source_changes(db: Session, source: KnowledgeSource) -> tuple[int, int]:
    """Publish new pages and apply staged changes without exposing unreviewed text."""

    new_pages = db.scalars(
        select(KnowledgeWebPage).where(
            KnowledgeWebPage.source_id == source.id,
            KnowledgeWebPage.review_status == "draft",
        )
    ).all()
    published_new = 0
    for web_page in new_pages:
        document = db.get(KnowledgeDocument, web_page.document_id)
        if document is None:
            continue
        web_page.review_status = "published"
        document.is_active = True
        published_new += 1

    revisions = db.scalars(
        select(KnowledgePageRevision)
        .where(
            KnowledgePageRevision.source_id == source.id,
            KnowledgePageRevision.status == "draft",
        )
        .order_by(KnowledgePageRevision.created_at.asc())
    ).all()
    published_updates = 0
    for revision in revisions:
        apply_revision(db, revision)
        published_updates += 1
    db.commit()
    return published_new, published_updates


def run_crawl_job(
    source_id: int,
    *,
    sync_run_id: int | None = None,
    trigger: str = "manual",
    attempt: int = 0,
) -> CrawlJobResult:
    with SessionLocal() as db:
        source = db.get(KnowledgeSource, source_id)
        if source is None:
            raise RuntimeError("网址来源不存在")
        sync_run = db.get(KnowledgeSyncRun, sync_run_id) if sync_run_id else None
        if sync_run is None:
            sync_run = KnowledgeSyncRun(
                tenant_id=source.tenant_id,
                source_id=source.id,
                trigger=trigger,
                attempt=attempt,
                status="queued",
            )
            db.add(sync_run)
            db.flush()
        source.status = "running"
        source.started_at = utcnow()
        source.completed_at = None
        source.error_message = None
        source.discovered_pages = 0
        source.imported_pages = 0
        source.failed_pages = 0
        sync_run.status = "running"
        sync_run.started_at = utcnow()
        db.commit()

        crawler: WebsiteCrawler | None = None
        seen_urls: set[str] = set()
        change_counts = {"new": 0, "changed": 0, "unchanged": 0}
        missing_pages = 0
        try:
            catalog_pages = {
                page.url: page
                for page in catalog_product_knowledge_pages(db, source)
            }
            crawler = WebsiteCrawler(
                source.root_url,
                max_pages=source.max_pages,
                max_depth=source.max_depth,
            )
            for page in crawler.crawl():
                # Nuxt product routes have no useful server-rendered body. If
                # the generic crawler ever starts extracting one, keep using
                # the price-free structured summary for stable product search.
                catalog_page = catalog_pages.pop(page.url, None)
                if catalog_page is not None:
                    page = catalog_page
                    result = persist_catalog_product_page(db, source, page)
                else:
                    result = persist_crawled_page(db, source, page)
                seen_urls.add(page.url)
                change_counts[result.change] += 1
                source.imported_pages += 1
                source.discovered_pages = crawler.discovered_count
                source.failed_pages = crawler.failed_count
                source.updated_at = utcnow()
                db.commit()
            for page in catalog_pages.values():
                result = persist_catalog_product_page(db, source, page)
                seen_urls.add(page.url)
                change_counts[result.change] += 1
                source.imported_pages += 1
                source.updated_at = utcnow()
                db.commit()
            source.discovered_pages = max(crawler.discovered_count, len(seen_urls))
            source.failed_pages = crawler.failed_count
            missing_pages = reconcile_missing_pages(
                db,
                source,
                seen_urls,
                authoritative=(
                    source.imported_pages > 0
                    and crawler.failed_count == 0
                    and not crawler.limit_reached
                ),
            )
            source.status = "partial" if crawler.failed_count else "completed"
            if source.imported_pages == 0:
                source.status = "failed"
            source.error_message = "\n".join(crawler.errors)[:4000] or None
        except Exception as exc:
            source.status = "failed"
            source.error_message = str(exc)[:4000]
        source.completed_at = utcnow()
        source.updated_at = utcnow()
        sync_run.status = source.status
        sync_run.new_pages = change_counts["new"]
        sync_run.changed_pages = change_counts["changed"]
        sync_run.unchanged_pages = change_counts["unchanged"]
        sync_run.missing_pages = missing_pages
        sync_run.failed_pages = source.failed_pages
        sync_run.error_message = source.error_message
        sync_run.completed_at = utcnow()
        db.commit()
        return CrawlJobResult(
            status=source.status,
            source_id=source.id,
            run_id=sync_run.id,
            error_message=source.error_message,
        )


def count_words(content: str) -> int:
    latin = re.findall(r"[A-Za-z0-9]+(?:[-_'][A-Za-z0-9]+)*", content)
    cjk = re.findall(r"[\u3400-\u9fff]", content)
    return len(latin) + len(cjk)
