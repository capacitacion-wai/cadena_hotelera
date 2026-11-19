"""GET /api/dashboard/summary — business KPI feed for the home page.

Driven by ``agent_app.business_kpis`` and ``agent_app.trend_charts``
in ``wai-app.json``. Each entry runs against EITHER:

* the project (read-only) **Postgres** — the default (``source`` absent
  or ``"project"``). SQL MUST be PostgreSQL dialect
  (``NOW() - INTERVAL '1 day'``, ``date_trunc('day', …)``).
* the app's own writable **SQLite** store (``source: "local"``) — for
  KPIs that measure the agent's OWN output (open alerts, pending
  recommendations, escalated decisions, replied channel emails…). SQL
  MUST be SQLite dialect (``datetime('now','-1 day')``, ``date(col)``)
  and reference local tables (``alerts``, ``recommendations``,
  ``communications``, ``decisions``, ``scoring``, ``channel_messages``,
  ``agent_runs``, ``events`` …).

Failures are isolated per KPI — one bad query yields a
``status="error"`` card instead of a 500."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.app_database import app_session
# 14.18.0 — WHAT A MEMBER SEES. A member USES the employee (chat, manual, its own encargos);
# it does not operate it. These screens expose everyone's work (1,125 conversations of the
# whole team on the Panel of a member, 04/10/2026), so the door is HERE and the menu only
# reflects it.
from app.auth import TokenPayload, require_admin, require_project_access
from app.config import APP_MANIFEST
from app.database import async_session

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


def _is_local(item: dict) -> bool:
    """A KPI/trend runs against the LOCAL SQLite store when it explicitly
    declares ``source: "local"`` — these measure the agent's own output
    tables, which never exist in the read-only project Postgres."""
    return (item.get("source") or "project") == "local"


def _kpis() -> list[dict]:
    return APP_MANIFEST.get("agent_app", {}).get("business_kpis", []) or []


def _trends() -> list[dict]:
    return APP_MANIFEST.get("agent_app", {}).get("trend_charts", []) or []


async def _run_scalar(s: AsyncSession, sql: str) -> Any:
    res = await s.execute(text(sql))
    row = res.first()
    if row is None:
        return None
    if hasattr(row, "value"):
        return row.value
    return list(row)[0] if len(row) > 0 else None


async def _run_rows(s: AsyncSession, sql: str) -> list[dict]:
    res = await s.execute(text(sql))
    return [dict(r._mapping) for r in res.all()]  # noqa: SLF001


@router.get("/summary")
async def summary(_: TokenPayload = Depends(require_admin)):
    kpi_results: list[dict] = []
    chart_results: list[dict] = []

    # Two backends: read-only project Postgres (default) and the app's
    # writable SQLite store (``source: "local"``). Open both lazily so a
    # dashboard with only-project or only-local items still works.
    async with async_session() as proj_s, app_session() as local_s:
        def _session_for(item: dict) -> AsyncSession:
            return local_s if _is_local(item) else proj_s

        for kpi in _kpis():
            entry = {
                "id": kpi.get("id"),
                "label": kpi.get("label"),
                # Localised variants — when present, the frontend's
                # ``usePickLabel`` picks the right one based on the
                # operator's UI language. Falls back to ``label``.
                "label_en": kpi.get("label_en"),
                "labels": kpi.get("labels"),
                # Business explanation (why this metric matters). May be a
                # plain string, ``description``/``description_en`` pair, or
                # a ``descriptions: {es,en}`` dict — the SPA resolves it to
                # the active language for the Welcome ("what I optimise").
                "description": kpi.get("description"),
                "description_en": kpi.get("description_en"),
                "descriptions": kpi.get("descriptions"),
                "format": kpi.get("format", "number"),
                "good_when": kpi.get("good_when", "higher"),
                "target": kpi.get("target"),
                "value": None,
                "status": "ok",
            }
            sess = _session_for(kpi)
            try:
                entry["value"] = await _run_scalar(sess, kpi["sql"])
            except Exception as exc:  # noqa: BLE001
                logger.warning("kpi %s failed: %s", kpi.get("id"), exc)
                entry["status"] = "error"
                entry["error"] = str(exc)
                # Reset the aborted transaction so a single bad query does
                # not poison every later KPI sharing this session with
                # ``current transaction is aborted`` (asyncpg).
                try:
                    await sess.rollback()
                except Exception:  # noqa: BLE001
                    pass
            kpi_results.append(entry)

        for chart in _trends():
            entry: dict[str, Any] = {
                "id": chart.get("id"),
                "label": chart.get("label"),
                "label_en": chart.get("label_en"),
                "labels": chart.get("labels"),
                "kind": chart.get("kind", "line"),
                "x": chart.get("x"),
                "y": chart.get("y"),
                "format": chart.get("format", "number"),
                "data": [],
                "status": "ok",
            }
            sess = _session_for(chart)
            try:
                entry["data"] = await _run_rows(sess, chart["sql"])
            except Exception as exc:  # noqa: BLE001
                logger.warning("chart %s failed: %s", chart.get("id"), exc)
                entry["status"] = "error"
                entry["error"] = str(exc)
                try:
                    await sess.rollback()
                except Exception:  # noqa: BLE001
                    pass
            chart_results.append(entry)

    return {
        "kpis": kpi_results,
        "trends": chart_results,
    }
