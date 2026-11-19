"""Funnel + cost of a standing order over a TIME RANGE (v7.140.0).

The operator's question is not "is it enabled?" but "is it earning its keep?":
how many times did the WHEN trigger, how many of those passed the ONLY IF, how
many actually reacted, how many were held back by anti-repetition, and what has
it cost. Those five numbers over a chosen window are what turns a standing order
from a black box into something you can govern.

Two sources, one per fact — never two paths to the same number:

* the FUNNEL (checks/met/fired/silenced/errors) comes from
  ``automation_eval_log`` for the days still in the ledger and from
  ``automation_daily_stats`` for the older days the prune already removed. The
  seam is the prune horizon, and the response says which part of the window came
  from the summary (``rollup_from``) so a "1 month" figure is honest.
* the COST comes from ``agent_runs`` joined through the ``automation_fired``
  event — the same join the history uses. Autonomous runs are never pruned, so
  cost needs no rollup. A deterministic fire (0 tokens) has no run and therefore
  correctly contributes 0 instead of being invisible.

Everything here is read-only and bounded: aggregates in the DB (COUNT/SUM), no
row-by-row scan, and ``for_automations`` computes the whole list in a fixed
number of queries so a table of 40 standing orders is not 200 round-trips.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import Float, case, cast, func, select

from app.app_database import app_session
from app.timeutil import business_tz, iso_utc

logger = logging.getLogger(__name__)

# The presets the UI offers. ``all`` means "since it exists" (no lower bound).
RANGES = ("1d", "7d", "30d", "90d", "all")


def resolve_range(preset: str | None = None, since: str | None = None,
                  until: str | None = None) -> tuple[datetime | None, datetime | None, str]:
    """Turn a preset (or an explicit custom range) into a naive-UTC window.

    Days are counted in BUSINESS time, because "last 7 days" for an operator
    starts at midnight their time, not at the current UTC clock minus 168h — the
    latter would split yesterday in two and make the numbers unexplainable.
    """
    tz = business_tz()
    now_local = datetime.now(tz)
    if (since or "").strip() or (until or "").strip():
        def _parse(v: str | None, end: bool) -> datetime | None:
            v = (v or "").strip()
            if not v:
                return None
            try:
                d = datetime.fromisoformat(v.replace("Z", ""))
            except ValueError:
                return None
            if d.tzinfo is None:      # a bare date is a BUSINESS day boundary
                d = d.replace(tzinfo=tz)
            if end and len(v) <= 10:  # "until=2026-07-27" includes that whole day
                d = d + timedelta(days=1)
            return d.astimezone(timezone.utc).replace(tzinfo=None)
        return _parse(since, False), _parse(until, True), "custom"
    preset = (preset or "7d").strip().lower()
    if preset == "all":
        return None, None, "all"
    days = {"1d": 1, "7d": 7, "30d": 30, "90d": 90}.get(preset, 7)
    start_local = (now_local - timedelta(days=days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(timezone.utc).replace(tzinfo=None), None, preset


def _ledger_horizon(retention_days: int) -> datetime:
    """First instant the row-by-row ledger can still be trusted for."""
    return (datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(days=max(int(retention_days), 1)))


def rows_where(col):
    """1 per row where the BOOLEAN column is true — a COUNT, never a boolean.

    Sounds pedantic; it is the difference between a real figure and a yes/no
    wearing a number's clothes. ``met``/``fired``/``silenced`` are ``Boolean``,
    and ``func.sum(col)`` inherits the column's type, so SQLAlchemy hands the
    aggregate back through the Boolean result processor: 28 comes out as
    ``True`` → 1. The panel showed "124 checks · 1 met · 1 fired" while the
    evaluations tab, counting rows over the same table, showed 4 — and the
    number looked plausible enough to be believed for a whole release. Wrapping
    the column in a ``CASE`` makes the expression Integer-typed, which is both
    correct and self-documenting about what is being counted.
    """
    return case((col.is_(True), 1), else_=0)


# Short local alias: the aggregate lists below read better one column per line.
_rows = rows_where


# CANONICAL shape of the funnel. It exists as a constant —and not repeated in every place
# that builds an item— because the failure mode of this contract is always the same: a
# path that emits the item without all the keys and a screen with gaps. It already
# happened twice (VIG-52 in 7.148.1 on the provenance side, and again in 7.171.1 on the
# cost side, with automations run by hand).
_FUNNEL_ZERO = {
    "checks": 0, "met": 0, "fired": 0, "silenced": 0, "errors": 0,
    "rollup_from": None,
}


async def funnel(automation_ids: list[int] | None, start: datetime | None,
                 end: datetime | None, retention_days: int = 14) -> dict[int, dict]:
    """Counters per automation for the window, stitching ledger + daily rollup.

    Returns ``{automation_id: {checks, met, fired, silenced, errors,
    rollup_from}}``. ``rollup_from`` is the ISO day from which the numbers come
    from the SUMMARY instead of the detail (``None`` when the whole window is
    still in the ledger) — the UI shows it as "detalle disponible desde …" so
    nobody looks for a row that was legitimately pruned.
    """
    from app.app_models.automations import AutomationEvalLog
    from app.app_models.automation_daily_stats import AutomationDailyStats

    horizon = _ledger_horizon(retention_days)
    ledger_start = start if (start and start > horizon) else horizon
    out: dict[int, dict] = {}

    def _slot(aid: int) -> dict:
        return out.setdefault(int(aid), dict(_FUNNEL_ZERO))

    async with app_session() as s:
        q = select(
            AutomationEvalLog.automation_id,
            func.count(AutomationEvalLog.id),
            func.sum(_rows(AutomationEvalLog.met)),
            func.sum(_rows(AutomationEvalLog.fired)),
            func.sum(_rows(AutomationEvalLog.silenced)),
            func.sum(case((AutomationEvalLog.error.isnot(None), 1), else_=0)),
        ).where(AutomationEvalLog.at >= ledger_start)
        if end is not None:
            q = q.where(AutomationEvalLog.at < end)
        if automation_ids:
            q = q.where(AutomationEvalLog.automation_id.in_(automation_ids))
        for aid, checks, met, fired, silenced, errors in (
                await s.execute(q.group_by(AutomationEvalLog.automation_id))).all():
            sl = _slot(aid)
            sl["checks"] += int(checks or 0)
            sl["met"] += int(met or 0)
            sl["fired"] += int(fired or 0)
            sl["silenced"] += int(silenced or 0)
            sl["errors"] += int(errors or 0)

        # Older part of the window: only if the caller actually asked for days
        # the ledger no longer holds.
        if start is None or start < horizon:
            tz = business_tz()
            first_day = (start.replace(tzinfo=timezone.utc).astimezone(tz).date().isoformat()
                         if start is not None else None)
            last_day = horizon.replace(tzinfo=timezone.utc).astimezone(tz).date().isoformat()
            rq = select(
                AutomationDailyStats.automation_id,
                func.sum(AutomationDailyStats.checks),
                func.sum(AutomationDailyStats.met),
                func.sum(AutomationDailyStats.fired),
                func.sum(AutomationDailyStats.silenced),
                func.sum(AutomationDailyStats.errors),
                func.min(AutomationDailyStats.day),
            ).where(AutomationDailyStats.day < last_day)
            if first_day:
                rq = rq.where(AutomationDailyStats.day >= first_day)
            if automation_ids:
                rq = rq.where(AutomationDailyStats.automation_id.in_(automation_ids))
            for aid, checks, met, fired, silenced, errors, min_day in (
                    await s.execute(rq.group_by(AutomationDailyStats.automation_id))).all():
                sl = _slot(aid)
                sl["checks"] += int(checks or 0)
                sl["met"] += int(met or 0)
                sl["fired"] += int(fired or 0)
                sl["silenced"] += int(silenced or 0)
                sl["errors"] += int(errors or 0)
                sl["rollup_from"] = min_day

    # The bucket that used to be an unexplained hole. On the real Vigía the funnel
    # read "121 met · 5 fired · 0 held back", and the operator quite reasonably
    # concluded his employee was swallowing 116 findings. It wasn't: the condition
    # held, but the per-item watch had already reported those items, so there was
    # nothing NEW to say. That is the anti-repetition earning its keep, and it
    # deserves a name instead of being the gap between two other numbers.
    # Derived, never stored: one truth per figure (`silenced` is the explicit
    # temporary silence, which is a different thing and already counted).
    for sl in out.values():
        sl["held"] = max(0, sl["met"] - sl["fired"] - sl["silenced"])
    return out


async def cost(automation_ids: list[int] | None, start: datetime | None,
               end: datetime | None) -> dict[int, dict]:
    """Accumulated spend per automation in the window (from ``agent_runs``).

    ``cost_usd`` is stored as TEXT (the provider's own string), so it is summed
    with a CAST in SQL — one aggregate, not a Python loop over runs.
    """
    from app.app_models.runtime import AgentRun, Event
    from app.automation_provenance import automation_id_of, receipt_of_live_automation
    out: dict[int, dict] = {}
    async with app_session() as s:
        q = (select(
                Event.source,
                func.count(AgentRun.id),
                func.sum(func.coalesce(cast(AgentRun.cost_usd, Float), 0.0)),
                func.sum(func.coalesce(AgentRun.tokens_in, 0)),
                func.sum(func.coalesce(AgentRun.tokens_out, 0)),
             )
             .join(Event, AgentRun.event_id == Event.id)
             .where(Event.type == "automation_fired",
                    receipt_of_live_automation(Event.source)))
        if start is not None:
            q = q.where(AgentRun.started_at >= start)
        if end is not None:
            q = q.where(AgentRun.started_at < end)
        if automation_ids:
            q = q.where(Event.source.in_([f"automation:{i}" for i in automation_ids]))
        for source, runs, usd, tin, tout in (
                await s.execute(q.group_by(Event.source))).all():
            aid = automation_id_of(source)
            if aid is None:
                continue
            out[aid] = {"runs": int(runs or 0),
                        "cost_usd": round(float(usd or 0.0), 4),
                        "tokens_in": int(tin or 0), "tokens_out": int(tout or 0)}
    return out


async def for_automations(automation_ids: list[int] | None = None, *,
                          preset: str | None = None, since: str | None = None,
                          until: str | None = None,
                          retention_days: int = 14) -> dict:
    """Funnel + cost for a set of automations in ONE call (4 queries total).

    This is what the table view asks for: the operator sorts by "most expensive"
    or "fires but never passes the ONLY IF", and that is only possible if the
    numbers arrive with the rows instead of per-row.
    """
    start, end, label = resolve_range(preset, since, until)
    f = await funnel(automation_ids, start, end, retention_days)
    c = await cost(automation_ids, start, end)
    items: dict[str, dict] = {}
    for aid in set(list(f.keys()) + list(c.keys())):
        # v7.171.1 — the funnel is ALWAYS seeded with ZERO, not only when there were
        # evaluations. An automation that has only been run BY HAND (the operator's Play
        # forces it without checking the "only if") shows up in the cost and NOT in the
        # funnel, so its row reached the screen without `checks`/`met`/`fired`: an empty
        # column where there should be a 0, and an API client that has to guess whether
        # the gap means zero or "unknown".
        stats = {**_FUNNEL_ZERO, **(f.get(aid) or {})}
        stats.update(c.get(aid) or {"runs": 0, "cost_usd": 0.0,
                                    "tokens_in": 0, "tokens_out": 0})
        # The two rates the operator actually reads: a WHEN that keeps firing
        # while the ONLY IF never holds is a mis-tuned trigger burning checks;
        # a high silenced share means the anti-repetition is doing the work.
        checks, met, fired = stats.get("checks", 0), stats.get("met", 0), stats.get("fired", 0)
        stats["met_rate"] = round(met / checks, 3) if checks else None
        stats["fire_rate"] = round(fired / met, 3) if met else None
        stats["cost_per_fire"] = (round(stats["cost_usd"] / fired, 4) if fired else None)
        items[str(aid)] = stats
    return {"range": {"preset": label,
                      "since": iso_utc(start), "until": iso_utc(end)},
            "items": items}
