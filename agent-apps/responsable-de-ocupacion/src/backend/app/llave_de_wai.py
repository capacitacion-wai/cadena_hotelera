"""INVARIANT — the employee's WAI key renews itself (12/11/2026).

The app calls the tenant's engine for its Composio connectors with ``WAI_AGENT_RUNTIME_JWT``: a key the platform signs
at deploy time, valid 30 days. Nothing renewed it, so a month after the last deploy every Composio operation answered
401 (the platform no longer recognised the employee) and the only way out was a redeploy.

Now, while it is still valid and has less than ``RENEW_DAYS`` left, it is exchanged at the engine for the same key with
30 more days (``POST /opencode/empleado/llave/renovar``). The renewed one is kept next to the app's database (the
Secret keeps the deploy-time one) and wins while it is newer. A failed renewal is retried after ``RETRY_S``, never on
every call.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx

from app.config import APP_DB_PATH

logger = logging.getLogger(__name__)

KEPT_PATH = Path(os.environ.get("WAI_KEY_KEPT_PATH") or Path(APP_DB_PATH).parent / "wai-key.json")
RENEW_DAYS = float(os.environ.get("WAI_KEY_RENEW_DAYS", "10"))
RETRY_S = 600
_lock = asyncio.Lock()
_last_try = 0.0


def claims(token: str) -> dict[str, Any] | None:
    """The payload WITHOUT verifying it (only dates and owner; the engine verifies)."""
    try:
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except Exception:  # noqa: BLE001 — not a JWT
        return None


def _kept(base: dict[str, Any]) -> str | None:
    try:
        token = json.loads(KEPT_PATH.read_text(encoding="utf-8")).get("llave")
    except Exception:  # noqa: BLE001 — none kept yet
        return None
    c = claims(token or "")
    if c and c.get("sub") == base.get("sub") and c.get("agent_id") == base.get("agent_id") \
            and c.get("exp", 0) > base.get("exp", 0) and c["exp"] > time.time():
        return token
    return None


def _keep(token: str) -> None:
    KEPT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = KEPT_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"llave": token}), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, KEPT_PATH)


async def llave(base: str, engine_url: str) -> str:
    """The newest valid WAI key of this employee (``base`` = the deploy-time one), renewing it first if it is about
    to expire. Not a JWT (or no engine): ``base`` as is."""
    global _last_try
    token = base
    c = claims(token)
    if not c or not engine_url:
        return token
    kept = _kept(c)
    if kept:
        token, c = kept, claims(kept) or c
    now = time.time()
    if not (now < c.get("exp", 0) < now + RENEW_DAYS * 86400) or now - _last_try < RETRY_S:
        return token
    async with _lock:
        if now - _last_try < RETRY_S:
            return token
        _last_try = now
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.post(f"{engine_url.rstrip('/')}/opencode/empleado/llave/renovar",
                                      headers={"Authorization": f"Bearer {token}"})
            new = (r.json() or {}).get("llave") if r.status_code == 200 else None
        except Exception as exc:  # noqa: BLE001 — an older engine or a blip: the current key still works
            logger.warning("wai key renewal failed: %s", exc)
            return token
        nc = claims(new or "")
        if nc and nc.get("exp", 0) > c.get("exp", 0):
            _keep(new)
            logger.info("wai key renewed until %s", time.strftime("%Y-%m-%d", time.gmtime(nc["exp"])))
            return new
        logger.warning("wai key renewal refused (%s)", r.status_code)
        return token
