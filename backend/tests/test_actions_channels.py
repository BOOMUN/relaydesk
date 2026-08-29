from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from backend.app.actions import ActionContext, propose_action
from backend.app.channels import ChannelProviderError, SendResult
from backend.app.database import SessionLocal
from backend.app.models import (
    ActionAttempt,
    ActionExecution,
    ChannelAccount,
    ChannelContactIdentity,
    ChannelWebhookEvent,
    Contact,
    Conversation,
    Message,
    MessageDeliveryAttempt,
    Tenant,
    User,
    WhatsAppTemplate,
    WhatsAppTemplateSyncRun,
    utcnow,
)


def _demo_conversation(client, *, phone: str = "+85261000001") -> dict:
    response = client.post(
        "/api/demo/inbound",
        json={
            "phone": phone,
            "display_name": "P1 Action Customer",
            "body": "I need a human agent",
        },
    )
    assert response.status_code == 201
    return response.json()["conversation"]


def test_action_definition_and_idempotent_text_send(authenticated_client):
    conversation = _demo_conversation(authenticated_client)
    definitions = authenticated_client.get("/api/actions/definitions")
    assert definitions.status_code == 200
    text_definition = next(
        item for item in definitions.json() if item["name"] == "whatsapp.text.send"
    )
    assert text_definition["permission_scope"] == "message:send"
    assert text_definition["timeout_seconds"] == 20
    assert text_definition["max_attempts"] == 1
    assert text_definition["input_schema"]["additionalProperties"] is False

    request = {
        "name": "whatsapp.text.send",
        "arguments": {
            "conversation_id": conversation["id"],
            "body": "Idempotent action reply",
            "sender_type": "agent",
        },
    }
    first = authenticated_client.post(
        "/api/actions", json=request, headers={"Idempotency-Key": "p1-text-send-1"}
    )
    second = authenticated_client.post(
        "/api/actions", json=request, headers={"Idempotency-Key": "p1-text-send-1"}
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["status"] == "succeeded"
    with SessionLocal() as db:
        assert db.scalar(
            select(func.count(Message.id)).where(
                Message.conversation_id == conversation["id"],
                Message.body == "Idempotent action reply",
            )
        ) == 1
        message_id = int(first.json()["result_json"]["message_id"])
        message = db.get(Message, message_id)
        assert message is not None
        assert message.metadata_json["action_execution_id"] == first.json()["id"]
        assert db.scalar(
            select(func.count(MessageDeliveryAttempt.id)).where(
                MessageDeliveryAttempt.message_id == message.id
            )
        ) == 1


def test_model_contact_update_waits_for_staff_confirmation(authenticated_client):
    conversation = _demo_conversation(authenticated_client, phone="+85261000002")
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.tenant_id == 1))
        assert user is not None
        execution = propose_action(
            db,
            ActionContext.for_model(user.tenant_id, source_message_id=1),
            "contact.update_profile",
            {
                "contact_id": conversation["contact"]["id"],
                "display_name": "Confirmed Name",
            },
            idempotency_key="model-contact-confirmation-1",
        )
        assert execution.status == "pending_confirmation"
        execution_id = execution.id
        contact = db.get(Contact, conversation["contact"]["id"])
        assert contact is not None and contact.display_name != "Confirmed Name"

    confirmed = authenticated_client.post(f"/api/actions/{execution_id}/confirm")
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "succeeded"
    with SessionLocal() as db:
        contact = db.get(Contact, conversation["contact"]["id"])
        assert contact is not None and contact.display_name == "Confirmed Name"


def test_meta_approved_template_and_interactive_message_apis(
    authenticated_client,
    monkeypatch,
):
    conversation_data = _demo_conversation(authenticated_client, phone="+85261000003")
    sent: list[tuple[str, str]] = []

    def fake_meta_send(self, outbound):
        sent.append((outbound.to, outbound.kind))
        return SendResult(
            provider="meta",
            external_message_id=f"wamid.p1.{len(sent)}",
            status="pending",
        )

    monkeypatch.setattr(
        "backend.app.channels.meta.MetaCloudChannelProvider.send", fake_meta_send
    )
    with SessionLocal() as db:
        conversation = db.get(Conversation, conversation_data["id"])
        assert conversation is not None
        default_account = db.get(ChannelAccount, conversation.channel_account_id)
        assert default_account is not None
        default_account.is_default = False
        meta = ChannelAccount(
            tenant_id=conversation.tenant_id,
            provider="meta",
            name="Meta P1 Test",
            external_account_id="phone-p1",
            phone_number_id="phone-p1",
            business_account_id="business-p1",
            capabilities=["text", "template", "buttons", "list", "webhook"],
            is_default=True,
        )
        db.add(meta)
        db.flush()
        conversation.channel_account_id = meta.id
        identity = ChannelContactIdentity(
            tenant_id=conversation.tenant_id,
            contact_id=conversation.contact_id,
            channel_account_id=meta.id,
            external_user_id=conversation.contact.wa_id,
            address="85261000003",
        )
        approved = WhatsAppTemplate(
            tenant_id=conversation.tenant_id,
            channel_account_id=meta.id,
            name="rental_ready",
            language="zh_HK",
            category="UTILITY",
            status="APPROVED",
            components=[],
        )
        rejected = WhatsAppTemplate(
            tenant_id=conversation.tenant_id,
            channel_account_id=meta.id,
            name="unapproved_offer",
            language="zh_HK",
            category="MARKETING",
            status="REJECTED",
            components=[],
        )
        db.add_all([identity, approved, rejected])
        db.commit()
        approved_id = approved.id
        rejected_id = rejected.id

    rejected_response = authenticated_client.post(
        f"/api/conversations/{conversation_data['id']}/whatsapp/template",
        json={"template_id": rejected_id, "components": []},
    )
    assert rejected_response.status_code == 502
    assert sent == []

    approved_response = authenticated_client.post(
        f"/api/conversations/{conversation_data['id']}/whatsapp/template",
        json={"template_id": approved_id, "components": []},
    )
    assert approved_response.status_code == 200
    assert approved_response.json()["content_type"] == "template"
    assert sent == [("85261000003", "template")]

    interactive = authenticated_client.post(
        f"/api/conversations/{conversation_data['id']}/whatsapp/interactive",
        json={
            "kind": "buttons",
            "body": "Choose one",
            "buttons": [
                {"id": "rent", "title": "Rent"},
                {"id": "support", "title": "Support"},
            ],
        },
    )
    assert interactive.status_code == 200
    assert interactive.json()["content_type"] == "buttons"
    assert sent[-1] == ("85261000003", "buttons")


def test_interactive_message_rejects_closed_service_window(authenticated_client):
    conversation_data = _demo_conversation(authenticated_client, phone="+85261000004")
    with SessionLocal() as db:
        conversation = db.get(Conversation, conversation_data["id"])
        assert conversation is not None
        conversation.service_window_expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    response = authenticated_client.post(
        f"/api/conversations/{conversation_data['id']}/whatsapp/interactive",
        json={
            "kind": "buttons",
            "body": "Expired",
            "buttons": [{"id": "one", "title": "One"}],
        },
    )
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "service_window_closed"


def test_webhook_event_is_durable_and_deduplicated(authenticated_client):
    payload = {
        "event": "messages.upsert",
        "instance": "agentdesk",
        "data": {
            "key": {
                "remoteJid": "85261000005@s.whatsapp.net",
                "fromMe": False,
                "id": "evolution.p1.dedupe.1",
            },
            "pushName": "Webhook P1",
            "message": {"conversation": "I need a human agent"},
            "messageType": "conversation",
        },
    }
    headers = {"X-AgentDesk-Webhook-Secret": "test-evolution-secret"}
    first = authenticated_client.post("/api/webhooks/evolution", json=payload, headers=headers)
    second = authenticated_client.post("/api/webhooks/evolution", json=payload, headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.json()["accepted_events"] == 1
    assert second.json()["accepted_events"] == 0
    with SessionLocal() as db:
        events = db.scalars(
            select(ChannelWebhookEvent).where(
                ChannelWebhookEvent.event_key == "message:evolution.p1.dedupe.1"
            )
        ).all()
        assert len(events) == 1
        assert events[0].status == "processed"
        assert events[0].attempt_count == 1


def test_failed_meta_template_sync_run_is_retained(authenticated_client, monkeypatch):
    with SessionLocal() as db:
        tenant = db.scalar(select(Tenant).where(Tenant.id == 1))
        assert tenant is not None
        account = ChannelAccount(
            tenant_id=tenant.id,
            provider="meta",
            name="Meta Sync Failure",
            external_account_id="sync-phone",
            phone_number_id="sync-phone",
            business_account_id="sync-business",
            capabilities=["template_sync"],
            is_default=False,
        )
        db.add(account)
        db.commit()
        account_id = account.id

    def failed_sync(self):
        raise ChannelProviderError(
            "Meta unavailable during test",
            code="provider_unavailable",
            retryable=True,
        )

    monkeypatch.setattr(
        "backend.app.channels.meta.MetaCloudChannelProvider.sync_templates", failed_sync
    )
    response = authenticated_client.post(
        "/api/whatsapp/templates/sync",
        json={"channel_account_id": account_id},
    )
    assert response.status_code == 502
    with SessionLocal() as db:
        runs = db.scalars(
            select(WhatsAppTemplateSyncRun).where(
                WhatsAppTemplateSyncRun.channel_account_id == account_id
            )
        ).all()
        assert len(runs) == 2
        assert all(run.status == "failed" for run in runs)
        execution = db.scalar(
            select(ActionExecution)
            .where(ActionExecution.action_name == "whatsapp.templates.sync")
            .order_by(ActionExecution.created_at.desc())
        )
        assert execution is not None
        assert execution.status == "failed"
        assert execution.attempt_count == 2
        assert db.scalar(
            select(func.count(ActionAttempt.id)).where(
                ActionAttempt.action_execution_id == execution.id
            )
        ) == 2
