"""Helpers over the managed-users directory (``AppUser``, v7.20.0).

Central place for: role resolution (admin gating), who may DECIDE the actions the
employee parks (approvers), phone→email identity resolution (supersedes
ChannelContact), and the boot-time guarantee that the employee's own administrator
exists. Everything is best-effort and never raises into a request path.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.app_database import app_session, app_write
from app.app_models.users import (
    AppUser, ROLE_ADMIN, ROLE_MEMBER, SOURCE_DEPLOY, SOURCE_PROJECT, VALID_ROLES,
)

logger = logging.getLogger(__name__)

# Routes through which a person in the directory can be CONTACTED — and therefore the
# only valid values of ``metadata_json.preferred_channel``. Each one requires a piece of
# data in their record: email, E.164 phone or Telegram handle. Slack is left out ON PURPOSE
# (the record has no Slack handle, so that preference could never be delivered:
# ``signal_notify._resolve_address`` would return None and the notice would be silently lost).
#
# v7.158.2 — this domain was copied by hand in FIVE places (the route, two tools, the
# identity prompt and the Users dropdown), all five with ("email", "whatsapp").
# Result: Telegram, supported as a channel since 7.155.0 and already choosable by the
# notifier, could not be marked as anyone's preferred channel — it did not appear in the
# dropdown and the route returned 422 if someone sent it. One single place, and a test
# that cross-checks the five.
REACHABLE_CHANNELS: tuple[str, ...] = ("email", "whatsapp", "telegram")

#: Human (Spanish) label of each route. The model-facing identity line (memory_prompt) uses
#: the key itself, which is already English; this set is what gates it.
CHANNEL_LABELS_ES: dict[str, str] = {
    "email": "correo", "whatsapp": "WhatsApp", "telegram": "Telegram",
}

_E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")


def valid_phones(phones) -> list[str]:
    """Keep only well-formed E.164 numbers (drop the rest silently)."""
    return [str(p).strip() for p in (phones or []) if _E164_RE.match(str(p).strip())]


def _canon_phone(p) -> str:
    """Digits-only canonical form for comparison, so a stored ``+34600111222``
    matches an inbound ``34600111222`` / ``+34 600 111 222`` (channels strip
    spaces/dashes but never add the ``+``, so a raw string match silently fails
    and drops the sender into a fresh memory bucket)."""
    return re.sub(r"\D", "", str(p or ""))


async def upsert_directory_user(
    *, email=None, phones=None, display_name=None, alias=None, nicknames=None,
    role=None, source, created_by, allow_role=False, metadata=None,
) -> AppUser:
    """Insert-or-UPDATE a directory user, keyed by email when present, so the
    same person is never duplicated. On an existing row: phones/nicknames are
    UNIONED, non-empty scalars overwritten, the row reactivated. ``role`` is
    applied ONLY when ``allow_role`` (operator paths) — the agent path can never
    escalate to admin."""
    now = datetime.now(timezone.utc)
    e = (email or "").strip().lower() or None
    ph = valid_phones(phones)
    nn = [str(x).strip() for x in (nicknames or []) if str(x).strip()]
    async with app_write() as s:
        existing = None
        if e:
            existing = (await s.execute(
                select(AppUser).where(AppUser.email == e)
            )).scalars().first()
        if existing is not None:
            # Anti-escalation: the AGENT path (allow_role=False) must never be
            # able to REACTIVATE or reshape an admin row an operator/sync
            # deactivated — otherwise `find_user`/`create_user` could silently
            # restore admin access (role survives deactivation). Leave it as-is.
            if not allow_role and norm_role(existing.role) == ROLE_ADMIN:
                return existing
            if ph:
                existing.phones_json = dumps_list(loads_list(existing.phones_json) + ph)
            if nn:
                existing.nicknames_json = dumps_list(loads_list(existing.nicknames_json) + nn)
            if display_name:
                existing.display_name = str(display_name).strip()
            if alias:
                existing.alias = str(alias).strip()
            if allow_role and role:
                existing.role = norm_role(role)
            if metadata:
                existing.metadata_json = merge_meta(existing.metadata_json, metadata)
            existing.active = 1
            existing.updated_at = now
            await s.commit()
            await s.refresh(existing)
            return existing
        row = AppUser(
            email=e, phones_json=dumps_list(ph),
            display_name=(str(display_name).strip() if display_name else None),
            alias=(str(alias).strip() if alias else None),
            nicknames_json=dumps_list(nn),
            role=(norm_role(role) if allow_role else ROLE_MEMBER),
            source=source, active=1, created_by=created_by,
            metadata_json=dumps_meta(metadata or {}),
            created_at=now, updated_at=now,
        )
        s.add(row)
        try:
            await s.commit()
        except IntegrityError:
            # Lost the concurrent-insert race (another request created the same
            # email between our SELECT and INSERT — the UNIQUE index caught it).
            # Recover by merging into the row that won.
            await s.rollback()
            if e:
                return await upsert_directory_user(
                    email=e, phones=ph, display_name=display_name, alias=alias,
                    nicknames=nn, role=role, source=source,
                    created_by=created_by, allow_role=allow_role, metadata=metadata,
                )
            raise
        await s.refresh(row)
        return row


def _autonomous_email() -> str:
    """The deploy-time employee identity — ALWAYS an admin (anti-lockout)."""
    return (os.environ.get("AGENT_AUTONOMOUS_USER_EMAIL") or "").strip().lower()


def loads_list(raw: str | None) -> list[str]:
    try:
        v = json.loads(raw or "[]")
        return [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else []
    except Exception:
        return []


def dumps_list(items) -> str:
    seen, out = set(), []
    for x in items or []:
        s = str(x).strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return json.dumps(out)


def norm_role(value) -> str:
    r = (str(value or "")).strip().lower()
    return r if r in VALID_ROLES else ROLE_MEMBER


def loads_meta(raw: str | None) -> dict:
    """Parse the ``metadata_json`` bag (extensible per-user attributes:
    ``preferred_channel``, ``credit_cap``…). Never raises."""
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def dumps_meta(d) -> str:
    try:
        return json.dumps(d if isinstance(d, dict) else {})
    except Exception:
        return "{}"


def merge_meta(raw: str | None, updates: dict) -> str:
    """Merge ``updates`` into the existing metadata bag; a key set to ``None``
    is removed. Returns the new JSON text (for a PATCH that must not clobber
    other keys)."""
    d = loads_meta(raw)
    for k, v in (updates or {}).items():
        if v is None:
            d.pop(k, None)
        else:
            d[k] = v
    return dumps_meta(d)


async def resolve_role(email: str | None) -> str:
    """``admin`` | ``member`` for a login email. The deploy identity is ALWAYS
    admin (so an operator can never be locked out of Administración — this is the
    anti-lockout guarantee, and ``AGENT_AUTONOMOUS_USER_EMAIL`` is always present
    in the app pod); everyone else is whatever the directory says. Fails CLOSED
    to ``member`` when absent/on error — never grant admin implicitly, or a
    transient "no admin exists" state would silently promote every member."""
    e = (email or "").strip().lower()
    if not e:
        return ROLE_MEMBER
    if e == _autonomous_email():
        return ROLE_ADMIN
    try:
        async with app_session() as s:
            role = (await s.execute(
                select(AppUser.role).where(AppUser.email == e, AppUser.active == 1)
            )).scalars().first()
        return norm_role(role) if role else ROLE_MEMBER
    except Exception:  # pragma: no cover — gating must never 500
        logger.debug("resolve_role failed for %s", e, exc_info=True)
        return ROLE_MEMBER


async def esta_autorizado(email: str | None) -> bool:
    """Can this person USE this employee? (v9.0.0)

    Different from `resolve_role`, which answers "with which role" and fails to `member`.
    This answers "whether they get in or not", and fails CLOSED: no email, no.

    The deployment identity always gets in — it is the same anti-lockout guarantee that
    `resolve_role` gives, and without it a badly edited directory would leave the employee
    with nobody able to fix it.

    On a database error it lets them through. It is the only concession and it is
    deliberate: this check sits at the door of the WHOLE API, and turning a SQLite hiccup
    into a general 403 would switch the whole employee off for everyone. The cost of
    being wrong the other way —locking out someone who does have access while the database
    recovers— is worse than letting through for a few seconds someone already inside the
    project.
    """
    e = (email or "").strip().lower()
    if not e:
        return False
    if e == _autonomous_email():
        return True
    try:
        async with app_session() as s:
            fila = (await s.execute(
                select(AppUser.id).where(AppUser.email == e, AppUser.active == 1)
            )).scalars().first()
        return fila is not None
    except Exception:  # pragma: no cover — a DB failure cannot switch the employee off
        logger.warning("esta_autorizado falló para %s; se deja pasar", e, exc_info=True)
        return True


async def is_admin(email: str | None) -> bool:
    return (await resolve_role(email)) == ROLE_ADMIN


async def can_approve(email: str | None) -> bool:
    """May this person DECIDE (approve/reject) an action the employee parked?

    Admins always may — governing the employee includes letting it act. Beyond
    them, an admin can designate named people (``app_users.can_approve``): the
    person responsible for the business call is often not the one who administers
    the employee, and forcing that person to be an admin handed them the whole
    governance panel just to say yes. Fails CLOSED on error/absence."""
    e = (email or "").strip().lower()
    if not e:
        return False
    if await is_admin(e):
        return True
    try:
        async with app_session() as s:
            flag = (await s.execute(
                select(AppUser.can_approve).where(
                    AppUser.email == e, AppUser.active == 1,
                )
            )).scalars().first()
        return bool(flag)
    except Exception:  # pragma: no cover — gating must never 500
        logger.debug("can_approve failed for %s", e, exc_info=True)
        return False


async def list_approvers() -> list[dict]:
    """Who can decide, so a member facing a 403 knows whom to ask instead of
    guessing. Admins + designated people, active only; a channel-only contact is
    excluded because they cannot log in to decide."""
    try:
        async with app_session() as s:
            rows = list((await s.execute(
                select(AppUser).where(AppUser.active == 1)
            )).scalars().all())
    except Exception:  # pragma: no cover
        logger.debug("list_approvers failed", exc_info=True)
        return []
    auto = _autonomous_email()
    out: list[dict] = []
    for r in rows:
        email = (r.email or "").strip().lower()
        if not email:
            continue
        admin = norm_role(r.role) == ROLE_ADMIN or email == auto
        if not (admin or bool(getattr(r, "can_approve", 0))):
            continue
        if loads_meta(r.metadata_json).get("channel_only") is True:
            continue
        out.append({
            "email": email,
            "display_name": r.display_name or r.alias,
            "role": ROLE_ADMIN if admin else norm_role(r.role),
            "designated": bool(getattr(r, "can_approve", 0)),
        })
    out.sort(key=lambda u: (u["display_name"] or u["email"]).lower())
    return out


async def bulk_add_project_users(
    entries: list[dict], *, role: str, created_by: str | None,
) -> dict:
    """Add several of the project's users in ONE write transaction — the default way
    to populate the directory now that nothing is mirrored automatically.

    ``entries`` are ``{email, name}`` dicts already checked against the platform's
    project-user list by the caller (only real members come through here, so
    everyone added this way can actually log in). Re-adding somebody MERGES
    (reactivates, keeps their role and contact points) instead of duplicating, so
    the operator can hit «añadir» twice without consequences. One transaction
    because a batch of fifty people must not become fifty trips through the write
    queue."""
    now = datetime.now(timezone.utc)
    r = norm_role(role)
    created, merged = [], []
    async with app_write() as s:
        for entry in entries or []:
            email = (str((entry or {}).get("email") or "")).strip().lower()
            if not email:
                continue
            name = (str((entry or {}).get("name") or "").strip() or None)
            row = (await s.execute(
                select(AppUser).where(AppUser.email == email)
            )).scalars().first()
            if row is not None:
                row.active = 1
                row.source = row.source or SOURCE_PROJECT
                if name and not row.display_name:
                    row.display_name = name
                # Membership is proven: they are NOT a channel-only contact.
                row.metadata_json = merge_meta(row.metadata_json, {"channel_only": False})
                row.updated_at = now
                merged.append(email)
                continue
            s.add(AppUser(
                email=email, display_name=name, role=r, source=SOURCE_PROJECT,
                active=1, created_by=created_by,
                metadata_json=dumps_meta({"channel_only": False}),
                created_at=now, updated_at=now,
            ))
            created.append(email)
        await s.commit()
    return {"created": created, "merged": merged}


async def resolve_directory_email(term: str) -> str | None:
    """Resolve a free term (name/alias/nickname/phone/exact email) to a
    directory user's canonical email. Returns None when there is NO match OR
    when the match is AMBIGUOUS (two+ distinct users match, e.g. two "Juan").
    An ambiguous None forces the caller to disambiguate instead of silently
    picking the wrong person — never guess who something is addressed to.

    Canonical resolver for EVERY person-reference in the app (calendar
    ``assigned_to``, MCP task/communication/approval recipients, …); the UI
    counterpart is ``components/UserPicker.tsx`` (v7.24.0)."""
    t = (term or "").strip().lower()
    if not t:
        return None
    tphone = _canon_phone(t)
    async with app_session() as s:
        rows = list((await s.execute(
            select(AppUser).where(AppUser.active == 1)
        )).scalars().all())
    matches: set[str] = set()
    for r in rows:
        if not r.email:
            continue
        names = [r.display_name, r.alias, *loads_list(r.nicknames_json)]
        if (t == r.email.strip().lower()
                or any((c or "").strip().lower() == t for c in names)
                or (tphone and any(_canon_phone(p) == tphone
                                   for p in loads_list(r.phones_json)))):
            matches.add(r.email.strip().lower())
    return matches.pop() if len(matches) == 1 else None


async def email_for_phone(phone: str) -> str | None:
    """Resolve a WhatsApp/E.164 phone → the contact's email via the directory.
    Best-effort; returns None when unmapped."""
    p = (phone or "").strip()
    if not p or "@" in p:
        return None
    target = _canon_phone(p)
    if not target:
        return None
    try:
        async with app_session() as s:
            rows = (await s.execute(
                select(AppUser.email, AppUser.phones_json).where(AppUser.active == 1)
            )).all()
        for email, phones_json in rows:
            if email and any(_canon_phone(x) == target for x in loads_list(phones_json)):
                return email.strip().lower()
    except Exception:  # pragma: no cover
        logger.debug("email_for_phone failed for %s", phone, exc_info=True)
    return None


async def email_for_telegram(handle: str) -> str | None:
    """Resolve a Telegram @username (lowercased, no '@') → the contact's email via
    the directory (``AppUser.telegram``). Best-effort; None when unmapped."""
    h = (handle or "").strip().lstrip("@").lower()
    if not h:
        return None
    try:
        async with app_session() as s:
            row = (await s.execute(
                select(AppUser.email).where(
                    AppUser.active == 1,
                    func.lower(AppUser.telegram) == h,
                    AppUser.email.isnot(None),
                )
            )).first()
        if row and row[0]:
            return row[0].strip().lower()
    except Exception:  # pragma: no cover
        logger.debug("email_for_telegram failed for %s", handle, exc_info=True)
    return None


async def telegram_for_email(email: str) -> str | None:
    """directory email → their Telegram handle (without "@"). Inverse of
    :func:`email_for_telegram`, so we can WRITE to them, not just recognise them.

    v7.155.0 — it was missing, and that is why Telegram was never chosen as the route to
    notify a person: we knew how to recognise whoever wrote, but not whom to write to.
    """
    e = (email or "").strip().lower()
    if not e or "@" not in e:
        return None
    try:
        async with app_session() as s:
            h = (await s.execute(
                select(AppUser.telegram).where(
                    AppUser.active == 1, AppUser.email == e,
                )
            )).scalars().first()
        return (h or "").strip().lstrip("@").lower() or None
    except Exception:  # pragma: no cover
        logger.debug("telegram_for_email failed for %s", email, exc_info=True)
    return None


async def phone_for_email(email: str) -> str | None:
    """Resolve a directory email → the contact's first E.164 phone. Inverse
    of :func:`email_for_phone`, used to materialise a directory reference
    into a WhatsApp handle at dispatch time (v7.24.0). Best-effort; returns
    None when the person has no phone (or no such user)."""
    e = (email or "").strip().lower()
    if not e or "@" not in e:
        return None
    try:
        async with app_session() as s:
            phones_json = (await s.execute(
                select(AppUser.phones_json).where(
                    AppUser.active == 1, AppUser.email == e,
                )
            )).scalars().first()
        for p in loads_list(phones_json):
            if (p or "").strip():
                return p.strip()
    except Exception:  # pragma: no cover
        logger.debug("phone_for_email failed for %s", email, exc_info=True)
    return None


async def _reconcile_email_uniqueness() -> None:
    """For an app EVOLVING on an existing DB (``create_all`` never ALTERs a
    table, so the unique index isn't added retroactively): merge any pre-existing
    email duplicates into the lowest-id row (union phones/nicknames, keep admin
    role / active if any duplicate had it), then create the partial UNIQUE index
    best-effort. No-op on a fresh DB (the model already created the index).
    Never raises."""
    try:
        async with app_write() as s:
            dup_emails = (await s.execute(
                select(AppUser.email).where(AppUser.email.isnot(None))
                .group_by(AppUser.email).having(func.count() > 1)
            )).scalars().all()
            for em in dup_emails:
                rows = list((await s.execute(
                    select(AppUser).where(AppUser.email == em).order_by(AppUser.id.asc())
                )).scalars().all())
                keeper, *dupes = rows
                for d in dupes:
                    keeper.phones_json = dumps_list(
                        loads_list(keeper.phones_json) + loads_list(d.phones_json))
                    keeper.nicknames_json = dumps_list(
                        loads_list(keeper.nicknames_json) + loads_list(d.nicknames_json))
                    keeper.display_name = keeper.display_name or d.display_name
                    keeper.alias = keeper.alias or d.alias
                    if norm_role(d.role) == ROLE_ADMIN:
                        keeper.role = ROLE_ADMIN
                    if d.active:
                        keeper.active = 1
                    await s.delete(d)
            await s.commit()
        # Separate txn: the index create may fail if a race left a dup we didn't
        # catch — don't let that roll back the merge above.
        async with app_write() as s:
            await s.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_app_users_email "
                "ON app_users(email) WHERE email IS NOT NULL"
            ))
            await s.commit()
    except Exception:  # pragma: no cover — best-effort, must never block boot
        logger.debug("email-uniqueness reconciliation skipped", exc_info=True)


async def _absorb_channel_contacts() -> None:
    """One-way migration (v7.24.0): move the legacy ``ChannelContact`` rows
    (WhatsApp phone → email map) into the directory — the phone joins the
    person's ficha (created if missing) and the legacy row is DELETED, so
    the migration is naturally once-only and an operator's later phone
    removal is never resurrected on restart. Rows that fail to absorb are
    kept and retried on the next boot. Never raises."""
    try:
        from sqlalchemy import delete as _delete
        from app.app_models.channels import ChannelContact
        async with app_session() as s:
            rows = list((await s.execute(select(ChannelContact))).scalars().all())
        absorbed = 0
        from app.agent_sessions import normalize_channel_handle
        for c in rows:
            email = (c.user_email or "").strip().lower()
            # Normalise the legacy handle (strip ``whatsapp:``/spaces) and
            # coerce to E.164 (add ``+`` for a bare digit string) BEFORE
            # validating — otherwise a ``whatsapp:+34…`` or ``34…`` row is
            # "unmappable" and its phone→email mapping is lost permanently
            # (the runtime no longer reads ChannelContact).
            phone = normalize_channel_handle(c.handle)
            if phone and not phone.startswith("+") and phone.isdigit():
                phone = "+" + phone
            if not email or not valid_phones([phone]):
                # Truly unmappable (no email / non-phone handle): keep inert.
                continue
            try:
                await upsert_directory_user(
                    email=email, phones=[phone],
                    display_name=(c.display_name or None),
                    source="migration", created_by="channel_contacts",
                )
            except Exception:
                logger.warning(
                    "channel-contact %s → %s not absorbed (will retry next boot)",
                    phone, email, exc_info=True,
                )
                continue
            async with app_write() as s:
                await s.execute(_delete(ChannelContact).where(ChannelContact.id == c.id))
                await s.commit()
            absorbed += 1
        if absorbed:
            logger.info(
                "absorbed %d legacy channel contact(s) into the users directory",
                absorbed,
            )
    except Exception:  # pragma: no cover
        logger.debug("channel-contacts migration skipped", exc_info=True)


def _seed_display_name(email: str) -> str | None:
    """The person's name as the platform knew it at deploy time. All that survives
    of ``USERS_SEED``: enough to give the admin a ficha with a readable name
    instead of a bare email, without seeding anybody else."""
    try:
        seed = json.loads(os.environ.get("USERS_SEED") or "[]")
    except Exception:
        return None
    for entry in seed if isinstance(seed, list) else []:
        if not isinstance(entry, dict):
            continue
        if (str(entry.get("email") or "")).strip().lower() == email:
            return (str(entry.get("name") or "").strip() or None)
    return None


async def bootstrap_directory() -> None:
    """At boot: (0) reconcile email uniqueness (dedup + unique index for evolving
    DBs), (1) absorb legacy channel contacts, and (2) ALWAYS guarantee the deploy
    identity is an active admin. Never raises.

    Since v7.145.0 it seeds NOTHING ELSE: a new employee is born with exactly one
    user — **its own administrator** — and the rest of the directory is composed on
    purpose from the Usuarios page (in bulk from the project's users, or as a
    channel-only contact). Mirroring the project was worse than it looked: the
    deploy snapshot resurrected people an operator had deleted, and the live sync
    DEACTIVATED whoever left the project, so the employee's directory (and with it
    who is admin, who gets notified, who the agent recognises) changed under the
    operator's feet. Existing employees keep every user they already have — this
    only stops writing new ones behind the operator's back.

    The admin row is tagged ``source=deploy``, not ``project``: it is a service
    identity, never a project membership."""
    now = datetime.now(timezone.utc)
    await _reconcile_email_uniqueness()
    await _absorb_channel_contacts()
    auto = _autonomous_email()
    if not auto:
        return
    name = _seed_display_name(auto)
    try:
        async with app_write() as s:
            row = (await s.execute(
                select(AppUser).where(AppUser.email == auto)
            )).scalars().first()
            if row is None:
                s.add(AppUser(
                    email=auto, display_name=name, role=ROLE_ADMIN,
                    source=SOURCE_DEPLOY, active=1, created_by="seed",
                    created_at=now, updated_at=now,
                ))
            else:
                row.role = ROLE_ADMIN
                row.active = 1
                row.source = SOURCE_DEPLOY  # migrate legacy source=project rows
                if name and not row.display_name:
                    row.display_name = name
            await s.commit()
    except Exception:  # pragma: no cover — the admin guarantee must never block boot
        logger.warning("users directory bootstrap failed (ignored)", exc_info=True)
