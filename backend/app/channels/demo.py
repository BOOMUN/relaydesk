from __future__ import annotations

import secrets
from collections.abc import Mapping
from typing import Any

from .base import ChannelProvider
from .types import ChannelEvent, OutboundMessage, SendResult


class DemoChannelProvider(ChannelProvider):
    provider_name = "demo"

    def send(self, message: OutboundMessage) -> SendResult:
        return SendResult(
            provider=self.provider_name,
            external_message_id=f"demo-{secrets.token_hex(8)}",
            status="sent",
            raw={"kind": message.kind, "idempotency_key": message.idempotency_key},
        )

    def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str]) -> bool:
        del raw_body, headers
        return True

    def parse_webhook(self, payload: dict[str, Any]) -> list[ChannelEvent]:
        del payload
        return []

    def connection_status(self) -> dict[str, Any]:
        return {"provider": "demo", "configured": False, "state": "demo"}
