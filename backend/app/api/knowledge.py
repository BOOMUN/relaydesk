from __future__ import annotations

from urllib.parse import urlsplit

import hashlib

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from redis.exceptions import RedisError
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import get_current_user, require_roles
from ..models import (
    AuditLog,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgePageRevision,
    KnowledgePageSyncState,
    KnowledgeSource,
    KnowledgeSyncRun,
    KnowledgeWebPage,
    User,
    UserRole,
    utcnow,
)
from ..schemas import (
    KnowledgeCreate,
    KnowledgePublishResponse,
    KnowledgeResponse,
    KnowledgeSourceCreate,
    KnowledgeSourceResponse,
    KnowledgeUpdate,
)
from ..services.knowledge_ingestion import (
    apply_revision,
    count_words,
    publish_source_changes,
    rebuild_document_chunks,
    run_crawl_job,
)
from ..services.knowledge_tasks import (
    KnowledgeQueueError,
    claim_daily_source,
    create_sync_run,
    daily_schedule_due,
    enqueue_source_sync,
    get_redis,
    next_daily_sync,
)
from ..config import settings
from ..services.web_crawler import CrawlError, normalize_public_root_url


router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])
manage_knowledge = require_roles(
    UserRole.ADMIN,
    UserRole.MANAGER,
    UserRole.KNOWLEDGE_MANAGER,
)


def _serialize_document(
    document: KnowledgeDocument,
    page: KnowledgeWebPage | None,
    pending: KnowledgePageRevision | None = None,
    sync_state: KnowledgePageSyncState | None = None,
) -> KnowledgeResponse:
    return KnowledgeResponse(
        id=document.id,
        title=document.title,
        content=document.content,
        source=document.source,
        category=document.category,
        is_active=document.is_active,
        source_id=page.source_id if page else None,
        source_type=page.content_type if page else "manual",
        source_url=page.url if page else None,
        review_status=page.review_status if page else ("published" if document.is_active else "draft"),
        language=page.language if page else "unknown",
        word_count=page.word_count if page else count_words(document.content),
        pending_update=pending is not None,
        pending_revision_id=pending.id if pending else None,
        pending_title=pending.title if pending else None,
        pending_content=pending.content if pending else None,
        pending_category=pending.category if pending else None,
        availability_status=sync_state.availability_status if sync_state else "active",
        consecutive_missing=sync_state.consecutive_missing if sync_state else 0,
        created_at=document.created_at,
        updated_at=document.updated_at,
    )


def _serialize_source(db: Session, source: KnowledgeSource) -> KnowledgeSourceResponse:
    counts = dict(
        db.execute(
            select(KnowledgeWebPage.review_status, func.count(KnowledgeWebPage.id))
            .where(KnowledgeWebPage.source_id == source.id)
            .group_by(KnowledgeWebPage.review_status)
        ).all()
    )
    pending_updates = db.scalar(
        select(func.count(func.distinct(KnowledgePageRevision.web_page_id))).where(
            KnowledgePageRevision.source_id == source.id,
            KnowledgePageRevision.status == "draft",
        )
    ) or 0
    suspected_removed = db.scalar(
        select(func.count(KnowledgePageSyncState.id))
        .join(KnowledgeWebPage, KnowledgeWebPage.id == KnowledgePageSyncState.web_page_id)
        .where(
            KnowledgeWebPage.source_id == source.id,
            KnowledgePageSyncState.availability_status == "suspected_missing",
        )
    ) or 0
    latest_run = db.scalar(
        select(KnowledgeSyncRun)
        .where(KnowledgeSyncRun.source_id == source.id)
        .order_by(KnowledgeSyncRun.queued_at.desc())
    )
    latest_finished_run = db.scalar(
        select(KnowledgeSyncRun)
        .where(
            KnowledgeSyncRun.source_id == source.id,
            KnowledgeSyncRun.status.not_in(("queued", "running")),
        )
        .order_by(KnowledgeSyncRun.completed_at.desc())
    )
    last_successful_run = db.scalar(
        select(KnowledgeSyncRun)
        .where(
            KnowledgeSyncRun.source_id == source.id,
            KnowledgeSyncRun.status == "completed",
            KnowledgeSyncRun.completed_at.is_not(None),
        )
        .order_by(KnowledgeSyncRun.completed_at.desc())
    )
    failed_task_count = db.scalar(
        select(func.count(KnowledgeSyncRun.id)).where(
            KnowledgeSyncRun.source_id == source.id,
            KnowledgeSyncRun.status == "failed",
        )
    ) or 0
    partial_task_count = db.scalar(
        select(func.count(KnowledgeSyncRun.id)).where(
            KnowledgeSyncRun.source_id == source.id,
            KnowledgeSyncRun.status == "partial",
        )
    ) or 0
    last_failed_run = db.scalar(
        select(KnowledgeSyncRun)
        .where(
            KnowledgeSyncRun.source_id == source.id,
            KnowledgeSyncRun.status == "failed",
        )
        .order_by(
            KnowledgeSyncRun.completed_at.desc(),
            KnowledgeSyncRun.queued_at.desc(),
        )
    )
    summary_run = latest_finished_run or latest_run
    return KnowledgeSourceResponse(
        id=source.id,
        root_url=source.root_url,
        domain=source.domain,
        status=source.status,
        max_pages=source.max_pages,
        max_depth=source.max_depth,
        discovered_pages=source.discovered_pages,
        imported_pages=source.imported_pages,
        failed_pages=source.failed_pages,
        draft_pages=int(counts.get("draft", 0)) + int(pending_updates),
        published_pages=int(counts.get("published", 0)),
        pending_updates=int(pending_updates),
        suspected_removed_pages=int(suspected_removed),
        error_message=source.error_message,
        auto_sync_enabled=True,
        sync_time=settings.knowledge_sync_time,
        sync_timezone=settings.knowledge_sync_timezone,
        next_sync_at=next_daily_sync(),
        next_retry_at=(
            latest_run.available_at
            if latest_run is not None
            and latest_run.status == "queued"
            and latest_run.attempt > 0
            else None
        ),
        last_sync_trigger=summary_run.trigger if summary_run else None,
        last_new_pages=summary_run.new_pages if summary_run else 0,
        last_changed_pages=summary_run.changed_pages if summary_run else 0,
        last_unchanged_pages=summary_run.unchanged_pages if summary_run else 0,
        last_missing_pages=summary_run.missing_pages if summary_run else 0,
        last_successful_sync_at=(
            last_successful_run.completed_at if last_successful_run else None
        ),
        failed_task_count=int(failed_task_count),
        partial_task_count=int(partial_task_count),
        last_failed_task_at=(
            (last_failed_run.completed_at or last_failed_run.queued_at)
            if last_failed_run
            else None
        ),
        last_failure_message=(last_failed_run.error_message if last_failed_run else None),
        created_at=source.created_at,
        started_at=source.started_at,
        completed_at=source.completed_at,
        updated_at=source.updated_at,
    )


def _queue_source(
    source: KnowledgeSource,
    *,
    trigger: str,
    user_id: int | None,
    background_tasks: BackgroundTasks,
) -> None:
    try:
        if settings.knowledge_queue_mode == "inline":
            result = create_sync_run(
                source.id,
                trigger=trigger,
                requested_by_user_id=user_id,
            )
            if result.created:
                background_tasks.add_task(
                    run_crawl_job,
                    source.id,
                    sync_run_id=result.run_id,
                    trigger=trigger,
                )
        else:
            enqueue_source_sync(
                source.id,
                trigger=trigger,
                requested_by_user_id=user_id,
            )
            if trigger in {"initial", "manual"}:
                due, local_day = daily_schedule_due()
                if due:
                    try:
                        claim_daily_source(get_redis(), source.id, local_day)
                    except RedisError:
                        pass
    except KnowledgeQueueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/sources", response_model=list[KnowledgeSourceResponse])
def list_sources(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    sources = db.scalars(
        select(KnowledgeSource)
        .where(KnowledgeSource.tenant_id == user.tenant_id)
        .order_by(KnowledgeSource.updated_at.desc())
    ).all()
    return [_serialize_source(db, source) for source in sources]


@router.post(
    "/sources",
    response_model=KnowledgeSourceResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_source(
    payload: KnowledgeSourceCreate,
    background_tasks: BackgroundTasks,
    user: User = Depends(manage_knowledge),
    db: Session = Depends(get_db),
):
    try:
        root_url = normalize_public_root_url(payload.root_url)
    except CrawlError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    existing = db.scalar(
        select(KnowledgeSource).where(
            KnowledgeSource.tenant_id == user.tenant_id,
            KnowledgeSource.root_url == root_url,
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="该网址已经添加，可在来源列表中重新采集")
    source = KnowledgeSource(
        tenant_id=user.tenant_id,
        created_by_user_id=user.id,
        root_url=root_url,
        domain=urlsplit(root_url).hostname or "",
        status="queued",
        max_pages=payload.max_pages,
        max_depth=payload.max_depth,
    )
    db.add(source)
    db.flush()
    db.add(
        AuditLog(
            tenant_id=user.tenant_id,
            user_id=user.id,
            action="knowledge.source_created",
            entity_type="knowledge_source",
            entity_id=str(source.id),
            details={"root_url": root_url},
        )
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="该网址已经添加") from exc
    db.refresh(source)
    _queue_source(
        source,
        trigger="initial",
        user_id=user.id,
        background_tasks=background_tasks,
    )
    db.refresh(source)
    return _serialize_source(db, source)


@router.post(
    "/sources/{source_id}/retry",
    response_model=KnowledgeSourceResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
@router.post(
    "/sources/{source_id}/sync",
    response_model=KnowledgeSourceResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_source(
    source_id: int,
    background_tasks: BackgroundTasks,
    user: User = Depends(manage_knowledge),
    db: Session = Depends(get_db),
):
    source = db.get(KnowledgeSource, source_id)
    if source is None or source.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="网址来源不存在")
    if source.status in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="该网址正在采集中")
    db.add(
        AuditLog(
            tenant_id=user.tenant_id,
            user_id=user.id,
            action="knowledge.source_sync_requested",
            entity_type="knowledge_source",
            entity_id=str(source.id),
        )
    )
    db.commit()
    db.refresh(source)
    _queue_source(
        source,
        trigger="manual",
        user_id=user.id,
        background_tasks=background_tasks,
    )
    db.refresh(source)
    return _serialize_source(db, source)


@router.post("/sources/{source_id}/publish", response_model=KnowledgePublishResponse)
def publish_source(
    source_id: int,
    user: User = Depends(manage_knowledge),
    db: Session = Depends(get_db),
):
    source = db.get(KnowledgeSource, source_id)
    if source is None or source.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="网址来源不存在")
    if source.status in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="请等待网址采集完成后再发布")
    published_new, published_updates = publish_source_changes(db, source)
    published_count = published_new + published_updates
    db.add(
        AuditLog(
            tenant_id=user.tenant_id,
            user_id=user.id,
            action="knowledge.source_published",
            entity_type="knowledge_source",
            entity_id=str(source.id),
            details={
                "published_count": published_count,
                "published_new_count": published_new,
                "published_update_count": published_updates,
            },
        )
    )
    db.commit()
    db.refresh(source)
    return KnowledgePublishResponse(
        source=_serialize_source(db, source),
        published_count=published_count,
        published_new_count=published_new,
        published_update_count=published_updates,
    )


@router.delete("/sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_source(
    source_id: int,
    user: User = Depends(manage_knowledge),
    db: Session = Depends(get_db),
):
    source = db.get(KnowledgeSource, source_id)
    if source is None or source.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="网址来源不存在")
    if source.status in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="采集运行时不能删除来源")
    page_rows = db.execute(
        select(KnowledgeWebPage.id, KnowledgeWebPage.document_id).where(
            KnowledgeWebPage.source_id == source.id
        )
    ).all()
    page_ids = [row.id for row in page_rows]
    document_ids = [row.document_id for row in page_rows]
    if document_ids:
        db.execute(
            delete(KnowledgePageRevision).where(KnowledgePageRevision.source_id == source.id)
        )
        db.execute(
            delete(KnowledgePageSyncState).where(
                KnowledgePageSyncState.web_page_id.in_(page_ids)
            )
        )
        db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id.in_(document_ids)))
        db.execute(delete(KnowledgeWebPage).where(KnowledgeWebPage.source_id == source.id))
        db.execute(delete(KnowledgeDocument).where(KnowledgeDocument.id.in_(document_ids)))
    db.execute(delete(KnowledgeSyncRun).where(KnowledgeSyncRun.source_id == source.id))
    db.delete(source)
    db.add(
        AuditLog(
            tenant_id=user.tenant_id,
            user_id=user.id,
            action="knowledge.source_deleted",
            entity_type="knowledge_source",
            entity_id=str(source_id),
        )
    )
    db.commit()


@router.get("", response_model=list[KnowledgeResponse])
def list_documents(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    documents = db.scalars(
        select(KnowledgeDocument)
        .where(KnowledgeDocument.tenant_id == user.tenant_id)
        .order_by(KnowledgeDocument.updated_at.desc())
    ).all()
    document_ids = [document.id for document in documents]
    pages = (
        db.scalars(
            select(KnowledgeWebPage).where(KnowledgeWebPage.document_id.in_(document_ids))
        ).all()
        if document_ids
        else []
    )
    page_by_document = {page.document_id: page for page in pages}
    page_ids = [page.id for page in pages]
    pending_revisions = (
        db.scalars(
            select(KnowledgePageRevision)
            .where(
                KnowledgePageRevision.web_page_id.in_(page_ids),
                KnowledgePageRevision.status == "draft",
            )
            .order_by(KnowledgePageRevision.created_at.desc())
        ).all()
        if page_ids
        else []
    )
    pending_by_page: dict[int, KnowledgePageRevision] = {}
    for revision in pending_revisions:
        pending_by_page.setdefault(revision.web_page_id, revision)
    sync_states = (
        db.scalars(
            select(KnowledgePageSyncState).where(
                KnowledgePageSyncState.web_page_id.in_(page_ids)
            )
        ).all()
        if page_ids
        else []
    )
    state_by_page = {state.web_page_id: state for state in sync_states}
    return [
        _serialize_document(
            document,
            page_by_document.get(document.id),
            pending_by_page.get(page_by_document[document.id].id)
            if document.id in page_by_document
            else None,
            state_by_page.get(page_by_document[document.id].id)
            if document.id in page_by_document
            else None,
        )
        for document in documents
    ]


@router.post("", response_model=KnowledgeResponse, status_code=status.HTTP_201_CREATED)
def create_document(
    payload: KnowledgeCreate,
    user: User = Depends(manage_knowledge),
    db: Session = Depends(get_db),
):
    document = KnowledgeDocument(tenant_id=user.tenant_id, **payload.model_dump())
    db.add(document)
    db.flush()
    rebuild_document_chunks(db, document)
    db.add(
        AuditLog(
            tenant_id=user.tenant_id,
            user_id=user.id,
            action="knowledge.created",
            entity_type="knowledge_document",
            entity_id=str(document.id),
            details={"title": document.title},
        )
    )
    db.commit()
    db.refresh(document)
    return _serialize_document(document, None)


@router.patch("/{document_id}", response_model=KnowledgeResponse)
def update_document(
    document_id: int,
    payload: KnowledgeUpdate,
    user: User = Depends(manage_knowledge),
    db: Session = Depends(get_db),
):
    document = db.get(KnowledgeDocument, document_id)
    if document is None or document.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="知识文档不存在")
    page = db.scalar(
        select(KnowledgeWebPage).where(KnowledgeWebPage.document_id == document.id)
    )
    changes = payload.model_dump(exclude_unset=True)
    review_status = changes.pop("review_status", None)
    pending_revision_id = changes.pop("pending_revision_id", None)

    if pending_revision_id is not None:
        revision = db.get(KnowledgePageRevision, pending_revision_id)
        if (
            revision is None
            or page is None
            or revision.web_page_id != page.id
            or revision.status != "draft"
        ):
            raise HTTPException(status_code=404, detail="待审核的网站更新不存在")
        for key in ("title", "content", "category"):
            if key in changes:
                setattr(revision, key, changes[key])
        revision.content_hash = hashlib.sha256(revision.content.encode("utf-8")).hexdigest()
        revision.word_count = count_words(revision.content)
        if review_status == "published":
            apply_revision(db, revision)
            pending = None
        else:
            pending = revision
        db.add(
            AuditLog(
                tenant_id=user.tenant_id,
                user_id=user.id,
                action=(
                    "knowledge.revision_published"
                    if review_status == "published"
                    else "knowledge.revision_updated"
                ),
                entity_type="knowledge_page_revision",
                entity_id=str(revision.id),
            )
        )
        db.commit()
        db.refresh(document)
        sync_state = db.scalar(
            select(KnowledgePageSyncState).where(
                KnowledgePageSyncState.web_page_id == page.id
            )
        )
        return _serialize_document(document, page, pending, sync_state)

    content_changed = "content" in changes
    for key, value in changes.items():
        setattr(document, key, value)
    if review_status is not None:
        document.is_active = review_status == "published"
        if page is not None:
            page.review_status = review_status
    elif "is_active" in changes and page is not None:
        page.review_status = "published" if document.is_active else "draft"
    if content_changed:
        # Use the configured multilingual provider for both manual documents
        # and crawled pages.  ``prefer_local`` is reserved for explicit test
        # fixtures and must not create a mixed-model production index.
        rebuild_document_chunks(db, document)
    db.add(
        AuditLog(
            tenant_id=user.tenant_id,
            user_id=user.id,
            action="knowledge.updated",
            entity_type="knowledge_document",
            entity_id=str(document.id),
        )
    )
    db.commit()
    db.refresh(document)
    pending = (
        db.scalar(
            select(KnowledgePageRevision)
            .where(
                KnowledgePageRevision.web_page_id == page.id,
                KnowledgePageRevision.status == "draft",
            )
            .order_by(KnowledgePageRevision.created_at.desc())
        )
        if page is not None
        else None
    )
    sync_state = (
        db.scalar(
            select(KnowledgePageSyncState).where(
                KnowledgePageSyncState.web_page_id == page.id
            )
        )
        if page is not None
        else None
    )
    return _serialize_document(document, page, pending, sync_state)


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(
    document_id: int,
    user: User = Depends(manage_knowledge),
    db: Session = Depends(get_db),
):
    document = db.get(KnowledgeDocument, document_id)
    if document is None or document.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="知识文档不存在")
    db.delete(document)
    db.add(
        AuditLog(
            tenant_id=user.tenant_id,
            user_id=user.id,
            action="knowledge.deleted",
            entity_type="knowledge_document",
            entity_id=str(document_id),
        )
    )
    db.commit()
