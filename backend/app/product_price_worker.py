from __future__ import annotations

import json
import logging
import time

from redis.exceptions import RedisError

from .config import settings
from .database import SessionLocal, create_tables
from .models import ProductPriceSyncRun
from .services.knowledge_tasks import RETRY_DELAYS, get_redis, source_task_lock
from .services.product_price_ingestion import run_product_price_sync
from .services.product_price_tasks import (
    PRODUCT_PRICE_PROCESSING_KEY,
    PRODUCT_PRICE_QUEUE_KEY,
    acknowledge_product_price_task,
    enqueue_product_price_sync,
    recover_product_price_processing_tasks,
    requeue_product_price_task,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s product-price-worker %(message)s",
)
logger = logging.getLogger(__name__)


def process_payload(payload: str) -> None:
    data = json.loads(payload)
    run_id = int(data["run_id"])
    source_id = int(data["source_id"])
    redis = get_redis()
    with source_task_lock(redis, source_id, kind="product_price") as acquired:
        if not acquired:
            logger.info("source %s already running; returning task to queue", source_id)
            redis.lpush(PRODUCT_PRICE_QUEUE_KEY, payload)
            time.sleep(2)
            return
        with SessionLocal() as db:
            run = db.get(ProductPriceSyncRun, run_id)
            if run is None or run.status != "queued":
                return
            trigger = run.trigger
            attempt = run.attempt
        logger.info(
            "running source=%s run=%s trigger=%s attempt=%s",
            source_id,
            run_id,
            trigger,
            attempt,
        )
        result = run_product_price_sync(
            source_id,
            sync_run_id=run_id,
            trigger=trigger,
            attempt=attempt,
        )
        logger.info("completed source=%s run=%s status=%s", source_id, run_id, result.status)
        if result.status == "failed" and attempt < len(RETRY_DELAYS):
            delay = RETRY_DELAYS[attempt]
            retry = enqueue_product_price_sync(
                source_id,
                trigger="retry",
                attempt=attempt + 1,
                delay_seconds=delay,
            )
            logger.warning(
                "scheduled retry source=%s run=%s delay_seconds=%s",
                source_id,
                retry.run_id,
                delay,
            )


def main() -> None:
    if settings.knowledge_queue_mode != "redis":
        logger.info("queue mode is %s; worker is not required", settings.knowledge_queue_mode)
        return
    create_tables()
    redis = get_redis()
    recovered = recover_product_price_processing_tasks(redis)
    logger.info("started recovered=%s", recovered)
    while True:
        payload: str | None = None
        try:
            redis = get_redis()
            payload = redis.brpoplpush(
                PRODUCT_PRICE_QUEUE_KEY,
                PRODUCT_PRICE_PROCESSING_KEY,
                timeout=10,
            )
            if payload is None:
                continue
            process_payload(payload)
            acknowledge_product_price_task(redis, payload)
        except (RedisError, OSError) as exc:
            logger.error("redis unavailable: %s", exc)
            time.sleep(5)
        except Exception:
            logger.exception("task failed unexpectedly")
            if payload is not None:
                try:
                    requeue_product_price_task(get_redis(), payload)
                except RedisError:
                    pass
            time.sleep(2)


if __name__ == "__main__":
    main()
