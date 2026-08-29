from __future__ import annotations

import hashlib
import json
from typing import Any


def stable_event_suffix(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def meta_message_body(item: dict[str, Any]) -> tuple[str, str]:
    message_type = str(item.get("type") or "unsupported")
    if message_type == "text":
        return str(item.get("text", {}).get("body", "")), "text"
    if message_type == "button":
        return str(item.get("button", {}).get("text", "")), "text"
    if message_type == "interactive":
        interactive = item.get("interactive") or {}
        answer = interactive.get("button_reply") or interactive.get("list_reply") or {}
        return str(answer.get("title") or answer.get("id") or ""), "text"
    return f"[{message_type} message]", message_type


def evolution_remote_jid(key: dict[str, Any]) -> str:
    primary = str(key.get("remoteJid") or "")
    alternate = str(key.get("remoteJidAlt") or "")
    if primary.endswith("@lid") and alternate:
        return alternate
    return primary or alternate


def evolution_recipient_jid(key: dict[str, Any]) -> str | None:
    for candidate in (
        str(key.get("remoteJid") or "").strip(),
        str(key.get("remoteJidAlt") or "").strip(),
    ):
        if candidate.endswith(("@lid", "@s.whatsapp.net")):
            return candidate
    return None


def jid_identifier(remote_jid: str) -> str:
    return remote_jid.split("@", 1)[0] if "@" in remote_jid else remote_jid


def matching_evolution_messages(
    value: dict[str, Any] | list[Any],
    external_id: str,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            key = node.get("key") if isinstance(node.get("key"), dict) else {}
            if key.get("id") == external_id or node.get("keyId") == external_id:
                matches.append(node)
                return
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return matches


def evolution_message_body(item: dict[str, Any]) -> tuple[str, str]:
    value = item.get("message") if isinstance(item.get("message"), dict) else {}
    for wrapper_name in (
        "ephemeralMessage",
        "viewOnceMessage",
        "viewOnceMessageV2",
        "documentWithCaptionMessage",
    ):
        wrapper = value.get(wrapper_name)
        if isinstance(wrapper, dict) and isinstance(wrapper.get("message"), dict):
            value = wrapper["message"]
            break
    if isinstance(value.get("conversation"), str):
        return value["conversation"], "text"
    for container_name, field_name in (
        ("extendedTextMessage", "text"),
        ("buttonsResponseMessage", "selectedDisplayText"),
        ("templateButtonReplyMessage", "selectedDisplayText"),
        ("listResponseMessage", "title"),
    ):
        container = value.get(container_name)
        if isinstance(container, dict) and container.get(field_name):
            return str(container[field_name]), "text"
    for container_name, content_type in {
        "imageMessage": "image",
        "videoMessage": "video",
        "audioMessage": "audio",
        "documentMessage": "document",
        "stickerMessage": "sticker",
    }.items():
        container = value.get(container_name)
        if isinstance(container, dict):
            return str(container.get("caption") or f"[{content_type} message]"), content_type
    message_type = str(item.get("messageType") or "unsupported")
    return f"[{message_type} message]", message_type
