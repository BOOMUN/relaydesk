from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from typing import Any

import httpx

from ..config import settings
from .base import ChannelCapabilityError, ChannelProvider, ChannelProviderError
from .common import meta_message_body, stable_event_suffix
from .types import ChannelEvent, OutboundMessage, SendResult


class MetaCloudChannelProvider(ChannelProvider):
    provider_name = "meta"

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float = 20,
    ) -> dict[str, Any]:
        if not settings.meta_access_token or not settings.meta_graph_version:
            raise ChannelProviderError(
                "Meta Cloud API is not fully configured",
                code="provider_not_configured",
            )
        try:
            response = httpx.request(
                method,
                f"https://graph.facebook.com/{settings.meta_graph_version}/{path.lstrip('/')}",
                headers={"Authorization": f"Bearer {settings.meta_access_token}"},
                json=payload,
                params=params,
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            raise ChannelProviderError(
                "Meta Cloud API is unavailable",
                code="provider_unavailable",
                retryable=True,
            ) from exc
        if response.is_error:
            retryable = response.status_code in {408, 429} or response.status_code >= 500
            try:
                error = response.json().get("error", {})
            except ValueError:
                error = {}
            raise ChannelProviderError(
                str(error.get("message") or f"Meta API returned HTTP {response.status_code}"),
                code=str(error.get("code") or f"provider_http_{response.status_code}"),
                retryable=retryable,
                status_code=response.status_code,
            )
        try:
            value = response.json()
        except ValueError as exc:
            raise ChannelProviderError(
                "Meta API returned invalid JSON", code="invalid_provider_response"
            ) from exc
        if not isinstance(value, dict):
            raise ChannelProviderError(
                "Meta API returned an unexpected response",
                code="invalid_provider_response",
            )
        return value

    def send(self, message: OutboundMessage) -> SendResult:
        phone_number_id = self.account.phone_number_id or settings.meta_phone_number_id
        if not phone_number_id:
            raise ChannelProviderError(
                "Meta phone number ID is missing", code="provider_not_configured"
            )
        body: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": message.to,
        }
        if message.kind == "text":
            body.update(
                {"type": "text", "text": {"preview_url": False, "body": message.text}}
            )
        elif message.kind == "template":
            if not message.template_name or not message.template_language:
                raise ChannelProviderError(
                    "Template name and language are required", code="invalid_action_input"
                )
            template: dict[str, Any] = {
                "name": message.template_name,
                "language": {"code": message.template_language},
            }
            if message.template_components:
                template["components"] = message.template_components
            body.update({"type": "template", "template": template})
        elif message.kind in {"buttons", "list"}:
            source = message.interactive
            interactive: dict[str, Any] = {
                "type": "button" if message.kind == "buttons" else "list",
                "body": {"text": str(source.get("body") or "")},
            }
            if source.get("header"):
                interactive["header"] = {
                    "type": "text",
                    "text": str(source["header"]),
                }
            if source.get("footer"):
                interactive["footer"] = {"text": str(source["footer"])}
            if message.kind == "buttons":
                interactive["action"] = {
                    "buttons": [
                        {
                            "type": "reply",
                            "reply": {"id": item["id"], "title": item["title"]},
                        }
                        for item in source.get("buttons", [])
                    ]
                }
            else:
                interactive["action"] = {
                    "button": str(source.get("button_text") or ""),
                    "sections": source.get("sections", []),
                }
            body.update({"type": "interactive", "interactive": interactive})
        else:
            raise ChannelCapabilityError(message.kind)
        payload = self._request("POST", f"{phone_number_id}/messages", payload=body)
        try:
            external_id = str(payload["messages"][0]["id"])
        except (KeyError, IndexError, TypeError) as exc:
            raise ChannelProviderError(
                "Meta response did not include a message ID",
                code="missing_provider_message_id",
            ) from exc
        return SendResult(
            provider=self.provider_name,
            external_message_id=external_id,
            status="pending",
            raw=payload,
        )

    def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str]) -> bool:
        if not settings.meta_app_secret:
            return settings.environment != "production"
        signature = headers.get("x-hub-signature-256")
        if not signature or not signature.startswith("sha256="):
            return False
        expected = hmac.new(
            settings.meta_app_secret.encode("utf-8"), raw_body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(signature.removeprefix("sha256="), expected)

    def parse_webhook(self, payload: dict[str, Any]) -> list[ChannelEvent]:
        events: list[ChannelEvent] = []
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value") if isinstance(change, dict) else None
                if not isinstance(value, dict):
                    continue
                metadata = value.get("metadata") or {}
                account_key = str(
                    metadata.get("phone_number_id")
                    or self.account.phone_number_id
                    or self.account.external_account_id
                )
                names = {
                    str(item.get("wa_id") or ""): str(
                        (item.get("profile") or {}).get("name") or "WhatsApp customer"
                    )
                    for item in value.get("contacts", [])
                    if isinstance(item, dict)
                }
                for item in value.get("messages", []):
                    if not isinstance(item, dict):
                        continue
                    external_id = str(item.get("id") or "")
                    if not external_id:
                        continue
                    sender = str(item.get("from") or "")
                    body, content_type = meta_message_body(item)
                    events.append(
                        ChannelEvent(
                            provider="meta",
                            account_key=account_key,
                            event_key=f"message:{external_id}",
                            event_type="message",
                            external_message_id=external_id,
                            payload=item,
                            sender_id=sender,
                            sender_address=sender,
                            display_name=names.get(sender, "WhatsApp customer"),
                            body=body,
                            content_type=content_type,
                            occurred_at=str(item.get("timestamp") or "") or None,
                        )
                    )
                for item in value.get("statuses", []):
                    if not isinstance(item, dict):
                        continue
                    external_id = str(item.get("id") or "")
                    status = item.get("status")
                    if not external_id or status is None:
                        continue
                    marker = item.get("timestamp") or stable_event_suffix(item)
                    events.append(
                        ChannelEvent(
                            provider="meta",
                            account_key=account_key,
                            event_key=f"status:{external_id}:{status}:{marker}",
                            event_type="status",
                            external_message_id=external_id,
                            payload=item,
                            delivery_status=status,
                            occurred_at=str(item.get("timestamp") or "") or None,
                        )
                    )
        return events

    def sync_templates(self) -> list[dict[str, Any]]:
        business_account_id = (
            self.account.business_account_id or settings.meta_business_account_id
        )
        if not business_account_id:
            raise ChannelProviderError(
                "Meta business account ID is required for template sync",
                code="provider_not_configured",
            )
        page = self._request(
            "GET",
            f"{business_account_id}/message_templates",
            params={
            "fields": (
                "id,name,language,status,category,components,parameter_format,"
                "quality_score,rejected_reason"
            ),
            "limit": 100,
            },
        )
        templates: list[dict[str, Any]] = []
        for _ in range(20):
            templates.extend(item for item in page.get("data", []) if isinstance(item, dict))
            next_url = (page.get("paging") or {}).get("next")
            if not next_url:
                break
            try:
                response = httpx.get(
                    str(next_url),
                    headers={"Authorization": f"Bearer {settings.meta_access_token}"},
                    timeout=20,
                )
                response.raise_for_status()
                page = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise ChannelProviderError(
                    "Meta template pagination failed",
                    code="template_sync_failed",
                    retryable=True,
                ) from exc
            if not isinstance(page, dict):
                break
        return templates

    def connection_status(self) -> dict[str, Any]:
        configured = bool(
            settings.meta_access_token
            and (self.account.phone_number_id or settings.meta_phone_number_id)
            and settings.meta_graph_version
        )
        return {
            "provider": "meta",
            "configured": configured,
            "state": "configured" if configured else "not_configured",
        }
