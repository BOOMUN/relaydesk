from __future__ import annotations

from fastapi.testclient import TestClient

from backend.app.services.agent import (
    VIP_PLAN_ANSWER_ZH,
    VIP_PLAN_SOURCE,
    _detect_language,
    format_ai_customer_message,
)


def test_authentication_and_dashboard(client: TestClient):
    assert client.get("/api/dashboard").status_code == 401

    login = client.post(
        "/api/auth/login",
        json={"email": "admin@test.local", "password": "TestPassword123!"},
    )
    assert login.status_code == 200
    assert login.json()["integration"] == {
        "openai": False,
        "whatsapp": False,
        "whatsapp_provider": "demo",
        "mode": "demo",
    }
    dashboard = client.get("/api/dashboard")
    assert dashboard.status_code == 200
    assert dashboard.json()["metrics"][0]["label"] == "待处理会话"


def test_knowledge_rag_route(authenticated_client: TestClient):
    created = authenticated_client.post(
        "/api/knowledge",
        json={
            "title": "退货政策",
            "category": "policy",
            "source": "test://returns",
            "content": "客户签收商品后七天内，在商品完整的情况下可以申请退货。",
        },
    )
    assert created.status_code == 201

    response = authenticated_client.post(
        "/api/demo/inbound",
        json={
            "phone": "+8613900010001",
            "display_name": "测试客户",
            "body": "商品退货期限是多久？",
        },
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["agent_route"] == "knowledge"
    assert payload["agent_answered"] is True
    assert payload["conversation"]["messages"][-1]["sender_type"] == "ai"
    assert "七天" in payload["conversation"]["messages"][-1]["body"]
    assert payload["conversation"]["messages"][-1]["metadata_json"]["sources"][0]["title"] == "退货政策"
    assert payload["conversation"]["messages"][-1]["metadata_json"]["language"] == "zh-CN"


def test_order_tool_and_human_handoff(authenticated_client: TestClient):
    order = authenticated_client.post(
        "/api/demo/inbound",
        json={
            "phone": "+8613900010002",
            "display_name": "订单客户",
            "body": "查询订单 ORD-1001",
        },
    )
    assert order.status_code == 201
    assert order.json()["agent_route"] == "order"
    assert "已发货" in order.json()["conversation"]["messages"][-1]["body"]

    complaint = authenticated_client.post(
        "/api/demo/inbound",
        json={
            "phone": "+8613900010003",
            "display_name": "投诉客户",
            "body": "我要投诉并转人工客服",
        },
    )
    assert complaint.status_code == 201
    payload = complaint.json()
    assert payload["agent_route"] == "handoff"
    assert payload["conversation"]["ai_enabled"] is False
    assert payload["conversation"]["priority"] == "high"
    assert payload["conversation"]["status"] == "pending"
    assert payload["conversation"]["messages"][-1]["body"] == "这边给你转接人工客服，请稍后"

    message_count = len(payload["conversation"]["messages"])
    follow_up = authenticated_client.post(
        "/api/demo/inbound",
        json={
            "phone": "+8613900010003",
            "display_name": "投诉客户",
            "body": "我再补充一项资料",
        },
    )
    assert follow_up.status_code == 201
    follow_up_payload = follow_up.json()
    assert follow_up_payload["agent_answered"] is False
    assert follow_up_payload["conversation"]["status"] == "pending"
    assert len(follow_up_payload["conversation"]["messages"]) == message_count + 1


def test_english_deterministic_routes_reply_in_english(authenticated_client: TestClient):
    cases = (
        (
            "+8613900010004",
            "Hi",
            "greeting",
            "Hello! How can I help you today?",
        ),
        (
            "+8613900010005",
            "How much?",
            "pricing",
            "Please tell me the destination or product name",
        ),
        (
            "+8613900010006",
            "Track order ORD-1001",
            "order",
            "Order ORD-1001 is currently shipped.",
        ),
    )
    for phone, body, route, expected_text in cases:
        response = authenticated_client.post(
            "/api/demo/inbound",
            json={"phone": phone, "display_name": "English test", "body": body},
        )
        assert response.status_code == 201
        payload = response.json()
        outbound = payload["conversation"]["messages"][-1]
        assert payload["agent_route"] == route
        assert expected_text in outbound["body"]
        assert outbound["metadata_json"]["language"] == "en"


def test_vip_policy_question_uses_verified_fixed_answer(
    authenticated_client: TestClient,
):
    response = authenticated_client.post(
        "/api/demo/inbound",
        json={
            "phone": "+8613900010010",
            "display_name": "VIP 测试客户",
            "body": "怎么样才能成为VIP",
        },
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["agent_route"] == "knowledge"
    assert payload["conversation"]["ai_enabled"] is True
    outbound = payload["conversation"]["messages"][-1]
    assert outbound["sender_type"] == "ai"
    assert format_ai_customer_message(VIP_PLAN_ANSWER_ZH, language="zh-CN").split(
        "\n————————————", 1
    )[0] in outbound["body"]
    assert "参考来源\n[1]" in outbound["body"]
    assert VIP_PLAN_SOURCE in outbound["body"]
    assert outbound["metadata_json"]["language"] == "zh-CN"
    assert outbound["metadata_json"]["sources"][0]["source"] == VIP_PLAN_SOURCE
    assert outbound["metadata_json"]["sources"][0]["deterministic"] is True


def test_vip_gift_question_with_greeting_uses_verified_fixed_answer(
    authenticated_client: TestClient,
):
    response = authenticated_client.post(
        "/api/demo/inbound",
        json={
            "phone": "+8613900010011",
            "display_name": "VIP gift smoke test",
            "body": "你好 爽wifi的VIP送什么",
        },
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["agent_route"] == "knowledge"
    assert payload["agent_answered"] is True
    outbound = payload["conversation"]["messages"][-1]
    assert format_ai_customer_message(VIP_PLAN_ANSWER_ZH, language="zh-CN").split(
        "\n————————————", 1
    )[0] in outbound["body"]
    assert "参考来源\n[1]" in outbound["body"]
    assert VIP_PLAN_SOURCE in outbound["body"]
    assert outbound["metadata_json"]["language"] == "zh-CN"
    assert outbound["metadata_json"]["sources"][0]["source"] == VIP_PLAN_SOURCE
    assert outbound["metadata_json"]["sources"][0]["deterministic"] is True


def test_order_write_actions_start_verified_business_form(authenticated_client: TestClient):
    cases = (
        ("+8613900010007", "取消订单 ORD-1001", "出发日期", "zh-CN"),
        ("+8613900010008", "請幫我修改收貨地址", "訂單號", "zh-TW"),
        (
            "+8613900010009",
            "cancel order ORD-1001",
            "departure date",
            "en",
        ),
    )
    for phone, body, expected_prompt, expected_language in cases:
        response = authenticated_client.post(
            "/api/demo/inbound",
            json={
                "phone": phone,
                "display_name": "订单变更客户",
                "body": body,
            },
        )
        assert response.status_code == 201
        payload = response.json()
        assert payload["agent_route"] == "order"
        assert payload["conversation"]["ai_enabled"] is True
        assert payload["conversation"]["priority"] == "normal"
        assert payload["conversation"]["status"] == "open"
        outbound = payload["conversation"]["messages"][-1]
        assert expected_prompt.casefold() in outbound["body"].casefold()
        assert outbound["metadata_json"]["language"] == expected_language
        assert outbound["metadata_json"].get("handoff") is not True
        assert outbound["metadata_json"]["sources"] == []


def test_human_handoff_paraphrases_use_fixed_reply_and_pending_queue(
    authenticated_client: TestClient,
):
    cases = (
        ("麻烦帮我找客服处理", "这边给你转接人工客服，请稍后"),
        ("不要机器人，让人来处理", "这边给你转接人工客服，请稍后"),
        ("我想聯絡客服人員處理", "這邊給你轉接人工客服，請稍後"),
        (
            "Could I speak to a support representative?",
            "I am transferring you to a human support agent now. Please wait a moment.",
        ),
    )
    for index, (body, expected_reply) in enumerate(cases, start=20):
        response = authenticated_client.post(
            "/api/demo/inbound",
            json={
                "phone": f"+8613900010{index:03d}",
                "display_name": f"Handoff {index}",
                "body": body,
            },
        )
        assert response.status_code == 201
        payload = response.json()
        assert payload["agent_route"] == "handoff"
        assert payload["conversation"]["status"] == "pending"
        assert payload["conversation"]["ai_enabled"] is False
        assert payload["conversation"]["messages"][-1]["body"] == expected_reply
        expected_language = _detect_language(body)
        assert payload["conversation"]["messages"][-1]["metadata_json"]["language"] == expected_language


def test_outbound_safety_boundary_syncs_handoff_text_and_language_state(
    authenticated_client: TestClient,
    monkeypatch,
):
    from backend.app.services import conversations
    from backend.app.services.agent import AgentResult, answer_has_language_mismatch

    cases = (
        (
            "+8613900010061",
            AgentResult(
                route="knowledge",
                answer="A human agent will continue to assist you.",
                reply_parts=["A human agent will continue to assist you."],
                handoff=False,
                sources=[],
                language="en",
            ),
            "handoff_text",
        ),
        (
            "+8613900010062",
            AgentResult(
                route="knowledge",
                answer="Japan WiFi 可以在机场领取。",
                reply_parts=["Japan WiFi 可以在机场领取。"],
                handoff=False,
                sources=[],
                language="en",
            ),
            "language_mismatch",
        ),
    )
    for phone, unsafe_result, expected_reason in cases:
        monkeypatch.setattr(
            conversations.support_agent_workflow,
            "run",
            lambda *_args, _result=unsafe_result, **_kwargs: _result,
        )
        response = authenticated_client.post(
            "/api/demo/inbound",
            json={"phone": phone, "display_name": "Safety guard", "body": "Support question"},
        )
        assert response.status_code == 201
        payload = response.json()
        conversation = payload["conversation"]
        outbound = conversation["messages"][-1]
        assert payload["agent_route"] == "handoff"
        assert conversation["status"] == "pending"
        assert conversation["ai_enabled"] is False
        assert conversation["priority"] == "high"
        assert outbound["metadata_json"]["safety_handoff_reason"] == expected_reason
        assert answer_has_language_mismatch(outbound["body"], "en") is False


def test_pending_handoff_is_pinned_and_exposed_to_realtime_events(
    authenticated_client: TestClient,
):
    from backend.app.api.events import _inbox_state

    handoff = authenticated_client.post(
        "/api/demo/inbound",
        json={
            "phone": "+8613900010200",
            "display_name": "转人工客户",
            "body": "请立即转人工客服",
        },
    )
    assert handoff.status_code == 201
    handoff_id = handoff.json()["conversation"]["id"]

    newer_normal = authenticated_client.post(
        "/api/demo/inbound",
        json={
            "phone": "+8613900010201",
            "display_name": "普通客户",
            "body": "你好",
        },
    )
    assert newer_normal.status_code == 201

    for sort in ("newest", "oldest", "priority"):
        rows = authenticated_client.get(
            "/api/conversations",
            params={"sort": sort},
        )
        assert rows.status_code == 200
        assert rows.json()[0]["id"] == handoff_id

    _, handoff_ids = _inbox_state(1)
    assert handoff_ids == (handoff_id,)


def test_conversation_order_changes_only_for_visible_message_activity(
    authenticated_client: TestClient,
):
    from datetime import timedelta

    from sqlalchemy import select

    from backend.app.database import SessionLocal
    from backend.app.models import Conversation, utcnow

    older = authenticated_client.post(
        "/api/demo/inbound",
        json={
            "phone": "+8613900010202",
            "display_name": "较早会话",
            "body": "你好",
        },
    ).json()["conversation"]
    newer = authenticated_client.post(
        "/api/demo/inbound",
        json={
            "phone": "+8613900010203",
            "display_name": "较新会话",
            "body": "你好",
        },
    ).json()["conversation"]

    with SessionLocal() as db:
        rows = {
            item.id: item
            for item in db.scalars(
                select(Conversation).where(Conversation.id.in_((older["id"], newer["id"])))
            ).all()
        }
        now = utcnow()
        rows[older["id"]].last_message_at = now - timedelta(hours=2)
        rows[older["id"]].updated_at = now
        rows[newer["id"]].last_message_at = now - timedelta(hours=1)
        rows[newer["id"]].updated_at = now - timedelta(hours=3)
        db.commit()

    def ordered_ids() -> list[int]:
        response = authenticated_client.get("/api/conversations", params={"sort": "newest"})
        assert response.status_code == 200
        return [item["id"] for item in response.json()]

    assert ordered_ids()[:2] == [newer["id"], older["id"]]

    opened = authenticated_client.patch(
        f"/api/conversations/{older['id']}",
        json={"mark_read": True},
    )
    assert opened.status_code == 200
    assert ordered_ids()[:2] == [newer["id"], older["id"]]

    note = authenticated_client.post(
        f"/api/conversations/{older['id']}/messages",
        json={"body": "内部备注不应置顶", "internal": True},
    )
    assert note.status_code == 200
    assert ordered_ids()[:2] == [newer["id"], older["id"]]

    reply = authenticated_client.post(
        f"/api/conversations/{older['id']}/messages",
        json={"body": "客服的新回复应立即置顶", "internal": False},
    )
    assert reply.status_code == 200
    assert ordered_ids()[:2] == [older["id"], newer["id"]]


def test_conversation_assignment_and_agent_reply(authenticated_client: TestClient):
    inbound = authenticated_client.post(
        "/api/demo/inbound",
        json={
            "phone": "+8613900010004",
            "display_name": "人工客户",
            "body": "我要人工客服",
        },
    ).json()
    conversation_id = inbound["conversation"]["id"]

    reply = authenticated_client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"body": "您好，我来继续处理您的问题。"},
    )
    assert reply.status_code == 200
    assert reply.json()["sender_type"] == "agent"

    solved = authenticated_client.patch(
        f"/api/conversations/{conversation_id}",
        json={"status": "solved", "mark_read": True},
    )
    assert solved.status_code == 200
    assert solved.json()["status"] == "solved"
    assert solved.json()["unread_count"] == 0


def test_message_translation_is_temporary_and_returns_traditional_chinese(
    authenticated_client: TestClient,
    monkeypatch,
):
    inbound = authenticated_client.post(
        "/api/demo/inbound",
        json={
            "phone": "+8613900010088",
            "display_name": "Translation customer",
            "body": "Could you tell me the current price for Korea?",
        },
    ).json()
    conversation = inbound["conversation"]
    source_message = next(
        item for item in conversation["messages"] if item["sender_type"] == "customer"
    )
    source_count = len(conversation["messages"])
    captured: list[str] = []

    def fake_translate(text: str) -> str:
        captured.append(text)
        return "請告訴我韓國目前的價格。"

    monkeypatch.setattr(
        "backend.app.api.conversations.translate_english_to_traditional",
        fake_translate,
    )
    response = authenticated_client.post(
        f"/api/conversations/{conversation['id']}/messages/{source_message['id']}/translate"
    )
    assert response.status_code == 200
    assert response.json() == {
        "message_id": source_message["id"],
        "source_language": "en",
        "target_language": "zh-TW",
        "translated_text": "請告訴我韓國目前的價格。",
    }
    assert captured == ["Could you tell me the current price for Korea?"]

    refreshed = authenticated_client.get(f"/api/conversations/{conversation['id']}").json()
    assert len(refreshed["messages"]) == source_count
    persisted = next(item for item in refreshed["messages"] if item["id"] == source_message["id"])
    assert "translated_text" not in persisted["metadata_json"]


def test_whatsapp_webhook_verification_and_message(authenticated_client: TestClient):
    verify = authenticated_client.get(
        "/api/webhooks/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "replace-with-a-random-token",
            "hub.challenge": "12345",
        },
    )
    assert verify.status_code == 200
    assert verify.text == "12345"

    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [
                                {"wa_id": "8613900010005", "profile": {"name": "Webhook 客户"}}
                            ],
                            "messages": [
                                {
                                    "id": "wamid.test.1",
                                    "from": "8613900010005",
                                    "type": "text",
                                    "text": {"body": "请转人工"},
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }
    received = authenticated_client.post("/api/webhooks/whatsapp", json=payload)
    assert received.status_code == 200
    conversations = authenticated_client.get("/api/conversations?search=Webhook").json()
    assert len(conversations) == 1
    assert conversations[0]["ai_route"] == "handoff"


def test_evolution_webhook_authentication_and_message(authenticated_client: TestClient):
    payload = {
        "event": "messages.upsert",
        "instance": "agentdesk",
        "data": {
            "key": {
                "remoteJid": "8613900010006@s.whatsapp.net",
                "fromMe": False,
                "id": "evolution.test.1",
            },
            "pushName": "Web 客户",
            "message": {"conversation": "我需要人工客服"},
            "messageType": "conversation",
        },
    }
    unauthorized = authenticated_client.post("/api/webhooks/evolution", json=payload)
    assert unauthorized.status_code == 401

    received = authenticated_client.post(
        "/api/webhooks/evolution",
        json=payload,
        headers={"X-AgentDesk-Webhook-Secret": "test-evolution-secret"},
    )
    assert received.status_code == 200
    conversations = authenticated_client.get("/api/conversations?search=Web 客户").json()
    assert len(conversations) == 1
    assert conversations[0]["contact"]["phone"] == "+8613900010006"
    assert conversations[0]["ai_route"] == "handoff"

    duplicate = authenticated_client.post(
        "/api/webhooks/evolution",
        json=payload,
        headers={"X-AgentDesk-Webhook-Secret": "test-evolution-secret"},
    )
    assert duplicate.status_code == 200
    detail = authenticated_client.get(
        f"/api/conversations/{conversations[0]['id']}"
    ).json()
    assert len([item for item in detail["messages"] if item["external_id"] == "evolution.test.1"]) == 1


def test_evolution_lid_is_preserved_for_replies(
    authenticated_client: TestClient,
    monkeypatch,
):
    from sqlalchemy import select

    from backend.app.config import settings
    from backend.app.channels import SendResult
    from backend.app.contact_attributes import EVOLUTION_RECIPIENT_JID_KEY
    from backend.app.database import SessionLocal
    from backend.app.models import Contact

    recipients: list[str] = []

    def fake_send(self, outbound):
        recipients.append(outbound.to)
        return SendResult(
            provider="evolution",
            external_message_id="evolution.lid.reply.1",
            status="pending",
        )

    monkeypatch.setattr(settings, "whatsapp_provider", "evolution")
    monkeypatch.setattr(
        "backend.app.channels.evolution.EvolutionChannelProvider.send", fake_send
    )
    payload = {
        "event": "messages.upsert",
        "instance": "agentdesk",
        "data": {
            "key": {
                "remoteJid": "123456789012345@lid",
                "remoteJidAlt": "8613900010099@s.whatsapp.net",
                "fromMe": False,
                "id": "evolution.lid.inbound.1",
            },
            "pushName": "LID 客户",
            "message": {"conversation": "你好"},
            "messageType": "conversation",
        },
    }
    response = authenticated_client.post(
        "/api/webhooks/evolution",
        json=payload,
        headers={"X-AgentDesk-Webhook-Secret": "test-evolution-secret"},
    )
    assert response.status_code == 200
    assert recipients == ["123456789012345@lid"]

    conversations = authenticated_client.get("/api/conversations?search=LID 客户").json()
    assert len(conversations) == 1
    assert conversations[0]["contact"]["phone"] == "+8613900010099"
    assert EVOLUTION_RECIPIENT_JID_KEY not in conversations[0]["contact"]["custom_attributes"]

    contact_id = conversations[0]["contact"]["id"]
    updated = authenticated_client.patch(
        f"/api/contacts/{contact_id}",
        json={"custom_attributes": {"customer_tier": "vip"}},
    )
    assert updated.status_code == 200
    assert updated.json()["custom_attributes"] == {"customer_tier": "vip"}
    with SessionLocal() as db:
        contact = db.scalar(select(Contact).where(Contact.id == contact_id))
        assert contact is not None
        assert contact.custom_attributes[EVOLUTION_RECIPIENT_JID_KEY] == "123456789012345@lid"


def test_whatsapp_integration_status(authenticated_client: TestClient):
    response = authenticated_client.get("/api/integrations/whatsapp")
    assert response.status_code == 200
    assert response.json() == {
        "provider": "demo",
        "configured": False,
        "state": "demo",
        "instance_name": None,
        "webhook_url": None,
        "qr_code": None,
        "message": None,
    }


def test_inbox_views_internal_notes_and_activity(authenticated_client: TestClient):
    inbound = authenticated_client.post(
        "/api/demo/inbound",
        json={
            "phone": "+8613900010010",
            "display_name": "Queue Customer",
            "body": "I need a human agent",
        },
    )
    assert inbound.status_code == 201
    conversation_id = inbound.json()["conversation"]["id"]

    stats = authenticated_client.get("/api/inbox/stats")
    assert stats.status_code == 200
    assert stats.json()["unassigned"] == 1

    unassigned = authenticated_client.get("/api/conversations?view=unassigned")
    assert unassigned.status_code == 200
    assert [item["id"] for item in unassigned.json()] == [conversation_id]

    note = authenticated_client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"body": "Verify the refund evidence before replying.", "internal": True},
    )
    assert note.status_code == 200
    assert note.json()["direction"] == "internal"
    assert note.json()["delivery_status"] == "internal"
    assert note.json()["external_id"] is None

    detail = authenticated_client.get(f"/api/conversations/{conversation_id}").json()
    assert detail["messages"][-1]["metadata_json"] == {"internal": True}
    assert detail["last_message"] != note.json()["body"]

    mine = authenticated_client.get("/api/conversations?view=mine")
    assert [item["id"] for item in mine.json()] == [conversation_id]
    activity = authenticated_client.get(f"/api/conversations/{conversation_id}/activity")
    assert activity.status_code == 200
    assert activity.json()[0]["action"] == "conversation.note_added"


def test_workspace_and_quick_replies(authenticated_client: TestClient):
    workspace = authenticated_client.get("/api/workspace")
    assert workspace.status_code == 200
    assert workspace.json()["max_agent_seats"] == 5
    assert workspace.json()["supported_locales"] == ["zh-TW"]
    assert workspace.json()["default_locale"] == "zh-TW"

    defaults = authenticated_client.get("/api/quick-replies")
    assert defaults.status_code == 200
    assert any(item["shortcut"] == "greeting" for item in defaults.json())

    created = authenticated_client.post(
        "/api/quick-replies",
        json={
            "shortcut": "refund-check",
            "title": "Refund check",
            "body": "We are checking your refund request.",
            "language": "zh-CN",
        },
    )
    assert created.status_code == 201
    deleted = authenticated_client.delete(f"/api/quick-replies/{created.json()['id']}")
    assert deleted.status_code == 204
    assert all(
        item["id"] != created.json()["id"]
        for item in authenticated_client.get("/api/quick-replies").json()
    )


def test_demo_inbound_is_disabled_for_live_provider(authenticated_client: TestClient):
    from backend.app.config import settings

    original = settings.whatsapp_provider
    settings.whatsapp_provider = "evolution"
    try:
        response = authenticated_client.post(
            "/api/demo/inbound",
            json={
                "phone": "+8613900010099",
                "display_name": "Should Not Send",
                "body": "This must be rejected in live mode.",
            },
        )
    finally:
        settings.whatsapp_provider = original
    assert response.status_code == 409


def test_unknown_message_fails_closed_and_greeting_stays_automatic(
    authenticated_client: TestClient,
):
    unknown = authenticated_client.post(
        "/api/demo/inbound",
        json={
            "phone": "+8613900010100",
            "display_name": "Unknown Intent",
            "body": "AGENTDESK-INBOUND-TEST",
        },
    )
    assert unknown.status_code == 201
    unknown_payload = unknown.json()
    assert unknown_payload["agent_route"] == "handoff"
    assert unknown_payload["conversation"]["ai_enabled"] is False
    assert unknown_payload["conversation"]["priority"] == "high"
    assert unknown_payload["conversation"]["messages"][-1]["metadata_json"]["sources"] == []

    greeting = authenticated_client.post(
        "/api/demo/inbound",
        json={
            "phone": "+8613900010101",
            "display_name": "Greeting Intent",
            "body": "你好！",
        },
    )
    assert greeting.status_code == 201
    greeting_payload = greeting.json()
    assert greeting_payload["agent_route"] == "greeting"
    assert greeting_payload["conversation"]["ai_enabled"] is True
    assert "协助" in greeting_payload["conversation"]["messages"][-1]["body"]
    assert greeting_payload["conversation"]["messages"][-1]["metadata_json"]["language"] == "zh-CN"


def test_low_similarity_knowledge_is_rejected(authenticated_client: TestClient):
    from backend.app.database import SessionLocal
    from backend.app.services.knowledge import retrieve_knowledge

    created = authenticated_client.post(
        "/api/knowledge",
        json={
            "title": "退换货政策",
            "category": "policy",
            "source": "test://returns",
            "content": "客户签收商品后七天内可以申请退货。",
        },
    )
    assert created.status_code == 201
    with SessionLocal() as db:
        assert retrieve_knowledge(db, 1, "AGENTDESK-INBOUND-TEST") == []


def test_generic_product_overlap_cannot_answer_an_unsupported_feature(
    authenticated_client: TestClient,
):
    created = authenticated_client.post(
        "/api/knowledge",
        json={
            "title": "WiFi 蛋常见问题",
            "category": "faq",
            "source": "test://wifi-faq",
            "content": "这里说明 WiFi 蛋租借、机场取还和多人共享方法。",
        },
    )
    assert created.status_code == 201
    response = authenticated_client.post(
        "/api/demo/inbound",
        json={
            "phone": "+8613900010103",
            "display_name": "Wrong source guard",
            "body": "WiFi 蛋支持量子卫星加密协议吗？",
        },
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["agent_route"] == "handoff"
    assert payload["conversation"]["status"] == "pending"
    assert payload["conversation"]["ai_enabled"] is False
    assert payload["conversation"]["messages"][-1]["metadata_json"]["sources"] == []


def test_early_delivery_receipt_is_reconciled(authenticated_client: TestClient):
    from sqlalchemy import select

    from backend.app.database import SessionLocal
    from backend.app.models import Conversation, Message

    external_id = "evolution.delivery.race.1"
    early_receipt = authenticated_client.post(
        "/api/webhooks/evolution",
        json={
            "event": "messages.update",
            "instance": "agentdesk",
            "data": {
                "key": {"id": external_id},
                "update": {"status": "DELIVERY_ACK"},
            },
        },
        headers={"X-AgentDesk-Webhook-Secret": "test-evolution-secret"},
    )
    assert early_receipt.status_code == 200

    inbound = authenticated_client.post(
        "/api/demo/inbound",
        json={
            "phone": "+8613900010102",
            "display_name": "Receipt Race",
            "body": "I need a human agent",
        },
    ).json()
    conversation_id = inbound["conversation"]["id"]
    with SessionLocal() as db:
        conversation = db.scalar(select(Conversation).where(Conversation.id == conversation_id))
        assert conversation is not None
        db.add(
            Message(
                tenant_id=conversation.tenant_id,
                conversation_id=conversation.id,
                external_id=external_id,
                direction="outbound",
                sender_type="agent",
                sender_name="Race test",
                body="Receipt race test",
                delivery_status="sent",
            )
        )
        db.commit()

    detail = authenticated_client.get(f"/api/conversations/{conversation_id}").json()
    raced_message = next(
        item for item in detail["messages"] if item["external_id"] == external_id
    )
    assert raced_message["delivery_status"] == "delivered"

    read_receipt = authenticated_client.post(
        "/api/webhooks/evolution",
        json={
            "event": "send.message.update",
            "instance": "agentdesk",
            "data": {"messageId": external_id, "status": "READ"},
        },
        headers={"X-AgentDesk-Webhook-Secret": "test-evolution-secret"},
    )
    assert read_receipt.status_code == 200
    detail = authenticated_client.get(f"/api/conversations/{conversation_id}").json()
    raced_message = next(
        item for item in detail["messages"] if item["external_id"] == external_id
    )
    assert raced_message["delivery_status"] == "read"


def test_evolution_key_id_and_numeric_status_are_supported(
    authenticated_client: TestClient,
):
    from sqlalchemy import select

    from backend.app.database import SessionLocal
    from backend.app.models import Conversation, Message, MessageDeliveryReceipt

    inbound = authenticated_client.post(
        "/api/demo/inbound",
        json={
            "phone": "+8613900010103",
            "display_name": "Evolution keyId",
            "body": "I need a human agent",
        },
    ).json()
    conversation_id = inbound["conversation"]["id"]
    external_id = "evolution.delivery.key-id.1"
    internal_message_id = "evolution-database-message-uuid"
    with SessionLocal() as db:
        conversation = db.scalar(select(Conversation).where(Conversation.id == conversation_id))
        assert conversation is not None
        db.add(
            Message(
                tenant_id=conversation.tenant_id,
                conversation_id=conversation.id,
                external_id=external_id,
                direction="outbound",
                sender_type="agent",
                sender_name="keyId test",
                body="keyId delivery test",
                delivery_status="pending",
            )
        )
        db.commit()

    delivered = authenticated_client.post(
        "/api/webhooks/evolution",
        json={
            "event": "MESSAGES_UPDATE",
            "instance": "agentdesk",
            "data": {
                "id": "evolution-receipt-row-uuid",
                "messageId": internal_message_id,
                "keyId": external_id,
                "status": 3,
            },
        },
        headers={"X-AgentDesk-Webhook-Secret": "test-evolution-secret"},
    )
    assert delivered.status_code == 200
    detail = authenticated_client.get(f"/api/conversations/{conversation_id}").json()
    message = next(item for item in detail["messages"] if item["external_id"] == external_id)
    assert message["delivery_status"] == "delivered"

    read = authenticated_client.post(
        "/api/webhooks/evolution",
        json={
            "event": "SEND_MESSAGE_UPDATE",
            "instance": "agentdesk",
            "data": {
                "messageId": internal_message_id,
                "keyId": external_id,
                "status": 4,
            },
        },
        headers={"X-AgentDesk-Webhook-Secret": "test-evolution-secret"},
    )
    assert read.status_code == 200
    detail = authenticated_client.get(f"/api/conversations/{conversation_id}").json()
    message = next(item for item in detail["messages"] if item["external_id"] == external_id)
    assert message["delivery_status"] == "read"
    with SessionLocal() as db:
        assert db.get(MessageDeliveryReceipt, external_id) is not None
        assert db.get(MessageDeliveryReceipt, internal_message_id) is None


def test_failed_message_can_be_retried_once_safely(
    authenticated_client: TestClient,
    monkeypatch,
):
    from sqlalchemy import select

    from backend.app.database import SessionLocal
    from backend.app.channels import ChannelProviderError, SendResult
    from backend.app.models import MessageDeliveryAttempt

    inbound = authenticated_client.post(
        "/api/demo/inbound",
        json={
            "phone": "+8613900010104",
            "display_name": "Retry Customer",
            "body": "I need a human agent",
        },
    ).json()
    conversation_id = inbound["conversation"]["id"]
    send_calls = 0

    def flaky_send(self, outbound):
        nonlocal send_calls
        send_calls += 1
        if send_calls == 1:
            raise ChannelProviderError(
                "provider rejected test message",
                code="provider_rejected",
            )
        return SendResult(
            provider="demo",
            external_message_id="demo.retry.success.1",
            status="sent",
        )

    monkeypatch.setattr("backend.app.channels.demo.DemoChannelProvider.send", flaky_send)
    failed = authenticated_client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"body": "Please retry this message", "internal": False},
    )
    assert failed.status_code == 200
    assert failed.json()["delivery_status"] == "failed"
    message_id = failed.json()["id"]

    retried = authenticated_client.post(
        f"/api/conversations/{conversation_id}/messages/{message_id}/retry"
    )
    assert retried.status_code == 200
    assert retried.json()["delivery_status"] == "sent"
    assert retried.json()["external_id"] == "demo.retry.success.1"
    duplicate = authenticated_client.post(
        f"/api/conversations/{conversation_id}/messages/{message_id}/retry"
    )
    assert duplicate.status_code == 409
    assert send_calls == 2

    with SessionLocal() as db:
        attempts = db.scalars(
            select(MessageDeliveryAttempt)
            .where(MessageDeliveryAttempt.message_id == message_id)
            .order_by(MessageDeliveryAttempt.attempt_number)
        ).all()
        assert [item.delivery_status for item in attempts] == ["failed", "sent"]
        assert [item.attempt_number for item in attempts] == [1, 2]


def test_delivery_status_can_be_reconciled_from_evolution(
    authenticated_client: TestClient,
    monkeypatch,
):
    from backend.app.database import SessionLocal
    from backend.app.models import ChannelAccount, Conversation, Message

    inbound = authenticated_client.post(
        "/api/demo/inbound",
        json={
            "phone": "+8613900010105",
            "display_name": "Reconcile Customer",
            "body": "I need a human agent",
        },
    ).json()
    conversation_id = inbound["conversation"]["id"]
    sent = authenticated_client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"body": "Reconcile this message", "internal": False},
    ).json()

    with SessionLocal() as db:
        conversation = db.get(Conversation, conversation_id)
        message = db.get(Message, sent["id"])
        assert conversation is not None and message is not None
        account = ChannelAccount(
            tenant_id=conversation.tenant_id,
            provider="evolution",
            name="Evolution reconciliation test",
            external_account_id="agentdesk-reconcile",
            instance_name="agentdesk",
            capabilities=["delivery_reconcile"],
            is_default=False,
        )
        db.add(account)
        db.flush()
        conversation.channel_account_id = account.id
        message.channel_account_id = account.id
        message.provider = "evolution"
        db.commit()
    monkeypatch.setattr(
        "backend.app.channels.evolution.EvolutionChannelProvider.delivery_statuses",
        lambda self, external_id: [2, 3, 4],
    )
    reconciled = authenticated_client.post(
        f"/api/conversations/{conversation_id}/messages/{sent['id']}/delivery/reconcile"
    )
    assert reconciled.status_code == 200
    assert reconciled.json()["delivery_status"] == "read"
