from __future__ import annotations

from decimal import Decimal
import hashlib
import hmac
import json

from sqlalchemy import func, select

from backend.app.actions.definitions import ACTION_REGISTRY
from backend.app.actions.handlers import HANDLERS
from backend.app.channels import SendResult
from backend.app.config import settings
from backend.app.database import SessionLocal
from backend.app.models import (
    ActionAttempt,
    ActionExecution,
    AuditLog,
    AutomationFormEvent,
    AutomationFormSession,
    ChannelAccount,
    ChannelWebhookEvent,
    KnowledgeDocument,
    KnowledgeSource,
    KnowledgeWebPage,
    Message,
    MessageDirection,
    ProductPriceSource,
    TeamMember,
    WhatsAppTemplate,
)
from backend.app.services.conversation_sessions import (
    CONTEXT_CLOSE_STATE_KEY,
    CONTEXT_SESSION_ID_KEY,
)
from backend.app.services.product_price_ingestion import (
    ScrapedOffer,
    ScrapedProduct,
    persist_product_catalog,
)
from backend.app.services.knowledge import retrieve_knowledge
from backend.app.services.web_crawler import CrawledPage


def _inbound(client, phone: str, body: str) -> dict:
    response = client.post(
        "/api/demo/inbound",
        json={"phone": phone, "display_name": "Acceptance customer", "body": body},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _publish_agent(client) -> None:
    assert client.get("/api/ai-agent").status_code == 200
    response = client.post("/api/ai-agent/publish")
    assert response.status_code == 200, response.text


def _latest_ai(payload: dict) -> dict:
    return next(
        message
        for message in reversed(payload["conversation"]["messages"])
        if message["sender_type"] == "ai"
    )


def test_website_input_creates_reviewable_knowledge_and_agent_drafts(
    authenticated_client,
    monkeypatch,
):
    class FakeCrawler:
        def __init__(self, root_url: str, *, max_pages: int, max_depth: int) -> None:
            self.root_url = root_url
            self.max_pages = max_pages
            self.max_depth = max_depth
            self.discovered_count = 2
            self.failed_count = 0
            self.errors: list[str] = []

        def crawl(self):
            yield CrawledPage(
                url="https://example.com/",
                title="Example Travel WiFi",
                content="# Travel WiFi\nJapan and Korea WiFi rental support and pickup guidance.",
                content_type="html",
                language="en",
                metadata={"section_path": "Travel WiFi"},
            )
            yield CrawledPage(
                url="https://example.com/help",
                title="Pickup Help",
                content="# Pickup\nAirport pickup instructions and return counter opening details.",
                content_type="html",
                language="en",
                metadata={"section_path": "Help > Pickup"},
            )

    monkeypatch.setattr(
        "backend.app.services.agent_profiles.WebsiteCrawler",
        FakeCrawler,
    )

    generated = authenticated_client.post(
        "/api/ai-agent/generate",
        json={"source_url": "https://example.com"},
    )
    assert generated.status_code == 200, generated.text
    agent_draft = generated.json()
    assert agent_draft["status"] == "draft"
    assert agent_draft["source_url"] == "https://example.com/"
    assert "2 个待审核页面草稿" in agent_draft["generation_summary"]
    assert "代理指令" in agent_draft["generation_summary"]

    sources = authenticated_client.get("/api/knowledge/sources")
    assert sources.status_code == 200
    source = sources.json()[0]
    assert source["root_url"] == "https://example.com/"
    assert source["status"] == "completed"
    assert source["draft_pages"] == 2
    assert source["published_pages"] == 0

    documents = authenticated_client.get("/api/knowledge").json()
    website_documents = [item for item in documents if item["source_id"] == source["id"]]
    assert len(website_documents) == 2
    assert all(item["review_status"] == "draft" for item in website_documents)
    assert all(item["is_active"] is False for item in website_documents)

    with SessionLocal() as db:
        stored_source = db.get(KnowledgeSource, source["id"])
        assert stored_source is not None
        assert db.scalar(
            select(func.count(KnowledgeWebPage.id)).where(
                KnowledgeWebPage.source_id == stored_source.id,
                KnowledgeWebPage.review_status == "draft",
            )
        ) == 2
        actions = set(
            db.scalars(
                select(AuditLog.action).where(
                    AuditLog.action.in_(
                        {
                            "knowledge.source_created",
                            "knowledge.website_drafts_generated",
                            "agent_profile.draft_generated",
                        }
                    )
                )
            ).all()
        )
        assert actions == {
            "knowledge.source_created",
            "knowledge.website_drafts_generated",
            "agent_profile.draft_generated",
        }


def test_current_message_language_controls_each_reply(authenticated_client):
    english = _inbound(authenticated_client, "+85262000001", "Hello, can you help me?")
    english_reply = _latest_ai(english)
    assert english_reply["metadata_json"]["language"] == "en"
    assert "Hello" in english_reply["body"]

    simplified = _inbound(authenticated_client, "+85262000001", "你好，请问可以帮我吗？")
    simplified_reply = _latest_ai(simplified)
    assert simplified_reply["metadata_json"]["language"] == "zh-CN"
    assert "协助" in simplified_reply["body"]

    traditional = _inbound(authenticated_client, "+85262000001", "你好，請問可以幫我嗎？")
    traditional_reply = _latest_ai(traditional)
    assert traditional_reply["metadata_json"]["language"] == "zh-TW"
    assert "協助" in traditional_reply["body"]


def test_reliable_answer_has_source_and_unknown_answer_does_not_invent(
    authenticated_client,
):
    created = authenticated_client.post(
        "/api/knowledge",
        json={
            "title": "Airport pickup policy",
            "category": "policy",
            "source": "https://docs.example.com/airport-pickup",
            "content": "Narita Airport WiFi pickup counter closes at 8 PM every day.",
        },
    )
    assert created.status_code == 201, created.text
    with SessionLocal() as db:
        direct_matches = retrieve_knowledge(
            db,
            1,
            "What time does the Narita Airport WiFi pickup counter close?",
        )
        assert direct_matches, "exact English evidence did not clear retrieval gates"

    supported = _inbound(
        authenticated_client,
        "+85262000002",
        "What time does the Narita Airport WiFi pickup counter close?",
    )
    supported_reply = _latest_ai(supported)
    assert supported["agent_route"] == "knowledge"
    assert "8 PM" in supported_reply["body"], json.dumps(
        supported_reply["metadata_json"], ensure_ascii=False
    )
    assert "Sources\n[1]" in supported_reply["body"]
    assert "https://docs.example.com/airport-pickup" in supported_reply["body"]
    assert supported_reply["metadata_json"]["sources"]

    unsupported = _inbound(
        authenticated_client,
        "+85262000003",
        "What is the AGENTDESK-UNLISTED-ORBITAL warranty code?",
    )
    unsupported_reply = _latest_ai(unsupported)
    assert unsupported["agent_route"] == "handoff"
    assert unsupported["conversation"]["ai_enabled"] is False
    assert unsupported_reply["metadata_json"]["sources"] == []
    assert "warranty code is" not in unsupported_reply["body"].casefold()


def test_product_price_ignores_conflicting_knowledge_and_uses_catalog(
    authenticated_client,
):
    misleading = authenticated_client.post(
        "/api/knowledge",
        json={
            "title": "Untrusted old Japan WiFi price",
            "category": "product",
            "source": "https://docs.example.com/old-price",
            "content": "The old Japan 5G WiFi page says the price is HK$999 per day.",
        },
    )
    assert misleading.status_code == 201

    with SessionLocal() as db:
        source = ProductPriceSource(
            tenant_id=1,
            created_by_user_id=1,
            name="Acceptance structured catalogue",
            root_url="https://catalog.example.com/",
            domain="catalog.example.com",
            adapter="test",
            status="completed",
        )
        db.add(source)
        db.flush()
        persist_product_catalog(
            db,
            source,
            [
                ScrapedProduct(
                    external_key="japan-5g",
                    canonical_url="https://catalog.example.com/japan-5g",
                    name="Japan 5G WiFi",
                    name_translations={"en": "Japan 5G WiFi", "zh-CN": "日本 5G WiFi"},
                    aliases=["Japan WiFi", "日本 WiFi"],
                    category="wifi_5g",
                    product_type="wifi_rental",
                    destination="日本",
                    network="5G",
                    description="Structured catalogue item",
                    metadata={},
                    offers=[
                        ScrapedOffer(
                            external_key="daily",
                            label="Daily rental",
                            currency="HKD",
                            price_amount=Decimal("48"),
                            original_amount=None,
                            unit="day",
                            duration_days=None,
                            data_label="Unlimited",
                        )
                    ],
                )
            ],
        )
        db.commit()

    quoted = _inbound(
        authenticated_client,
        "+85262000004",
        "How much is the Japan 5G WiFi for one day?",
    )
    reply = _latest_ai(quoted)
    assert quoted["agent_route"] == "pricing"
    assert "HK$48" in reply["body"]
    assert "HK$999" not in reply["body"]
    assert reply["metadata_json"]["sources"][0]["source_type"] == "structured_product"
    assert reply["metadata_json"]["sources"][0]["source"] == (
        "https://catalog.example.com/"
    )


def test_all_registered_actions_share_permissions_idempotency_and_audit(
    authenticated_client,
):
    assert ACTION_REGISTRY
    for name, definition in ACTION_REGISTRY.items():
        assert definition.name == name
        assert definition.permission_scope
        assert definition.allowed_callers
        assert definition.allowed_roles
        assert definition.risk_level in {"low", "medium", "high"}
        assert definition.timeout_seconds > 0
        assert definition.max_attempts > 0
        assert definition.handler_name in HANDLERS
        assert definition.input_schema.get("additionalProperties") is False

    conversation = _inbound(authenticated_client, "+85262000005", "Hello")[
        "conversation"
    ]
    request = {
        "name": "conversation.update",
        "arguments": {"conversation_id": conversation["id"], "priority": "urgent"},
    }
    first = authenticated_client.post(
        "/api/actions",
        json=request,
        headers={"Idempotency-Key": "acceptance-action-1"},
    )
    second = authenticated_client.post(
        "/api/actions",
        json=request,
        headers={"Idempotency-Key": "acceptance-action-1"},
    )
    assert first.status_code == second.status_code == 200
    execution = first.json()
    assert execution["id"] == second.json()["id"]
    assert execution["status"] == "succeeded"
    assert execution["permission_scope"] == "conversation:update"
    assert execution["idempotency_key"] == "acceptance-action-1"

    failed = authenticated_client.post(
        "/api/actions",
        json={
            "name": "conversation.update",
            "arguments": {"conversation_id": 999999, "priority": "high"},
        },
        headers={"Idempotency-Key": "acceptance-action-failure-1"},
    )
    assert failed.status_code == 200
    assert failed.json()["status"] == "failed"
    assert failed.json()["failure_reason"]

    with SessionLocal() as db:
        execution_row = db.get(ActionExecution, execution["id"])
        assert execution_row is not None
        assert db.scalar(
            select(func.count(ActionAttempt.id)).where(
                ActionAttempt.action_execution_id == execution_row.id
            )
        ) == 1
        audit_actions = set(
            db.scalars(
                select(AuditLog.action).where(
                    AuditLog.entity_type == "action_execution",
                    AuditLog.entity_id.in_({execution_row.id, failed.json()["id"]}),
                )
            ).all()
        )
        assert {"action.requested", "action.succeeded", "action.failed"} <= audit_actions


def test_agentdesk_owns_contact_team_agent_tag_and_conversation_state(
    authenticated_client,
):
    created = _inbound(authenticated_client, "+85262000006", "Hello")
    conversation = created["conversation"]
    contact_id = conversation["contact"]["id"]
    with SessionLocal() as db:
        membership = db.scalar(select(TeamMember).order_by(TeamMember.id))
        assert membership is not None
        team_id = membership.team_id
        user_id = membership.user_id

    contact = authenticated_client.patch(
        f"/api/contacts/{contact_id}",
        json={
            "display_name": "AgentDesk-owned contact",
            "tags": ["vip", "wifi-lead"],
            "custom_attributes": {"travel_month": "2026-10"},
        },
    )
    assert contact.status_code == 200, contact.text
    assert contact.json()["display_name"] == "AgentDesk-owned contact"
    assert set(contact.json()["tags"]) == {"vip", "wifi-lead"}
    assert contact.json()["custom_attributes"] == {"travel_month": "2026-10"}

    updated = authenticated_client.patch(
        f"/api/conversations/{conversation['id']}",
        json={
            "priority": "high",
            "assigned_team_id": team_id,
            "assigned_user_id": user_id,
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["priority"] == "high"
    assert updated.json()["assigned_team_id"] == team_id
    assert updated.json()["assigned_user_id"] == user_id
    assert authenticated_client.get("/api/contacts").status_code == 200
    assert authenticated_client.get("/api/teams").status_code == 200
    assert authenticated_client.get("/api/agents").status_code == 200


def test_meta_signed_webhook_deduplicates_and_supports_templates_and_buttons(
    authenticated_client,
    monkeypatch,
):
    secret = "acceptance-meta-app-secret"
    monkeypatch.setattr(settings, "meta_app_secret", secret)
    sent_kinds: list[str] = []

    def fake_send(self, outbound):
        sent_kinds.append(outbound.kind)
        return SendResult(
            provider="meta",
            external_message_id=f"wamid.acceptance.{len(sent_kinds)}",
            status="pending",
        )

    def fake_templates(self):
        return [
            {
                "id": "template-acceptance-1",
                "name": "rental_ready",
                "language": "en_US",
                "status": "APPROVED",
                "category": "UTILITY",
                "components": [],
            }
        ]

    monkeypatch.setattr(
        "backend.app.channels.meta.MetaCloudChannelProvider.send",
        fake_send,
    )
    monkeypatch.setattr(
        "backend.app.channels.meta.MetaCloudChannelProvider.sync_templates",
        fake_templates,
    )

    with SessionLocal() as db:
        account = ChannelAccount(
            tenant_id=1,
            provider="meta",
            name="Meta acceptance",
            external_account_id="meta-phone-acceptance",
            phone_number_id="meta-phone-acceptance",
            business_account_id="meta-business-acceptance",
            capabilities=["text", "template", "template_sync", "buttons", "list", "webhook"],
            is_default=False,
            is_active=True,
        )
        db.add(account)
        db.commit()
        account_id = account.id

    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "meta-business-acceptance",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": "meta-phone-acceptance"},
                            "contacts": [
                                {
                                    "wa_id": "85262000007",
                                    "profile": {"name": "Meta acceptance customer"},
                                }
                            ],
                            "messages": [
                                {
                                    "from": "85262000007",
                                    "id": "wamid.acceptance.inbound.1",
                                    "timestamp": "1787900000",
                                    "type": "text",
                                    "text": {"body": "Please connect me to a human agent"},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": f"sha256={signature}",
    }
    invalid = authenticated_client.post(
        "/api/webhooks/whatsapp",
        content=raw,
        headers={**headers, "X-Hub-Signature-256": "sha256=invalid"},
    )
    assert invalid.status_code == 401

    first = authenticated_client.post(
        "/api/webhooks/whatsapp", content=raw, headers=headers
    )
    duplicate = authenticated_client.post(
        "/api/webhooks/whatsapp", content=raw, headers=headers
    )
    assert first.status_code == duplicate.status_code == 200
    assert first.json()["accepted_events"] == 1
    assert duplicate.json()["accepted_events"] == 0

    with SessionLocal() as db:
        event = db.scalar(
            select(ChannelWebhookEvent).where(
                ChannelWebhookEvent.event_key == "message:wamid.acceptance.inbound.1"
            )
        )
        assert event is not None
        assert event.status == "processed"
        assert event.attempt_count == 1
        inbound = db.scalar(
            select(Message).where(
                Message.external_id == "wamid.acceptance.inbound.1",
                Message.direction == MessageDirection.INBOUND.value,
            )
        )
        assert inbound is not None
        conversation_id = inbound.conversation_id

    synced = authenticated_client.post(
        "/api/whatsapp/templates/sync",
        json={"channel_account_id": account_id},
    )
    assert synced.status_code == 200, synced.text
    assert synced.json()["status"] == "completed"
    assert synced.json()["approved_count"] == 1
    with SessionLocal() as db:
        template = db.scalar(
            select(WhatsAppTemplate).where(
                WhatsAppTemplate.channel_account_id == account_id,
                WhatsAppTemplate.name == "rental_ready",
            )
        )
        assert template is not None and template.status == "APPROVED"
        template_id = template.id

    template_send = authenticated_client.post(
        f"/api/conversations/{conversation_id}/whatsapp/template",
        json={"template_id": template_id, "components": []},
    )
    assert template_send.status_code == 200, template_send.text
    buttons = authenticated_client.post(
        f"/api/conversations/{conversation_id}/whatsapp/interactive",
        json={
            "kind": "buttons",
            "body": "Choose support type",
            "buttons": [
                {"id": "rental", "title": "Rental"},
                {"id": "support", "title": "Support"},
            ],
        },
    )
    assert buttons.status_code == 200, buttons.text
    assert {"text", "template", "buttons"} <= set(sent_kinds)


def test_handoff_stops_ai_and_resume_preserves_context_and_form_state(
    authenticated_client,
):
    _publish_agent(authenticated_client)
    phone = "+85262000008"
    _inbound(authenticated_client, phone, "I need to track my order")
    answered = _inbound(authenticated_client, phone, "AB-12345")
    answered_inbound = next(
        message
        for message in answered["conversation"]["messages"]
        if message["direction"] == "inbound" and message["body"] == "AB-12345"
    )
    original_context_id = answered_inbound["metadata_json"][CONTEXT_SESSION_ID_KEY]

    handed_off = _inbound(authenticated_client, phone, "I need a human agent")
    conversation_id = handed_off["conversation"]["id"]
    assert handed_off["agent_route"] == "handoff"
    assert handed_off["conversation"]["ai_enabled"] is False
    with SessionLocal() as db:
        session = db.scalar(
            select(AutomationFormSession).where(
                AutomationFormSession.conversation_id == conversation_id
            )
        )
        assert session is not None
        assert session.status == "handed_off"
        assert session.answers_json["order_number"] == "AB-12345"
        tracked = db.scalar(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.direction == MessageDirection.INBOUND.value,
                Message.body == "I need a human agent",
            )
            .order_by(Message.id.desc())
        )
        assert tracked is not None
        assert tracked.metadata_json[CONTEXT_CLOSE_STATE_KEY] == "paused"

    message_count = len(handed_off["conversation"]["messages"])
    staff_owned = _inbound(
        authenticated_client,
        phone,
        "Additional details while the human agent owns the conversation",
    )
    assert staff_owned["agent_answered"] is False
    assert len(staff_owned["conversation"]["messages"]) == message_count + 1

    resumed = authenticated_client.patch(
        f"/api/conversations/{conversation_id}",
        json={"ai_enabled": True},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["ai_enabled"] is True

    continued = _inbound(authenticated_client, phone, "2026-09-15")
    continued_reply = _latest_ai(continued)
    assert "destination" in continued_reply["body"].casefold()
    continued_inbound = next(
        message
        for message in continued["conversation"]["messages"]
        if message["direction"] == "inbound" and message["body"] == "2026-09-15"
    )
    assert continued_inbound["metadata_json"][CONTEXT_SESSION_ID_KEY] == original_context_id

    with SessionLocal() as db:
        session = db.scalar(
            select(AutomationFormSession).where(
                AutomationFormSession.conversation_id == conversation_id
            )
        )
        assert session is not None
        assert session.status == "active"
        assert session.answers_json == {
            "order_number": "AB-12345",
            "departure_date": "2026-09-15",
        }
        assert db.scalar(
            select(func.count(AutomationFormEvent.id)).where(
                AutomationFormEvent.session_id == session.id,
                AutomationFormEvent.event_type == "resumed_after_handoff",
            )
        ) == 1
