from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response, status
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..config import settings
from ..database import get_db
from ..dependencies import get_current_user, require_roles
from ..models import (
    AuditLog,
    Product,
    ProductPriceHistory,
    ProductPriceOffer,
    ProductPriceSource,
    ProductPriceSyncRun,
    User,
    UserRole,
)
from ..schemas import (
    ProductPriceHistoryResponse,
    ProductPriceProductResponse,
    ProductPriceSourceCreate,
    ProductPriceSourceResponse,
)
from ..services.knowledge_tasks import (
    KnowledgeQueueError,
    claim_daily_source,
    daily_schedule_due,
    get_redis,
    next_daily_sync,
)
from ..services.product_price_ingestion import run_product_price_sync
from ..services.product_price_tasks import (
    create_product_price_sync_run,
    enqueue_product_price_sync,
)
from ..services.web_crawler import CrawlError, normalize_public_root_url


router = APIRouter(prefix="/api/product-prices", tags=["product-prices"])
manage_prices = require_roles(
    UserRole.ADMIN,
    UserRole.MANAGER,
    UserRole.KNOWLEDGE_MANAGER,
)


def _serialize_source(db: Session, source: ProductPriceSource) -> ProductPriceSourceResponse:
    latest_run = db.scalar(
        select(ProductPriceSyncRun)
        .where(ProductPriceSyncRun.source_id == source.id)
        .order_by(ProductPriceSyncRun.queued_at.desc())
    )
    latest_finished = db.scalar(
        select(ProductPriceSyncRun)
        .where(
            ProductPriceSyncRun.source_id == source.id,
            ProductPriceSyncRun.status.not_in(("queued", "running")),
        )
        .order_by(ProductPriceSyncRun.completed_at.desc())
    )
    summary = latest_finished or latest_run
    return ProductPriceSourceResponse(
        id=source.id,
        name=source.name,
        root_url=source.root_url,
        domain=source.domain,
        adapter=source.adapter,
        status=source.status,
        auto_sync_enabled=source.auto_sync_enabled,
        max_pages=source.max_pages,
        discovered_products=source.discovered_products,
        imported_products=source.imported_products,
        imported_offers=source.imported_offers,
        failed_pages=source.failed_pages,
        error_message=source.error_message,
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
        last_sync_trigger=summary.trigger if summary else None,
        last_new_products=summary.new_products if summary else 0,
        last_new_offers=summary.new_offers if summary else 0,
        last_changed_offers=summary.changed_offers if summary else 0,
        last_unchanged_offers=summary.unchanged_offers if summary else 0,
        created_at=source.created_at,
        started_at=source.started_at,
        completed_at=source.completed_at,
        updated_at=source.updated_at,
    )


def _serialize_product(product: Product) -> ProductPriceProductResponse:
    return ProductPriceProductResponse(
        id=product.id,
        source_id=product.source_id,
        source_name=product.source.name,
        source_url=product.source.root_url,
        canonical_url=product.canonical_url,
        name=product.name,
        name_translations=product.name_translations or {},
        aliases=product.aliases or [],
        category=product.category,
        product_type=product.product_type,
        destination=product.destination,
        network=product.network,
        description=product.description,
        metadata_json=product.metadata_json or {},
        is_active=product.is_active,
        last_seen_at=product.last_seen_at,
        updated_at=product.updated_at,
        offers=[offer for offer in product.offers if offer.is_active],
    )


def _queue_source(
    source: ProductPriceSource,
    *,
    trigger: str,
    user_id: int | None,
    background_tasks: BackgroundTasks,
) -> None:
    try:
        if settings.knowledge_queue_mode == "inline":
            result = create_product_price_sync_run(
                source.id,
                trigger=trigger,
                requested_by_user_id=user_id,
            )
            if result.created:
                background_tasks.add_task(
                    run_product_price_sync,
                    source.id,
                    sync_run_id=result.run_id,
                    trigger=trigger,
                )
        else:
            enqueue_product_price_sync(
                source.id,
                trigger=trigger,
                requested_by_user_id=user_id,
            )
            if trigger in {"initial", "manual"}:
                due, local_day = daily_schedule_due()
                if due:
                    try:
                        claim_daily_source(
                            get_redis(), source.id, local_day, kind="product_price"
                        )
                    except RedisError:
                        pass
    except KnowledgeQueueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/sources", response_model=list[ProductPriceSourceResponse])
def list_sources(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    sources = db.scalars(
        select(ProductPriceSource)
        .where(ProductPriceSource.tenant_id == user.tenant_id)
        .order_by(ProductPriceSource.updated_at.desc())
    ).all()
    return [_serialize_source(db, source) for source in sources]


@router.post(
    "/sources",
    response_model=ProductPriceSourceResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_source(
    payload: ProductPriceSourceCreate,
    background_tasks: BackgroundTasks,
    user: User = Depends(manage_prices),
    db: Session = Depends(get_db),
):
    try:
        root_url = normalize_public_root_url(payload.root_url)
    except CrawlError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    existing = db.scalar(
        select(ProductPriceSource).where(
            ProductPriceSource.tenant_id == user.tenant_id,
            ProductPriceSource.root_url == root_url,
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="该商品网址已经添加")
    domain = urlsplit(root_url).hostname or ""
    source = ProductPriceSource(
        tenant_id=user.tenant_id,
        created_by_user_id=user.id,
        name=payload.name or domain.removeprefix("www."),
        root_url=root_url,
        domain=domain,
        max_pages=payload.max_pages,
        status="queued",
    )
    db.add(source)
    db.flush()
    db.add(
        AuditLog(
            tenant_id=user.tenant_id,
            user_id=user.id,
            action="product_price.source_created",
            entity_type="product_price_source",
            entity_id=str(source.id),
            details={"root_url": root_url},
        )
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="该商品网址已经添加") from exc
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
    "/sources/{source_id}/sync",
    response_model=ProductPriceSourceResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
@router.post(
    "/sources/{source_id}/retry",
    response_model=ProductPriceSourceResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def sync_source(
    source_id: int,
    background_tasks: BackgroundTasks,
    user: User = Depends(manage_prices),
    db: Session = Depends(get_db),
):
    source = db.get(ProductPriceSource, source_id)
    if source is None or source.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="商品价格来源不存在")
    if source.status in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="该商品网址正在同步")
    db.add(
        AuditLog(
            tenant_id=user.tenant_id,
            user_id=user.id,
            action="product_price.source_sync_requested",
            entity_type="product_price_source",
            entity_id=str(source.id),
        )
    )
    db.commit()
    _queue_source(
        source,
        trigger="manual",
        user_id=user.id,
        background_tasks=background_tasks,
    )
    db.refresh(source)
    return _serialize_source(db, source)


@router.delete("/sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_source(
    source_id: int,
    user: User = Depends(manage_prices),
    db: Session = Depends(get_db),
):
    source = db.get(ProductPriceSource, source_id)
    if source is None or source.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="商品价格来源不存在")
    if source.status in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="同步运行时不能删除来源")
    db.delete(source)
    db.add(
        AuditLog(
            tenant_id=user.tenant_id,
            user_id=user.id,
            action="product_price.source_deleted",
            entity_type="product_price_source",
            entity_id=str(source_id),
        )
    )
    db.commit()


@router.get("/products", response_model=list[ProductPriceProductResponse])
def list_products(
    response: Response,
    source_id: int | None = None,
    q: str = Query(default="", max_length=160),
    category: str | None = Query(default=None, max_length=80),
    include_inactive: bool = False,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    statement = (
        select(Product)
        .join(ProductPriceSource, ProductPriceSource.id == Product.source_id)
        .where(Product.tenant_id == user.tenant_id)
        .options(selectinload(Product.source), selectinload(Product.offers))
        .order_by(ProductPriceSource.name, Product.category, Product.name)
    )
    if source_id is not None:
        statement = statement.where(Product.source_id == source_id)
    if category:
        statement = statement.where(Product.category == category)
    if not include_inactive:
        statement = statement.where(Product.is_active.is_(True))
    products = list(db.scalars(statement).unique().all())
    query = q.strip().casefold()
    if query:
        products = [
            product
            for product in products
            if query
            in " ".join(
                [
                    product.name,
                    product.destination or "",
                    product.network or "",
                    *(product.aliases or []),
                    *(product.name_translations or {}).values(),
                ]
            ).casefold()
        ]
    response.headers["X-Total-Count"] = str(len(products))
    return [_serialize_product(product) for product in products]


@router.get(
    "/offers/{offer_id}/history",
    response_model=list[ProductPriceHistoryResponse],
)
def offer_history(
    offer_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    offer = db.get(ProductPriceOffer, offer_id)
    if offer is None or offer.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="商品价格规格不存在")
    return db.scalars(
        select(ProductPriceHistory)
        .where(ProductPriceHistory.offer_id == offer.id)
        .order_by(ProductPriceHistory.observed_at.desc())
    ).all()
