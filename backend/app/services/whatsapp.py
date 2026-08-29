from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import quote

import httpx

from ..config import settings


class WhatsAppError(RuntimeError):
    pass


class EvolutionAPIError(WhatsAppError):
    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"Evolution API returned HTTP {status_code}: {message}")


@dataclass(slots=True)
class WhatsAppConnection:
    provider: str
    configured: bool
    state: str
    instance_name: str | None = None
    webhook_url: str | None = None
    qr_code: str | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EvolutionClient:
    webhook_events = [
        "MESSAGES_UPSERT",
        "MESSAGES_UPDATE",
        "SEND_MESSAGE_UPDATE",
        "CONNECTION_UPDATE",
        "QRCODE_UPDATED",
    ]

    def __init__(self) -> None:
        if not settings.evolution_enabled:
            raise WhatsAppError("Evolution API is not fully configured")
        self.base_url = settings.evolution_api_url.rstrip("/")
        self.instance_name = settings.evolution_instance_name
        self.headers = {
            "apikey": str(settings.evolution_api_key),
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout: float = 20,
    ) -> dict[str, Any] | list[Any]:
        try:
            response = httpx.request(
                method,
                f"{self.base_url}{path}",
                headers=self.headers,
                json=payload,
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            raise WhatsAppError("Evolution API is unavailable") from exc
        if response.is_error:
            message = _response_error_message(response)
            raise EvolutionAPIError(response.status_code, message)
        try:
            data = response.json()
        except ValueError as exc:
            raise WhatsAppError("Evolution API returned invalid JSON") from exc
        if not isinstance(data, (dict, list)):
            raise WhatsAppError("Evolution API returned an unexpected response")
        return data

    def connection(self) -> WhatsAppConnection:
        encoded = quote(self.instance_name, safe="")
        try:
            payload = self._request("GET", f"/instance/connectionState/{encoded}")
        except EvolutionAPIError as exc:
            if exc.status_code == 404:
                return WhatsAppConnection(
                    provider="evolution",
                    configured=True,
                    state="not_created",
                    instance_name=self.instance_name,
                    webhook_url=settings.evolution_webhook_url,
                )
            return WhatsAppConnection(
                provider="evolution",
                configured=True,
                state="unavailable",
                instance_name=self.instance_name,
                webhook_url=settings.evolution_webhook_url,
                message=str(exc),
            )
        except WhatsAppError as exc:
            return WhatsAppConnection(
                provider="evolution",
                configured=True,
                state="unavailable",
                instance_name=self.instance_name,
                webhook_url=settings.evolution_webhook_url,
                message=str(exc),
            )
        state = _evolution_state(payload)
        qr_code = None
        if state == "connecting":
            try:
                qr_payload = self._request("GET", f"/instance/connect/{encoded}")
                qr_code = _evolution_qr_code(qr_payload)
            except WhatsAppError:
                pass
        return WhatsAppConnection(
            provider="evolution",
            configured=True,
            state=_normalize_connection_state(state),
            instance_name=self.instance_name,
            webhook_url=settings.evolution_webhook_url,
            qr_code=qr_code,
        )

    def connect(self) -> WhatsAppConnection:
        encoded = quote(self.instance_name, safe="")
        try:
            state_payload = self._request("GET", f"/instance/connectionState/{encoded}")
            state = _evolution_state(state_payload)
        except EvolutionAPIError as exc:
            if exc.status_code != 404:
                raise
            state = None
        if state is None:
            self._request(
                "POST",
                "/instance/create",
                payload={
                    "instanceName": self.instance_name,
                    "integration": "WHATSAPP-BAILEYS",
                    "qrcode": True,
                    "rejectCall": True,
                    "msgCall": "此号码暂不接听 WhatsApp 通话，请发送文字消息。",
                    "groupsIgnore": True,
                    "alwaysOnline": False,
                    "readMessages": False,
                    "readStatus": False,
                    "syncFullHistory": False,
                },
                timeout=30,
            )
        self._configure_webhook()
        payload = self._request("GET", f"/instance/connect/{encoded}", timeout=30)
        qr_code = _evolution_qr_code(payload)
        response_state = _evolution_state(payload) or state
        if qr_code and response_state != "open":
            response_state = "connecting"
        return WhatsAppConnection(
            provider="evolution",
            configured=True,
            state=_normalize_connection_state(response_state),
            instance_name=self.instance_name,
            webhook_url=settings.evolution_webhook_url,
            qr_code=qr_code,
        )

    def _configure_webhook(self) -> None:
        encoded = quote(self.instance_name, safe="")
        self._request(
            "POST",
            f"/webhook/set/{encoded}",
            payload={
                "webhook": {
                    "enabled": True,
                    "url": settings.evolution_webhook_url,
                    "headers": {
                        "X-AgentDesk-Webhook-Secret": settings.evolution_webhook_secret
                    },
                    "byEvents": False,
                    "base64": False,
                    "events": self.webhook_events,
                }
            },
        )

def connect_evolution() -> WhatsAppConnection:
    if settings.whatsapp_provider != "evolution":
        raise WhatsAppError("WhatsApp provider is not set to evolution")
    return EvolutionClient().connect()


def _response_error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:240] or "request failed"
    if isinstance(payload, dict):
        value = payload.get("message") or payload.get("error") or payload.get("response")
        if isinstance(value, list):
            return "; ".join(str(item) for item in value)[:240]
        if value:
            return str(value)[:240]
    return "request failed"


def _evolution_state(payload: dict[str, Any] | list[Any]) -> str | None:
    if not isinstance(payload, dict):
        return None
    instance = payload.get("instance")
    if isinstance(instance, dict):
        state = instance.get("state") or instance.get("status") or instance.get("connectionStatus")
        if state:
            return str(state).lower()
    state = payload.get("state") or payload.get("status") or payload.get("connectionStatus")
    return str(state).lower() if state else None


def _normalize_connection_state(state: str | None) -> str:
    return {
        "open": "connected",
        "connected": "connected",
        "connecting": "connecting",
        "close": "disconnected",
        "closed": "disconnected",
        "disconnected": "disconnected",
        None: "not_created",
    }.get(state, state or "not_created")


def _evolution_qr_code(payload: dict[str, Any] | list[Any]) -> str | None:
    if not isinstance(payload, dict):
        return None
    candidates: list[Any] = [payload.get("base64")]
    qrcode = payload.get("qrcode")
    if isinstance(qrcode, dict):
        candidates.append(qrcode.get("base64"))
    instance = payload.get("instance")
    if isinstance(instance, dict):
        instance_qr = instance.get("qrcode")
        if isinstance(instance_qr, dict):
            candidates.append(instance_qr.get("base64"))
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        value = candidate.strip()
        if value.startswith("data:image/"):
            return value
        return f"data:image/png;base64,{value}"
    return None
