from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..actions import ActionContext, propose_action
from ..config import settings
from ..models import (
    ActionStatus,
    AuditLog,
    Conversation,
    ConversationStatus,
    Message,
    MessageDirection,
    MessageSender,
    utcnow,
)
from .action_messaging import send_text_action
from .agent import AI_OUTBOUND_LANGUAGE, normalize_ai_outbound_text


CONTEXT_SESSION_ID_KEY = "ai_context_session_id"
CONTEXT_SESSION_STARTED_AT_KEY = "ai_context_session_started_at"
CONTEXT_CLOSE_DUE_AT_KEY = "ai_context_close_due_at"
CONTEXT_CLOSE_STATE_KEY = "ai_context_close_state"
CONTEXT_CLOSE_MESSAGE = "AI 智能結束當前對話"


@dataclass(slots=True)
class ContextCloseResult:
    checked: int = 0
    closed: int = 0
    failed: int = 0
    skipped: int = 0


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return _as_utc(parsed)


def _message_metadata(message: Message) -> dict:
    return dict(message.metadata_json or {})


def latest_context_inbound(db: Session, conversation_id: int) -> Message | None:
    """Return the newest inbound that participates in the new context lifecycle.

    Legacy messages do not have a session ID and are intentionally ignored, so
    enabling this feature cannot send closure notices to historical customers.
    """

    candidates = db.scalars(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.direction == MessageDirection.INBOUND.value,
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(100)
    ).all()
    for message in candidates:
        if _message_metadata(message).get(CONTEXT_SESSION_ID_KEY):
            return message
    return None


def prepare_inbound_context(
    db: Session,
    conversation: Conversation,
    *,
    now: datetime,
    force_new: bool = False,
) -> dict[str, str]:
    """Start or extend a durable 30-minute customer context session."""

    now = _as_utc(now)
    previous = latest_context_inbound(db, conversation.id)
    previous_metadata = _message_metadata(previous) if previous is not None else {}
    previous_state = str(previous_metadata.get(CONTEXT_CLOSE_STATE_KEY) or "")
    previous_session_id = str(previous_metadata.get(CONTEXT_SESSION_ID_KEY) or "")
    can_continue = bool(
        previous
        and previous_session_id
        and previous_state not in {"closed", "cancelled"}
        and not force_new
    )

    if can_continue:
        session_id = previous_session_id
        started_at = str(previous_metadata.get(CONTEXT_SESSION_STARTED_AT_KEY) or now.isoformat())
        previous_metadata[CONTEXT_CLOSE_STATE_KEY] = "continued"
        previous_metadata.pop(CONTEXT_CLOSE_DUE_AT_KEY, None)
        previous_metadata["ai_context_continued_at"] = now.isoformat()
        previous.metadata_json = previous_metadata
    else:
        session_id = uuid4().hex
        started_at = now.isoformat()

    close_due = now + timedelta(minutes=settings.ai_context_inactivity_minutes)
    return {
        CONTEXT_SESSION_ID_KEY: session_id,
        CONTEXT_SESSION_STARTED_AT_KEY: started_at,
        CONTEXT_CLOSE_DUE_AT_KEY: close_due.isoformat(),
        CONTEXT_CLOSE_STATE_KEY: "waiting",
    }


def latest_context_session_id(db: Session, conversation_id: int) -> str | None:
    inbound = latest_context_inbound(db, conversation_id)
    if inbound is None:
        return None
    value = _message_metadata(inbound).get(CONTEXT_SESSION_ID_KEY)
    return str(value) if value else None


def mark_context_session_closed(
    db: Session,
    conversation: Conversation,
    *,
    reason: str,
    now: datetime | None = None,
) -> str | None:
    """Persist a context boundary without altering historical message bodies."""

    closed_at = _as_utc(now or utcnow())
    inbound = latest_context_inbound(db, conversation.id)
    if inbound is None:
        return None
    metadata = _message_metadata(inbound)
    metadata[CONTEXT_CLOSE_STATE_KEY] = "closed"
    metadata.pop(CONTEXT_CLOSE_DUE_AT_KEY, None)
    metadata["ai_context_closed_at"] = closed_at.isoformat()
    metadata["ai_context_close_reason"] = reason
    inbound.metadata_json = metadata
    return str(metadata.get(CONTEXT_SESSION_ID_KEY) or "") or None


def mark_context_session_paused(
    db: Session,
    conversation: Conversation,
    *,
    reason: str,
    now: datetime | None = None,
) -> str | None:
    """Suspend an AI context during human ownership without discarding it."""

    paused_at = _as_utc(now or utcnow())
    inbound = latest_context_inbound(db, conversation.id)
    if inbound is None:
        return None
    metadata = _message_metadata(inbound)
    metadata[CONTEXT_CLOSE_STATE_KEY] = "paused"
    metadata.pop(CONTEXT_CLOSE_DUE_AT_KEY, None)
    metadata["ai_context_paused_at"] = paused_at.isoformat()
    metadata["ai_context_pause_reason"] = reason
    inbound.metadata_json = metadata
    return str(metadata.get(CONTEXT_SESSION_ID_KEY) or "") or None


def mark_context_session_resumed(
    db: Session,
    conversation: Conversation,
    *,
    reason: str,
    now: datetime | None = None,
) -> str | None:
    """Mark a paused context resumable; the next inbound extends its timer."""

    resumed_at = _as_utc(now or utcnow())
    inbound = latest_context_inbound(db, conversation.id)
    if inbound is None:
        return None
    metadata = _message_metadata(inbound)
    if metadata.get(CONTEXT_CLOSE_STATE_KEY) != "paused":
        return str(metadata.get(CONTEXT_SESSION_ID_KEY) or "") or None
    metadata[CONTEXT_CLOSE_STATE_KEY] = "resumed"
    metadata.pop(CONTEXT_CLOSE_DUE_AT_KEY, None)
    metadata["ai_context_resumed_at"] = resumed_at.isoformat()
    metadata["ai_context_resume_reason"] = reason
    inbound.metadata_json = metadata
    return str(metadata.get(CONTEXT_SESSION_ID_KEY) or "") or None


def _latest_inbound_id(db: Session, conversation_id: int) -> int | None:
    return db.scalar(
        select(Message.id)
        .where(
            Message.conversation_id == conversation_id,
            Message.direction == MessageDirection.INBOUND.value,
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(1)
    )


def _existing_close_message(
    db: Session,
    conversation_id: int,
    inbound_id: int,
) -> Message | None:
    messages = db.scalars(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.direction == MessageDirection.OUTBOUND.value,
            Message.sender_type == MessageSender.AI.value,
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(20)
    ).all()
    for message in messages:
        metadata = _message_metadata(message)
        if metadata.get("closure_for_inbound_id") == inbound_id:
            return message
    return None


def _is_due(metadata: dict, now: datetime) -> bool:
    state = str(metadata.get(CONTEXT_CLOSE_STATE_KEY) or "")
    due_at = _parse_datetime(metadata.get(CONTEXT_CLOSE_DUE_AT_KEY))
    if due_at is None or due_at > now:
        return False
    if state in {"waiting", "retry"}:
        return True
    if state != "processing":
        return False
    claimed_at = _parse_datetime(metadata.get("ai_context_close_claimed_at"))
    stale_after = timedelta(minutes=settings.ai_context_close_retry_minutes)
    return claimed_at is None or claimed_at + stale_after <= now


def close_due_context_sessions(
    db: Session,
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> ContextCloseResult:
    """Send the inactivity notice, then move successfully sent sessions to solved."""

    current_time = _as_utc(now or utcnow())
    result = ContextCloseResult()
    conversations = db.scalars(
        select(Conversation)
        .where(
            Conversation.ai_enabled.is_(True),
            Conversation.status.in_(
                [ConversationStatus.OPEN.value, ConversationStatus.PENDING.value]
            ),
        )
        .options(selectinload(Conversation.contact))
        .order_by(Conversation.updated_at)
        .limit(max(1, min(limit, 500)))
    ).all()

    for conversation in conversations:
        if conversation.contact.is_blocked:
            continue
        inbound = latest_context_inbound(db, conversation.id)
        if inbound is None:
            continue
        metadata = _message_metadata(inbound)
        # Local QA conversations stay visible in the inbox but must never reach
        # the configured WhatsApp provider.
        if metadata.get("local_test") is True:
            continue
        if not _is_due(metadata, current_time):
            continue
        result.checked += 1

        metadata[CONTEXT_CLOSE_STATE_KEY] = "processing"
        metadata["ai_context_close_claimed_at"] = current_time.isoformat()
        inbound.metadata_json = metadata
        db.commit()

        if _latest_inbound_id(db, conversation.id) != inbound.id:
            metadata = _message_metadata(inbound)
            metadata[CONTEXT_CLOSE_STATE_KEY] = "continued"
            metadata.pop(CONTEXT_CLOSE_DUE_AT_KEY, None)
            inbound.metadata_json = metadata
            db.commit()
            result.skipped += 1
            continue

        body = normalize_ai_outbound_text(CONTEXT_CLOSE_MESSAGE)
        message = _existing_close_message(db, conversation.id, inbound.id)
        close_metadata = {
            "route": "session_close",
            "language": AI_OUTBOUND_LANGUAGE,
            CONTEXT_SESSION_ID_KEY: metadata.get(CONTEXT_SESSION_ID_KEY),
            "context_closed": False,
            "close_reason": "inactivity",
            "inactivity_minutes": settings.ai_context_inactivity_minutes,
            "closure_for_inbound_id": inbound.id,
        }
        message = send_text_action(
            db,
            conversation,
            context=ActionContext.for_system(
                conversation.tenant_id,
                source_message_id=inbound.id,
            ),
            body=body,
            sender_type=MessageSender.AI.value,
            sender_name="RelayDesk AI",
            metadata=close_metadata,
            idempotency_key=(
                f"session-close:{inbound.id}:"
                f"{metadata.get('ai_context_close_claimed_at', current_time.isoformat())}"
            ),
            existing_message=message,
        )
        delivery_status = message.delivery_status
        error_code = str(
            (message.metadata_json or {}).get("action_error_code") or ""
        ) or None
        if delivery_status == "failed":
            retry_at = current_time + timedelta(minutes=settings.ai_context_close_retry_minutes)
            metadata = _message_metadata(inbound)
            metadata[CONTEXT_CLOSE_STATE_KEY] = "retry"
            metadata[CONTEXT_CLOSE_DUE_AT_KEY] = retry_at.isoformat()
            metadata["ai_context_close_last_error"] = error_code
            inbound.metadata_json = metadata
            result.failed += 1
        else:
            close_execution = propose_action(
                db,
                ActionContext.for_system(
                    conversation.tenant_id,
                    source_message_id=inbound.id,
                ),
                "conversation.update",
                {
                    "conversation_id": conversation.id,
                    "status": ConversationStatus.SOLVED.value,
                    "reason": "inactivity",
                },
                idempotency_key=f"session-solved:{inbound.id}",
            )
            if close_execution.status != ActionStatus.SUCCEEDED.value:
                raise RuntimeError(
                    "Conversation close action failed: "
                    f"{close_execution.error_code or close_execution.status}"
                )
            message_metadata = _message_metadata(message)
            message_metadata["context_closed"] = True
            message.metadata_json = message_metadata
            db.add(
                AuditLog(
                    tenant_id=conversation.tenant_id,
                    user_id=None,
                    action="conversation.ai_context_closed",
                    entity_type="conversation",
                    entity_id=str(conversation.id),
                    details={
                        "reason": "inactivity",
                        "inactivity_minutes": settings.ai_context_inactivity_minutes,
                        "session_id": metadata.get(CONTEXT_SESSION_ID_KEY),
                    },
                )
            )
            result.closed += 1

        db.commit()
        db.refresh(message)

    return result


__all__ = [
    "CONTEXT_CLOSE_DUE_AT_KEY",
    "CONTEXT_CLOSE_MESSAGE",
    "CONTEXT_CLOSE_STATE_KEY",
    "CONTEXT_SESSION_ID_KEY",
    "ContextCloseResult",
    "close_due_context_sessions",
    "latest_context_inbound",
    "latest_context_session_id",
    "mark_context_session_closed",
    "mark_context_session_paused",
    "mark_context_session_resumed",
    "prepare_inbound_context",
]
