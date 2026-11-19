"""THE CONNECTION HISTORY of this employee (14.27.0, 17/10/2026): who connected, changed the account, authorized,
activated or disconnected each connector, and when.

Why: a customer asked who disconnected their employee's Odoo and with which Odoo user it had been connected, and
nobody could say — this app only logged «POST …/disconnect» (no person), pod logs rotate within a day or two and
Composio deletes the account on disconnect.

Every such action is recorded IN THE PLATFORM (`POST /backend/agents/{AGENT_ID}/connection-events`, table
`agent_connection_events`), with the person of THIS app's session (`actor`: the platform only sees our service
secret) and the NON-secret data of the account (address, database, user — never keys or tokens). It is also logged
here with the person. Recording never breaks the action: if the platform cannot be reached, the log line stays.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx

from app.config import AGENT_ID, BACKEND_API_SECRET, WAI_BACKEND_URL

logger = logging.getLogger(__name__)

#: The account fields that are NOT secret (the platform keeps only these too).
ACCOUNT_FIELDS = ("url", "base_url", "instance", "host", "db", "database", "login", "username", "user", "email",
                  "account", "connected_account_id")


def account_of(credentials: Optional[Dict[str, Any]], **extra: Any) -> Dict[str, str]:
    """The non-secret part of what identifies an account: from the credentials typed in (never the key) plus `extra`
    (e.g. the connected account id)."""
    fuera = {k: str(v) for k, v in (credentials or {}).items() if k in ACCOUNT_FIELDS and v not in (None, "")}
    fuera.update({k: str(v) for k, v in extra.items() if k in ACCOUNT_FIELDS and v not in (None, "")})
    return fuera


def _url() -> str:
    return f"{WAI_BACKEND_URL.rstrip('/')}/backend/agents/{int(AGENT_ID)}/connection-events"


async def record(connector: str, action: str, actor: str, account: Optional[Dict[str, str]] = None,
                 result: str = "ok", detail: Optional[str] = None) -> None:
    """Record one connection event (`action`: connect, change_account, authorize, activate, disconnect). Never
    raises."""
    logger.info("connector %s: %s by %s (%s)%s", connector, action, actor or "?", result,
                f" account={account}" if account else "")
    if not (WAI_BACKEND_URL and BACKEND_API_SECRET and AGENT_ID):
        return
    body = {"connector": connector, "action": action, "actor": actor, "account": account or {},
            "result": result, **({"detail": detail[:2000]} if detail else {})}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(_url(), headers={"X-API-Secret": BACKEND_API_SECRET}, json=body)
        if resp.status_code // 100 != 2:
            logger.warning("connection event not recorded in the platform (HTTP %s): %s", resp.status_code,
                           resp.text[:200])
    except Exception as e:  # noqa: BLE001 — recording never breaks the action
        logger.warning("connection event not recorded in the platform: %s", e)


async def history(connector: Optional[str] = None, limit: int = 100) -> Dict[str, Any]:
    """The history kept by the platform, latest first. `{events: [...], loaded}` (`loaded` False: unreachable)."""
    if not (WAI_BACKEND_URL and BACKEND_API_SECRET and AGENT_ID):
        return {"events": [], "loaded": False}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(_url(), headers={"X-API-Secret": BACKEND_API_SECRET},
                                    params={"limit": limit, **({"connector": connector} if connector else {})})
        if resp.status_code // 100 == 2:
            return {"events": (resp.json() or {}).get("events") or [], "loaded": True}
        logger.warning("connection history HTTP %s: %s", resp.status_code, resp.text[:200])
    except Exception as e:  # noqa: BLE001
        logger.warning("connection history unreachable: %s", e)
    return {"events": [], "loaded": False}
