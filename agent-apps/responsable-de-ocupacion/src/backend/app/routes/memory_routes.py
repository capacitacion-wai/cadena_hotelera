"""Operations → Memoria tab API.

Lets an operator view and manage the agent's PER-USER memory (the
``user_memory`` / ``user_episodes`` / ``user_files`` tables written by
the MCP memory tools and the channel poller). Mirrors what the agent can
do, but operator-driven: list users, browse a user's layered memory, add
or edit facts, pin/unpin, and delete facts / episodes / files.

Every endpoint is gated by ``require_project_access`` (same JWT +
project gate as the rest of ``/api/agent``). Operator-created facts are
marked ``source='operator'`` and pinned by default so they are protected
from the agent's automatic supersede/forget policy.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.timeutil import iso_utc
from app.app_database import app_session, app_write
from app.app_models.memory import UserEpisode, UserFile, UserMemory
from app.app_models.runtime import Event
from app.auth import require_project_access, require_admin, resolve_scoped_email

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agent/memory", tags=["memory"])

MEMORY_KINDS = ("fact", "preference", "procedure")


def _norm(email: str) -> str:
    from app.memory_utils import valid_memory_user

    # Operators may share the autonomous service email in demo tenants.
    e = valid_memory_user(email, allow_autonomous_identity=True)
    if not e:
        raise HTTPException(
            status_code=400,
            detail="user_email must be a valid human email address",
        )
    return e


def _artifacts_from(blob: str | None) -> list[dict]:
    """Decode the artifact-ref JSON stored in a fact's ``provenance`` or an
    episode's ``source_ref`` (written by the MCP memory tools). Returns []
    for legacy/plain values so the UI degrades gracefully."""
    if not blob:
        return []
    try:
        data = json.loads(blob)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [
        {"filename": d.get("filename"), "workspace_path": d.get("workspace_path")}
        for d in data
        if isinstance(d, dict) and d.get("workspace_path")
    ]


def _parse_dt(value: str | None) -> datetime | None:
    """Parse a ``date_from`` / ``date_to`` query value. Accepts a plain
    date (``YYYY-MM-DD``) or a full ISO datetime. Returns None on blanks
    or anything unparseable (so a bad filter just no-ops)."""
    s = (value or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _date_bounds(date_from: str | None, date_to: str | None):
    """Return (lo, hi) datetimes for a created_at range. ``date_to`` given
    as a plain date is treated inclusively (end of that day)."""
    lo = _parse_dt(date_from)
    hi = _parse_dt(date_to)
    if hi is not None and len((date_to or "").strip()) == 10:
        from datetime import timedelta
        hi = hi + timedelta(days=1)  # inclusive end-of-day
    return lo, hi


def _fact_dict(f: UserMemory) -> dict:
    return {
        "id": f.id, "kind": f.kind, "content": f.content,
        "tags": f.tags, "pinned": bool(f.pinned), "source": f.source,
        "confidence": f.confidence, "superseded_by": f.superseded_by,
        "artifacts": _artifacts_from(f.provenance),
        "access_count": int(getattr(f, "access_count", 0) or 0),
        "last_accessed_at": iso_utc(getattr(f, "last_accessed_at", None)),
        "created_at": iso_utc(f.created_at),
        "updated_at": iso_utc(f.updated_at),
    }


def _episode_dict(ep: UserEpisode) -> dict:
    return {
        "id": ep.id, "summary": ep.summary, "source": ep.source,
        "outcome": ep.outcome, "artifacts": _artifacts_from(ep.source_ref),
        "created_at": iso_utc(ep.created_at),
    }


def _file_dict(x: UserFile) -> dict:
    return {
        "id": x.id, "filename": x.filename, "workspace_path": x.workspace_path,
        "mime": x.mime, "size": x.size, "source": x.source,
        "description": x.description,
        "created_at": iso_utc(x.created_at),
    }


@router.get("/users")
async def list_users(q: str = Query(""), user=Depends(require_admin)):
    # The roster enumerates EVERY person the employee has memory about — an
    # admin surface (the Chat self-service viewer runs with hideUserPicker and
    # never calls this). Per-user reads/writes below are scoped own-or-admin.
    """Every user with stored memory + per-layer counts, most-recently
    active first. Powers the user picker in the Memoria tab."""
    async with app_session() as s:
        facts = dict((await s.execute(
            select(UserMemory.user_email, func.count(UserMemory.id))
            .where(UserMemory.superseded_by.is_(None))
            .group_by(UserMemory.user_email)
        )).all())
        episodes = dict((await s.execute(
            select(UserEpisode.user_email, func.count(UserEpisode.id))
            .group_by(UserEpisode.user_email)
        )).all())
        files = dict((await s.execute(
            select(UserFile.user_email, func.count(UserFile.id))
            .group_by(UserFile.user_email)
        )).all())
        last_facts = dict((await s.execute(
            select(UserMemory.user_email, func.max(UserMemory.updated_at))
            .where(UserMemory.superseded_by.is_(None))
            .group_by(UserMemory.user_email)
        )).all())
        last_eps = dict((await s.execute(
            select(UserEpisode.user_email, func.max(UserEpisode.created_at))
            .group_by(UserEpisode.user_email)
        )).all())
        last_files = dict((await s.execute(
            select(UserFile.user_email, func.max(UserFile.created_at))
            .group_by(UserFile.user_email)
        )).all())
    emails = set(facts) | set(episodes) | set(files)
    needle = (q or "").strip().lower()
    if needle:
        emails = {e for e in emails if needle in (e or "").lower()}

    def _last_active(e: str):
        candidates = [last_facts.get(e), last_eps.get(e), last_files.get(e)]
        candidates = [c for c in candidates if c is not None]
        return max(candidates) if candidates else None

    users = [
        {
            "user_email": e,
            "facts": int(facts.get(e, 0)),
            "episodes": int(episodes.get(e, 0)),
            "files": int(files.get(e, 0)),
            "last_active": iso_utc(_last_active(e)),
        }
        for e in emails
    ]
    users.sort(key=lambda u: (u["last_active"] or ""), reverse=True)
    return {"users": users}


@router.get("/list")
async def list_memory_for_user(
    user_email: str = Query(...),
    include_superseded: bool = Query(False),
    user=Depends(require_project_access),
):
    """A user's full memory: facts (live by default), episodes, files."""
    e = _norm(await resolve_scoped_email(user_email, user))
    async with app_session() as s:
        fstmt = (
            select(UserMemory)
            .where(UserMemory.user_email == e)
            .order_by(UserMemory.pinned.desc(), UserMemory.updated_at.desc())
        )
        if not include_superseded:
            fstmt = fstmt.where(UserMemory.superseded_by.is_(None))
        facts = list((await s.execute(fstmt)).scalars().all())
        episodes = list((await s.execute(
            select(UserEpisode)
            .where(UserEpisode.user_email == e)
            .order_by(UserEpisode.created_at.desc())
        )).scalars().all())
        files = list((await s.execute(
            select(UserFile)
            .where(UserFile.user_email == e)
            .order_by(UserFile.created_at.desc())
        )).scalars().all())
    return {
        "user_email": e,
        "facts": [
            {
                "id": f.id, "kind": f.kind, "content": f.content,
                "tags": f.tags, "pinned": bool(f.pinned), "source": f.source,
                "confidence": f.confidence,
                "superseded_by": f.superseded_by,
                "artifacts": _artifacts_from(f.provenance),
                "created_at": iso_utc(f.created_at),
                "updated_at": iso_utc(f.updated_at),
            }
            for f in facts
        ],
        "episodes": [
            {
                "id": ep.id, "summary": ep.summary, "source": ep.source,
                "outcome": ep.outcome,
                "artifacts": _artifacts_from(ep.source_ref),
                "created_at": iso_utc(ep.created_at),
            }
            for ep in episodes
        ],
        "files": [
            {
                "id": x.id, "filename": x.filename,
                "workspace_path": x.workspace_path, "mime": x.mime,
                "size": x.size, "source": x.source, "description": x.description,
                "created_at": iso_utc(x.created_at),
            }
            for x in files
        ],
    }


_PAGE_MAX = 200


def _page(limit: int, offset: int) -> tuple[int, int]:
    lim = max(1, min(int(limit or 50), _PAGE_MAX))
    off = max(0, int(offset or 0))
    return lim, off


@router.get("/facts")
async def query_facts(
    user_email: str = Query(...),
    q: str = Query(""),
    kind: str = Query(""),
    source: str = Query(""),
    pinned: bool | None = Query(None),
    include_superseded: bool = Query(False),
    date_from: str = Query(""),
    date_to: str = Query(""),
    sort: str = Query("recent"),
    limit: int = Query(50),
    offset: int = Query(0),
    user=Depends(require_project_access),
):
    """Paginated, filtered facts for a user. Filters: free text (content/
    tags), kind, source, pinned, date range (created_at), obsolete toggle."""
    e = _norm(await resolve_scoped_email(user_email, user))
    lim, off = _page(limit, offset)
    lo, hi = _date_bounds(date_from, date_to)

    def _apply(stmt):
        stmt = stmt.where(UserMemory.user_email == e)
        if not include_superseded:
            stmt = stmt.where(UserMemory.superseded_by.is_(None))
        needle = (q or "").strip()
        if needle:
            like = f"%{needle}%"
            stmt = stmt.where(
                UserMemory.content.ilike(like) | UserMemory.tags.ilike(like)
            )
        k = (kind or "").strip().lower()
        if k in MEMORY_KINDS:
            stmt = stmt.where(UserMemory.kind == k)
        src = (source or "").strip().lower()
        if src:
            stmt = stmt.where(UserMemory.source == src)
        if pinned is not None:
            stmt = stmt.where(UserMemory.pinned.is_(bool(pinned)))
        if lo is not None:
            stmt = stmt.where(UserMemory.created_at >= lo)
        if hi is not None:
            stmt = stmt.where(UserMemory.created_at < hi)
        return stmt

    if sort == "oldest":
        order = (UserMemory.updated_at.asc(),)
    elif sort == "pinned":
        order = (UserMemory.pinned.desc(), UserMemory.updated_at.desc())
    else:  # recent
        order = (UserMemory.updated_at.desc(),)

    async with app_session() as s:
        total = int((await s.execute(
            _apply(select(func.count(UserMemory.id)))
        )).scalar() or 0)
        rows = list((await s.execute(
            _apply(select(UserMemory)).order_by(*order).limit(lim).offset(off)
        )).scalars().all())
    return {
        "items": [_fact_dict(f) for f in rows],
        "total": total, "limit": lim, "offset": off,
    }


@router.get("/episodes")
async def query_episodes(
    user_email: str = Query(...),
    q: str = Query(""),
    source: str = Query(""),
    date_from: str = Query(""),
    date_to: str = Query(""),
    sort: str = Query("recent"),
    limit: int = Query(50),
    offset: int = Query(0),
    user=Depends(require_project_access),
):
    e = _norm(await resolve_scoped_email(user_email, user))
    lim, off = _page(limit, offset)
    lo, hi = _date_bounds(date_from, date_to)

    def _apply(stmt):
        stmt = stmt.where(UserEpisode.user_email == e)
        needle = (q or "").strip()
        if needle:
            like = f"%{needle}%"
            stmt = stmt.where(
                UserEpisode.summary.ilike(like) | UserEpisode.outcome.ilike(like)
            )
        src = (source or "").strip().lower()
        if src:
            stmt = stmt.where(UserEpisode.source == src)
        if lo is not None:
            stmt = stmt.where(UserEpisode.created_at >= lo)
        if hi is not None:
            stmt = stmt.where(UserEpisode.created_at < hi)
        return stmt

    order = (UserEpisode.created_at.asc(),) if sort == "oldest" else (UserEpisode.created_at.desc(),)
    async with app_session() as s:
        total = int((await s.execute(
            _apply(select(func.count(UserEpisode.id)))
        )).scalar() or 0)
        rows = list((await s.execute(
            _apply(select(UserEpisode)).order_by(*order).limit(lim).offset(off)
        )).scalars().all())
    return {
        "items": [_episode_dict(x) for x in rows],
        "total": total, "limit": lim, "offset": off,
    }


@router.get("/files")
async def query_files(
    user_email: str = Query(...),
    q: str = Query(""),
    source: str = Query(""),
    date_from: str = Query(""),
    date_to: str = Query(""),
    sort: str = Query("recent"),
    limit: int = Query(50),
    offset: int = Query(0),
    user=Depends(require_project_access),
):
    e = _norm(await resolve_scoped_email(user_email, user))
    lim, off = _page(limit, offset)
    lo, hi = _date_bounds(date_from, date_to)

    def _apply(stmt):
        stmt = stmt.where(UserFile.user_email == e)
        needle = (q or "").strip()
        if needle:
            like = f"%{needle}%"
            stmt = stmt.where(
                UserFile.filename.ilike(like)
                | UserFile.description.ilike(like)
                | UserFile.workspace_path.ilike(like)
            )
        src = (source or "").strip().lower()
        if src:
            stmt = stmt.where(UserFile.source == src)
        if lo is not None:
            stmt = stmt.where(UserFile.created_at >= lo)
        if hi is not None:
            stmt = stmt.where(UserFile.created_at < hi)
        return stmt

    order = (UserFile.created_at.asc(),) if sort == "oldest" else (UserFile.created_at.desc(),)
    async with app_session() as s:
        total = int((await s.execute(
            _apply(select(func.count(UserFile.id)))
        )).scalar() or 0)
        rows = list((await s.execute(
            _apply(select(UserFile)).order_by(*order).limit(lim).offset(off)
        )).scalars().all())
    return {
        "items": [_file_dict(x) for x in rows],
        "total": total, "limit": lim, "offset": off,
    }


@router.get("/stats")
async def memory_stats(
    user_email: str = Query(...),
    user=Depends(require_project_access),
):
    """Size/health metrics for a user's memory, used by the UI header to
    explain how big the memory is and whether it is over the auto-compact
    threshold."""
    e = _norm(await resolve_scoped_email(user_email, user))
    from app.config import (
        AGENT_MEMORY_FACTS_KEEP,
        AGENT_MEMORY_AUTOCOMPACT_OVER,
    )
    async with app_session() as s:
        facts_live = int((await s.execute(
            select(func.count(UserMemory.id)).where(
                UserMemory.user_email == e,
                UserMemory.superseded_by.is_(None),
            )
        )).scalar() or 0)
        facts_total = int((await s.execute(
            select(func.count(UserMemory.id)).where(UserMemory.user_email == e)
        )).scalar() or 0)
        chars_facts = int((await s.execute(
            select(func.coalesce(func.sum(func.length(UserMemory.content)), 0)).where(
                UserMemory.user_email == e,
                UserMemory.superseded_by.is_(None),
            )
        )).scalar() or 0)
        episodes_total = int((await s.execute(
            select(func.count(UserEpisode.id)).where(UserEpisode.user_email == e)
        )).scalar() or 0)
        files_total = int((await s.execute(
            select(func.count(UserFile.id)).where(UserFile.user_email == e)
        )).scalar() or 0)
        oldest = (await s.execute(
            select(func.min(UserMemory.created_at)).where(UserMemory.user_email == e)
        )).scalar()
        newest = (await s.execute(
            select(func.max(UserMemory.updated_at)).where(UserMemory.user_email == e)
        )).scalar()

    facts_superseded = max(facts_total - facts_live, 0)
    over = max(int(AGENT_MEMORY_AUTOCOMPACT_OVER), 1)
    cap = int(AGENT_MEMORY_FACTS_KEEP)
    block_chars = 0
    block_tokens = 0
    try:
        from app.agent_sessions import build_memory_block_detail
        detail = await build_memory_block_detail(e, enqueue_autocompact=False)
        block_chars = int(detail.get("chars") or len(detail.get("text") or ""))
        block_tokens = int(detail.get("tokens") or 0)
    except Exception:
        pass
    return {
        "user_email": e,
        "facts_live": facts_live,
        "facts_superseded": facts_superseded,
        "facts_total": facts_total,
        "episodes_total": episodes_total,
        "files_total": files_total,
        "chars_facts": chars_facts,
        "block_chars": block_chars,
        "block_tokens": block_tokens,
        "oldest": iso_utc(oldest),
        "newest": iso_utc(newest),
        "facts_cap": cap,
        "autocompact_over": over,
        "over_facts_cap": bool(cap > 0 and facts_live > cap),
        "over_autocompact": bool(facts_live > over or episodes_total > over),
    }


@router.get("/block")
async def memory_block_preview(
    user_email: str = Query(...),
    user=Depends(require_project_access),
):
    """The EXACT working-memory text injected into the prompt right now,
    plus which rows are included. Lets the UI badge each stored entry as
    applied (in the block) vs stored-but-not-injected."""
    e = _norm(await resolve_scoped_email(user_email, user))
    from app.agent_sessions import build_memory_block_detail
    detail = await build_memory_block_detail(e, enqueue_autocompact=False)
    return {
        "user_email": e,
        "text": detail.get("text") or "",
        "empty": bool(detail.get("empty")),
        "included_fact_ids": detail.get("fact_ids") or [],
        "included_episode_ids": detail.get("episode_ids") or [],
        "included_file_ids": detail.get("file_ids") or [],
    }


@router.get("/export")
async def export_memory_for_user(
    user_email: str = Query(...),
    user=Depends(require_project_access),
):
    """Full, portable JSON dump of everything the agent remembers about a
    user (facts incl. superseded, episodes, files). The Memoria tab offers
    it as a download so an operator can back up / audit a person's memory."""
    e = _norm(await resolve_scoped_email(user_email, user))
    async with app_session() as s:
        facts = list((await s.execute(
            select(UserMemory)
            .where(UserMemory.user_email == e)
            .order_by(UserMemory.created_at.asc())
        )).scalars().all())
        episodes = list((await s.execute(
            select(UserEpisode)
            .where(UserEpisode.user_email == e)
            .order_by(UserEpisode.created_at.asc())
        )).scalars().all())
        files = list((await s.execute(
            select(UserFile)
            .where(UserFile.user_email == e)
            .order_by(UserFile.created_at.asc())
        )).scalars().all())
    return {
        "user_email": e,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "facts": [_fact_dict(f) for f in facts],
        "episodes": [_episode_dict(ep) for ep in episodes],
        "files": [_file_dict(x) for x in files],
    }


class FactCreate(BaseModel):
    user_email: str
    content: str
    kind: str = "fact"
    tags: str | None = None
    pinned: bool = True


@router.post("/facts")
async def create_fact(body: FactCreate, user=Depends(require_project_access)):
    """Operator adds a fact/preference/procedure. Defaults to pinned +
    ``source='operator'`` so the agent never auto-forgets it."""
    e = _norm(await resolve_scoped_email(body.user_email, user))
    content = (body.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="content is required")
    kind = (body.kind or "fact").strip().lower()
    if kind not in MEMORY_KINDS:
        raise HTTPException(status_code=400, detail=f"kind must be one of {MEMORY_KINDS}")
    key = hashlib.sha256(f"operator|{uuid.uuid4()}".encode()).hexdigest()
    async with app_write() as s:
        row = UserMemory(
            idempotency_key=key,
            user_email=e,
            kind=kind,
            content=content,
            tags=(body.tags or None),
            pinned=bool(body.pinned),
            source="operator",
            provenance="operator",
        )
        s.add(row)
        await s.flush()
        await s.commit()
        new_id = row.id
    return {"ok": True, "id": new_id}


class FactUpdate(BaseModel):
    content: str | None = None
    kind: str | None = None
    tags: str | None = None
    pinned: bool | None = None


@router.patch("/facts/{fact_id}")
async def update_fact(
    fact_id: int,
    body: FactUpdate,
    user_email: str = Query(...),
    user=Depends(require_project_access),
):
    """Edit a fact's content / kind / tags / pinned flag."""
    owner = _norm(await resolve_scoped_email(user_email, user))
    values: dict = {"updated_at": datetime.now(timezone.utc)}
    if body.content is not None:
        c = body.content.strip()
        if not c:
            raise HTTPException(status_code=400, detail="content must not be empty")
        values["content"] = c
    if body.kind is not None:
        k = body.kind.strip().lower()
        if k not in MEMORY_KINDS:
            raise HTTPException(status_code=400, detail=f"kind must be one of {MEMORY_KINDS}")
        values["kind"] = k
    if body.tags is not None:
        values["tags"] = body.tags or None
    if body.pinned is not None:
        values["pinned"] = bool(body.pinned)
    async with app_write() as s:
        res = await s.execute(
            update(UserMemory)
            .where(UserMemory.id == fact_id, UserMemory.user_email == owner)
            .values(**values)
        )
        await s.commit()
    if not res.rowcount:
        raise HTTPException(status_code=404, detail="fact not found")
    return {"ok": True}


@router.delete("/facts/{fact_id}")
async def delete_fact(
    fact_id: int,
    user_email: str = Query(...),
    user=Depends(require_project_access),
):
    owner = _norm(await resolve_scoped_email(user_email, user))
    async with app_write() as s:
        res = await s.execute(
            sql_delete(UserMemory).where(
                UserMemory.id == fact_id,
                UserMemory.user_email == owner,
            )
        )
        await s.commit()
    if not res.rowcount:
        raise HTTPException(status_code=404, detail="fact not found")
    return {"ok": True}


class FactBulk(BaseModel):
    ids: list[int]
    action: str  # delete | pin | unpin | supersede
    user_email: str
    force: bool = False


@router.post("/facts/bulk")
async def bulk_facts(body: FactBulk, user=Depends(require_project_access)):
    """Apply one action to many facts at once (organize without clicking
    item by item). ``supersede`` marks them obsolete (reversible via the
    "show obsolete" filter) by rolling them up into the user's newest live
    fact; ``delete`` is permanent."""
    owner = _norm(await resolve_scoped_email(body.user_email, user))
    ids = [int(i) for i in (body.ids or []) if i]
    action = (body.action or "").strip().lower()
    if not ids:
        raise HTTPException(status_code=400, detail="ids is required")
    if action not in ("delete", "pin", "unpin", "supersede"):
        raise HTTPException(status_code=400, detail="invalid action")
    affected = 0
    async with app_write() as s:
        if action == "delete":
            res = await s.execute(
                sql_delete(UserMemory).where(
                    UserMemory.id.in_(ids),
                    UserMemory.user_email == owner,
                )
            )
            affected = res.rowcount or 0
        elif action in ("pin", "unpin"):
            res = await s.execute(
                update(UserMemory)
                .where(
                    UserMemory.id.in_(ids),
                    UserMemory.user_email == owner,
                )
                .values(pinned=(action == "pin"), updated_at=datetime.now(timezone.utc))
            )
            affected = res.rowcount or 0
        else:  # supersede -> mark obsolete, rolled up into newest live fact
            pin_clause = [] if body.force else [UserMemory.pinned.is_(False)]
            target = (await s.execute(
                select(UserMemory.id)
                .where(
                    UserMemory.user_email == owner,
                    UserMemory.superseded_by.is_(None),
                    UserMemory.id.notin_(ids),
                    *pin_clause,
                )
                .order_by(UserMemory.updated_at.desc())
                .limit(1)
            )).scalar()
            if target is None:
                await s.commit()
                return {"ok": True, "affected": 0}
            res = await s.execute(
                update(UserMemory)
                .where(
                    UserMemory.user_email == owner,
                    UserMemory.id.in_(ids),
                    UserMemory.superseded_by.is_(None),
                    *([] if body.force else [UserMemory.pinned.is_(False)]),
                )
                .values(superseded_by=int(target))
            )
            affected = res.rowcount or 0
        await s.commit()
    return {"ok": True, "affected": affected}


@router.delete("/episodes/{episode_id}")
async def delete_episode(
    episode_id: int,
    user_email: str = Query(...),
    user=Depends(require_project_access),
):
    owner = _norm(await resolve_scoped_email(user_email, user))
    async with app_write() as s:
        res = await s.execute(
            sql_delete(UserEpisode).where(
                UserEpisode.id == episode_id,
                UserEpisode.user_email == owner,
            )
        )
        await s.commit()
    if not res.rowcount:
        raise HTTPException(status_code=404, detail="episode not found")
    return {"ok": True}


class EpisodeUpdate(BaseModel):
    summary: str | None = None
    outcome: str | None = None


@router.patch("/episodes/{episode_id}")
async def update_episode(
    episode_id: int,
    body: EpisodeUpdate,
    user_email: str = Query(...),
    user=Depends(require_project_access),
):
    """Edit an episode's summary / outcome."""
    owner = _norm(await resolve_scoped_email(user_email, user))
    values: dict = {}
    if body.summary is not None:
        sm = body.summary.strip()
        if not sm:
            raise HTTPException(status_code=400, detail="summary must not be empty")
        values["summary"] = sm
    if body.outcome is not None:
        values["outcome"] = body.outcome or None
    if not values:
        return {"ok": True}
    async with app_write() as s:
        res = await s.execute(
            update(UserEpisode)
            .where(UserEpisode.id == episode_id, UserEpisode.user_email == owner)
            .values(**values)
        )
        await s.commit()
    if not res.rowcount:
        raise HTTPException(status_code=404, detail="episode not found")
    return {"ok": True}


@router.delete("/files/{file_id}")
async def delete_file(
    file_id: int,
    user_email: str = Query(...),
    user=Depends(require_project_access),
):
    """Remove a file from the memory INDEX (the bytes on the workspace
    PVC are left untouched — manage those from the Workspace tab)."""
    owner = _norm(await resolve_scoped_email(user_email, user))
    async with app_write() as s:
        res = await s.execute(
            sql_delete(UserFile).where(
                UserFile.id == file_id,
                UserFile.user_email == owner,
            )
        )
        await s.commit()
    if not res.rowcount:
        raise HTTPException(status_code=404, detail="file not found")
    return {"ok": True}


MEMORY_MAINTENANCE_EVENT_TYPE = "memory_maintenance"


@router.post("/compact")
async def compact_memory_for_user(
    user_email: str = Query(...),
    user=Depends(require_project_access),
):
    """Optimise a user's memory.

    Runs the DETERMINISTIC compaction pass immediately (dedupe facts,
    dedupe indexed files, prune old episodes) AND enqueues an
    agent-driven ``memory_maintenance`` run so the agent can also merge
    semantically (with judgement) using the same memory tools. The
    enqueue is best-effort and never fails the operator request."""
    e = _norm(await resolve_scoped_email(user_email, user))
    # 1) Deterministic pass — reuse the MCP tool's shared core.
    from app.mcp_server import _compact_user_memory
    report = await _compact_user_memory(e)

    # 2) Enqueue an agent-driven compaction run (time-bucketed
    #    fingerprint dedupes accidental double-clicks within 5 min).
    enqueued = False
    try:
        bucket = int(time.time()) // 300
        fp = f"memmaint:{e}:{bucket}"
        async with app_write() as s:
            res = await s.execute(
                sqlite_insert(Event)
                .values(
                    fingerprint=fp,
                    type=MEMORY_MAINTENANCE_EVENT_TYPE,
                    source="operator:memory",
                    payload_json=json.dumps({"user_email": e}),
                    status="PENDING",
                    attempts=0,
                    max_attempts=1,
                    detected_at=datetime.now(timezone.utc),
                )
                .on_conflict_do_nothing(index_elements=[Event.fingerprint])
            )
            await s.commit()
            enqueued = bool(res.rowcount)
    except Exception:  # pragma: no cover — enqueue is best-effort
        logger.warning("memory_maintenance enqueue failed for %s", e, exc_info=True)

    return {"ok": True, "user_email": e, "report": report, "enqueued": enqueued}


@router.delete("/users/{user_email:path}")
async def forget_user(user_email: str, user=Depends(require_project_access)):
    """Forget EVERYTHING about a user (facts + episodes + files index).
    The workspace bytes are not deleted."""
    e = _norm(await resolve_scoped_email(user_email, user))
    async with app_write() as s:
        await s.execute(sql_delete(UserMemory).where(UserMemory.user_email == e))
        await s.execute(sql_delete(UserEpisode).where(UserEpisode.user_email == e))
        await s.execute(sql_delete(UserFile).where(UserFile.user_email == e))
        await s.commit()
    return {"ok": True}
