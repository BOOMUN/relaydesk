from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from backend.app.channels import ChannelProviderError, SendResult
from backend.app.channels.demo import DemoChannelProvider
from backend.app.database import SessionLocal
from backend.app.models import (
    Contact,
    Conversation,
    Message,
    MessageDeliveryAttempt,
    MessageDirection,
    MessageSender,
    utcnow,
)
from backend.app.services.conversation_sessions import (
    CONTEXT_CLOSE_DUE_AT_KEY,
    CONTEXT_CLOSE_MESSAGE,
    CONTEXT_CLOSE_STATE_KEY,
    CONTEXT_SESSION_ID_KEY,
    close_due_context_sessions,
)
from backend.app.services.conversations import receive_inbound


def _parsed(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def test_context_session_extends_then_sends_notice_and_solves(
    authenticated_client,
    monkeypatch,
):
    del authenticated_client
    closure_sends: list[str] = []

    original_send = DemoChannelProvider.send

    def fake_close_send(self, outbound):
        if outbound.text == CONTEXT_CLOSE_MESSAGE:
            assert outbound.to
            closure_sends.append(outbound.text)
            return SendResult(
                provider="demo",
                external_message_id="closure-success-1",
                status="sent",
            )
        return original_send(self, outbound)

    monkeypatch.setattr(
        "backend.app.channels.demo.DemoChannelProvider.send",
        fake_close_send,
    )

    with SessionLocal() as db:
        first = receive_inbound(
            db,
            tenant_id=1,
            wa_id="85260000301",
            phone="+85260000301",
            display_name="Context Customer",
            body="你好",
        )
        conversation_id = first.conversation.id
        first_inbound = db.scalar(
            select(Message).where(
                Message.conversation_id == conversation_id,
                Message.direction == MessageDirection.INBOUND.value,
            )
        )
        assert first_inbound is not None
        first_metadata = dict(first_inbound.metadata_json)
        first_session_id = first_metadata[CONTEXT_SESSION_ID_KEY]

        receive_inbound(
            db,
            tenant_id=1,
            wa_id="85260000301",
            phone="+85260000301",
            display_name="Context Customer",
            body="你好",
        )
        inbound_messages = db.scalars(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.direction == MessageDirection.INBOUND.value,
            )
            .order_by(Message.id)
        ).all()
        assert len(inbound_messages) == 2
        assert inbound_messages[0].metadata_json[CONTEXT_CLOSE_STATE_KEY] == "continued"
        assert all(
            item.metadata_json[CONTEXT_SESSION_ID_KEY] == first_session_id
            for item in inbound_messages
        )
        latest_due = _parsed(
            inbound_messages[-1].metadata_json[CONTEXT_CLOSE_DUE_AT_KEY]
        )

        not_due = close_due_context_sessions(
            db,
            now=latest_due - timedelta(microseconds=1),
        )
        assert not_due.closed == 0

        closed = close_due_context_sessions(
            db,
            now=latest_due + timedelta(seconds=1),
        )
        assert closed.closed == 1
        assert closed.failed == 0
        conversation = db.get(Conversation, conversation_id)
        assert conversation is not None
        assert conversation.status == "solved"
        latest = db.scalar(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.id.desc())
            .limit(1)
        )
        assert latest is not None
        assert latest.sender_type == MessageSender.AI.value
        assert latest.body == CONTEXT_CLOSE_MESSAGE
        assert latest.metadata_json["route"] == "session_close"
        assert latest.metadata_json["context_closed"] is True
        assert latest.metadata_json[CONTEXT_SESSION_ID_KEY] == first_session_id
        assert closure_sends == [CONTEXT_CLOSE_MESSAGE]

        reopened = receive_inbound(
            db,
            tenant_id=1,
            wa_id="85260000301",
            phone="+85260000301",
            display_name="Context Customer",
            body="你好",
        )
        assert reopened.conversation.status == "open"
        newest_inbound = db.scalar(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.direction == MessageDirection.INBOUND.value,
            )
            .order_by(Message.id.desc())
            .limit(1)
        )
        assert newest_inbound is not None
        assert newest_inbound.metadata_json[CONTEXT_SESSION_ID_KEY] != first_session_id


def test_close_notice_failure_retries_same_message_before_solving(
    authenticated_client,
    monkeypatch,
):
    del authenticated_client
    attempts = 0

    original_send = DemoChannelProvider.send

    def flaky_send(self, outbound):
        nonlocal attempts
        if outbound.text != CONTEXT_CLOSE_MESSAGE:
            return original_send(self, outbound)
        assert outbound.to
        attempts += 1
        if attempts == 1:
            raise ChannelProviderError(
                "temporary provider failure",
                code="provider_unavailable",
                retryable=False,
            )
        return SendResult(
            provider="demo",
            external_message_id="closure-retry-success",
            status="sent",
        )

    monkeypatch.setattr(
        "backend.app.channels.demo.DemoChannelProvider.send",
        flaky_send,
    )

    with SessionLocal() as db:
        inbound_result = receive_inbound(
            db,
            tenant_id=1,
            wa_id="85260000302",
            phone="+85260000302",
            display_name="Retry Customer",
            body="你好",
        )
        conversation_id = inbound_result.conversation.id
        inbound = db.scalar(
            select(Message).where(
                Message.conversation_id == conversation_id,
                Message.direction == MessageDirection.INBOUND.value,
            )
        )
        assert inbound is not None
        due_at = _parsed(inbound.metadata_json[CONTEXT_CLOSE_DUE_AT_KEY])

        failed = close_due_context_sessions(db, now=due_at + timedelta(seconds=1))
        assert failed.failed == 1
        assert db.get(Conversation, conversation_id).status == "open"
        db.refresh(inbound)
        assert inbound.metadata_json[CONTEXT_CLOSE_STATE_KEY] == "retry"
        retry_at = _parsed(inbound.metadata_json[CONTEXT_CLOSE_DUE_AT_KEY])

        succeeded = close_due_context_sessions(db, now=retry_at + timedelta(seconds=1))
        assert succeeded.closed == 1
        assert db.get(Conversation, conversation_id).status == "solved"
        close_messages = [
            item
            for item in db.scalars(
                select(Message).where(Message.conversation_id == conversation_id)
            ).all()
            if (item.metadata_json or {}).get("route") == "session_close"
        ]
        assert len(close_messages) == 1
        assert close_messages[0].delivery_status == "sent"
        delivery_attempts = db.scalars(
            select(MessageDeliveryAttempt)
            .where(MessageDeliveryAttempt.message_id == close_messages[0].id)
            .order_by(MessageDeliveryAttempt.attempt_number)
        ).all()
        assert [item.delivery_status for item in delivery_attempts] == ["failed", "sent"]


def test_handoff_pauses_context_without_auto_solving(
    authenticated_client,
):
    del authenticated_client
    with SessionLocal() as db:
        inbound_result = receive_inbound(
            db,
            tenant_id=1,
            wa_id="85260000303",
            phone="+85260000303",
            display_name="Handoff Customer",
            body="請轉人工客服",
        )
        conversation_id = inbound_result.conversation.id
        inbound = db.scalar(
            select(Message).where(
                Message.conversation_id == conversation_id,
                Message.direction == MessageDirection.INBOUND.value,
            )
        )
        assert inbound is not None
        assert inbound.metadata_json[CONTEXT_CLOSE_STATE_KEY] == "paused"
        assert inbound.metadata_json["ai_context_pause_reason"] == "handoff"
        result = close_due_context_sessions(db, now=utcnow() + timedelta(days=2))
        assert result.closed == 0
        conversation = db.get(Conversation, conversation_id)
        assert conversation is not None
        assert conversation.status == "pending"
        assert conversation.ai_enabled is False


def test_local_test_context_never_calls_whatsapp_provider(
    authenticated_client,
    monkeypatch,
):
    del authenticated_client

    original_send = DemoChannelProvider.send

    def unexpected_send(self, outbound):
        if outbound.text == CONTEXT_CLOSE_MESSAGE:
            raise AssertionError(
                f"local test attempted provider send: {outbound.to} {outbound.text}"
            )
        return original_send(self, outbound)

    monkeypatch.setattr(
        "backend.app.channels.demo.DemoChannelProvider.send",
        unexpected_send,
    )

    with SessionLocal() as db:
        inbound_result = receive_inbound(
            db,
            tenant_id=1,
            wa_id="99900000305",
            phone="+99900000305",
            display_name="Local QA Conversation",
            body="local test",
        )
        conversation_id = inbound_result.conversation.id
        inbound = db.scalar(
            select(Message).where(
                Message.conversation_id == conversation_id,
                Message.direction == MessageDirection.INBOUND.value,
            )
        )
        assert inbound is not None
        metadata = dict(inbound.metadata_json)
        metadata["local_test"] = True
        metadata[CONTEXT_CLOSE_STATE_KEY] = "waiting"
        metadata[CONTEXT_CLOSE_DUE_AT_KEY] = utcnow().isoformat()
        inbound.metadata_json = metadata
        inbound_result.conversation.status = "open"
        inbound_result.conversation.ai_enabled = True
        db.commit()

        result = close_due_context_sessions(db, now=utcnow() + timedelta(days=2))

        assert result.checked == 0
        assert result.closed == 0
        assert result.failed == 0
        conversation = db.get(Conversation, conversation_id)
        assert conversation is not None
        assert conversation.status == "open"
        db.refresh(inbound)
        assert inbound.metadata_json[CONTEXT_CLOSE_STATE_KEY] == "waiting"
        close_messages = [
            item
            for item in db.scalars(
                select(Message).where(Message.conversation_id == conversation_id)
            ).all()
            if (item.metadata_json or {}).get("route") == "session_close"
        ]
        assert close_messages == []


def test_legacy_untracked_conversation_is_not_closed(
    authenticated_client,
):
    del authenticated_client
    with SessionLocal() as db:
        contact = Contact(
            tenant_id=1,
            wa_id="85260000304",
            phone="+85260000304",
            display_name="Legacy Customer",
        )
        db.add(contact)
        db.flush()
        conversation = Conversation(
            tenant_id=1,
            contact_id=contact.id,
            subject="Legacy conversation",
            status="open",
            ai_enabled=True,
        )
        db.add(conversation)
        db.flush()
        db.add(
            Message(
                tenant_id=1,
                conversation_id=conversation.id,
                direction=MessageDirection.INBOUND.value,
                sender_type=MessageSender.CUSTOMER.value,
                body="legacy",
                metadata_json={},
                created_at=utcnow() - timedelta(days=2),
            )
        )
        db.commit()
        result = close_due_context_sessions(db, now=utcnow() + timedelta(days=2))
        assert result.checked == 0
        assert db.get(Conversation, conversation.id).status == "open"
