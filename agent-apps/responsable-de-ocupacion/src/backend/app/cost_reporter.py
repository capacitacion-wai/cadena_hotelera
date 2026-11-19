"""Tenant cost / credit accounting for autonomous agent apps.

Every agent execution — interactive chat, channel message, event
trigger or scheduled task — must land in the tenant's billing ledger
the SAME way the WAI *code agent* does. The code agent posts each turn
to ``genai-api`` ``POST /genai-api/agent-costs`` which writes a row in
``public.agent_cost_log``; the platform ``/usage`` page then converts
that USD into **agent credits** via ``message_limits.agent_cost_per_credit``
(cluster default ``$0.10 = 1 credit``) and counts it against the tenant
monthly limit.

Reference implementation we mirror here:
``genai-chain-sql/opencode_agent/background_runner._post_agent_cost``.

Design notes
------------
* **Fire-and-forget**: a billing failure must NEVER break a run. Every
  call is wrapped so exceptions are swallowed (logged at debug).
* **No-op when unconfigured**: if the deployer didn't inject
  ``GENAI_API_REST_URL`` (local dev / older deploys) we simply skip the
  POST — local ``agent_runs.cost_usd`` is still persisted by the caller.
* **Zero-cost runs are skipped** (matches the code agent): a run that
  reported no spend doesn't pollute the ledger.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from app.config import (
    AGENT_COST_PER_CREDIT,
    GENAI_API_REST_URL,
    PROJECT_ID,
)

logger = logging.getLogger("agent.cost")

# Same endpoint the code agent posts to (served under the genai-api
# ``/genai-api`` prefix). Kept in one place so a future rename is a
# single-line change.
_AGENT_COSTS_ENDPOINT = "/genai-api/agent-costs"


def usd_to_credits(cost_usd: Optional[float]) -> float:
    """Convert a USD amount to agent credits using the tenant rate.

    ``credits = cost_usd / agent_cost_per_credit`` — identical to the
    platform ``UsageStatsService._agent_credits_for_period`` so the
    figure the app shows matches the WAI ``/usage`` page.
    """
    try:
        usd = float(cost_usd or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if usd <= 0 or AGENT_COST_PER_CREDIT <= 0:
        return 0.0
    return usd / AGENT_COST_PER_CREDIT


def _coerce_usd(cost_usd) -> float:
    try:
        return float(cost_usd or 0.0)
    except (TypeError, ValueError):
        return 0.0


# A 4xx (other than 429) is a permanent reject — the row is malformed for
# the ledger, so retrying it forever is pointless. Network errors, timeouts,
# 5xx and 429 are transient: buffer and retry.
def _is_transient_status(status: int) -> bool:
    return status == 429 or status >= 500


async def _post_once(payload: dict, *, timeout: float = 10.0) -> tuple[bool, str]:
    """POST one cost row. Returns ``(ok, reason)``. ``ok`` False with an
    empty reason means a PERMANENT reject (don't buffer); a non-empty reason
    means TRANSIENT (buffer + retry)."""
    url = f"{GENAI_API_REST_URL.rstrip('/')}{_AGENT_COSTS_ENDPOINT}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload)
    except Exception as exc:  # network / timeout — transient
        return False, f"network: {exc!r}"[:300]
    if resp.status_code >= 400:
        if _is_transient_status(resp.status_code):
            return False, f"http {resp.status_code}: {resp.text[:200]}"
        logger.debug(
            "cost report permanently rejected (%s): %s",
            resp.status_code, resp.text[:300],
        )
        return False, ""  # permanent — caller must NOT buffer
    return True, ""


async def _enqueue_pending(payload: dict, error: str) -> None:
    """Stash a failed cost row for later retry. Best-effort."""
    import json as _json
    try:
        from app.app_database import app_write
        from app.app_models.runtime import PendingCostReport

        async with app_write() as s:
            s.add(PendingCostReport(
                payload_json=_json.dumps(payload, ensure_ascii=False),
                attempts=1,
                last_error=error or None,
            ))
            await s.commit()
        logger.info("cost report buffered for retry (%s)", error[:120])
    except Exception:  # pragma: no cover — billing must never break a run
        logger.debug("could not buffer cost report", exc_info=True)


async def flush_pending_cost_reports(limit: int = 25) -> int:
    """Retry buffered cost rows. Returns how many were successfully sent.
    Drops permanently-rejected rows. Safe + best-effort; never raises."""
    import json as _json
    from datetime import datetime

    if not GENAI_API_REST_URL:
        return 0
    sent = 0
    try:
        from sqlalchemy import delete as _delete, select as _select, update as _update
        from app.app_database import app_session, app_write
        from app.app_models.runtime import PendingCostReport

        async with app_session() as s:
            rows = list((await s.execute(
                _select(PendingCostReport)
                .order_by(PendingCostReport.created_at.asc())
                .limit(limit)
            )).scalars().all())
        for row in rows:
            try:
                payload = _json.loads(row.payload_json)
            except Exception:
                payload = None
            if not isinstance(payload, dict):
                # v7.158.0 — a WRITE with `app_write`, like the other two branches of this
                # same function. It used `app_session` (a READ session), skipping the lock
                # that serializes SQLite's single writer. It is exactly the pattern that
                # caused the employee's "database is locked": 8 workers, the poller and the
                # recovery writing at the same time.
                async with app_write() as s:  # corrupt — drop it
                    await s.execute(_delete(PendingCostReport).where(
                        PendingCostReport.id == row.id))
                    await s.commit()
                continue
            ok, reason = await _post_once(payload)
            if ok or not reason:  # success OR permanent reject ⇒ remove
                async with app_write() as s:
                    await s.execute(_delete(PendingCostReport).where(
                        PendingCostReport.id == row.id))
                    await s.commit()
                if ok:
                    sent += 1
            else:  # still transient — bump attempt, keep for next drain
                async with app_write() as s:
                    await s.execute(
                        _update(PendingCostReport)
                        .where(PendingCostReport.id == row.id)
                        .values(
                            attempts=PendingCostReport.attempts + 1,
                            last_error=reason[:300],
                            last_attempt_at=datetime.utcnow(),
                        )
                    )
                    await s.commit()
    except Exception:  # pragma: no cover — billing must never break a run
        logger.debug("flush_pending_cost_reports failed", exc_info=True)
    return sent


async def report_run_cost(
    *,
    session_id: Optional[str],
    user_id: Optional[str],
    cost_usd,
    tokens_input: int = 0,
    tokens_output: int = 0,
    model_name: Optional[str] = None,
    project_id: Optional[int] = None,
    activity: Optional[str] = None,
) -> None:
    """Register one execution's cost in the tenant ledger.

    Mirrors the code agent's ``_post_agent_cost``: a single row per
    execution (chat turn / autonomous run) carrying the aggregated cost
    and tokens. Safe to ``await`` from any finaliser — it never raises.

    On a transient failure (network / 5xx / 429) the row is buffered to
    ``pending_cost_reports`` and retried opportunistically on the next
    successful report, so a brief genai-api outage doesn't drop spend.
    """
    usd = _coerce_usd(cost_usd)
    if not GENAI_API_REST_URL or usd <= 0:
        return
    if not user_id:
        # Billing requires a subject; without it the row is useless and
        # the platform NOT NULL constraint would reject it anyway.
        logger.debug("skipping cost report: no user_id (cost=$%.6f)", usd)
        return

    pid = project_id if project_id is not None else (PROJECT_ID or None)
    payload = {
        "session_id": session_id or "agent-app",
        "user_id": user_id,
        "project_id": pid,
        "cost_usd": usd,
        "tokens_input": int(tokens_input or 0),
        "tokens_output": int(tokens_output or 0),
        "model_name": model_name or None,
        # 7.153.0 — WHAT the employee was doing when it spent this. Without this label,
        # the maintenance of its memory got mixed with the work and there was no way to
        # see whether "tidying up what was learned" pays off — which is exactly what exposed
        # 14 paid consolidation rounds that merged nothing. The ledger ignores it if unknown.
        "activity": (activity or None),
    }
    # HOT WARNING: this does NETWORK I/O. If someone calls it with the write lock held,
    # every other writer waits for the POST to finish — and, measured, a slow POST is
    # 15 s during which the employee has no pool, the probe stops answering, the pod
    # leaves the Service and nginx returns 503. INC-AGO-101 exposed it, failing on that
    # 503 while having nothing to do with what it measures. It is not silently fixed
    # here —that would HIDE the fault—: it is REPORTED in the log, with the call site,
    # so the day someone repeats it, it shows.
    try:
        from app.app_database import _in_write
        if _in_write.get():
            import traceback
            pila = traceback.format_stack(limit=4)[:-1]
            quien = (pila[-1].strip().split("\n")[0] if pila else "?")
            logger.warning(
                "report_run_cost llamado DENTRO del candado de escritura (%s): hace "
                "red, as\u00ed que todos los dem\u00e1s escritores esperan detr\u00e1s. "
                "Ll\u00e1malo DESPU\u00c9S de cerrar el `async with app_write(...)`.",
                quien[:200])
    except Exception:  # pragma: no cover — a warning must never break billing
        pass
    ok, reason = await _post_once(payload)
    if ok:
        logger.info(
            "registered agent cost $%.6f (%.2f credits) user=%s project=%s",
            usd, usd_to_credits(usd), user_id, pid,
        )
        # Opportunistic drain: a working endpoint is the best moment to
        # retry anything that piled up during an outage.
        try:
            await flush_pending_cost_reports()
        except Exception:  # pragma: no cover
            pass
    elif reason:  # transient — don't lose the spend
        await _enqueue_pending(payload, reason)
