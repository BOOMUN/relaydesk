from __future__ import annotations

import asyncio
import json
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select

from ..database import SessionLocal
from ..dependencies import get_current_user
from ..models import (
    Contact,
    Conversation,
    Message,
    MessageDeliveryAttempt,
    MessageDeliveryReceipt,
    QuickReply,
)
from ..services.realtime import InboxSignal, subscribe_inbox, unsubscribe_inbox


router = APIRouter(prefix="/api", tags=["events"])


def _timestamp(value: datetime | None) -> str:
    return value.isoformat() if value else ""


def _inbox_state(
    tenant_id: int,
) -> tuple[tuple[str, str, str, str, str], tuple[int, ...]]:
    with SessionLocal() as db:
        conversation_at = db.scalar(
            select(func.max(Conversation.updated_at)).where(Conversation.tenant_id == tenant_id)
        )
        message_at = db.scalar(
            select(func.max(Message.created_at)).where(Message.tenant_id == tenant_id)
        )
        contact_at = db.scalar(
            select(func.max(Contact.updated_at)).where(Contact.tenant_id == tenant_id)
        )
        quick_reply_at = db.scalar(
            select(func.max(QuickReply.updated_at)).where(QuickReply.tenant_id == tenant_id)
        )
        direct_delivery_at = db.scalar(
            select(func.max(MessageDeliveryReceipt.received_at))
            .join(Message, Message.external_id == MessageDeliveryReceipt.external_id)
            .where(Message.tenant_id == tenant_id)
        )
        attempt_delivery_at = db.scalar(
            select(func.max(MessageDeliveryReceipt.received_at))
            .join(
                MessageDeliveryAttempt,
                MessageDeliveryAttempt.external_id == MessageDeliveryReceipt.external_id,
            )
            .where(MessageDeliveryAttempt.tenant_id == tenant_id)
        )
        delivery_version = max(
            _timestamp(direct_delivery_at),
            _timestamp(attempt_delivery_at),
        )
        handoff_ids = tuple(
            db.scalars(
                select(Conversation.id)
                .where(
                    Conversation.tenant_id == tenant_id,
                    Conversation.status == "pending",
                    Conversation.ai_route == "handoff",
                    Conversation.ai_enabled.is_(False),
                )
                .order_by(Conversation.updated_at.desc())
            ).all()
        )
    return (
        (
            _timestamp(conversation_at),
            _timestamp(message_at),
            _timestamp(contact_at),
            _timestamp(quick_reply_at),
            delivery_version,
        ),
        handoff_ids,
    )


@router.get("/events")
async def events(request: Request, user: User = Depends(get_current_user)):
    tenant_id = user.tenant_id

    async def stream():
        subscription = subscribe_inbox(tenant_id)
        try:
            state = await asyncio.to_thread(_inbox_state, tenant_id)
            version, handoff_ids = state
            initial_payload = json.dumps(
                {
                    "type": "inbox.updated",
                    "activity": "initial",
                    "version": version,
                    "handoff_ids": handoff_ids,
                }
            )
            yield f"data: {initial_payload}\n\n"

            heartbeat = 0
            while not await request.is_disconnected():
                signal: InboxSignal | None
                try:
                    signal = await asyncio.wait_for(subscription.queue.get(), timeout=1)
                except TimeoutError:
                    signal = None

                current = await asyncio.to_thread(_inbox_state, tenant_id)
                if signal is not None or current != state:
                    state = current
                    version, handoff_ids = current
                    payload = json.dumps(
                        {
                            "type": "inbox.updated",
                            "activity": signal.activity if signal else "state",
                            "conversation_id": signal.conversation_id if signal else None,
                            "sender_type": signal.sender_type if signal else None,
                            "version": version,
                            "handoff_ids": handoff_ids,
                        }
                    )
                    yield f"data: {payload}\n\n"
                    heartbeat = 0
                elif heartbeat >= 7:
                    yield ": keep-alive\n\n"
                    heartbeat = 0
                heartbeat += 1
        finally:
            unsubscribe_inbox(subscription)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
