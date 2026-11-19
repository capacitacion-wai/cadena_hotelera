"""Fernet helpers for channel credentials (IMAP passwords, Twilio tokens).

Credentials are stored encrypted in the local SQLite. The key is
provided to the pod through the K8s Secret ``CHANNEL_ENCRYPTION_KEY``.
Without it, no IMAP/WhatsApp channel can be created or read — the SPA
renders "Falta CHANNEL_ENCRYPTION_KEY" and those wizard options are
disabled.

Why Fernet (and not AES-GCM directly):
* ships authenticated encryption out of the box (HMAC-SHA256),
* key rotation requires changing one env var + re-saving channels,
* the urlsafe-b64 token fits cleanly in a SQLite TEXT column.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

from app.config import CHANNEL_ENCRYPTION_KEY


_DERIVED_KEY: bytes | None = None


def _key() -> bytes:
    """Return a 32-byte urlsafe key for Fernet.

    The env value MUST be a Fernet-native key: the urlsafe-b64 encoding
    of 32 random bytes (``Fernet.generate_key().decode()`` — the
    deployer generates exactly this). Short passphrases are rejected:
    deriving a key from a human string via SHA-256 silently reduces the
    effective entropy of every credential stored at rest.

    Generate one locally with::

        python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    """
    global _DERIVED_KEY
    if _DERIVED_KEY is not None:
        return _DERIVED_KEY
    raw = (CHANNEL_ENCRYPTION_KEY or "").strip()
    if not raw:
        raise RuntimeError(
            "CHANNEL_ENCRYPTION_KEY is not configured. The deployer must "
            "mount a K8s Secret with a urlsafe-b64-32-byte Fernet key "
            "(generate with `cryptography.fernet.Fernet.generate_key()`). "
            "IMAP/WhatsApp channels cannot be created until this is set."
        )
    try:
        b = raw.encode("ascii")
        decoded = base64.urlsafe_b64decode(b + b"=" * (-len(b) % 4))
        if len(decoded) == 32:
            _DERIVED_KEY = b if len(b) == 44 else base64.urlsafe_b64encode(decoded)
            return _DERIVED_KEY
    except Exception:
        pass
    raise RuntimeError(
        "CHANNEL_ENCRYPTION_KEY is not a valid Fernet key (expected the "
        "urlsafe-b64 encoding of 32 bytes). Generate one with "
        "`cryptography.fernet.Fernet.generate_key()` — passphrases are "
        "rejected to protect the at-rest credential entropy."
    )


def encrypt_blob(payload: dict[str, Any]) -> str:
    """Encrypt ``payload`` to a Fernet token (str)."""
    from cryptography.fernet import Fernet
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return Fernet(_key()).encrypt(raw).decode("ascii")


def decrypt_blob(token: str) -> dict[str, Any]:
    """Decrypt a Fernet token back to the original dict.

    Raises ``RuntimeError`` when the token is unreadable (key rotated
    without re-saving the channel). The caller (watcher / executor)
    flips ``status='error'`` and waits for the operator.
    """
    from cryptography.fernet import Fernet, InvalidToken
    if not token:
        raise RuntimeError("empty IMAP credential blob")
    try:
        raw = Fernet(_key()).decrypt(token.encode("ascii"))
    except InvalidToken as exc:
        raise RuntimeError(
            "could not decrypt IMAP credentials — most likely "
            "CHANNEL_ENCRYPTION_KEY was rotated without re-saving the "
            "channel. The operator must re-enter the App Password."
        ) from exc
    return json.loads(raw.decode("utf-8"))


def encryption_available() -> bool:
    """Return True when ``CHANNEL_ENCRYPTION_KEY`` is set. Used by the
    routes layer to disable the IMAP wizard option."""
    return bool((CHANNEL_ENCRYPTION_KEY or "").strip())
