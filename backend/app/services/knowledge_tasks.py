from __future__ import annotations

import json
import logging
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import select

from ..config import settings
from ..database import SessionLocal
from ..models import (
    KnowledgeSource,
    KnowledgeSyncRun,
    ProductPriceSource,
    ProductPriceSyncRun,
    utcnow,
)


logger = logging.getLogger(__name__)
QUEUE_KEY = "agentdesk:knowledge:queue"
PROCESSING_KEY = "agentdesk:knowledge:processing"
DELAYED_KEY = "agentdesk:knowledge:delayed"
SCHEDULER_LOCK_KEY = "agentdesk:knowledge:scheduler-lock"
SOURCE_LOCK_PREFIX = "agentdesk:knowledge:source-lock:"
DAILY_KEY_PREFIX = "agentdesk:knowledge:daily:"
SOURCE_LOCK_SECONDS = 6 * 60 * 60
RETRY_DELAYS = (30 * 60, 90 * 60)


class KnowledgeQueueError(RuntimeError):
    pass


@dataclass(slots=True)
class EnqueueResult:
    run_id: int
    source_id: int
    created: bool
    available_at: datetime


def get_redis() -> Redis:
    return Redis.from_url(
        settings.knowledge_redis_url,
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=15,
        health_check_interval=30,
    )


def sync_timezone() -> ZoneInfo:
    try:
        return ZoneInfo(settings.knowledge_sync_timezone)
    except ZoneInfoNotFoundError as exc:
        raise KnowledgeQueueError(
            f"无效的知识库同步时区：{settings.knowledge_sync_timezone}"
        ) from exc


def next_daily_sync(now: datetime | None = None) -> datetime:
    zone = sync_timezone()
    local_now = (now or datetime.now(timezone.utc)).astimezone(zone)
    candidate = local_now.replace(
        hour=settings.knowledge_sync_hour,
        minute=settings.knowledge_sync_minute,
        second=0,
        microsecond=0,
    )
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate


def create_sync_run(
    source_id: int,
    *,
    trigger: str,
    requested_by_user_id: int | None = None,
    attempt: int = 0,
    delay_seconds: int = 0,
) -> EnqueueResult:
    available_at = utcnow() + timedelta(seconds=max(0, delay_seconds))
    with SessionLocal() as db:
        source = db.get(KnowledgeSource, source_id)
        if source is None:
            raise KnowledgeQueueError("网址来源不存在")
        active = db.scalar(
            select(KnowledgeSyncRun)
            .where(
                KnowledgeSyncRun.source_id == source.id,
                KnowledgeSyncRun.status.in_(("queued", "running")),
            )
            .order_by(KnowledgeSyncRun.queued_at.desc())
        )
        if active is not None:
            return EnqueueResult(
                run_id=active.id,
                source_id=source.id,
                created=False,
                available_at=active.available_at,
            )
        run = KnowledgeSyncRun(
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
        return EnqueueResult(
            run_id=run.id,
            source_id=source.id,
            created=True,
            available_at=run.available_at,
        )


def task_payload(run_id: int, source_id: int, *, kind: str = "knowledge") -> str:
    return json.dumps(
        {"kind": kind, "run_id": run_id, "source_id": source_id},
        separators=(",", ":"),
    )


def push_sync_run(result: EnqueueResult, *, delay_seconds: int = 0) -> None:
    if not result.created:
        return
    payload = task_payload(result.run_id, result.source_id)
    try:
        redis = get_redis()
        if delay_seconds > 0:
            redis.zadd(DELAYED_KEY, {payload: result.available_at.timestamp()})
        else:
            redis.lpush(QUEUE_KEY, payload)
    except RedisError as exc:
        mark_enqueue_failed(result.run_id, "知识库任务队列暂时不可用")
        raise KnowledgeQueueError("知识库任务队列暂时不可用，请确认 Redis 已启动") from exc


def enqueue_source_sync(
    source_id: int,
    *,
    trigger: str,
    requested_by_user_id: int | None = None,
    attempt: int = 0,
    delay_seconds: int = 0,
) -> EnqueueResult:
    result = create_sync_run(
        source_id,
        trigger=trigger,
        requested_by_user_id=requested_by_user_id,
        attempt=attempt,
        delay_seconds=delay_seconds,
    )
    push_sync_run(result, delay_seconds=delay_seconds)
    return result


def mark_enqueue_failed(run_id: int, message: str) -> None:
    with SessionLocal() as db:
        run = db.get(KnowledgeSyncRun, run_id)
        if run is None:
            return
        run.status = "failed"
        run.error_message = message
        run.completed_at = utcnow()
        source = db.get(KnowledgeSource, run.source_id)
        if source is not None:
            source.status = "failed"
            source.error_message = message
            source.completed_at = utcnow()
        db.commit()


def promote_due_tasks(redis: Redis | None = None, *, now: datetime | None = None) -> int:
    client = redis or get_redis()
    cutoff = (now or utcnow()).timestamp()
    due = client.zrangebyscore(DELAYED_KEY, 0, cutoff, start=0, num=100)
    promoted = 0
    for payload in due:
        with client.pipeline(transaction=True) as pipe:
            pipe.zrem(DELAYED_KEY, payload)
            pipe.lpush(QUEUE_KEY, payload)
            removed, _ = pipe.execute()
        if removed:
            promoted += 1
    return promoted


def recover_processing_tasks(redis: Redis | None = None) -> int:
    """Return tasks left in the processing list after an unclean worker exit."""

    client = redis or get_redis()
    recovered = 0
    while True:
        payload = client.rpop(PROCESSING_KEY)
        if payload is None:
            break
        try:
            data = json.loads(payload)
            run_id = int(data["run_id"])
            kind = str(data.get("kind") or "knowledge")
            with SessionLocal() as db:
                run_model = ProductPriceSyncRun if kind == "product_price" else KnowledgeSyncRun
                source_model = ProductPriceSource if kind == "product_price" else KnowledgeSource
                run = db.get(run_model, run_id)
                if run is None or run.status not in {"queued", "running"}:
                    continue
                run.status = "queued"
                run.started_at = None
                source = db.get(source_model, run.source_id)
                if source is None:
                    continue
                source.status = "queued"
                source.error_message = None
                db.commit()
            client.lpush(QUEUE_KEY, payload)
            recovered += 1
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            logger.warning("discarded malformed processing payload")
    return recovered


def acknowledge_task(redis: Redis, payload: str) -> None:
    redis.lrem(PROCESSING_KEY, 1, payload)


def requeue_processing_task(redis: Redis, payload: str) -> None:
    with redis.pipeline(transaction=True) as pipe:
        pipe.lrem(PROCESSING_KEY, 1, payload)
        pipe.lpush(QUEUE_KEY, payload)
        pipe.execute()


def claim_daily_source(
    redis: Redis,
    source_id: int,
    local_day: date,
    *,
    kind: str = "knowledge",
) -> bool:
    key = f"{DAILY_KEY_PREFIX}{kind}:{local_day.isoformat()}:{source_id}"
    return bool(redis.set(key, "1", nx=True, ex=3 * 24 * 60 * 60))


def release_daily_source(
    redis: Redis,
    source_id: int,
    local_day: date,
    *,
    kind: str = "knowledge",
) -> None:
    redis.delete(f"{DAILY_KEY_PREFIX}{kind}:{local_day.isoformat()}:{source_id}")


def daily_schedule_due(now: datetime | None = None) -> tuple[bool, date]:
    zone = sync_timezone()
    local_now = (now or datetime.now(timezone.utc)).astimezone(zone)
    due = (local_now.hour, local_now.minute) >= (
        settings.knowledge_sync_hour,
        settings.knowledge_sync_minute,
    )
    return due, local_now.date()


@contextmanager
def source_task_lock(
    redis: Redis,
    source_id: int,
    *,
    kind: str = "knowledge",
) -> Iterator[bool]:
    key = f"{SOURCE_LOCK_PREFIX}{kind}:{source_id}"
    token = secrets.token_urlsafe(24)
    acquired = bool(redis.set(key, token, nx=True, ex=SOURCE_LOCK_SECONDS))
    try:
        yield acquired
    finally:
        if acquired:
            redis.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then "
                "return redis.call('del', KEYS[1]) else return 0 end",
                1,
                key,
                token,
            )


def scheduler_lock(redis: Redis) -> bool:
    return bool(redis.set(SCHEDULER_LOCK_KEY, str(time.time()), nx=True, ex=25))
