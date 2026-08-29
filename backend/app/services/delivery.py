from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import Message, MessageDeliveryAttempt, MessageDeliveryReceipt, utcnow


_SUCCESS_ORDER = {
    "pending": 0,
    "sent": 1,
    "delivered": 2,
    "read": 3,
    "played": 4,
    "deleted": 5,
}

_DELIVERY_STATUS_ALIASES = {
    "0": "failed",
    "1": "pending",
    "2": "sent",
    "3": "delivered",
    "4": "read",
    "5": "played",
    "ERROR": "failed",
    "FAILED": "failed",
    "FAILURE": "failed",
    "PENDING": "pending",
    "SERVER_ACK": "sent",
    "SERVERACK": "sent",
    "SENT": "sent",
    "DELIVERY_ACK": "delivered",
    "DELIVERYACK": "delivered",
    "DELIVERED": "delivered",
    "READ": "read",
    "READ_ACK": "read",
    "PLAYED": "played",
    "DELETED": "deleted",
}


def normalize_evolution_delivery_status(value: object) -> str | None:
    """Map Evolution/Baileys string and protobuf enum statuses to UI states."""

    if value is None or isinstance(value, bool):
        return None
    raw = str(value).strip().upper().replace("-", "_").replace(" ", "_")
    return _DELIVERY_STATUS_ALIASES.get(raw)


def merge_delivery_status(current: str, incoming: str) -> str:
    """Keep a later successful receipt when provider events arrive out of order."""

    if incoming == "failed":
        return current if current in {"delivered", "read", "played", "deleted"} else incoming
    if current == "failed":
        return incoming
    current_rank = _SUCCESS_ORDER.get(current, -1)
    incoming_rank = _SUCCESS_ORDER.get(incoming, -1)
    return incoming if incoming_rank >= current_rank else current


def record_delivery_receipt(db: Session, external_id: str, status: object) -> bool:
    """Persist every receipt first, then update a matching message when available."""

    normalized = normalize_evolution_delivery_status(status)
    if normalized is None:
        return False
    for attempt in range(2):
        receipt = db.get(MessageDeliveryReceipt, external_id)
        if receipt is None:
            receipt = MessageDeliveryReceipt(
                external_id=external_id,
                delivery_status=normalized,
                received_at=utcnow(),
            )
            db.add(receipt)
        else:
            receipt.delivery_status = merge_delivery_status(
                receipt.delivery_status,
                normalized,
            )
            receipt.received_at = utcnow()

        delivery_attempt = db.scalar(
            select(MessageDeliveryAttempt).where(
                MessageDeliveryAttempt.external_id == external_id
            )
        )
        if delivery_attempt is not None:
            delivery_attempt.delivery_status = merge_delivery_status(
                delivery_attempt.delivery_status,
                receipt.delivery_status,
            )
            delivery_attempt.updated_at = utcnow()

        message = db.scalar(select(Message).where(Message.external_id == external_id))
        if message is None and delivery_attempt is not None:
            message = db.get(Message, delivery_attempt.message_id)
        if message is not None:
            message.delivery_status = merge_delivery_status(
                message.delivery_status,
                receipt.delivery_status,
            )
        try:
            db.commit()
            return True
        except IntegrityError:
            db.rollback()
            if attempt == 1:
                raise
    return False


def record_delivery_attempt(
    db: Session,
    message: Message,
    *,
    provider: str,
    external_id: str | None,
    delivery_status: str,
    error_code: str | None = None,
) -> MessageDeliveryAttempt:
    """Append one provider attempt without exposing provider errors to customers."""

    if external_id:
        existing = db.scalar(
            select(MessageDeliveryAttempt).where(
                MessageDeliveryAttempt.provider == provider,
                MessageDeliveryAttempt.external_id == external_id,
            )
        )
        if existing is not None:
            existing.delivery_status = merge_delivery_status(
                existing.delivery_status,
                delivery_status,
            )
            existing.error_code = error_code
            existing.updated_at = utcnow()
            db.commit()
            return existing

    attempt_number = int(
        db.scalar(
            select(func.max(MessageDeliveryAttempt.attempt_number)).where(
                MessageDeliveryAttempt.message_id == message.id
            )
        )
        or 0
    ) + 1
    attempt = MessageDeliveryAttempt(
        tenant_id=message.tenant_id,
        message_id=message.id,
        channel_account_id=message.channel_account_id,
        provider=provider,
        attempt_number=attempt_number,
        external_id=external_id,
        delivery_status=delivery_status,
        error_code=error_code,
    )
    db.add(attempt)
    db.commit()
    db.refresh(attempt)
    reconcile_delivery_receipts(db, [message])
    return attempt


def reconcile_delivery_receipts(db: Session, messages: Iterable[Message]) -> bool:
    """Apply receipts that reached the webhook before their outbound message was committed."""

    candidates = {
        message.external_id: message
        for message in messages
        if message.external_id
    }
    if not candidates:
        return False
    receipts = db.scalars(
        select(MessageDeliveryReceipt).where(
            MessageDeliveryReceipt.external_id.in_(candidates)
        )
    ).all()
    attempts = {
        attempt.external_id: attempt
        for attempt in db.scalars(
            select(MessageDeliveryAttempt).where(
                MessageDeliveryAttempt.external_id.in_(candidates)
            )
        ).all()
        if attempt.external_id
    }
    changed = False
    for receipt in receipts:
        message = candidates[receipt.external_id]
        merged = merge_delivery_status(message.delivery_status, receipt.delivery_status)
        if merged != message.delivery_status:
            message.delivery_status = merged
            changed = True
        attempt = attempts.get(receipt.external_id)
        if attempt is not None:
            attempt_status = merge_delivery_status(
                attempt.delivery_status,
                receipt.delivery_status,
            )
            if attempt_status != attempt.delivery_status:
                attempt.delivery_status = attempt_status
                attempt.updated_at = utcnow()
                changed = True
    if changed:
        db.commit()
    return changed
