from __future__ import annotations

from typing import Any


INTERNAL_CONTACT_ATTRIBUTE_PREFIX = "_agentdesk_"
EVOLUTION_RECIPIENT_JID_KEY = "_agentdesk_evolution_recipient_jid"


def public_contact_attributes(value: dict[str, Any] | None) -> dict[str, Any]:
    return {
        key: item
        for key, item in (value or {}).items()
        if not key.startswith(INTERNAL_CONTACT_ATTRIBUTE_PREFIX)
    }


def merge_public_contact_attributes(
    current: dict[str, Any] | None,
    incoming: dict[str, Any] | None,
) -> dict[str, Any]:
    internal = {
        key: item
        for key, item in (current or {}).items()
        if key.startswith(INTERNAL_CONTACT_ATTRIBUTE_PREFIX)
    }
    public = public_contact_attributes(incoming)
    return {**public, **internal}
