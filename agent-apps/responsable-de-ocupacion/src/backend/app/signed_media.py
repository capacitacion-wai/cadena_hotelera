"""Short-lived signed URLs for OUTBOUND channel media.

WhatsApp via Twilio can only attach media by a **publicly fetchable URL**
(``MediaUrl``) — it has no bytes-upload like the Cloud API. The agent's
deliverables live on the read-only workspace mount with no public URL. So we
mint a **short-lived, HMAC-signed, single-file** link served by the app's
existing public ingress (see ``routes.channel_media_routes``); Twilio fetches
it within seconds and the token then expires.

Security: the token carries an expiry, binds exactly ONE ``workspace_path``
and is signed with a key DERIVED from ``AGENT_SHARED_TOKEN`` — the per-app
secret this pod already has (it talks Basic to its sister agent pod with it).
The serving route additionally confines the path with ``realpath`` so it can
never escape the workspace.

Why that key (14.0.0): links used to be signed with the tenant's JWT key, which the pod no
longer receives (it could be used to mint anyone's token). ``AGENT_SHARED_TOKEN`` belongs
to THIS app and nobody else; it is derived with HMAC and a label of its own so that a file
link never works as the Basic credential nor the other way round. When the key changes,
links issued by the previous version stop working ONCE — they live 10 minutes and Twilio
fetches them within seconds, so it only affects a send in flight during the deployment.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Optional

from app.config import AGENT_SHARED_TOKEN

logger = logging.getLogger(__name__)

# Twilio fetches the MediaUrl almost immediately; keep the window tight.
DEFAULT_TTL_SEC = 600  # 10 min


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


_ETIQUETA_LLAVE = b"wai-media-v1"


def _secret() -> bytes:
    """Signing key for the links. Without ``AGENT_SHARED_TOKEN`` (local development) the
    key would be the one of an empty string, i.e. public: it refuses instead of signing with it."""
    base = (AGENT_SHARED_TOKEN or "").encode("utf-8")
    if not base:
        raise RuntimeError("AGENT_SHARED_TOKEN vacío: no se pueden firmar enlaces de ficheros")
    return hmac.new(base, _ETIQUETA_LLAVE, hashlib.sha256).digest()


def sign_media_token(workspace_path: str, ttl_sec: int = DEFAULT_TTL_SEC) -> str:
    """Return a signed token granting time-limited read of ONE workspace
    file. ``workspace_path`` is relative to ``/workspace``."""
    payload = {
        "p": (workspace_path or "").lstrip("/"),
        "exp": int(time.time()) + max(int(ttl_sec or DEFAULT_TTL_SEC), 30),
    }
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = _b64e(hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_media_token(token: str) -> Optional[str]:
    """Validate a token and return its ``workspace_path``, or ``None`` when
    malformed / tampered / expired. Never raises."""
    try:
        body, _, sig = (token or "").partition(".")
        if not body or not sig:
            return None
        expected = _b64e(
            hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(_b64d(body))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        path = str(payload.get("p") or "").lstrip("/")
        if not path or ".." in path.split("/"):
            return None
        return path
    except Exception:  # pragma: no cover — any parse error = invalid token
        return None


def public_base_url(channel=None) -> str:
    """Resolve the app's public base URL used to build signed media links.

    Order: per-channel override (``reply_config.public_base_url``) → the
    deployer-injected ``APP_PUBLIC_BASE_URL`` env. Returns ``""`` when
    neither is set (callers then fall back to naming the file in the body)."""
    if channel is not None:
        try:
            import json as _json
            cfg = _json.loads(getattr(channel, "reply_config", "") or "{}")
            override = str(cfg.get("public_base_url") or "").strip()
            if override:
                return override.rstrip("/")
        except Exception:
            pass
    from app.config import APP_PUBLIC_BASE_URL
    return (APP_PUBLIC_BASE_URL or "").rstrip("/")


def signed_media_url(workspace_path: str, channel=None,
                     ttl_sec: int = DEFAULT_TTL_SEC) -> Optional[str]:
    """Full public URL for Twilio to fetch a workspace file, or ``None`` when
    no public base URL is configured."""
    base = public_base_url(channel)
    if not base:
        return None
    if not AGENT_SHARED_TOKEN:
        # No key, no link (see `_secret`): the file is named in the body, same as when the
        # public URL is missing. It does not happen in a deployed pod: it is always injected.
        logger.warning("AGENT_SHARED_TOKEN vacío: el adjunto %s va nombrado, sin enlace",
                       workspace_path)
        return None
    token = sign_media_token(workspace_path, ttl_sec)
    return f"{base}/api/channels/media/{token}"
