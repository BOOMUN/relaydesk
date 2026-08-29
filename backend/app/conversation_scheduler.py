from __future__ import annotations

import logging
import time

from .config import settings
from .database import SessionLocal, create_tables
from .services.conversation_sessions import close_due_context_sessions
from .services.business_automation import expire_automation_sessions


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s conversation-scheduler %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> None:
    create_tables()
    logger.info(
        "started inactivity_minutes=%s interval_seconds=%s",
        settings.ai_context_inactivity_minutes,
        settings.ai_context_scheduler_interval_seconds,
    )
    while True:
        try:
            with SessionLocal() as db:
                result = close_due_context_sessions(db)
                expired_forms = expire_automation_sessions(db)
            if result.closed or result.failed or result.skipped or expired_forms:
                logger.info(
                    "checked=%s closed=%s failed=%s skipped=%s expired_forms=%s",
                    result.checked,
                    result.closed,
                    result.failed,
                    result.skipped,
                    expired_forms,
                )
            time.sleep(settings.ai_context_scheduler_interval_seconds)
        except (OSError, RuntimeError) as exc:
            logger.error("scheduler dependency unavailable: %s", exc)
            time.sleep(5)
        except Exception:
            logger.exception("scheduler iteration failed")
            time.sleep(5)


if __name__ == "__main__":
    main()
