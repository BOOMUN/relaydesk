from __future__ import annotations

import httpx

from backend.app.config import settings
from backend.app.services.whatsapp import EvolutionClient


def test_evolution_connect_creates_instance_and_configures_webhook(monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_provider", "evolution")
    monkeypatch.setattr(settings, "evolution_api_url", "http://evolution.test")
    monkeypatch.setattr(settings, "evolution_api_key", "test-api-key")
    monkeypatch.setattr(settings, "evolution_instance_name", "agentdesk")
    monkeypatch.setattr(
        settings,
        "evolution_webhook_url",
        "http://host.docker.internal:8000/api/webhooks/evolution",
    )
    monkeypatch.setattr(settings, "evolution_webhook_secret", "test-webhook-secret")
    calls: list[tuple[str, str, dict | None]] = []

    def fake_request(method, url, *, headers, json, timeout):
        calls.append((method, url, json))
        request = httpx.Request(method, url)
        if url.endswith("/instance/connectionState/agentdesk"):
            return httpx.Response(404, json={"message": "not found"}, request=request)
        if url.endswith("/instance/create"):
            return httpx.Response(201, json={"instance": {"status": "connecting"}}, request=request)
        if url.endswith("/webhook/set/agentdesk"):
            return httpx.Response(201, json={"enabled": True}, request=request)
        if url.endswith("/instance/connect/agentdesk"):
            return httpx.Response(200, json={"base64": "cXItY29kZQ=="}, request=request)
        raise AssertionError(f"Unexpected request: {method} {url}")

    monkeypatch.setattr(httpx, "request", fake_request)

    result = EvolutionClient().connect()

    assert result.state == "connecting"
    assert result.qr_code == "data:image/png;base64,cXItY29kZQ=="
    assert [call[1] for call in calls] == [
        "http://evolution.test/instance/connectionState/agentdesk",
        "http://evolution.test/instance/create",
        "http://evolution.test/webhook/set/agentdesk",
        "http://evolution.test/instance/connect/agentdesk",
    ]
    webhook_payload = calls[2][2]
    assert webhook_payload is not None
    assert webhook_payload["webhook"]["headers"] == {
        "X-AgentDesk-Webhook-Secret": "test-webhook-secret"
    }
