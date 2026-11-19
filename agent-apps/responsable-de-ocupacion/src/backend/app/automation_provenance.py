"""Which automation produced this? — and what did this automation produce?
(v7.140.0)

The second half of governing autonomous work. The funnel says how often a
standing order acted; this says WHAT it left behind: how many alerts, tasks,
decisions, deliverables it created, how many of those are still open, and — read
backwards — which standing order is responsible for the alert you are looking at.

The key point is that NO new column is needed. The chain already exists:

    automation → automation_fired event → agent_run → entity.created_by_run_id

and for a 0-token deterministic fire the receipt event is written by
``automations_service._settled_fire_event`` and the entity carries
``source_event_id``. So provenance is a JOIN over data that is already there,
which is why it can be added without touching the entities' schema and cannot
disagree with the history view (same join).

Both directions are BATCHED (one query per entity kind, or one lookup for a list
of ids) — a per-row lookup in a list of 200 alerts would be the "arco de iglesia"
the operator was right to be wary of.
"""

from __future__ import annotations

import logging

from sqlalchemy import and_, func, or_, select

from app.app_database import app_session

logger = logging.getLogger(__name__)


def automation_id_of(source) -> int | None:
    """The id of the order a fire receipt belongs to, or ``None``.

    A live receipt is EXACTLY ``automation:{id}``: two pieces. When an order is deleted,
    ``delete_automation`` relabels its receipts to ``automation:{id}:deleted`` precisely
    so that no live id inherits them — SQLite reuses the id of a deleted row, so the next
    order created gets the same number. Splitting on ":" and keeping the second piece
    undid that unlinking: the new order with id 2 appeared in the list with
    "Pendientes 2 / 7" without ever having fired, while its detail (which compares the
    exact origin) said it had produced nothing. Measured in demo on 06/09/2026.

    Every receipt reader goes through here; one that parses on its own inherits again.
    """
    partes = str(source or "").split(":")
    if len(partes) != 2 or partes[0] != "automation":
        return None
    try:
        return int(partes[1])
    except ValueError:
        return None


def receipt_of_live_automation(column):
    """SQL predicate: only receipts with the exact shape ``automation:{id}``.

    The second condition excludes any mark with more segments (today ``:deleted``),
    so that the filter and :func:`automation_id_of` say the same thing from SQL.
    """
    return and_(column.like("automation:%"), column.not_like("automation:%:%"))

# Entity kinds that can be produced by a reaction, with the field that means
# "still needs a human". Keyed by the name the UI already uses.
_KINDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "alerts": ("Alert", ("open", "acknowledged", "in_progress")),
    "recommendations": ("Recommendation", ("open", "acknowledged", "in_progress")),
    "decisions": ("Decision", ("open", "acknowledged", "in_progress")),
    "scheduled_items": ("ScheduledItem", ("pending", "scheduled", "in_progress", "overdue")),
    "approvals": ("Approval", ("pending",)),
    "communications": ("Communication", ("draft", "ready")),
}


# Physical table → the kind key above. Lets a generic, manifest-driven surface ask
# "is this row something an automation could have produced?" without hardcoding
# the mapping in every route.
_TABLE_TO_KIND: dict[str, str] = {
    "alerts": "alerts",
    "recommendations": "recommendations",
    "decisions": "decisions",
    "scheduled_items": "scheduled_items",
    "approvals": "approvals",
    "communications": "communications",
}


def kind_for_table(table: str) -> str | None:
    return _TABLE_TO_KIND.get((table or "").strip().lower())


def open_states_of(kind: str) -> tuple[str, ...]:
    """The states that mean "still needs a human" for ``kind``.

    Single source: the same tuple that produces the "(2 abiertas)" count also
    filters the list that count links to, so the figure and the rows behind it
    cannot tell different stories.
    """
    spec = _KINDS.get((kind or "").strip().lower())
    return spec[1] if spec else ()


def traceable_columns(table: str) -> dict:
    """Which provenance columns the MODEL of ``table`` really has.

    Asked from the model, not from the manifest: ``created_by_run_id`` /
    ``source_event_id`` are plumbing, so an app never lists them among its
    ``list_columns`` — a manifest-based check would find nothing and the
    "created by this automation" filter would quietly return the whole table,
    the worst possible failure for a traceability link.
    """
    kind = kind_for_table(table)
    spec = _KINDS.get(kind or "")
    if spec is None:
        return {"kind": None, "run": False, "event": False, "status": False,
                "open_states": ()}
    try:
        model = _model(spec[0])
    except AttributeError:  # pragma: no cover — kind not modelled in this app
        return {"kind": kind, "run": False, "event": False, "status": False,
                "open_states": ()}
    return {"kind": kind,
            "run": hasattr(model, "created_by_run_id"),
            "event": hasattr(model, "source_event_id"),
            "status": hasattr(model, "status"),
            "open_states": spec[1]}


# Kinds with a FIXED page in the SPA instead of a manifest-authored surface.
# Everything else must come from the manifest — never from a guess.
_FIXED_SURFACE: dict[str, str] = {
    "scheduled_items": "/encargos",
    "approvals": "/approvals",
}


def surface_of(kind: str) -> dict:
    """How THIS employee names and reaches the entities of ``kind``.

    The counter is useless — worse, it is wrong — if it is labelled with the
    generic table name. The Vigía calls its alerts "Incidencias detectadas"
    under the slug ``incidencias``; the panel was showing "Avisos" and linking
    to ``/domain/alertas``, a slug that does not exist in this app, so every
    figure looked foreign and every link was dead. Each app renames its
    entities, so the label and the route can only come from the manifest.

    Returns ``{label, slug, path}``; ``slug``/``path`` are ``None`` when the
    kind has no surface in this app (it happens: a table can be written without
    being exposed), and then the UI shows the count without a link instead of
    inventing one.
    """
    from app.config import APP_MANIFEST

    table = next((t for t, k in _TABLE_TO_KIND.items() if k == kind), kind)
    for e in (APP_MANIFEST.get("agent_app", {}).get("domain_entities", []) or []):
        if str(e.get("table") or e.get("slug") or "").strip().lower() != table:
            continue
        slug = (e.get("slug") or "").strip()
        label = (e.get("label") or e.get("title") or slug or kind).strip()
        return {"label": label, "slug": slug or None,
                "path": f"/domain/{slug}" if slug else _FIXED_SURFACE.get(kind)}
    return {"label": kind, "slug": None, "path": _FIXED_SURFACE.get(kind)}


def _model(name: str):
    from app.app_models import business
    return getattr(business, name)


async def _event_ids(session, automation_id: int) -> list[int]:
    """Every fire receipt of this automation (normal runs AND 0-token ones)."""
    from app.app_models.runtime import Event
    return [int(i) for i in (await session.execute(
        select(Event.id).where(Event.type == "automation_fired",
                               Event.source == f"automation:{automation_id}")
    )).scalars().all()]


async def produced_by(automation_id: int) -> dict:
    """Counts per entity kind produced by this automation: total and still open.

    Two paths are unioned because both are legitimate fires: entities written BY
    THE AGENT during the reaction run (``created_by_run_id``) and entities written
    DETERMINISTICALLY without a run (``source_event_id`` pointing at the receipt).
    Counting only the first would make deterministic automations look idle.
    """
    from app.app_models.runtime import AgentRun

    out: dict[str, dict] = {}
    async with app_session() as s:
        ev_ids = await _event_ids(s, automation_id)
        if not ev_ids:
            return {"items": {}, "total": 0, "open": 0}
        run_ids = [int(i) for i in (await s.execute(
            select(AgentRun.id).where(AgentRun.event_id.in_(ev_ids))
        )).scalars().all()]
        total = open_total = 0
        for kind, (model_name, open_states) in _KINDS.items():
            model = _model(model_name)
            conds = []
            if run_ids and hasattr(model, "created_by_run_id"):
                conds.append(model.created_by_run_id.in_(run_ids))
            if hasattr(model, "source_event_id"):
                conds.append(model.source_event_id.in_(ev_ids))
            if not conds:
                continue
            where = or_(*conds) if len(conds) > 1 else conds[0]
            n = (await s.execute(
                select(func.count(model.id)).where(where))).scalar() or 0
            if not n:
                continue
            n_open = (await s.execute(select(func.count(model.id)).where(
                where, model.status.in_(open_states)))).scalar() or 0
            out[kind] = {"total": int(n), "open": int(n_open), **surface_of(kind)}
            total += int(n)
            open_total += int(n_open)
    return {"items": out, "total": total, "open": open_total}


async def open_counts_batch() -> dict[int, dict]:
    """``{automation_id: {"produced": n, "open": m}}`` for EVERY automation, in a
    fixed number of queries (one per entity kind, not one per automation).

    This is what lets the table show "abiertas" as a column. Doing it per row
    would be 7 queries × 40 rows on a screen that also refreshes — the shape of
    thing that turns a governance feature into a performance problem. The grouping
    happens in SQL over ``events.source``, so nothing comes back to Python to be
    re-joined, and no ``IN (…)`` of ids can blow SQLite's parameter limit.
    """
    from app.app_models.runtime import AgentRun, Event

    out: dict[int, dict] = {}

    def _slot(source: str) -> dict | None:
        aid = automation_id_of(source)
        if aid is None:
            return None
        return out.setdefault(aid, {"produced": 0, "open": 0})

    async with app_session() as s:
        for _kind, (model_name, open_states) in _KINDS.items():
            model = _model(model_name)
            if not hasattr(model, "status"):
                continue
            # ONE query of two hops per kind. The receipt event is reached either
            # directly (deterministic fire → ``source_event_id``) or through the
            # reaction run (``created_by_run_id`` → run → event); COALESCE picks
            # whichever exists, which is also what stops a row that has BOTH from
            # being counted twice.
            event_id = (func.coalesce(model.source_event_id, AgentRun.event_id)
                        if hasattr(model, "source_event_id") else AgentRun.event_id)
            q = (select(Event.source, func.count(model.id),
                        func.sum(_open_flag(model, open_states)))
                 .select_from(model)
                 .outerjoin(AgentRun, model.created_by_run_id == AgentRun.id)
                 .join(Event, Event.id == event_id)
                 .where(Event.type == "automation_fired",
                        receipt_of_live_automation(Event.source))
                 .group_by(Event.source))
            for source, total, n_open in (await s.execute(q)).all():
                sl = _slot(source)
                if sl is not None:
                    sl["produced"] += int(total or 0)
                    sl["open"] += int(n_open or 0)
    return out


def _open_flag(model, open_states: tuple[str, ...]):
    """1 when the row still needs a human, 0 otherwise — summed in SQL so "open"
    costs the same single pass as "total"."""
    from sqlalchemy import case
    return case((model.status.in_(open_states), 1), else_=0)


async def origin_of(kind: str, entity_ids: list[int]) -> dict[str, dict]:
    """REVERSE lookup, batched: ``{entity_id: {automation_id, title, via}}``.

    ``via`` is ``run`` (the agent created it while reacting) or ``event`` (a
    deterministic reaction created it directly) — worth surfacing, because it
    tells the operator whether an LLM judged the case or a template fired.
    Entities with no automation behind them simply don't appear: the badge must
    never be invented.
    """
    if not entity_ids:
        return {}
    spec = _KINDS.get(kind)
    if spec is None:
        return {}
    from app.app_models.automations import Automation
    from app.app_models.runtime import AgentRun, Event

    model = _model(spec[0])
    ids = [int(i) for i in entity_ids if i is not None]
    if not ids:
        return {}
    out: dict[str, dict] = {}
    async with app_session() as s:
        cols = [model.id]
        has_run = hasattr(model, "created_by_run_id")
        has_ev = hasattr(model, "source_event_id")
        if has_run:
            cols.append(model.created_by_run_id)
        if has_ev:
            cols.append(model.source_event_id)
        rows = (await s.execute(select(*cols).where(model.id.in_(ids)))).all()
        run_ids, ev_ids = set(), set()
        for row in rows:
            vals = list(row)
            if has_run and vals[1]:
                run_ids.add(int(vals[1]))
            if has_ev and vals[-1]:
                ev_ids.add(int(vals[-1]))
        # run → its event (so both paths collapse into "which event fired this")
        run_to_ev: dict[int, int] = {}
        if run_ids:
            for rid, eid in (await s.execute(
                select(AgentRun.id, AgentRun.event_id)
                .where(AgentRun.id.in_(run_ids), AgentRun.event_id.isnot(None))
            )).all():
                run_to_ev[int(rid)] = int(eid)
                ev_ids.add(int(eid))
        if not ev_ids:
            return {}
        ev_to_aid: dict[int, int] = {}
        for eid, source in (await s.execute(
            select(Event.id, Event.source).where(
                Event.id.in_(ev_ids), Event.type == "automation_fired")
        )).all():
            aid = automation_id_of(source)
            if aid is None:
                continue
            ev_to_aid[int(eid)] = aid
        if not ev_to_aid:
            return {}
        titles = dict((await s.execute(
            select(Automation.id, Automation.title)
            .where(Automation.id.in_(set(ev_to_aid.values())))
        )).all())
        for row in rows:
            vals = list(row)
            ent_id = int(vals[0])
            eid = int(vals[-1]) if (has_ev and vals[-1]) else None
            via = "event"
            if eid is None and has_run and vals[1]:
                eid = run_to_ev.get(int(vals[1]))
                via = "run"
            aid = ev_to_aid.get(eid) if eid else None
            if aid is None:
                continue
            out[str(ent_id)] = {"automation_id": aid,
                                "title": titles.get(aid) or f"Automatización {aid}",
                                "via": via}
    return out


async def producers_of(kind: str) -> list[dict]:
    """``[{id, title, count}]`` — the standing orders that have produced rows of
    this kind, most productive first (v7.143.1).

    Feeds the "trabajo autónomo" filter on the business surfaces. It lists only
    automations with rows HERE, because an option that returns an empty list is
    an option that makes the operator doubt the filter.

    ONE grouped query over the same chain the stamp uses (plus one for the
    titles): the alternative — reading every ``automation_fired`` event into
    Python — grows with the months the employee has been running, and this is a
    dropdown.
    """
    spec = _KINDS.get((kind or "").strip().lower())
    if spec is None:
        return []
    from app.app_models.automations import Automation
    from app.app_models.runtime import AgentRun, Event

    model = _model(spec[0])
    tbl, run, ev = model.__table__, AgentRun.__table__, Event.__table__
    has_run = "created_by_run_id" in tbl.c
    has_ev = "source_event_id" in tbl.c
    if not (has_run or has_ev):
        return []
    src = tbl
    if has_run:
        src = tbl.outerjoin(run, run.c.id == tbl.c.created_by_run_id)
    # The row's originating event: its own receipt, or the one that spawned the
    # run that wrote it. COALESCE collapses both fire paths into one group-by.
    if has_ev and has_run:
        origin = func.coalesce(tbl.c.source_event_id, run.c.event_id)
    elif has_ev:
        origin = tbl.c.source_event_id
    else:
        origin = run.c.event_id
    stmt = (
        select(ev.c.source, func.count().label("n"))
        .select_from(src.join(ev, ev.c.id == origin))
        .where(ev.c.type == "automation_fired", receipt_of_live_automation(ev.c.source))
        .group_by(ev.c.source)
    )
    counts: dict[int, int] = {}
    async with app_session() as s:
        for source, n in (await s.execute(stmt)).all():
            aid = automation_id_of(source)
            if aid is None:
                continue
            counts[aid] = int(n)
        if not counts:
            return []
        titles = dict((await s.execute(
            select(Automation.id, Automation.title)
            .where(Automation.id.in_(counts))
        )).all())
    items = [{"id": aid, "title": titles.get(aid) or f"Automatización {aid}",
              "count": n} for aid, n in counts.items()]
    items.sort(key=lambda i: (-i["count"], i["title"]))
    return items


def sqlalchemy_provenance_filter(model, automation_id: int, *, only_open: bool = False):
    """SQLAlchemy WHERE clause: rows produced by ``automation_id``.

    Shared by domain list (raw SQL), Encargos and Approvals (ORM). Same chain as
    ``_provenance_filter`` in ``domain_routes`` — one semantics, no drift.
    """
    from sqlalchemy import and_

    table = getattr(model, "__tablename__", "")
    trace = traceable_columns(table)
    if trace["kind"] is None:
        return None
    from app.app_models.runtime import AgentRun, Event

    ev_q = select(Event.id).where(
        Event.type == "automation_fired",
        Event.source == f"automation:{automation_id}",
    )
    parts = []
    if trace["run"]:
        parts.append(model.created_by_run_id.in_(
            select(AgentRun.id).where(AgentRun.event_id.in_(ev_q))
        ))
    if trace["event"]:
        parts.append(model.source_event_id.in_(ev_q))
    if not parts:
        return None
    clause = or_(*parts)
    if only_open and trace["status"] and trace["open_states"]:
        clause = and_(clause, model.status.in_(list(trace["open_states"])))
    return clause


async def automation_title(automation_id: int) -> str:
    from app.app_models.automations import Automation

    async with app_session() as s:
        title = (await s.execute(
            select(Automation.title).where(Automation.id == automation_id)
        )).scalar()
    return title or f"Automatización {automation_id}"


async def provenance_context(automation_id: int, *, only_open: bool) -> dict:
    return {"id": automation_id, "title": await automation_title(automation_id),
            "only_open": only_open}


def parse_provenance_query(request) -> tuple[int | None, bool]:
    """``(automation_id | None, only_open)`` from query params."""
    raw = (getattr(request, "query_params", {}) or {}).get("created_by_automation") or ""
    raw = str(raw).strip()
    if not raw:
        return None, False
    try:
        aid = int(raw)
    except ValueError:
        return None, False
    only_open = str((getattr(request, "query_params", {}) or {}).get("only_open") or "").strip()
    only_open = only_open.lower() in ("1", "true", "yes")
    return aid, only_open


async def attach_origin(kind: str, items: list[dict], key: str = "id") -> list[dict]:
    """Stamp ``created_by_automation`` on a list of serialised entities.

    Best-effort by design: a listing must render even if provenance can't be
    resolved — a missing badge is a cosmetic loss, a 500 is not.
    """
    if not items:
        return items
    try:
        found = await origin_of(kind, [i.get(key) for i in items])
    except Exception:  # pragma: no cover
        logger.debug("origin_of failed for %s (ignored)", kind, exc_info=True)
        return items
    for it in items:
        o = found.get(str(it.get(key)))
        if o:
            it["created_by_automation"] = o
    return items
