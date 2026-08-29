from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


MessageKind = Literal["text", "template", "buttons", "list"]
ChannelEventKind = Literal["message", "status"]


@dataclass(slots=True)
class OutboundMessage:
    to: str
    kind: MessageKind
    text: str = ""
    template_name: str | None = None
    template_language: str | None = None
    template_components: list[dict[str, Any]] = field(default_factory=list)
    interactive: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str = ""


@dataclass(slots=True)
class SendResult:
    provider: str
    external_message_id: str
    status: str = "pending"
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ChannelEvent:
    provider: str
    account_key: str
    event_key: str
    event_type: ChannelEventKind
    external_message_id: str | None
    payload: dict[str, Any]
    sender_id: str | None = None
    sender_address: str | None = None
    display_name: str = "WhatsApp customer"
    body: str = ""
    content_type: str = "text"
    delivery_status: object | None = None
    occurred_at: str | None = None
