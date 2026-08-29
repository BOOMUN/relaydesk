from __future__ import annotations

import logging
import time

from redis.exceptions import RedisError
from sqlalchemy import select

from .config import settings
from .database import SessionLocal, create_tables
from .models import KnowledgeSource, ProductPriceSource
from .services.knowledge_tasks import (
    claim_daily_source,
    daily_schedule_due,
    enqueue_source_sync,
    get_redis,
    promote_due_tasks,
    release_daily_source,
    scheduler_lock,
)
from .services.product_price_tasks import (
    enqueue_product_price_sync,
    promote_due_product_price_tasks,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s knowledge-scheduler %(message)s",
)
logger = logging.getLogger(__name__)


def schedule_daily_sources() -> int:
    due, local_day = daily_schedule_due()
    if not due:
        return 0
    redis = get_redis()
    with SessionLocal() as db:
        source_ids = db.scalars(select(KnowledgeSource.id).order_by(KnowledgeSource.id)).all()
    created = 0
    for source_id in source_ids:
        if not claim_daily_source(redis, source_id, local_day):
            continue
        try:
            result = enqueue_source_sync(source_id, trigger="scheduled")
            created += int(result.created)
        except Exception:
            release_daily_source(redis, source_id, local_day)
            logger.exception("failed to schedule source=%s", source_id)
    with SessionLocal() as db:
        price_source_ids = db.scalars(
            select(ProductPriceSource.id)
            .where(ProductPriceSource.auto_sync_enabled.is_(True))
            .order_by(ProductPriceSource.id)
        ).all()
    for source_id in price_source_ids:
        if not claim_daily_source(redis, source_id, local_day, kind="product_price"):
            continue
        try:
            result = enqueue_product_price_sync(source_id, trigger="scheduled")
            created += int(result.created)
        except Exception:
            release_daily_source(redis, source_id, local_day, kind="product_price")
            logger.exception("failed to schedule product price source=%s", source_id)
    return created


def main() -> None:
    if settings.knowledge_queue_mode != "redis":
        logger.info("queue mode is %s; scheduler is not required", settings.knowledge_queue_mode)
        return
    create_tables()
    logger.info(
        "started daily_time=%s timezone=%s",
        settings.knowledge_sync_time,
        settings.knowledge_sync_timezone,
    )
    while True:
        try:
            redis = get_redis()
            if scheduler_lock(redis):
                promoted = promote_due_tasks(redis)
                promoted += promote_due_product_price_tasks(redis)
                scheduled = schedule_daily_sources()
                if promoted or scheduled:
                    logger.info("promoted=%s scheduled=%s", promoted, scheduled)
            time.sleep(15)
        except (RedisError, OSError) as exc:
            logger.error("redis unavailable: %s", exc)
            time.sleep(5)
        except Exception:
            logger.exception("scheduler iteration failed")
            time.sleep(5)


if __name__ == "__main__":
    main()
