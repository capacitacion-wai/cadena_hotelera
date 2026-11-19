"""Managed users — the employee's contact directory (v7.20.0).

CRUD over ``AppUser`` + ``/me`` (the caller's own role and whether they can decide
parked actions, so the SPA can gate Administración and Aprobaciones) + ``/bulk``
(add several of the project's users at once) + ``/approvers`` (whom to ask for a
decision).

The directory is composed BY HAND (v7.145.0): there is no ``/sync`` any more and
nothing is mirrored from the project automatically — see ``bootstrap_directory``
for why a mirror was worse than it looked.

Access model: reading the directory needs project access (used by recipient
pickers and the agent); creating/editing/deleting and — crucially — setting the
``admin`` role or the approver flag require the **admin** role
(``require_admin``). This is what makes the role operator-only: a member cannot
promote anyone, nor make themselves an approver.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query
from sqlalchemy import delete as sql_delete, select, update

from app.app_database import app_session, app_write
from app.app_models.users import AppUser, ROLE_ADMIN, SOURCE_MANUAL
from app.auth import require_admin, require_project_access, TokenPayload
from app.timeutil import iso_utc
from app.users_directory import (
    bulk_add_project_users, can_approve as can_approve_email, dumps_list,
    list_approvers, loads_list, loads_meta, merge_meta, norm_role, resolve_role,
    upsert_directory_user,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agent", tags=["users"])

_E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")


def _row(r: AppUser) -> dict[str, Any]:
    meta = loads_meta(r.metadata_json)
    return {
        "id": r.id,
        "email": r.email,
        "phones": loads_list(r.phones_json),
        "telegram": getattr(r, "telegram", None),
        "display_name": r.display_name,
        "alias": r.alias,
        "nicknames": loads_list(r.nicknames_json),
        "role": r.role,
        # May decide the actions the employee parks without being an admin.
        # Admins always can, so the UI shows the effective answer, not the raw flag.
        "can_approve": bool(getattr(r, "can_approve", 0)) or norm_role(r.role) == ROLE_ADMIN,
        "designated_approver": bool(getattr(r, "can_approve", 0)),
        "source": r.source,
        "active": bool(r.active),
        # Optional per-user attributes (metadata bag): where to reach them and a
        # personal credit cap. Absent → null.
        "preferred_channel": meta.get("preferred_channel"),
        "credit_cap": meta.get("credit_cap"),
        # True = not a WAI project member → cannot authenticate (channel-only
        # contact). None = unknown (membership couldn't be checked). Clarity signal.
        "channel_only": meta.get("channel_only"),
        "updated_at": iso_utc(r.updated_at),
    }


def _meta_updates(body: dict[str, Any]) -> dict[str, Any]:
    """Extract the metadata-bag updates (``preferred_channel``, ``credit_cap``)
    present in a create/patch body. A value that clears the attribute maps to
    ``None`` (merge_meta removes it)."""
    out: dict[str, Any] = {}
    if "preferred_channel" in body:
        from app.users_directory import REACHABLE_CHANNELS
        pref = (str(body.get("preferred_channel") or "")).strip().lower()
        if pref not in ("", "auto", *REACHABLE_CHANNELS):
            raise HTTPException(
                422,
                "preferred_channel must be one of "
                f"{', '.join(REACHABLE_CHANNELS)} or empty",
            )
        out["preferred_channel"] = pref if pref in REACHABLE_CHANNELS else None
    if "credit_cap" in body:
        raw = body.get("credit_cap")
        if raw in (None, "", 0, "0"):
            out["credit_cap"] = None
        else:
            try:
                cap = float(raw)
            except (TypeError, ValueError):
                raise HTTPException(422, "credit_cap must be a number")
            if cap < 0:
                raise HTTPException(422, "credit_cap must be >= 0")
            out["credit_cap"] = cap if cap > 0 else None
    return out


def _validate(body: dict[str, Any], *, partial: bool) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "email" in body or not partial:
        email = (str(body.get("email") or "")).strip().lower() or None
        if email and ("@" not in email or "." not in email.split("@")[-1]):
            raise HTTPException(422, "email must be valid or empty")
        out["email"] = email
    if "phones" in body:
        phones = body.get("phones") or []
        if not isinstance(phones, list):
            raise HTTPException(422, "phones must be a list")
        for p in phones:
            if not _E164_RE.match(str(p).strip()):
                raise HTTPException(422, f"phone {p!r} must be E.164 (+34600111222)")
        out["phones_json"] = dumps_list(phones)
    if "nicknames" in body:
        nn = body.get("nicknames") or []
        if not isinstance(nn, list):
            raise HTTPException(422, "nicknames must be a list")
        out["nicknames_json"] = dumps_list(nn)
    if "telegram" in body:
        # Telegram @username — normalise to lowercase, no leading '@'. Resolves a
        # Telegram sender to this person (email_for_telegram), like phones for WhatsApp.
        out["telegram"] = (str(body.get("telegram") or "").strip().lstrip("@").lower() or None)
    if "display_name" in body:
        out["display_name"] = (str(body.get("display_name") or "").strip() or None)
    if "alias" in body:
        out["alias"] = (str(body.get("alias") or "").strip() or None)
    if "role" in body:
        out["role"] = norm_role(body.get("role"))
    if "can_approve" in body:
        out["can_approve"] = 1 if body.get("can_approve") else 0
    if "active" in body:
        out["active"] = 1 if body.get("active") else 0
    # A directory entry must be reachable somehow.
    if not partial:
        if not out.get("email") and not loads_list(out.get("phones_json")):
            raise HTTPException(422, "a user needs at least an email or a phone")
    return out


@router.get("/users")
async def list_users(
    q: str = Query("", description="search in email/name/alias/nicknames/phone"),
    include_inactive: bool = Query(False),
    user: TokenPayload = Depends(require_project_access),
):
    async with app_session() as s:
        stmt = select(AppUser).order_by(AppUser.display_name.asc(), AppUser.email.asc())
        if not include_inactive:
            stmt = stmt.where(AppUser.active == 1)
        rows = list((await s.execute(stmt)).scalars().all())
    ql = q.strip().lower()
    items = [_row(r) for r in rows]
    if ql:
        def _hit(it: dict) -> bool:
            hay = " ".join(str(x) for x in (
                it["email"], it["display_name"], it["alias"],
                " ".join(it["nicknames"]), " ".join(it["phones"]),
            ) if x).lower()
            return ql in hay
        items = [it for it in items if _hit(it)]
    return {"items": items}


@router.get("/users/me")
async def whoami(user: TokenPayload = Depends(require_project_access)):
    """The caller's role — the SPA reads this to show/hide Administración. The role
    is set by the employee's DIRECTORY (``resolve_role``, with anti-lockout for the
    deployment identity), NOT by the platform's ``is_superuser``: a WAI admin can be
    ``member`` in this particular employee (INC-AGO-39)."""
    role = await resolve_role(user.email)
    return {
        "email": user.email,
        "role": role,
        "is_admin": role == ROLE_ADMIN,
        # Deciding parked actions is a SEPARATE permission from administering the
        # employee, so the Aprobaciones screen must ask for it explicitly instead
        # of assuming "admin" (a designated approver is usually a plain member).
        "can_approve": await can_approve_email(user.email),
    }


@router.post("/users")
async def create_user(
    body: dict[str, Any] = Body(...),
    authorization: str = Header(None),
    user: TokenPayload = Depends(require_admin),
):
    values = _validate(body, partial=False)
    # Upsert by email so an operator re-adding the same person MERGES instead
    # of duplicating (duplicates broke role/phone resolution). Operator path →
    # role is honoured (allow_role=True). Detect a pre-existing email up front so
    # the UI can warn "merged with existing" rather than a silent overwrite.
    merged = False
    if values.get("email"):
        async with app_session() as s:
            merged = bool((await s.execute(
                select(AppUser.id).where(AppUser.email == values["email"])
            )).scalars().first())
    # channel_only: authoritative cross-check against the project's user list.
    meta = _meta_updates(body) or {}
    flag = await _channel_only_flag(authorization, values.get("email"))
    notice: str | None = None
    if flag is not None:
        meta["channel_only"] = flag
        # The operator asked for a channel-only contact but this email IS a project
        # member: say so instead of stamping a lie that would hide the fact they
        # can log in (and wrongly block them from being an approver).
        if bool(body.get("channel_only")) and flag is False:
            notice = "is_project_member"
    elif body.get("channel_only") is not None:
        # Membership unverifiable (platform unreachable) → trust the operator's
        # explicit intent rather than leaving it unknown.
        meta["channel_only"] = bool(body.get("channel_only"))
    if meta.get("channel_only") is True:
        # A contact who cannot log in is only useful if the employee can REACH
        # them; without a phone or a Telegram handle it is a dead entry that
        # silently never receives anything.
        if not loads_list(values.get("phones_json")) and not values.get("telegram"):
            raise HTTPException(
                422,
                "a channel-only contact needs a phone or a Telegram handle: they "
                "cannot log in, so a channel is the only way to reach them",
            )
        if values.get("can_approve"):
            raise HTTPException(
                422,
                "a channel-only contact cannot be an approver: decisions are taken "
                "in the panel and they cannot log in",
            )
    row = await upsert_directory_user(
        email=values.get("email"),
        phones=loads_list(values.get("phones_json")),
        display_name=values.get("display_name"),
        alias=values.get("alias"),
        nicknames=loads_list(values.get("nicknames_json")),
        role=values.get("role"),
        source=SOURCE_MANUAL,
        created_by=getattr(user, "email", None),
        allow_role=True,
        metadata=meta or None,
    )
    # telegram + can_approve aren't kwargs of upsert_directory_user (kept lean) —
    # set them on the resolved row when provided (create or merge-into-existing).
    extras = {k: values[k] for k in ("telegram", "can_approve") if values.get(k) is not None}
    if extras and row is not None:
        async with app_write() as s:
            await s.execute(update(AppUser).where(AppUser.id == row.id).values(**extras))
            await s.commit()
            row = (await s.execute(select(AppUser).where(AppUser.id == row.id))).scalar_one()
    return {**_row(row), "merged": merged, "notice": notice}


@router.patch("/users/{item_id}")
async def update_user(
    item_id: int,
    body: dict[str, Any] = Body(...),
    authorization: str = Header(None),
    user: TokenPayload = Depends(require_admin),
):
    values = _validate(body, partial=True)
    meta_up = _meta_updates(body)
    # If the email is being (re)set, recompute channel_only against the project.
    if "email" in values:
        flag = await _channel_only_flag(authorization, values.get("email"))
        if flag is not None:
            meta_up = {**(meta_up or {}), "channel_only": flag}
    if not values and not meta_up:
        raise HTTPException(422, "no updatable fields supplied")
    values["updated_at"] = datetime.now(timezone.utc)
    async with app_write() as s:
        # Guard against pointing this row at an email already used by ANOTHER
        # entry (there is no DB unique constraint; a dup would break identity
        # resolution). Merge into that other row by hand if you meant to.
        if values.get("email"):
            clash = (await s.execute(
                select(AppUser.id).where(
                    AppUser.email == values["email"], AppUser.id != item_id
                )
            )).scalars().first()
            if clash:
                raise HTTPException(409, f"another user already has {values['email']}")
        existing_meta = None
        if meta_up or values.get("can_approve"):
            existing_meta = (await s.execute(
                select(AppUser.metadata_json).where(AppUser.id == item_id)
            )).scalars().first()
        if meta_up:
            # Merge into the existing bag so we never clobber other metadata keys.
            values["metadata_json"] = merge_meta(existing_meta, meta_up)
        if values.get("can_approve"):
            # Same boundary as on create: someone who cannot log in cannot decide.
            resulting = loads_meta(values.get("metadata_json") or existing_meta)
            if resulting.get("channel_only") is True:
                raise HTTPException(
                    422,
                    "a channel-only contact cannot be an approver: decisions are "
                    "taken in the panel and they cannot log in",
                )
        res = await s.execute(
            update(AppUser).where(AppUser.id == item_id).values(**values)
        )
        if res.rowcount == 0:
            raise HTTPException(404, f"user {item_id} not found")
        await s.commit()
        row = (await s.execute(select(AppUser).where(AppUser.id == item_id))).scalar_one()
    return _row(row)


@router.delete("/users/{item_id}")
async def delete_user(
    item_id: int,
    user: TokenPayload = Depends(require_admin),
):
    async with app_write() as s:
        res = await s.execute(sql_delete(AppUser).where(AppUser.id == item_id))
        await s.commit()
    return {"deleted": bool(res.rowcount)}


@router.get("/users/approvers")
async def approvers(_: TokenPayload = Depends(require_project_access)):
    """Who can decide the actions the employee parks — read by the Aprobaciones
    screen so a member who cannot decide is told WHOM to ask instead of just
    seeing the buttons fail. Project access (not admin): knowing whom to ask is
    not a privilege."""
    return {"items": await list_approvers()}


@router.post("/users/bulk")
async def bulk_add_users(
    body: dict[str, Any] = Body(...),
    authorization: str = Header(None),
    user: TokenPayload = Depends(require_admin),
):
    """Add several of the project's users at once — the default way to populate the
    directory since v7.145.0 (nothing is mirrored automatically any more).

    Every email is checked against the platform's live project-user list: this
    path only admits REAL members (so everyone added here can actually log in),
    and an email that is not one comes back in ``skipped`` instead of silently
    becoming a contact that can never authenticate — for that there is the
    explicit channel-only contact. Idempotent: re-adding someone merges."""
    emails = body.get("emails")
    if not isinstance(emails, list) or not emails:
        raise HTTPException(422, "emails must be a non-empty list")
    wanted = {(str(e or "")).strip().lower() for e in emails if str(e or "").strip()}
    if not wanted:
        raise HTTPException(422, "emails must be a non-empty list")
    role = norm_role(body.get("role"))

    fetched = await _fetch_project_users(authorization)
    if fetched is None:
        raise HTTPException(
            503,
            "the WAI platform is unreachable, so project membership cannot be "
            "verified right now — retry, or add the person as a contact",
        )
    members = {
        (str(x.get("email") or "")).strip().lower():
            (x.get("name") or x.get("display_name") or "")
        for x in fetched if isinstance(x, dict) and (x.get("email") or "")
    }
    entries = [{"email": e, "name": members[e]} for e in sorted(wanted) if e in members]
    skipped = sorted(wanted - set(members))
    result = await bulk_add_project_users(
        entries, role=role, created_by=getattr(user, "email", None),
    )
    return {**result, "skipped": skipped}


# ── Project-membership hybrid (v7.129.0) ─────────────────────────────
# The employee app does NOT own auth — a person can log in ONLY if the WAI
# platform issues them a JWT carrying this PROJECT_ID. So a directory person who
# is NOT a project member can never authenticate; they are a CHANNEL-ONLY contact
# (reachable via email/WhatsApp/Telegram). We cross-check membership against the
# platform's project-user list and record it as a clarity signal (metadata
# ``channel_only``) — never a new security boundary.
async def _fetch_project_users(authorization: str | None) -> list[dict] | None:
    """Live pull of the project's users from the platform (forwards the caller's
    token). Returns a list of dicts or None when unavailable (no token / down)."""
    from app.config import WAI_BACKEND_URL, PROJECT_ID
    if not (WAI_BACKEND_URL and authorization):
        return None
    try:
        import httpx
        # v7.155.0 — the path was `/api/v1/projects/...` and the platform mounts its API
        # under `/backend` (like ALL the other calls of this employee: budget-status,
        # write-grant, runtime-write-policy…). It returned 404, the code read it as
        # "platform down" and the screen said "the WAI platform is not responding right
        # now" — when it was responding perfectly and the error was ours. Verified
        # against the cluster: /api/v1/... → 404, /backend/projects/{id}/users → 401.
        url = f"{WAI_BACKEND_URL.rstrip('/')}/backend/projects/{PROJECT_ID}/users"
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.get(url, headers={"Authorization": authorization})
        if r.status_code >= 400:
            logger.warning("project-users fetch: HTTP %s", r.status_code)
            return None
        body = r.json()
        data = body.get("data") if isinstance(body, dict) else body
        return data if isinstance(data, list) else None
    except Exception:
        logger.warning("project-users fetch failed", exc_info=True)
        return None


async def _channel_only_flag(authorization: str | None, email: str | None) -> bool | None:
    """True when ``email`` is NOT a project member (→ cannot authenticate, channel
    only). None when membership can't be determined (platform unreachable) — then
    the flag is left as-is (unknown), never guessed."""
    e = (email or "").strip().lower()
    if not e:
        return None
    fetched = await _fetch_project_users(authorization)
    if fetched is None:
        return None
    members = {(str(x.get("email") or "")).strip().lower()
               for x in fetched if isinstance(x, dict)}
    return e not in members


@router.get("/users/project-available")
async def project_available_users(
    authorization: str = Header(None),
    user: TokenPayload = Depends(require_admin),
):
    """The project's users, each flagged with whether they are already in the
    directory — feeds the bulk 'add from project' table, which shows EVERYBODY (the
    ones already added appear ticked and locked) so the admin can see the whole
    project at a glance instead of a list with mysterious gaps. Picking from here
    also guarantees a REAL, authenticatable person instead of a typed email.

    ``source=unavailable`` with an empty list when the platform is unreachable (the
    UI then offers manual entry)."""
    fetched = await _fetch_project_users(authorization)
    if fetched is None:
        return {"users": [], "available": [], "source": "unavailable"}
    async with app_session() as s:
        existing = {
            (e or "").strip().lower()
            for (e,) in (await s.execute(
                select(AppUser.email).where(
                    AppUser.email.isnot(None), AppUser.active == 1,
                )
            )).all()
        }
    out: list[dict[str, Any]] = []
    for entry in fetched:
        if not isinstance(entry, dict):
            continue
        email = (str(entry.get("email") or "")).strip().lower()
        if not email:
            continue
        out.append({
            "email": email,
            "name": entry.get("name") or entry.get("display_name") or "",
            "in_directory": email in existing,
        })
    out.sort(key=lambda u: (u["name"] or u["email"]).lower())
    return {
        "users": out,
        # Kept for the pickers that only ever wanted the addable ones.
        "available": [u for u in out if not u["in_directory"]],
        "source": "live",
    }
