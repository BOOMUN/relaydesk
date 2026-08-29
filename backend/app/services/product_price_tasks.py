from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import select

from ..database import SessionLocal
from ..models import ProductPriceSource, ProductPriceSyncRun, utcnow
from .knowledge_tasks import KnowledgeQueueError, get_redis, task_payload


PRODUCT_PRICE_QUEUE_KEY = "agentdesk:product-price:queue"
PRODUCT_PRICE_PROCESSING_KEY = "agentdesk:product-price:processing"
PRODUCT_PRICE_DELAYED_KEY = "agentdesk:product-price:delayed"


@dataclass(slots=True)
class ProductPriceEnqueueResult:
    run_id: int
    source_id: int
    created: bool
    available_at: datetime


def create_product_price_sync_run(
    source_id: int,
    *,
    trigger: str,
    requested_by_user_id: int | None = None,
    attempt: int = 0,
    delay_seconds: int = 0,
) -> ProductPriceEnqueueResult:
    available_at = utcnow() + timedelta(seconds=max(0, delay_seconds))
    with SessionLocal() as db:
        source = db.get(ProductPriceSource, source_id)
        if source is None:
            raise KnowledgeQueueError("商品价格来源不存在")
        active = db.scalar(
            select(ProductPriceSyncRun)
            .where(
                ProductPriceSyncRun.source_id == source.id,
                ProductPriceSyncRun.status.in_(("queued", "running")),
            )
            .order_by(ProductPriceSyncRun.queued_at.desc())
        )
        if active is not None:
            return ProductPriceEnqueueResult(
                run_id=active.id,
                source_id=source.id,
                created=False,
                available_at=active.available_at,
            )
        run = ProductPriceSyncRun(
            tenant_id=source.tenant_id,
            source_id=source.id,
            requested_by_user_id=requested_by_user_id,
            trigger=trigger,
            attempt=attempt,
            status="queued",
            available_at=available_at,
        )
        db.add(run)
        source.status = "queued"
        source.error_message = None
        source.updated_at = utcnow()
        db.commit()
        db.refresh(run)
        return ProductPriceEnqueueResult(
            run_id=run.id,
            source_id=source.id,
            created=True,
            available_at=run.available_at,
        )


def mark_product_price_enqueue_failed(run_id: int, message: str) -> None:
    with SessionLocal() as db:
        run = db.get(ProductPriceSyncRun, run_id)
        if run is None:
            return
        run.status = "failed"
        run.error_message = message
        run.completed_at = utcnow()
        source = db.get(ProductPriceSource, run.source_id)
        if source is not None:
            source.status = "failed"
            source.error_message = message
            source.completed_at = utcnow()
        db.commit()


def push_product_price_sync_run(
    result: ProductPriceEnqueueResult,
    *,
    delay_seconds: int = 0,
) -> None:
    if not result.created:
        return
    payload = task_payload(result.run_id, result.source_id, kind="product_price")
    try:
        redis = get_redis()
        if delay_seconds > 0:
            redis.zadd(PRODUCT_PRICE_DELAYED_KEY, {payload: result.available_at.timestamp()})
        else:
            redis.lpush(PRODUCT_PRICE_QUEUE_KEY, payload)
    except RedisError as exc:
        mark_product_price_enqueue_failed(result.run_id, "商品价格任务队列暂时不可用")
        raise KnowledgeQueueError("商品价格任务队列暂时不可用，请确认 Redis 已启动") from exc


def enqueue_product_price_sync(
    source_id: int,
    *,
    trigger: str,
    requested_by_user_id: int | None = None,
    attempt: int = 0,
    delay_seconds: int = 0,
) -> ProductPriceEnqueueResult:
    result = create_product_price_sync_run(
        source_id,
        trigger=trigger,
        requested_by_user_id=requested_by_user_id,
        attempt=attempt,
        delay_seconds=delay_seconds,
    )
    push_product_price_sync_run(result, delay_seconds=delay_seconds)
    return result


def promote_due_product_price_tasks(
    redis: Redis | None = None,
    *,
    now: datetime | None = None,
) -> int:
    client = redis or get_redis()
    cutoff = (now or utcnow()).timestamp()
    due = client.zrangebyscore(PRODUCT_PRICE_DELAYED_KEY, 0, cutoff, start=0, num=100)
    promoted = 0
    for payload in due:
        with client.pipeline(transaction=True) as pipe:
            pipe.zrem(PRODUCT_PRICE_DELAYED_KEY, payload)
            pipe.lpush(PRODUCT_PRICE_QUEUE_KEY, payload)
            removed, _ = pipe.execute()
        if removed:
            promoted += 1
    return promoted


def recover_product_price_processing_tasks(redis: Redis | None = None) -> int:
    client = redis or get_redis()
    recovered = 0
    while True:
        payload = client.rpop(PRODUCT_PRICE_PROCESSING_KEY)
        if payload is None:
            break
        try:
            data = json.loads(payload)
            run_id = int(data["run_id"])
            with SessionLocal() as db:
                run = db.get(ProductPriceSyncRun, run_id)
                if run is None or run.status not in {"queued", "running"}:
                    continue
                source = db.get(ProductPriceSource, run.source_id)
                if source is None:
                    continue
                run.status = "queued"
                run.started_at = None
                source.status = "queued"
                source.error_message = None
                db.commit()
            client.lpush(PRODUCT_PRICE_QUEUE_KEY, payload)
            recovered += 1
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return recovered


def acknowledge_product_price_task(redis: Redis, payload: str) -> None:
    redis.lrem(PRODUCT_PRICE_PROCESSING_KEY, 1, payload)


def requeue_product_price_task(redis: Redis, payload: str) -> None:
    with redis.pipeline(transaction=True) as pipe:
        pipe.lrem(PRODUCT_PRICE_PROCESSING_KEY, 1, payload)
        pipe.lpush(PRODUCT_PRICE_QUEUE_KEY, payload)
        pipe.execute()
