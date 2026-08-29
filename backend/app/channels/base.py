from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any

from ..models import ChannelAccount
from .types import ChannelEvent, OutboundMessage, SendResult


class ChannelProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "provider_error",
        retryable: bool = False,
        status_code: int | None = None,
    ) -> None:
        self.code = code
        self.retryable = retryable
        self.status_code = status_code
        super().__init__(message)


class ChannelCapabilityError(ChannelProviderError):
    def __init__(self, capability: str) -> None:
        super().__init__(
            f"Channel provider does not support {capability}",
            code="unsupported_capability",
            retryable=False,
        )


class ChannelProvider(ABC):
    provider_name: str

    def __init__(self, account: ChannelAccount) -> None:
        self.account = account

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset(self.account.capabilities or [])

    @abstractmethod
    def send(self, message: OutboundMessage) -> SendResult:
        raise NotImplementedError

    @abstractmethod
    def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str]) -> bool:
        raise NotImplementedError

    @abstractmethod
    def parse_webhook(self, payload: dict[str, Any]) -> list[ChannelEvent]:
        raise NotImplementedError

    def sync_templates(self) -> list[dict[str, Any]]:
        raise ChannelCapabilityError("template_sync")

    def delivery_statuses(self, external_message_id: str) -> list[object]:
        del external_message_id
        raise ChannelCapabilityError("delivery_reconcile")

    def connection_status(self) -> dict[str, Any]:
        return {
            "provider": self.provider_name,
            "configured": True,
            "state": self.account.connection_state,
        }
