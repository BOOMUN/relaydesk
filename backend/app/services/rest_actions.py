from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
import ipaddress
import json
import re
import socket
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import httpx

from ..models import RestActionEndpoint
from .secret_store import SecretStoreError, decrypt_secret


MAX_RESPONSE_BYTES = 64 * 1024
ALLOWED_RESPONSE_TYPES = (
    "application/json",
    "application/problem+json",
    "text/plain",
)
SENSITIVE_REST_TERMS = re.compile(
    r"(?:refund|cancel|void|address|recipient|phone|email|password|identity|"
    r"退款|取消|地址|收件|電話|手机|郵箱|邮箱|密碼|密码)",
    re.I,
)


class RestActionSecurityError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "rest_action_security_error",
        retryable: bool = False,
    ) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class PublicOrigin:
    url: str
    hostname: str
    port: int
    addresses: tuple[str, ...]


def _public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    return bool(address.is_global)


def _normalize_hostname(raw: str) -> str:
    try:
        hostname = raw.rstrip(".").encode("idna").decode("ascii").casefold()
    except UnicodeError as exc:
        raise RestActionSecurityError("Invalid endpoint hostname", code="invalid_host") from exc
    if (
        not hostname
        or hostname == "localhost"
        or hostname.endswith((".localhost", ".local", ".internal", ".home", ".lan"))
    ):
        raise RestActionSecurityError("Local endpoint host is forbidden", code="private_host")
    return hostname


def resolve_public_addresses(hostname: str, port: int) -> tuple[str, ...]:
    try:
        records = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise RestActionSecurityError(
            "Endpoint DNS lookup failed", code="dns_resolution_failed", retryable=True
        ) from exc
    addresses = tuple(sorted({str(record[4][0]).split("%", 1)[0] for record in records}))
    if not addresses:
        raise RestActionSecurityError("Endpoint DNS returned no addresses", code="dns_empty")
    if any(not _public_ip(value) for value in addresses):
        raise RestActionSecurityError(
            "Endpoint resolves to a private or non-routable address",
            code="private_address",
        )
    return addresses


def validate_public_origin(value: str, *, resolve_dns: bool = True) -> PublicOrigin:
    parsed = urlsplit(str(value).strip())
    if parsed.scheme.casefold() != "https":
        raise RestActionSecurityError("REST Action endpoints must use HTTPS", code="https_required")
    if parsed.username or parsed.password:
        raise RestActionSecurityError("Endpoint credentials cannot appear in the URL", code="url_credentials_forbidden")
    if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise RestActionSecurityError("Base URL must contain only scheme and host", code="invalid_base_url")
    if not parsed.hostname:
        raise RestActionSecurityError("Endpoint hostname is required", code="invalid_host")
    hostname = _normalize_hostname(parsed.hostname)
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise RestActionSecurityError("Endpoint port is invalid", code="invalid_port") from exc
    if not 1 <= port <= 65535:
        raise RestActionSecurityError("Endpoint port is invalid", code="invalid_port")
    if _public_ip(hostname):
        addresses = (hostname,)
    elif any(character == ":" for character in hostname) or hostname.replace(".", "").isdigit():
        raise RestActionSecurityError("Private or invalid IP endpoint", code="private_address")
    else:
        addresses = resolve_public_addresses(hostname, port) if resolve_dns else ()
    host_text = f"[{hostname}]" if ":" in hostname else hostname
    suffix = "" if port == 443 else f":{port}"
    return PublicOrigin(
        url=f"https://{host_text}{suffix}",
        hostname=hostname,
        port=port,
        addresses=addresses,
    )


def normalize_action_path(value: str, *, allow_wildcard: bool = False) -> str:
    raw = str(value).strip()
    if not raw.startswith("/") or len(raw) > 500:
        raise RestActionSecurityError("Action path must start with /", code="invalid_path")
    decoded = raw
    for _ in range(3):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    if any(token in decoded for token in ("\\", "\x00", "?", "#")):
        raise RestActionSecurityError("Action path contains forbidden characters", code="invalid_path")
    if "://" in decoded or any(part in {".", ".."} for part in decoded.split("/")):
        raise RestActionSecurityError("Action path traversal is forbidden", code="path_traversal")
    if not allow_wildcard and any(character in decoded for character in "*?["):
        raise RestActionSecurityError("Invocation path cannot contain wildcards", code="invalid_path")
    if allow_wildcard and any(character in decoded for character in "?["):
        raise RestActionSecurityError("Only * is supported in approved path patterns", code="invalid_path_pattern")
    return quote(decoded, safe="/!*'();:@&=+$,~.-_")


def method_is_allowed(endpoint: RestActionEndpoint, method: str) -> bool:
    normalized = method.strip().upper()
    return normalized in {str(item).upper() for item in endpoint.allowed_methods or []}


def path_is_allowed(endpoint: RestActionEndpoint, path: str) -> bool:
    normalized_path = normalize_action_path(path)
    pattern = normalize_action_path(endpoint.path_pattern, allow_wildcard=True)
    return fnmatchcase(normalized_path, pattern)


def rest_action_requires_identity(
    endpoint: RestActionEndpoint,
    *,
    path: str,
    json_body: dict[str, Any] | list[Any] | None,
) -> bool:
    """Return the shared identity prerequisite for a configured REST Action."""

    material = f"{path}\n{json.dumps(json_body, ensure_ascii=False, default=str)}"
    return bool(endpoint.requires_identity_verification or SENSITIVE_REST_TERMS.search(material))


def _verify_connected_address(response: httpx.Response) -> None:
    stream = response.extensions.get("network_stream")
    if stream is None or not hasattr(stream, "get_extra_info"):
        return
    address = stream.get_extra_info("server_addr")
    if isinstance(address, (tuple, list)) and address:
        address = address[0]
    if address and not _public_ip(str(address)):
        raise RestActionSecurityError(
            "Connected address is private or non-routable",
            code="dns_rebinding_blocked",
        )


def execute_rest_action(
    endpoint: RestActionEndpoint,
    *,
    method: str,
    path: str,
    query: dict[str, str | int | float | bool | None],
    json_body: dict[str, Any] | list[Any] | None,
) -> dict[str, Any]:
    if endpoint.status != "approved" or endpoint.approved_at is None:
        raise RestActionSecurityError("REST Action endpoint is not approved", code="endpoint_not_approved")
    normalized_method = method.strip().upper()
    if not method_is_allowed(endpoint, normalized_method):
        raise RestActionSecurityError("HTTP method is not approved", code="method_not_approved")
    normalized_path = normalize_action_path(path)
    if not path_is_allowed(endpoint, normalized_path):
        raise RestActionSecurityError("Request path is not approved", code="path_not_approved")
    origin = validate_public_origin(endpoint.base_url, resolve_dns=True)
    headers = {"Accept": "application/json, text/plain;q=0.8"}
    if endpoint.secret_ciphertext:
        if not endpoint.secret_header_name:
            raise RestActionSecurityError("Credential header is missing", code="credential_configuration_error")
        try:
            headers[endpoint.secret_header_name] = decrypt_secret(endpoint.secret_ciphertext)
        except SecretStoreError as exc:
            raise RestActionSecurityError(str(exc), code="credential_decryption_failed") from exc
    url = f"{origin.url}{normalized_path}"
    try:
        with httpx.Client(
            timeout=httpx.Timeout(float(endpoint.timeout_seconds), connect=min(5.0, float(endpoint.timeout_seconds))),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            with client.stream(
                normalized_method,
                url,
                params=query or None,
                json=json_body,
                headers=headers,
            ) as response:
                _verify_connected_address(response)
                if 300 <= response.status_code < 400:
                    raise RestActionSecurityError(
                        "REST Action redirects are forbidden",
                        code="redirect_forbidden",
                    )
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
                if content_type and not any(
                    content_type == item or content_type.endswith("+json")
                    for item in ALLOWED_RESPONSE_TYPES
                ):
                    raise RestActionSecurityError(
                        "REST Action response type is not allowed",
                        code="response_type_forbidden",
                    )
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise RestActionSecurityError(
                            "REST Action response exceeds 64 KiB",
                            code="response_too_large",
                        )
                text = bytes(body).decode(response.encoding or "utf-8", errors="replace")
                parsed_body: Any = text
                if content_type.endswith("json") or content_type.endswith("+json"):
                    try:
                        parsed_body = json.loads(text) if text else None
                    except ValueError as exc:
                        raise RestActionSecurityError(
                            "REST Action returned invalid JSON",
                            code="invalid_json_response",
                        ) from exc
                if response.status_code >= 400:
                    raise RestActionSecurityError(
                        f"REST Action returned HTTP {response.status_code}",
                        code="external_http_error",
                        retryable=response.status_code >= 500,
                    )
                return {
                    "endpoint_id": endpoint.id,
                    "status_code": response.status_code,
                    "content_type": content_type or "text/plain",
                    "body": parsed_body,
                }
    except RestActionSecurityError:
        raise
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        raise RestActionSecurityError(
            "REST Action network request failed",
            code="rest_network_error",
            retryable=True,
        ) from exc


__all__ = [
    "PublicOrigin",
    "RestActionSecurityError",
    "execute_rest_action",
    "method_is_allowed",
    "normalize_action_path",
    "path_is_allowed",
    "rest_action_requires_identity",
    "resolve_public_addresses",
    "validate_public_origin",
]
