from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import httpx

from ..config import settings
from .base import ChannelCapabilityError, ChannelProvider, ChannelProviderError
from .common import (
    evolution_message_body,
    evolution_recipient_jid,
    evolution_remote_jid,
    jid_identifier,
    matching_evolution_messages,
    stable_event_suffix,
)
from .types import ChannelEvent, OutboundMessage, SendResult


class EvolutionChannelProvider(ChannelProvider):
    provider_name = "evolution"

    @property
    def instance_name(self) -> str:
        return self.account.instance_name or settings.evolution_instance_name

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout: float = 20,
    ) -> dict[str, Any] | list[Any]:
        if not settings.evolution_api_key:
            raise ChannelProviderError(
                "Evolution API is not configured", code="provider_not_configured"
            )
        try:
            response = httpx.request(
                method,
                f"{settings.evolution_api_url.rstrip('/')}{path}",
                headers={
                    "apikey": settings.evolution_api_key,
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            raise ChannelProviderError(
                "Evolution API is unavailable",
                code="provider_unavailable",
                retryable=True,
            ) from exc
        if response.is_error:
            raise ChannelProviderError(
                f"Evolution API returned HTTP {response.status_code}",
                code=f"provider_http_{response.status_code}",
                retryable=response.status_code in {408, 429} or response.status_code >= 500,
                status_code=response.status_code,
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise ChannelProviderError(
                "Evolution API returned invalid JSON", code="invalid_provider_response"
            ) from exc
        if not isinstance(data, (dict, list)):
            raise ChannelProviderError(
                "Evolution API returned an unexpected response",
                code="invalid_provider_response",
            )
        return data

    @staticmethod
    def _recipient(value: str) -> str:
        stripped = value.strip()
        return stripped if "@" in stripped else re.sub(r"\D", "", stripped)

    @staticmethod
    def _message_id(payload: dict[str, Any] | list[Any]) -> str:
        if not isinstance(payload, dict):
            raise ChannelProviderError(
                "Evolution did not return a message object",
                code="invalid_provider_response",
            )
        external_id = (
            (payload.get("key") or {}).get("id")
            or ((payload.get("message") or {}).get("key") or {}).get("id")
            or payload.get("id")
        )
        if not external_id:
            raise ChannelProviderError(
                "Evolution response did not include a message ID",
                code="missing_provider_message_id",
            )
        return str(external_id)

    def send(self, message: OutboundMessage) -> SendResult:
        encoded = quote(self.instance_name, safe="")
        recipient = self._recipient(message.to)
        if message.kind == "text":
            path = f"/message/sendText/{encoded}"
            payload = {"number": recipient, "text": message.text, "linkPreview": False}
        elif message.kind == "buttons":
            path = f"/message/sendButtons/{encoded}"
            source = message.interactive
            payload = {
                "number": recipient,
                "title": str(source.get("header") or ""),
                "description": str(source.get("body") or ""),
                "footer": str(source.get("footer") or ""),
                "buttons": [
                    {
                        "type": "reply",
                        "displayText": item["title"],
                        "id": item["id"],
                    }
                    for item in source.get("buttons", [])
                ],
            }
        elif message.kind == "list":
            path = f"/message/sendList/{encoded}"
            source = message.interactive
            payload = {
                "number": recipient,
                "title": str(source.get("header") or ""),
                "description": str(source.get("body") or ""),
                "buttonText": str(source.get("button_text") or ""),
                "footerText": str(source.get("footer") or ""),
                "sections": [
                    {
                        "title": section["title"],
                        "rows": [
                            {
                                "title": row["title"],
                                "description": row.get("description") or "",
                                "rowId": row["id"],
                            }
                            for row in section.get("rows", [])
                        ],
                    }
                    for section in source.get("sections", [])
                ],
            }
        elif message.kind == "template":
            raise ChannelCapabilityError("approved_meta_templates")
        else:
            raise ChannelCapabilityError(message.kind)
        response = self._request("POST", path, payload=payload)
        return SendResult(
            provider="evolution",
            external_message_id=self._message_id(response),
            status="pending",
            raw=response if isinstance(response, dict) else {},
        )

    def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str]) -> bool:
        del raw_body
        expected = settings.evolution_webhook_secret
        supplied = headers.get("x-agentdesk-webhook-secret")
        return bool(expected and supplied and hmac.compare_digest(supplied, expected))

    @staticmethod
    def _items(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict) and isinstance(value.get("messages"), list):
            return [item for item in value["messages"] if isinstance(item, dict)]
        return [value] if isinstance(value, dict) else []

    def parse_webhook(self, payload: dict[str, Any]) -> list[ChannelEvent]:
        account_key = str(payload.get("instance") or self.instance_name)
        event_name = str(payload.get("event") or "").lower().replace("_", ".")
        events: list[ChannelEvent] = []
        if event_name == "messages.upsert":
            for item in self._items(payload.get("data")):
                key = item.get("key") if isinstance(item.get("key"), dict) else {}
                if key.get("fromMe", False):
                    continue
                remote_jid = evolution_remote_jid(key)
                if not remote_jid or remote_jid.endswith("@g.us") or remote_jid == "status@broadcast":
                    continue
                external_id = str(key.get("id") or item.get("id") or "")
                if not external_id:
                    continue
                sender_id = jid_identifier(remote_jid)
                body, content_type = evolution_message_body(item)
                provider_metadata: dict[str, Any] = {}
                recipient_jid = evolution_recipient_jid(key)
                if recipient_jid:
                    provider_metadata["recipient_jid"] = recipient_jid
                event_payload = dict(item)
                event_payload["_agentdesk_provider_metadata"] = provider_metadata
                events.append(
                    ChannelEvent(
                        provider="evolution",
                        account_key=account_key,
                        event_key=f"message:{external_id}",
                        event_type="message",
                        external_message_id=external_id,
                        payload=event_payload,
                        sender_id=sender_id,
                        sender_address=recipient_jid or sender_id,
                        display_name=str(
                            item.get("pushName") or item.get("notifyName") or "WhatsApp customer"
                        ),
                        body=body,
                        content_type=content_type,
                        occurred_at=str(item.get("messageTimestamp") or "") or None,
                    )
                )
        elif event_name in {"messages.update", "send.message.update"}:
            for item in self._items(payload.get("data")):
                key = item.get("key") if isinstance(item.get("key"), dict) else {}
                update = item.get("update") if isinstance(item.get("update"), dict) else {}
                message = item.get("message") if isinstance(item.get("message"), dict) else {}
                message_key = message.get("key") if isinstance(message.get("key"), dict) else {}
                external_id = (
                    key.get("id")
                    or item.get("keyId")
                    or message_key.get("id")
                    or item.get("messageId")
                    or item.get("id")
                )
                status = item.get("status")
                if status is None:
                    status = update.get("status")
                if status is None:
                    status = message.get("status")
                if not external_id or status is None:
                    continue
                marker = (
                    item.get("updatedAt")
                    or item.get("date_time")
                    or stable_event_suffix(item)
                )
                events.append(
                    ChannelEvent(
                        provider="evolution",
                        account_key=account_key,
                        event_key=f"status:{external_id}:{status}:{marker}",
                        event_type="status",
                        external_message_id=str(external_id),
                        payload=item,
                        delivery_status=status,
                    )
                )
        return events

    def delivery_statuses(self, external_message_id: str) -> list[object]:
        encoded = quote(self.instance_name, safe="")
        payload = self._request(
            "POST",
            f"/chat/findMessages/{encoded}",
            payload={"where": {"key": {"id": external_message_id}}, "limit": 10},
        )
        values: list[object] = []
        for item in matching_evolution_messages(payload, external_message_id):
            if item.get("status") is not None:
                values.append(item["status"])
            for field in ("MessageUpdate", "messageUpdate", "updates"):
                updates = item.get(field)
                if isinstance(updates, dict):
                    updates = [updates]
                if not isinstance(updates, list):
                    continue
                values.extend(
                    update["status"]
                    for update in updates
                    if isinstance(update, dict) and update.get("status") is not None
                )
        return values

    def connection_status(self) -> dict[str, Any]:
        if not settings.evolution_enabled:
            return {"provider": "evolution", "configured": False, "state": "not_configured"}
        encoded = quote(self.instance_name, safe="")
        try:
            payload = self._request("GET", f"/instance/connectionState/{encoded}")
        except ChannelProviderError as exc:
            return {
                "provider": "evolution",
                "configured": True,
                "state": "unavailable",
                "message": str(exc),
            }
        state = "unknown"
        if isinstance(payload, dict):
            instance = payload.get("instance") or {}
            state = str(
                (instance.get("state") if isinstance(instance, dict) else None)
                or payload.get("state")
                or payload.get("status")
                or "unknown"
            ).lower()
        return {
            "provider": "evolution",
            "configured": True,
            "state": {
                "open": "connected",
                "connected": "connected",
                "connecting": "connecting",
                "close": "disconnected",
                "closed": "disconnected",
            }.get(state, state),
        }
