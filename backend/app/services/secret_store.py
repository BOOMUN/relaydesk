from __future__ import annotations

import hashlib

from cryptography.fernet import Fernet, InvalidToken

from ..config import settings


class SecretStoreError(RuntimeError):
    pass


def _cipher() -> Fernet:
    raw = str(settings.secrets_encryption_key or "").strip()
    if not raw:
        raise SecretStoreError(
            "AGENTDESK_SECRETS_ENCRYPTION_KEY is required before saving credentials"
        )
    try:
        return Fernet(raw.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise SecretStoreError(
            "AGENTDESK_SECRETS_ENCRYPTION_KEY is not a valid Fernet key"
        ) from exc


def encrypt_secret(value: str) -> tuple[str, str]:
    normalized = str(value).strip()
    if not normalized:
        raise SecretStoreError("Credential cannot be empty")
    ciphertext = _cipher().encrypt(normalized.encode("utf-8")).decode("ascii")
    fingerprint = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return ciphertext, fingerprint


def decrypt_secret(ciphertext: str) -> str:
    try:
        return _cipher().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, UnicodeEncodeError) as exc:
        raise SecretStoreError(
            "Credential cannot be decrypted with the configured key"
        ) from exc


__all__ = ["SecretStoreError", "decrypt_secret", "encrypt_secret"]
