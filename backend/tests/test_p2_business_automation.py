from __future__ import annotations

from datetime import timedelta, timezone
from types import SimpleNamespace

from cryptography.fernet import Fernet
import httpx
import pytest
from sqlalchemy import select

from backend.app.config import settings
from backend.app.database import SessionLocal
from backend.app.models import (
    ActionExecution,
    AutomationFormSession,
    Contact,
    RestActionEndpoint,
    SensitiveOperationRequest,
    utcnow,
)
from backend.app.services.rest_actions import (
    PublicOrigin,
    RestActionSecurityError,
    execute_rest_action,
    normalize_action_path,
    validate_public_origin,
)


def _publish_agent(client, **changes):
    assert client.get("/api/ai-agent").status_code == 200
    if changes:
        saved = client.patch("/api/ai-agent/draft", json=changes)
        assert saved.status_code == 200, saved.text
    published = client.post("/api/ai-agent/publish")
    assert published.status_code == 200, published.text
    return published.json()["active_version"]


def _inbound(client, phone: str, body: str):
    response = client.post(
        "/api/demo/inbound",
        json={"phone": phone, "display_name": "P2 customer", "body": body},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _last_ai_body(payload: dict) -> str:
    messages = payload["conversation"]["messages"]
    return next(
        item["body"]
        for item in reversed(messages)
        if item["sender_type"] == "ai"
    )


def test_order_form_collects_pauses_resumes_and_modifies(authenticated_client):
    _publish_agent(authenticated_client, order_intake_enabled=True)
    phone = "+85290001001"

    started = _inbound(authenticated_client, phone, "我要查询订单状态")
    assert "订单号" in _last_ai_body(started)

    answered = _inbound(authenticated_client, phone, "AB-12345")
    assert "出发日期" in _last_ai_body(answered)

    paused = _inbound(authenticated_client, phone, "先暂停填写")
    assert "暂停" in _last_ai_body(paused)

    resumed = _inbound(authenticated_client, phone, "继续填写")
    assert "出发日期" in _last_ai_body(resumed)

    changed = _inbound(authenticated_client, phone, "修改订单号为CD-99999")
    assert "出发日期" in _last_ai_body(changed)

    dated = _inbound(authenticated_client, phone, "2026-09-15")
    assert "目的地" in _last_ai_body(dated)

    completed = _inbound(authenticated_client, phone, "日本")
    assert completed["agent_route"] == "handoff"
    assert completed["conversation"]["ai_enabled"] is False

    sessions = authenticated_client.get(
        f"/api/automation/sessions?conversation_id={completed['conversation']['id']}"
    )
    assert sessions.status_code == 200
    session_payload = sessions.json()[0]
    assert [item["key"] for item in session_payload["definition_json"]["fields"]] == [
        "order_number",
        "departure_date",
        "destination",
    ]
    assert session_payload["current_step"] == 3

    with SessionLocal() as db:
        session = db.scalar(select(AutomationFormSession))
        assert session is not None
        assert session.status == "completed"
        assert session.answers_json == {
            "order_number": "CD-99999",
            "departure_date": "2026-09-15",
            "destination": "日本",
        }


def test_order_form_timeout_becomes_real_handoff(authenticated_client):
    _publish_agent(authenticated_client, order_intake_enabled=True)
    phone = "+85290001002"
    started = _inbound(authenticated_client, phone, "track my order")
    conversation_id = started["conversation"]["id"]
    with SessionLocal() as db:
        session = db.scalar(
            select(AutomationFormSession).where(
                AutomationFormSession.conversation_id == conversation_id
            )
        )
        assert session is not None
        session.expires_at = utcnow() - timedelta(minutes=1)
        db.commit()

    expired = _inbound(authenticated_client, phone, "AB-12345")
    assert expired["agent_route"] == "handoff"
    assert "timed out" in _last_ai_body(expired).casefold()
    assert expired["conversation"]["ai_enabled"] is False


def test_sensitive_order_requires_identity_and_human_confirmation(authenticated_client):
    _publish_agent(authenticated_client, order_intake_enabled=True)
    phone = "+85290001003"
    _inbound(authenticated_client, phone, "我要申请退款")
    _inbound(authenticated_client, phone, "RF-88991")
    _inbound(authenticated_client, phone, "2026-10-01")
    _inbound(authenticated_client, phone, "韩国")
    completed = _inbound(authenticated_client, phone, "行程取消")
    conversation_id = completed["conversation"]["id"]

    pending = authenticated_client.get(
        f"/api/actions?conversation_id={conversation_id}&status=pending_confirmation"
    )
    assert pending.status_code == 200
    sensitive = next(
        item for item in pending.json() if item["action_name"] == "order.sensitive.request"
    )
    assert sensitive["requires_identity_verification"] is True
    assert sensitive["identity_verified"] is False

    verification = authenticated_client.post(
        f"/api/automation/conversations/{conversation_id}/identity-verifications",
        json={
            "method": "order_details",
            "evidence_reference": "registered-phone-and-order-match",
            "evidence_hint": "phone/order matched",
            "expires_minutes": 30,
        },
    )
    assert verification.status_code == 200, verification.text
    refreshed = authenticated_client.get(
        f"/api/actions?conversation_id={conversation_id}&status=pending_confirmation"
    )
    refreshed_sensitive = next(
        item
        for item in refreshed.json()
        if item["action_name"] == "order.sensitive.request"
    )
    assert refreshed_sensitive["identity_verified"] is True
    confirmed = authenticated_client.post(f"/api/actions/{sensitive['id']}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "succeeded"
    assert confirmed.json()["result_json"]["external_operation_executed"] is False

    with SessionLocal() as db:
        request = db.scalar(select(SensitiveOperationRequest))
        assert request is not None
        assert request.operation == "refund"
        assert request.status == "approved_for_manual_execution"


def test_lead_scoring_tags_and_assigns(authenticated_client):
    team_id = authenticated_client.get("/api/teams").json()[0]["id"]
    _publish_agent(
        authenticated_client,
        lead_qualification={
            "enabled": True,
            "trigger_terms": ["business quote"],
            "questions": [
                {
                    "id": "fleet_size",
                    "prompt": "请选择预计设备数量",
                    "prompt_en": "Choose the expected device quantity",
                    "kind": "single_choice",
                    "options": [
                        {"value": "small", "label": "1-9", "score": 0},
                        {"value": "large", "label": "10+", "score": 10},
                    ],
                }
            ],
            "grades": [
                {"name": "cold", "min_score": 0, "tag": "lead:cold"},
                {
                    "name": "hot",
                    "min_score": 10,
                    "tag": "lead:hot",
                    "priority": "high",
                    "team_id": team_id,
                },
            ],
        },
    )
    phone = "+85290001004"
    started = _inbound(authenticated_client, phone, "I need a business quote")
    assert "device quantity" in _last_ai_body(started)
    completed = _inbound(authenticated_client, phone, "10+")
    assert completed["agent_route"] == "knowledge"
    assert completed["conversation"]["priority"] == "high"
    assert completed["conversation"]["assigned_team_id"] == team_id

    with SessionLocal() as db:
        session = db.scalar(select(AutomationFormSession))
        contact = db.scalar(select(Contact))
        assert session is not None and session.score == 10 and session.grade == "hot"
        assert contact is not None and "lead:hot" in contact.tags
        assert contact.custom_attributes["lead_score"] == 10


def test_rest_endpoint_secret_is_encrypted_and_policy_is_approved(
    authenticated_client,
    monkeypatch,
):
    monkeypatch.setattr(settings, "secrets_encryption_key", Fernet.generate_key().decode())
    created = authenticated_client.post(
        "/api/automation/rest-endpoints",
        json={
            "name": "Orders read API",
            "base_url": "https://api.example.com",
            "path_pattern": "/v1/orders/*",
            "allowed_methods": ["GET"],
            "secret_header_name": "Authorization",
            "secret_value": "Bearer secret-value",
        },
    )
    assert created.status_code == 200, created.text
    endpoint = created.json()
    assert endpoint["has_secret"] is True
    assert "secret_value" not in endpoint

    with SessionLocal() as db:
        stored = db.get(RestActionEndpoint, endpoint["id"])
        assert stored is not None
        assert stored.secret_ciphertext and "secret-value" not in stored.secret_ciphertext

    monkeypatch.setattr(
        "backend.app.api.automation.validate_public_origin",
        lambda value, resolve_dns=True: PublicOrigin(
            url="https://api.example.com",
            hostname="api.example.com",
            port=443,
            addresses=("93.184.216.34",),
        ),
    )
    approved = authenticated_client.post(
        f"/api/automation/rest-endpoints/{endpoint['id']}/approve"
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"

    monkeypatch.setattr(
        "backend.app.actions.handlers.execute_rest_action",
        lambda endpoint, **kwargs: {
            "endpoint_id": endpoint.id,
            "status_code": 200,
            "content_type": "application/json",
            "body": {"ok": True, "path": kwargs["path"]},
        },
    )
    execution = authenticated_client.post(
        "/api/actions",
        json={
            "name": "rest.api.call",
            "arguments": {
                "endpoint_id": endpoint["id"],
                "method": "GET",
                "path": "/v1/orders/AB-123",
            },
        },
    )
    assert execution.status_code == 200, execution.text
    assert execution.json()["status"] == "succeeded"


def test_rest_security_rejects_private_hosts_and_path_traversal():
    for url in (
        "http://example.com",
        "https://127.0.0.1",
        "https://169.254.169.254",
        "https://localhost",
    ):
        try:
            validate_public_origin(url, resolve_dns=False)
        except RestActionSecurityError:
            pass
        else:
            raise AssertionError(f"unsafe URL was accepted: {url}")

    for path in ("/v1/../admin", "/v1/%2e%2e/admin", "https://evil.test/path"):
        try:
            normalize_action_path(path)
        except RestActionSecurityError:
            pass
        else:
            raise AssertionError(f"unsafe path was accepted: {path}")


@pytest.mark.parametrize(
    ("status_code", "server_address", "expected_code"),
    (
        (302, "93.184.216.34", "redirect_forbidden"),
        (200, "127.0.0.1", "dns_rebinding_blocked"),
    ),
)
def test_rest_action_rejects_redirects_and_dns_rebinding(
    monkeypatch,
    status_code,
    server_address,
    expected_code,
):
    from backend.app.services import rest_actions

    class NetworkStream:
        @staticmethod
        def get_extra_info(name):
            return (server_address, 443) if name == "server_addr" else None

    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            status_code,
            request=request,
            headers={
                "content-type": "application/json",
                **({"location": "https://127.0.0.1/private"} if status_code == 302 else {}),
            },
            json={"ok": True},
            extensions={"network_stream": NetworkStream()},
        )
    )
    real_client = httpx.Client
    monkeypatch.setattr(
        rest_actions,
        "validate_public_origin",
        lambda value, resolve_dns=True: PublicOrigin(
            url="https://api.example.com",
            hostname="api.example.com",
            port=443,
            addresses=("93.184.216.34",),
        ),
    )
    monkeypatch.setattr(
        rest_actions.httpx,
        "Client",
        lambda **kwargs: real_client(transport=transport, timeout=kwargs.get("timeout")),
    )
    endpoint = SimpleNamespace(
        id=99,
        base_url="https://api.example.com",
        path_pattern="/v1/orders/*",
        allowed_methods=["GET"],
        timeout_seconds=10,
        requires_identity_verification=False,
        secret_ciphertext=None,
        secret_header_name=None,
        status="approved",
        approved_at=utcnow().replace(tzinfo=timezone.utc),
    )
    with pytest.raises(RestActionSecurityError) as exc_info:
        execute_rest_action(
            endpoint,
            method="GET",
            path="/v1/orders/AB-123",
            query={},
            json_body=None,
        )
    assert exc_info.value.code == expected_code


def test_web_search_requires_published_policy_and_adds_citations(
    authenticated_client,
    monkeypatch,
):
    from backend.app.services import web_search
    from backend.app.services.agent import support_agent_workflow

    calls = []

    def fake_search(query, *, allowed_domains=None):
        calls.append((query, allowed_domains))
        return [
            {
                "title": "WiFi 蛋保養說明",
                "content": "WiFi 蛋故障保養期為十二個月，客戶須保留訂單證明。",
                "source": "https://support.trusted.example/warranty",
                "source_url": "https://support.trusted.example/warranty",
                "page_title": "WiFi 蛋保養說明",
                "section_path": "保養期",
                "source_type": "web_search",
            }
        ]

    monkeypatch.setattr(
        support_agent_workflow,
        "_retrieve",
        lambda state: {"context": [], "sources": [], "product_intent": False},
    )
    monkeypatch.setattr(web_search, "search_public_web", fake_search)

    _publish_agent(authenticated_client, web_search_enabled=False)
    disabled = _inbound(
        authenticated_client,
        "+85290001005",
        "WiFi 蛋故障的保養期限是多久？",
    )
    assert calls == []
    disabled_outbound = disabled["conversation"]["messages"][-1]
    assert disabled_outbound["metadata_json"]["sources"] == []

    _publish_agent(
        authenticated_client,
        web_search_enabled=True,
        web_search_allowed_domains=["trusted.example"],
    )
    enabled = _inbound(
        authenticated_client,
        "+85290001006",
        "WiFi 蛋故障的保養期限是多久？",
    )
    assert calls and calls[-1][1] == ["trusted.example"]
    outbound = enabled["conversation"]["messages"][-1]
    assert "https://support.trusted.example/warranty" in outbound["body"]
    assert "參考來源" in outbound["body"]
    assert outbound["metadata_json"]["sources"][0]["source_type"] == "web_search"


def test_web_search_provider_rejects_redirects_and_filters_domains(monkeypatch):
    from backend.app.services import web_search

    monkeypatch.setattr(settings, "web_search_provider", "brave")
    monkeypatch.setattr(settings, "web_search_api_key", "test-key")
    monkeypatch.setattr(
        web_search,
        "validate_public_origin",
        lambda value, resolve_dns=True: PublicOrigin(
            url="https://api.search.brave.com",
            hostname="api.search.brave.com",
            port=443,
            addresses=("93.184.216.34",),
        ),
    )
    real_client = httpx.Client

    def client_for(response_factory):
        transport = httpx.MockTransport(response_factory)
        return lambda **kwargs: real_client(transport=transport, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(
        web_search.httpx,
        "Client",
        client_for(
            lambda request: httpx.Response(
                302,
                request=request,
                headers={"location": "https://127.0.0.1/private"},
            )
        ),
    )
    assert web_search.search_public_web("WiFi warranty") == []

    monkeypatch.setattr(
        web_search.httpx,
        "Client",
        client_for(
            lambda request: httpx.Response(
                200,
                request=request,
                json={
                    "web": {
                        "results": [
                            {
                                "title": "Trusted result",
                                "description": "Verified support information.",
                                "url": "https://help.trusted.example/article",
                            },
                            {
                                "title": "Other result",
                                "description": "Must be filtered.",
                                "url": "https://untrusted.example/article",
                            },
                        ]
                    }
                },
            )
        ),
    )
    rows = web_search.search_public_web(
        "WiFi warranty",
        allowed_domains=["trusted.example"],
    )
    assert [item["source_url"] for item in rows] == [
        "https://help.trusted.example/article"
    ]
