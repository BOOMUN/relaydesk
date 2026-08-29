from __future__ import annotations

from sqlalchemy.orm import Session

from ..actions import ActionContext, propose_action
from ..channels import get_channel_provider
from ..models import (
    ActionStatus,
    Conversation,
    Message,
    MessageDirection,
    MessageSender,
    utcnow,
)
from .delivery import record_delivery_attempt


def _record_failed_outbound(
    db: Session,
    conversation: Conversation,
    *,
    body: str,
    sender_type: str,
    sender_name: str,
    metadata: dict,
    action_execution,
    existing_message: Message | None = None,
) -> Message:
    provider = get_channel_provider(
        db,
        conversation.tenant_id,
        conversation.channel_account_id,
    )
    now = utcnow()
    failure_metadata = {
        **metadata,
        "action_execution_id": action_execution.id,
        "action_error_code": action_execution.error_code,
        "action_failure_reason": action_execution.failure_reason,
    }
    if existing_message is None:
        message = Message(
            tenant_id=conversation.tenant_id,
            conversation_id=conversation.id,
            channel_account_id=provider.account.id,
            provider=provider.provider_name,
            direction=MessageDirection.OUTBOUND.value,
            sender_type=sender_type,
            sender_name=sender_name,
            body=body,
            delivery_status="failed",
            metadata_json=failure_metadata,
            created_at=now,
        )
        db.add(message)
        db.flush()
    else:
        message = existing_message
        message.channel_account_id = provider.account.id
        message.provider = provider.provider_name
        message.delivery_status = "failed"
        message.metadata_json = {
            **dict(message.metadata_json or {}),
            **failure_metadata,
        }
    conversation.channel_account_id = provider.account.id
    conversation.last_message_at = now
    conversation.updated_at = now
    if sender_type == MessageSender.AGENT.value:
        conversation.unread_count = 0
    db.commit()
    db.refresh(message)
    record_delivery_attempt(
        db,
        message,
        provider=provider.provider_name,
        external_id=None,
        delivery_status="failed",
        error_code=action_execution.error_code or "action_failed",
    )
    return message


def send_text_action(
    db: Session,
    conversation: Conversation,
    *,
    context: ActionContext,
    body: str,
    sender_type: str,
    sender_name: str,
    metadata: dict | None = None,
    idempotency_key: str | None = None,
    existing_message: Message | None = None,
) -> Message:
    """Execute the sole text-send action and retain a failed chat bubble."""

    execution = propose_action(
        db,
        context,
        "whatsapp.text.send",
        {
            "conversation_id": conversation.id,
            "existing_message_id": existing_message.id if existing_message else None,
            "body": body,
            "sender_type": sender_type,
            "sender_name": sender_name,
            "metadata": dict(metadata or {}),
        },
        idempotency_key=idempotency_key,
    )
    if execution.status == ActionStatus.SUCCEEDED.value:
        message = db.get(Message, execution.result_json.get("message_id"))
        if message is None:
            raise RuntimeError("Send action succeeded without a message record")
        return message
    return _record_failed_outbound(
        db,
        conversation,
        body=body,
        sender_type=sender_type,
        sender_name=sender_name,
        metadata=dict(metadata or {}),
        action_execution=execution,
        existing_message=existing_message,
    )


__all__ = ["send_text_action"]
