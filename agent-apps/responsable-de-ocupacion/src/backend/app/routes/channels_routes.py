"""REST API for the operator's "Canales" section.

Surface (all under ``/api/channels``):

  GET    /                       list all channels for the operator
  POST   /                       create a channel (OAuth | IMAP)
  GET    /capabilities           UI introspection (allowed kinds / auths)
  GET    /{id}                   detail
  PATCH  /{id}                   update non-credential config
  DELETE /{id}                   delete + best-effort disconnect
  POST   /{id}/authorize         (re)start OAuth or accept IMAP creds
  POST   /{id}/test              one-shot adapter.test_connection
  GET    /{id}/messages          paginated history
  POST   /oauth/callback         RETIRED (14.0.0) — always 410

All routes require the same JWT the rest of the SPA uses
(``require_project_access``). Per-tenant authorization (which operator
can manage which channel) is implicit: the local SQLite is owned by
the agent-app pod, which is one-per-app, and the JWT is gated to that
app by the SPA's login flow.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.app_database import app_session, app_write
from app.app_models.channels import Channel, ChannelMessage
from app.auth import require_project_access, require_admin, TokenPayload
from app.channels.encryption import encrypt_blob, encryption_available
from app.channels.imap_smtp import autodetect_servers
from app.config import (
    AUTH_SERVICE_URL,
    CHANNELS_ALLOWED_AUTH_METHODS,
    CHANNELS_ALLOWED_KINDS,
    CHANNELS_DEFAULTS,
    CHANNELS_ENABLED,
    CHANNELS_MIN_POLL_INTERVAL_SEC,
)
from app.mcp_connectors import is_enabled as _composio_disponible

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/channels", tags=["channels"])


# ─────────────────────────────────────────────────────────────────────
# Pydantic schemas — used to validate the JSON sub-trees we accept on
# create / patch. The route handlers parse the raw body manually so we
# can short-circuit early errors (missing auth_method, etc.) before the
# Pydantic layer runs.
# ─────────────────────────────────────────────────────────────────────


class _Filter(BaseModel):
    # email channels: GLOBAL rules — sender domains ("acme.com") or full
    # addresses ("person@acme.com").
    from_domain_allow: list[str] = Field(default_factory=list)
    # whatsapp channels: GLOBAL rules — sender numbers in E.164. An entry
    # can be a prefix ("+34") to allow a whole country code.
    from_numbers_allow: list[str] = Field(default_factory=list)
    # slack channels: OPTIONAL whitelist of Slack user ids (U…/W…). The
    # channel membership is already the trust boundary, so this is empty
    # by default (anyone in the watched channel can talk to the bot).
    from_users_allow: list[str] = Field(default_factory=list)
    # email/whatsapp channels (v7.24.0): PEOPLE from the user directory,
    # stored as their canonical directory email (the UserPicker fills it).
    # Email: the sender address must equal the entry. WhatsApp: the sender
    # number is resolved to its directory owner (``email_for_phone``) and
    # matched — editing a phone in Usuarios updates the allow-list behaviour
    # without touching the channel. Email/WhatsApp channels require at least
    # ONE of (global rules, directory people).
    allow_directory_users: list[str] = Field(default_factory=list)
    subject_regex: Optional[str] = None
    label: Optional[str] = None


class _ReplyConfig(BaseModel):
    signature: Optional[str] = None
    cc: list[str] = Field(default_factory=list)
    bcc: list[str] = Field(default_factory=list)
    auto_mark_read: bool = True


class _NotifyConfig(BaseModel):
    # Recipients of proactive notifications (set by the OPERATOR; the
    # agent never chooses them). Emails for kind=email, E.164 numbers
    # for kind=whatsapp.
    notify_to: list[str] = Field(default_factory=list)
    # 'agent'         — notify whenever the agent decides it's relevant
    # 'critical_only' — only severity='critical'
    # 'never'         — paused (recipients kept but no sends)
    policy: str = "agent"
    # Anti-spam cap (0 = unlimited).
    max_per_day: int = 0


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")
# Allow-list entries may be full numbers or prefixes ("+34").
_E164_PREFIX_RE = re.compile(r"^\+[1-9]\d{0,14}$")
# Slack ids: channels start with C (public), G (private/legacy) or D
# (DM); users with U or W. Followed by base-32-ish uppercase chars.
_SLACK_CHANNEL_RE = re.compile(r"^[CGD][A-Z0-9]{6,}$")
# Telegram bot token from @BotFather: "<bot_id>:<35-char base64-ish secret>".
_TELEGRAM_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")
_SLACK_USER_RE = re.compile(r"^[UW][A-Z0-9]{6,}$")


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _iso_utc(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat()


def _coerce_process_since(value: Any) -> Optional[datetime]:
    """Resolve the operator's "process messages since" choice into a
    tz-aware UTC datetime, or ``None`` for "all history". Accepts:

      * ``None`` / ``""`` / ``"all"`` / ``"todos"`` → ``None`` (all history)
      * ``"now"`` / ``"ahora"``                     → current UTC time
      * an ISO-8601 datetime string (``"2026-06-13T09:00"`` or with a
        ``Z`` / offset; a date-only ``"2026-06-13"`` is accepted too)

    Raises a 422 on anything else so the SPA shows the error inline.
    """
    if value is None:
        return None
    if isinstance(value, str):
        v = value.strip()
        low = v.lower()
        if low in ("", "all", "todos", "todo", "any", "*"):
            return None
        if low in ("now", "ahora"):
            return datetime.now(timezone.utc)
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(
                422,
                f"invalid process_since: {value!r} — expected an ISO-8601 "
                "datetime, 'now', or 'all'",
            )
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    raise HTTPException(
        422, f"invalid process_since type: {type(value).__name__}",
    )


def _row_to_dict(c: Channel) -> dict[str, Any]:
    return {
        "id": c.id,
        "kind": c.kind,
        "display_name": c.display_name,
        "mailbox_address": c.mailbox_address,
        "auth_method": c.auth_method,
        "composio_toolkit": c.composio_toolkit,
        "composio_user_id": c.composio_user_id,
        "status": c.status,
        "status_reason": c.status_reason,
        "poll_interval_sec": c.poll_interval_sec,
        "cursor": c.cursor,
        "process_since": _iso_utc(c.process_since),
        "filter_config": _safe_json(c.filter_config, {}),
        "reply_mode": c.reply_mode,
        "reply_config": _safe_json(c.reply_config, {}),
        "notify_config": _safe_json(c.notify_config, {}),
        "is_active": bool(c.is_active),
        "last_polled_at": _iso_utc(c.last_polled_at),
        "last_message_at": _iso_utc(c.last_message_at),
        "messages_received": c.messages_received,
        "messages_replied": c.messages_replied,
        "errors_count": c.errors_count,
        "created_at": _iso_utc(c.created_at),
        "updated_at": _iso_utc(c.updated_at),
    }


def _safe_json(raw: Optional[str], fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except Exception:
        return fallback


def _validate_filter(filter_cfg: _Filter, kind: str = "email") -> None:
    """The sender whitelist is REQUIRED. Without it the inbox would be
    wide open. Email/WhatsApp channels accept GLOBAL rules (domains /
    E.164 numbers-or-prefixes) and/or PEOPLE from the directory
    (``allow_directory_users``, canonical emails) — at least one of the
    two lists must be non-empty. See ``patterns/external-channels.md``."""
    for u in filter_cfg.allow_directory_users:
        v = u.strip().lower() if isinstance(u, str) else ""
        if not v or not _EMAIL_RE.match(v):
            raise HTTPException(
                422,
                f"invalid directory user in allow_directory_users: {u!r} "
                "(expected the person's directory email)",
            )
    if kind == "whatsapp":
        if not filter_cfg.from_numbers_allow and not filter_cfg.allow_directory_users:
            raise HTTPException(
                422,
                "the sender whitelist is required: set number rules "
                "(filter_config.from_numbers_allow, E.164 or prefix) and/or "
                "directory people (filter_config.allow_directory_users)",
            )
        for n in filter_cfg.from_numbers_allow:
            if not isinstance(n, str) or not _E164_PREFIX_RE.match(n.strip()):
                raise HTTPException(
                    422, f"invalid E.164 number/prefix in allow-list: {n!r}",
                )
    elif kind == "slack":
        # The watched channel IS the trust boundary, so the user
        # allow-list is OPTIONAL. When present, entries must be Slack
        # user ids (U…/W…).
        for u in filter_cfg.from_users_allow:
            if not isinstance(u, str) or not _SLACK_USER_RE.match(u.strip()):
                raise HTTPException(
                    422, f"invalid Slack user id in allow-list: {u!r}",
                )
    else:
        if not filter_cfg.from_domain_allow and not filter_cfg.allow_directory_users:
            raise HTTPException(
                422,
                "the sender whitelist is required: set domain rules "
                "(filter_config.from_domain_allow) and/or directory people "
                "(filter_config.allow_directory_users)",
            )
        for d in filter_cfg.from_domain_allow:
            v = d.strip().lower() if isinstance(d, str) else ""
            # Entries may be a bare domain ("acme.com") or a full
            # address ("person@acme.com") — the poller matches suffix
            # vs exact accordingly.
            if not v or not (
                _EMAIL_RE.match(v) or re.match(r"^[a-z0-9.-]+$", v)
            ):
                raise HTTPException(
                    422, f"invalid domain/address in allow-list: {d!r}",
                )
    if filter_cfg.subject_regex:
        try:
            re.compile(filter_cfg.subject_regex)
        except re.error as exc:
            raise HTTPException(422, f"invalid subject_regex: {exc}")


def _validate_notify(cfg: _NotifyConfig, kind: str = "email") -> None:
    """Validate the proactive-notification recipients/policy. Recipients
    are optional (empty ⇒ the channel simply never notifies), but when
    present they must match the channel kind."""
    if cfg.policy not in ("agent", "critical_only", "never"):
        raise HTTPException(
            422,
            f"notify_config.policy must be agent|critical_only|never, "
            f"got {cfg.policy!r}",
        )
    if cfg.max_per_day < 0:
        raise HTTPException(422, "notify_config.max_per_day must be >= 0")
    for r in cfg.notify_to:
        v = r.strip() if isinstance(r, str) else ""
        if kind == "whatsapp":
            # A recipient is a DIRECTORY person (canonical email, resolved
            # to their phone at dispatch time — v7.24.0) or a raw E.164
            # number (legacy configs keep working).
            if not (_E164_RE.match(v) or _EMAIL_RE.match(v.lower())):
                raise HTTPException(
                    422,
                    f"invalid recipient in notify_to: {r!r} (expected a "
                    "directory user's email or an E.164 number)",
                )
        elif kind == "slack":
            # A Slack recipient is a channel id (post there) or a user id
            # (the notifier opens a DM via conversations.open).
            if not (_SLACK_CHANNEL_RE.match(v) or _SLACK_USER_RE.match(v)):
                raise HTTPException(
                    422,
                    f"invalid Slack recipient in notify_to: {r!r} "
                    "(expected a channel id C…/G… or user id U…/W…)",
                )
        else:
            if not _EMAIL_RE.match(v.lower()):
                raise HTTPException(
                    422, f"invalid email recipient in notify_to: {r!r}",
                )


def _validate_kind(kind: str) -> None:
    if kind not in CHANNELS_ALLOWED_KINDS:
        raise HTTPException(
            422,
            f"kind {kind!r} not allowed. Allowed: {CHANNELS_ALLOWED_KINDS}",
        )


def _validate_auth_method(method: str) -> None:
    if method not in CHANNELS_ALLOWED_AUTH_METHODS:
        raise HTTPException(
            422,
            f"auth_method {method!r} not allowed. "
            f"Allowed: {CHANNELS_ALLOWED_AUTH_METHODS}",
        )


_KIND_AUTH_COMBOS: dict[str, set[str]] = {
    # Composio is NOT an email channel (only MCP tooling) — email is IMAP/SMTP.
    "email": {"imap_smtp"},
    "whatsapp": {"twilio"},
    "slack": {"bot_token"},
    "telegram": {"telegram_bot"},
}


def _validate_kind_auth_combo(kind: str, method: str) -> None:
    valid = _KIND_AUTH_COMBOS.get(kind, set())
    if method not in valid:
        raise HTTPException(
            422,
            f"auth_method {method!r} is not valid for kind {kind!r}. "
            f"Valid: {sorted(valid)}",
        )


def _validate_reply_mode(mode: str) -> None:
    if mode not in {"auto", "never"}:
        raise HTTPException(422, f"reply_mode must be auto|never, got {mode!r}")


def _validate_poll_interval(seconds: int) -> int:
    if seconds < CHANNELS_MIN_POLL_INTERVAL_SEC:
        raise HTTPException(
            422,
            f"poll_interval_sec must be >= {CHANNELS_MIN_POLL_INTERVAL_SEC}",
        )
    if seconds > 3600:
        raise HTTPException(422, "poll_interval_sec must be <= 3600 (1h)")
    return seconds


async def _ensure_unique_address(s, kind: str, address: str) -> None:
    """One channel per (kind, address). A duplicate would double the
    polling and the agent cost for the same inbox/number — the model
    also carries a UNIQUE constraint as the last line of defence, this
    check turns the violation into a friendly 409."""
    existing = (await s.execute(
        select(Channel.id).where(
            Channel.kind == kind,
            Channel.mailbox_address == address,
        )
    )).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            409,
            f"a {kind} channel for {address!r} already exists (id={existing})",
        )


# ─────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────


@router.get("/capabilities")
async def capabilities(user: TokenPayload = Depends(require_project_access)):
    """UI introspection — the wizard reads this to know which channel
    kinds and auth methods to offer, plus whether IMAP is available
    (depends on CHANNEL_ENCRYPTION_KEY being present)."""
    return {
        "enabled": CHANNELS_ENABLED,
        "allowed_kinds": CHANNELS_ALLOWED_KINDS,
        "allowed_auth_methods": CHANNELS_ALLOWED_AUTH_METHODS,
        "imap_available": encryption_available(),
        # The SAME condition as the connectors (14.0.0): Composio is reached through the
        # engine with the employee's runtime JWT; there is no longer an HMAC secret in this pod.
        "oauth_available": bool(AUTH_SERVICE_URL and _composio_disponible()),
        "min_poll_interval_sec": CHANNELS_MIN_POLL_INTERVAL_SEC,
        "defaults": CHANNELS_DEFAULTS,
        "supported_oauth_toolkits": ["gmail"],
        "supported_imap_domains": [
            "gmail.com", "googlemail.com", "outlook.com", "hotmail.com",
            "office365.com", "icloud.com", "yahoo.com",
        ],
        # WhatsApp uses the same Fernet blob as IMAP, so availability
        # tracks the same env var (CHANNEL_ENCRYPTION_KEY).
        "whatsapp_available": encryption_available(),
        # Slack (bot token) is stored in the same encrypted blob, so it
        # also depends only on CHANNEL_ENCRYPTION_KEY.
        "slack_available": encryption_available(),
        # Telegram (bot token) — same Fernet blob, same env dependency.
        "telegram_available": encryption_available(),
    }


@router.get("")
async def list_channels(
    user: TokenPayload = Depends(require_project_access),
    kind: Optional[str] = None,
    status: Optional[str] = None,
):
    async with app_session() as s:
        stmt = select(Channel).order_by(desc(Channel.created_at))
        if kind:
            stmt = stmt.where(Channel.kind == kind)
        if status:
            stmt = stmt.where(Channel.status == status)
        rows = (await s.execute(stmt)).scalars().all()

        # 24h counters for every channel in two grouped queries — the
        # list cards advertise "Recibidos 24h / Respondidos 24h" and the
        # lifetime totals would be misleading there.
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        recv = dict((await s.execute(
            select(ChannelMessage.channel_id, func.count())
            .where(ChannelMessage.received_at >= cutoff)
            .group_by(ChannelMessage.channel_id)
        )).all())
        repl = dict((await s.execute(
            select(ChannelMessage.channel_id, func.count())
            .where(ChannelMessage.replied_at >= cutoff)
            .group_by(ChannelMessage.channel_id)
        )).all())

    out = []
    for c in rows:
        d = _row_to_dict(c)
        d["counters_24h"] = {
            "received": int(recv.get(c.id, 0)),
            "replied": int(repl.get(c.id, 0)),
        }
        out.append(d)
    return {"channels": out}


def _resolve_origin_from_request(referer: str) -> str:
    """The auth-service requires an explicit ``return_origin`` so the
    popup can postMessage the SPA after the OAuth dance. We derive it
    from the Referer header; the SPA always lives on the same origin."""
    if not referer:
        return ""
    from urllib.parse import urlparse
    parsed = urlparse(referer)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


@router.post("")
async def create_channel(
    payload: dict = Body(...),
    user: TokenPayload = Depends(require_admin),
):
    """Create a channel. The shape of ``payload`` depends on
    ``auth_method``:

    ```jsonc
    {
      "display_name": "Soporte Acme",
      "kind": "email",
      "auth_method": "imap_smtp",            // email is IMAP/SMTP only
      "poll_interval_sec": 60,
      "filter_config": {"from_domain_allow": ["acme.com"]},
      "reply_mode": "auto",
      "reply_config": {"signature": "..."},

      // when auth_method = imap_smtp
      "email": "soporte@acme.com",
      "password": "<app-password>",
      "imap_host": "imap.gmail.com",         // optional, autodetected
      "imap_port": 993,
      "smtp_host": "smtp.gmail.com",
      "smtp_port": 587,

      // when kind = whatsapp, auth_method = twilio
      "whatsapp_number": "+34600111222",     // Twilio WhatsApp sender
      "account_sid": "AC...",
      "auth_token": "<twilio-auth-token>"
      // filter_config uses from_numbers_allow instead of from_domain_allow
    }
    ```

    Returns the inserted channel plus, for OAuth, an ``authorize_url``
    pointing at the auth-service. The SPA opens that URL in a popup.
    """
    if not isinstance(payload, dict):
        raise HTTPException(422, "JSON body expected")
    kind = (payload.get("kind") or "email").strip().lower()
    auth_method = (payload.get("auth_method") or "").strip().lower()
    if not auth_method:
        raise HTTPException(422, "auth_method is required")
    _validate_kind(kind)
    _validate_auth_method(auth_method)
    _validate_kind_auth_combo(kind, auth_method)

    display_name = (payload.get("display_name") or "").strip()
    if not display_name:
        raise HTTPException(422, "display_name is required")
    reply_mode = (payload.get("reply_mode") or "auto").strip().lower()
    _validate_reply_mode(reply_mode)
    poll_interval = _validate_poll_interval(int(payload.get("poll_interval_sec") or 60))

    try:
        filter_cfg = _Filter(**(payload.get("filter_config") or {}))
    except Exception as exc:
        raise HTTPException(422, f"invalid filter_config: {exc}")
    _validate_filter(filter_cfg, kind)

    try:
        reply_cfg = _ReplyConfig(**(payload.get("reply_config") or {}))
    except Exception as exc:
        raise HTTPException(422, f"invalid reply_config: {exc}")

    try:
        notify_cfg = _NotifyConfig(**(payload.get("notify_config") or {}))
    except Exception as exc:
        raise HTTPException(422, f"invalid notify_config: {exc}")
    if not (payload.get("notify_config") or {}).get("policy"):
        # Default policy: notify when recipients are set, else paused.
        notify_cfg.policy = "agent" if notify_cfg.notify_to else "never"
    _validate_notify(notify_cfg, kind)

    now = datetime.now(timezone.utc)
    # "Process messages since" floor. Default to NOW so connecting a busy
    # mailbox does NOT replay its whole history through the agent — the
    # operator must explicitly pick "all history" (process_since=null) to
    # opt into that. An explicit ISO datetime starts from a past instant.
    if "process_since" in payload:
        process_since = _coerce_process_since(payload.get("process_since"))
    else:
        process_since = now
    base_values = {
        "kind": kind,
        "display_name": display_name,
        "auth_method": auth_method,
        "poll_interval_sec": poll_interval,
        "process_since": process_since,
        "filter_config": filter_cfg.model_dump_json(),
        "reply_mode": reply_mode,
        "reply_config": reply_cfg.model_dump_json(),
        "notify_config": notify_cfg.model_dump_json(),
        "created_at": now,
        "updated_at": now,
    }

    if auth_method == "twilio":
        # kind == "whatsapp" (enforced by _validate_kind_auth_combo).
        if not encryption_available():
            raise HTTPException(
                503,
                "WhatsApp channels are not available "
                "(CHANNEL_ENCRYPTION_KEY not configured)",
            )
        whatsapp_number = (payload.get("whatsapp_number") or "").strip()
        account_sid = (payload.get("account_sid") or "").strip()
        auth_token = payload.get("auth_token") or ""
        if not whatsapp_number or not account_sid or not auth_token:
            raise HTTPException(
                422,
                "whatsapp_number, account_sid and auth_token are required "
                "for WhatsApp (Twilio) channels",
            )
        if not _E164_RE.match(whatsapp_number):
            raise HTTPException(
                422, f"whatsapp_number must be E.164 (+34600111222), got {whatsapp_number!r}",
            )
        cfg = {
            "account_sid": account_sid,
            "auth_token": auth_token,
            "whatsapp_number": whatsapp_number,
        }
        # Test before saving — same contract as IMAP.
        test_result = await _test_twilio_config(whatsapp_number, cfg)
        if not test_result.ok:
            raise HTTPException(400, f"Twilio test failed: {test_result.message}")
        base_values.update({
            "mailbox_address": whatsapp_number,
            "imap_config_encrypted": encrypt_blob(cfg),
            "status": "active",
            "is_active": 1,
        })
        async with app_write() as s:
            await _ensure_unique_address(s, kind, whatsapp_number)
            res = await s.execute(sqlite_insert(Channel).values(**base_values))
            new_id = res.inserted_primary_key[0]
            row = (await s.execute(
                select(Channel).where(Channel.id == new_id)
            )).scalar_one()
            await s.commit()
        return {"channel": _row_to_dict(row), "test_result": {"ok": True, "detail": test_result.detail}}

    if auth_method == "bot_token":
        # kind == "slack" (enforced by _validate_kind_auth_combo).
        if not encryption_available():
            raise HTTPException(
                503,
                "Slack channels are not available "
                "(CHANNEL_ENCRYPTION_KEY not configured)",
            )
        bot_token = (payload.get("bot_token") or "").strip()
        slack_channel_id = (
            payload.get("slack_channel_id") or payload.get("channel_id") or ""
        ).strip()
        if not bot_token or not slack_channel_id:
            raise HTTPException(
                422,
                "bot_token and slack_channel_id are required for Slack channels",
            )
        if not bot_token.startswith("xoxb-"):
            raise HTTPException(
                422,
                f"bot_token must be a bot token (starts with 'xoxb-'), "
                f"got {bot_token[:6]!r}…",
            )
        if not _SLACK_CHANNEL_RE.match(slack_channel_id):
            raise HTTPException(
                422,
                f"slack_channel_id must be a Slack channel id (C…/G…), "
                f"got {slack_channel_id!r}",
            )
        cfg = {"bot_token": bot_token, "channel_id": slack_channel_id}
        # Test before saving — verifies the token, scopes and that the
        # bot is actually a member of the channel.
        test_result = await _test_slack_config(slack_channel_id, cfg)
        if not test_result.ok:
            raise HTTPException(400, f"Slack test failed: {test_result.message}")
        base_values.update({
            "mailbox_address": slack_channel_id,
            "imap_config_encrypted": encrypt_blob(cfg),
            "status": "active",
            "is_active": 1,
        })
        async with app_write() as s:
            await _ensure_unique_address(s, kind, slack_channel_id)
            res = await s.execute(sqlite_insert(Channel).values(**base_values))
            new_id = res.inserted_primary_key[0]
            row = (await s.execute(
                select(Channel).where(Channel.id == new_id)
            )).scalar_one()
            await s.commit()
        return {"channel": _row_to_dict(row), "test_result": {"ok": True, "detail": test_result.detail}}

    if auth_method == "telegram_bot":
        # kind == "telegram" (enforced by _validate_kind_auth_combo).
        if not encryption_available():
            raise HTTPException(
                503,
                "Telegram channels are not available "
                "(CHANNEL_ENCRYPTION_KEY not configured)",
            )
        bot_token = (payload.get("bot_token") or "").strip()
        if not bot_token:
            raise HTTPException(422, "bot_token is required for Telegram channels")
        if not _TELEGRAM_TOKEN_RE.match(bot_token):
            raise HTTPException(
                422,
                "bot_token must look like '123456789:ABCdef...' (from @BotFather)",
            )
        cfg = {"bot_token": bot_token}
        # Test before saving — getMe verifies the token and returns the bot's @username.
        test_result = await _test_telegram_config(cfg)
        if not test_result.ok:
            raise HTTPException(400, f"Telegram test failed: {test_result.message}")
        bot_username = (test_result.detail or {}).get("bot_username") or ""
        # mailbox_address = the bot's @username (display + uniqueness key). Fall back
        # to the numeric bot id when the bot somehow has no username.
        address = f"@{bot_username}" if bot_username else f"bot:{bot_token.split(':', 1)[0]}"
        base_values.update({
            "mailbox_address": address,
            "imap_config_encrypted": encrypt_blob(cfg),
            "status": "active",
            "is_active": 1,
        })
        async with app_write() as s:
            await _ensure_unique_address(s, kind, address)
            res = await s.execute(sqlite_insert(Channel).values(**base_values))
            new_id = res.inserted_primary_key[0]
            row = (await s.execute(
                select(Channel).where(Channel.id == new_id)
            )).scalar_one()
            await s.commit()
        return {"channel": _row_to_dict(row), "test_result": {"ok": True, "detail": test_result.detail}}

    # auth_method == "imap_smtp"
    if not encryption_available():
        raise HTTPException(
            503,
            "IMAP channels are not available (CHANNEL_ENCRYPTION_KEY not configured)",
        )
    email = (payload.get("email") or "").strip().lower()
    password = payload.get("password") or ""
    if not email or not password:
        raise HTTPException(422, "email and password are required for IMAP channels")
    if not _EMAIL_RE.match(email):
        raise HTTPException(422, f"invalid email format: {email!r}")
    auto = autodetect_servers(email) or {}
    cfg = {
        "user": email,
        "password": password,
        "host": (payload.get("imap_host") or "").strip() or (auto.get("imap") or ("",))[0],
        "port": int(payload.get("imap_port") or (auto.get("imap") or ("", 993))[1]),
        "use_ssl": bool(payload.get("imap_use_ssl", auto.get("imap") and auto["imap"][2])),
        "smtp_host": (payload.get("smtp_host") or "").strip() or (auto.get("smtp") or ("",))[0],
        "smtp_port": int(payload.get("smtp_port") or (auto.get("smtp") or ("", 587))[1]),
        "smtp_use_tls": bool(payload.get("smtp_use_tls", auto.get("smtp") and auto["smtp"][2])),
    }
    if not cfg["host"] or not cfg["smtp_host"]:
        raise HTTPException(
            422,
            "Could not autodetect IMAP/SMTP servers for this domain. "
            "Pass imap_host, imap_port, smtp_host, smtp_port explicitly.",
        )

    # Test before saving — failing here saves the operator from a
    # zombie channel.
    test_result = await _test_imap_config(email, cfg)
    if not test_result.ok:
        raise HTTPException(400, f"IMAP test failed: {test_result.message}")

    base_values.update({
        "mailbox_address": email,
        "imap_config_encrypted": encrypt_blob(cfg),
        "status": "active",
        "is_active": 1,
    })
    async with app_write() as s:
        await _ensure_unique_address(s, kind, email)
        res = await s.execute(sqlite_insert(Channel).values(**base_values))
        new_id = res.inserted_primary_key[0]
        row = (await s.execute(
            select(Channel).where(Channel.id == new_id)
        )).scalar_one()
        await s.commit()
    return {"channel": _row_to_dict(row), "test_result": {"ok": True, "detail": test_result.detail}}


@router.get("/{channel_id}")
async def get_channel(
    channel_id: int,
    user: TokenPayload = Depends(require_project_access),
):
    async with app_session() as s:
        row = (await s.execute(
            select(Channel).where(Channel.id == channel_id)
        )).scalar_one_or_none()
        if row is None:
            raise HTTPException(404)
        # Recent message counters (24h) for the detail page header.
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        recv24 = (await s.execute(
            select(func.count()).select_from(ChannelMessage).where(
                ChannelMessage.channel_id == channel_id,
                ChannelMessage.received_at >= cutoff,
            )
        )).scalar() or 0
        replied24 = (await s.execute(
            select(func.count()).select_from(ChannelMessage).where(
                ChannelMessage.channel_id == channel_id,
                ChannelMessage.replied_at >= cutoff,
            )
        )).scalar() or 0
    out = _row_to_dict(row)
    out["counters_24h"] = {"received": int(recv24), "replied": int(replied24)}
    return out


@router.patch("/{channel_id}")
async def patch_channel(
    channel_id: int,
    payload: dict = Body(...),
    user: TokenPayload = Depends(require_admin),
):
    """Update non-credential config. Switching ``auth_method`` is NOT
    supported — delete and create a new channel instead."""
    values: dict[str, Any] = {}
    if "display_name" in payload:
        name = (payload.get("display_name") or "").strip()
        if not name:
            raise HTTPException(422, "display_name must not be empty")
        values["display_name"] = name
    if "poll_interval_sec" in payload:
        values["poll_interval_sec"] = _validate_poll_interval(int(payload["poll_interval_sec"] or 60))
    if "process_since" in payload:
        # null/"all" ⇒ process all history; ISO datetime / "now" ⇒ floor.
        values["process_since"] = _coerce_process_since(payload.get("process_since"))
    pending_filter: Optional[_Filter] = None
    if "filter_config" in payload:
        try:
            pending_filter = _Filter(**(payload.get("filter_config") or {}))
        except Exception as exc:
            raise HTTPException(422, f"invalid filter_config: {exc}")
        # Kind-aware validation happens below, once the row is loaded.
    if "reply_mode" in payload:
        _validate_reply_mode(payload["reply_mode"])
        values["reply_mode"] = payload["reply_mode"]
    if "reply_config" in payload:
        try:
            rc = _ReplyConfig(**(payload.get("reply_config") or {}))
        except Exception as exc:
            raise HTTPException(422, f"invalid reply_config: {exc}")
        values["reply_config"] = rc.model_dump_json()
    pending_notify: Optional[_NotifyConfig] = None
    if "notify_config" in payload:
        try:
            pending_notify = _NotifyConfig(**(payload.get("notify_config") or {}))
        except Exception as exc:
            raise HTTPException(422, f"invalid notify_config: {exc}")
        if not (payload.get("notify_config") or {}).get("policy"):
            pending_notify.policy = (
                "agent" if pending_notify.notify_to else "never"
            )
        # Kind-aware validation happens below, once the row is loaded.
    if "is_active" in payload:
        active = bool(payload["is_active"])
        values["is_active"] = 1 if active else 0
        # Sync the VISIBLE status with is_active: deactivating a channel must leave it
        # 'inactive' (it used to stay 'active' and the panel showed "Active"); on reactivating
        # it goes back to 'active', clearing the previous reason.
        values["status"] = "active" if active else "inactive"
        if active:
            values["status_reason"] = None
    if not values and pending_filter is None and pending_notify is None:
        raise HTTPException(422, "no editable field provided")
    values["updated_at"] = datetime.now(timezone.utc)

    async with app_write() as s:
        row = (await s.execute(
            select(Channel).where(Channel.id == channel_id)
        )).scalar_one_or_none()
        if row is None:
            raise HTTPException(404)
        if pending_filter is not None:
            _validate_filter(pending_filter, row.kind or "email")
            values["filter_config"] = pending_filter.model_dump_json()
        if pending_notify is not None:
            _validate_notify(pending_notify, row.kind or "email")
            values["notify_config"] = pending_notify.model_dump_json()
        await s.execute(
            update(Channel).where(Channel.id == channel_id).values(**values)
        )
        await s.commit()
        row = (await s.execute(
            select(Channel).where(Channel.id == channel_id)
        )).scalar_one()
    return _row_to_dict(row)


@router.delete("/{channel_id}")
async def delete_channel(
    channel_id: int,
    user: TokenPayload = Depends(require_admin),
):
    async with app_session() as s:
        row = (await s.execute(
            select(Channel).where(Channel.id == channel_id)
        )).scalar_one_or_none()
        if row is None:
            raise HTTPException(404)
    # Best-effort upstream disconnect (Composio account revoke).
    try:
        from app.channels.base import get_adapter
        adapter = get_adapter(row)
        await adapter.disconnect()
    except Exception as exc:
        logger.warning("disconnect best-effort failed for channel %s: %s", channel_id, exc)
    async with app_write() as s:
        # ON DELETE CASCADE handles channel_messages thanks to the FK.
        from sqlalchemy import delete
        await s.execute(delete(Channel).where(Channel.id == channel_id))
        await s.commit()
    return {"deleted": channel_id}


@router.post("/{channel_id}/authorize")
async def authorize_channel(
    channel_id: int,
    payload: dict = Body(default={}),
    user: TokenPayload = Depends(require_admin),
):
    """Re-authorize an existing channel:

    * OAuth — returns a fresh authorize_url (used after the operator
      clicks "Reautorizar" because Composio rejected the token).
    * IMAP — accepts ``{password, ...}`` and re-tests the connection.
    * Twilio (WhatsApp) — accepts ``{auth_token, account_sid?}`` and
      re-tests the connection (used after a token rotation).
    """
    async with app_session() as s:
        row = (await s.execute(
            select(Channel).where(Channel.id == channel_id)
        )).scalar_one_or_none()
        if row is None:
            raise HTTPException(404)

    if row.auth_method == "oauth_composio":
        if not (AUTH_SERVICE_URL and _composio_disponible()):
            raise HTTPException(503, "OAuth not available")

        # CHANGE ACCOUNT (v6.36.1): the operator wants to connect a
        # DIFFERENT account, not just refresh the current token. Without
        # this branch the self-healing probe below would see the old
        # token still works and silently re-activate the SAME account, so
        # the popup never lets them switch. When ``change_account`` is set
        # (optionally with a new ``composio_user_id``/``account``):
        #   1. revoke the current Composio connected account so it is not
        #      silently reused,
        #   2. repoint the channel at the new account identity when given
        #      (a different entity guarantees a fresh connection),
        #   3. ALWAYS return a fresh authorize_url with forced account
        #      selection — never short-circuit on the still-valid token.
        change_account = bool(payload.get("change_account"))
        new_user = (
            payload.get("composio_user_id") or payload.get("account") or ""
        ).strip().lower()
        if change_account or new_user:
            try:
                from app.channels.base import get_adapter
                await get_adapter(row).disconnect()
            except Exception:  # best-effort — revoke can be done in Composio
                logger.warning(
                    "change-account: disconnect best-effort failed for channel %s",
                    channel_id,
                )
            values: dict[str, Any] = {
                "composio_connection_id": None,
                "status": "pending",
                "status_reason": None,
                "errors_count": 0,
                "updated_at": datetime.now(timezone.utc),
            }
            if new_user and new_user != (row.composio_user_id or "").strip().lower():
                async with app_session() as s:
                    await _ensure_unique_address(s, row.kind, new_user)
                values["composio_user_id"] = new_user
                # Placeholder until the OAuth callback reports the mailbox
                # the operator actually authorized.
                values["mailbox_address"] = new_user
            async with app_write() as s:
                await s.execute(
                    update(Channel).where(Channel.id == channel_id).values(**values)
                )
                await s.commit()
                row = (await s.execute(
                    select(Channel).where(Channel.id == channel_id)
                )).scalar_one()
            return {"authorize_url": _build_authorize_url(
                row,
                return_origin=(payload.get("return_origin") or "").strip(),
                force=True,
            )}

        # Self-healing probe: Composio completes the consent on its own
        # hosted page (connect.composio.dev) and the browser round-trip
        # back to our /oauth/callback is NOT guaranteed (the wai_state
        # query param is dropped along the redirect chain). If the
        # connection already works, activate the channel right here
        # instead of sending the operator through another popup.
        try:
            from app.channels.base import get_adapter
            probe = await get_adapter(row).test_connection()
        except Exception:
            probe = None
        if probe is not None and probe.ok:
            await _activate_channel_row(row, probe)
            return {"reauthorized": True, "detail": probe.detail}
        return {"authorize_url": _build_authorize_url(
            row, return_origin=(payload.get("return_origin") or "").strip()
        )}

    if row.auth_method == "imap_smtp":
        if not encryption_available():
            raise HTTPException(503, "IMAP not available")
        password = payload.get("password") or ""
        if not password:
            raise HTTPException(422, "password is required")
        from app.channels.encryption import decrypt_blob
        try:
            existing = decrypt_blob(row.imap_config_encrypted or "")
        except Exception:
            existing = {"user": row.mailbox_address}
        existing["password"] = password
        # Allow overriding host/port via reauth (Exchange admins
        # sometimes change them).
        for k in ("imap_host", "imap_port", "imap_use_ssl",
                  "smtp_host", "smtp_port", "smtp_use_tls"):
            if k in payload:
                existing[k.replace("imap_", "").replace("smtp_", "smtp_")] = payload[k]
        test_result = await _test_imap_config(row.mailbox_address, existing)
        if not test_result.ok:
            raise HTTPException(400, f"IMAP test failed: {test_result.message}")
        async with app_write() as s:
            await s.execute(
                update(Channel).where(Channel.id == channel_id).values(
                    imap_config_encrypted=encrypt_blob(existing),
                    status="active",
                    status_reason=None,
                    errors_count=0,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            await s.commit()
        return {"reauthorized": True, "detail": test_result.detail}

    if row.auth_method == "twilio":
        if not encryption_available():
            raise HTTPException(503, "WhatsApp channels not available")
        auth_token = payload.get("auth_token") or ""
        if not auth_token:
            raise HTTPException(422, "auth_token is required")
        from app.channels.encryption import decrypt_blob
        try:
            existing = decrypt_blob(row.imap_config_encrypted or "")
        except Exception:
            existing = {"whatsapp_number": row.mailbox_address}
        existing["auth_token"] = auth_token
        if payload.get("account_sid"):
            existing["account_sid"] = (payload["account_sid"] or "").strip()
        test_result = await _test_twilio_config(row.mailbox_address, existing)
        if not test_result.ok:
            raise HTTPException(400, f"Twilio test failed: {test_result.message}")
        async with app_write() as s:
            await s.execute(
                update(Channel).where(Channel.id == channel_id).values(
                    imap_config_encrypted=encrypt_blob(existing),
                    status="active",
                    status_reason=None,
                    errors_count=0,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            await s.commit()
        return {"reauthorized": True, "detail": test_result.detail}

    if row.auth_method == "bot_token":
        if not encryption_available():
            raise HTTPException(503, "Slack channels not available")
        bot_token = (payload.get("bot_token") or "").strip()
        if not bot_token:
            raise HTTPException(422, "bot_token is required")
        if not bot_token.startswith("xoxb-"):
            raise HTTPException(
                422, "bot_token must be a bot token (starts with 'xoxb-')",
            )
        from app.channels.encryption import decrypt_blob
        try:
            existing = decrypt_blob(row.imap_config_encrypted or "")
        except Exception:
            existing = {"channel_id": row.mailbox_address}
        existing["bot_token"] = bot_token
        if payload.get("slack_channel_id"):
            existing["channel_id"] = (payload["slack_channel_id"] or "").strip()
        test_result = await _test_slack_config(
            existing.get("channel_id") or row.mailbox_address, existing,
        )
        if not test_result.ok:
            raise HTTPException(400, f"Slack test failed: {test_result.message}")
        async with app_write() as s:
            await s.execute(
                update(Channel).where(Channel.id == channel_id).values(
                    imap_config_encrypted=encrypt_blob(existing),
                    status="active",
                    status_reason=None,
                    errors_count=0,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            await s.commit()
        return {"reauthorized": True, "detail": test_result.detail}

    if row.auth_method == "telegram_bot":
        if not encryption_available():
            raise HTTPException(503, "Telegram channels not available")
        bot_token = (payload.get("bot_token") or "").strip()
        if not bot_token:
            raise HTTPException(422, "bot_token is required")
        if not _TELEGRAM_TOKEN_RE.match(bot_token):
            raise HTTPException(422, "bot_token must look like '123456789:ABCdef...'")
        existing = {"bot_token": bot_token}
        test_result = await _test_telegram_config(existing)
        if not test_result.ok:
            raise HTTPException(400, f"Telegram test failed: {test_result.message}")
        async with app_write() as s:
            await s.execute(
                update(Channel).where(Channel.id == channel_id).values(
                    imap_config_encrypted=encrypt_blob(existing),
                    status="active",
                    status_reason=None,
                    errors_count=0,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            await s.commit()
        return {"reauthorized": True, "detail": test_result.detail}

    raise HTTPException(400, f"unknown auth_method {row.auth_method!r}")


@router.post("/{channel_id}/test")
async def test_channel(
    channel_id: int,
    user: TokenPayload = Depends(require_project_access),
):
    async with app_session() as s:
        row = (await s.execute(
            select(Channel).where(Channel.id == channel_id)
        )).scalar_one_or_none()
        if row is None:
            raise HTTPException(404)
    try:
        from app.channels.base import get_adapter
        adapter = get_adapter(row)
        result = await adapter.test_connection()
    except Exception as exc:
        return {"ok": False, "message": str(exc)}
    # Self-healing: a channel stuck in pending/unauthorized whose
    # connection demonstrably works (e.g. OAuth consent finished on
    # Composio's hosted page but the callback never reached us) is
    # flipped to active so the poller picks it up.
    if result.ok and (row.status or "") != "active":
        await _activate_channel_row(row, result)
    return {"ok": result.ok, "message": result.message, "detail": result.detail}


@router.get("/{channel_id}/messages")
async def list_messages(
    channel_id: int,
    # SECURITY: admin only. Each row is ONE person's conversation with the employee over
    # WhatsApp/Telegram/email (sender, body, reply). The senders are people in the
    # directory; with `require_project_access` a member could read their colleagues'
    # threads. The Channels page is already admin-only in the SPA.
    user: TokenPayload = Depends(require_admin),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: Optional[str] = None,
    direction: Optional[str] = None,
):
    async with app_session() as s:
        stmt = select(ChannelMessage).where(ChannelMessage.channel_id == channel_id)
        if status:
            stmt = stmt.where(ChannelMessage.status == status)
        if direction:
            stmt = stmt.where(ChannelMessage.direction == direction)
        total = (await s.execute(
            select(func.count()).select_from(stmt.subquery())
        )).scalar()
        rows = (await s.execute(
            stmt.order_by(desc(ChannelMessage.received_at))
                .limit(limit).offset(offset)
        )).scalars().all()
    return {
        "total": int(total or 0),
        "rows": [
            {
                "id": m.id,
                "provider_message_id": m.provider_message_id,
                "provider_thread_id": m.provider_thread_id,
                "direction": m.direction or "inbound",
                "sender": m.sender,
                "subject": m.subject,
                "body_excerpt": m.body_excerpt,
                "received_at": _iso_utc(m.received_at),
                "status": m.status,
                "event_id": m.event_id,
                "run_id": m.run_id,
                "reply_provider_message_id": m.reply_provider_message_id,
                "replied_at": _iso_utc(m.replied_at),
                "reply_subject": m.reply_subject,
                "reply_body": m.reply_body,
                "reply_attachments": _safe_json(m.reply_attachments_json, []),
                "error": m.error,
            }
            for m in rows
        ],
    }


# ─────────────────────────────────────────────────────────────────────
# OAuth callback (called by auth-service after the popup completes)
# ─────────────────────────────────────────────────────────────────────


@router.post("/oauth/callback", status_code=410)
async def oauth_callback():
    """RETIRED (14.0.0). It was the auth-service notice when the consent of a Composio email
    channel (``oauth_composio``) finished, authenticated with the auth-service's GLOBAL HMAC
    secret. That channel type was already retired (email is IMAP/SMTP) and the secret has
    left the pod: there is nothing left to verify the signature with, and accepting the
    notice without verifying it would let anyone activate a channel. It answers 410 so
    whoever still calls it knows the route went away on purpose — not that the pod is
    broken. An old channel is reactivated just with "Test" (``_activate_channel_row``)."""
    raise HTTPException(
        status_code=410,
        detail=("Este aviso ya no existe: los canales de correo por Composio están retirados. "
                "Usa un canal IMAP/SMTP; un canal antiguo se reactiva con «Probar»."),
    )


# ─────────────────────────────────────────────────────────────────────
# Internal helpers — not routes
# ─────────────────────────────────────────────────────────────────────


async def _activate_channel_row(row: Channel, test_result) -> None:
    """Mark a channel ``active`` after a successful connection test.

    Used by the self-healing paths (test / re-authorize): the OAuth
    consent may complete on Composio's side without our /oauth/callback
    ever being hit, so a working connection is the source of truth.
    Persists the mailbox the provider reports when available.
    """
    values: dict[str, Any] = {
        "status": "active",
        "status_reason": None,
        "errors_count": 0,
        "updated_at": datetime.now(timezone.utc),
    }
    detail = getattr(test_result, "detail", None)
    detected = (detail or {}).get("mailbox") if isinstance(detail, dict) else None
    if row.auth_method == "oauth_composio" and detected:
        values["mailbox_address"] = str(detected).strip().lower()
    async with app_write() as s:
        await s.execute(
            update(Channel).where(Channel.id == row.id).values(**values)
        )
        await s.commit()


def _build_authorize_url(
    channel: Channel, return_origin: str = "", force: bool = False,
) -> str:
    """The SPA opens this URL in a popup. The auth-service runs
    Composio's OAuth and finally POSTs to our /oauth/callback above.

    ``force`` asks the auth-service / provider to FORCE account selection
    (``prompt=select_account``) so the operator can pick a DIFFERENT
    account on reconnect instead of the provider silently reusing the
    currently logged-in session. Used by the "change account" flow."""
    params = {
        "user_email": channel.composio_user_id or "",
        "server_name": f"channel:{channel.id}",
        "return_origin": return_origin,
        # Round-tripped through Composio so the callback knows which
        # channel to mark active.
        "wai_channel_id": str(channel.id),
    }
    if force:
        params["force"] = "1"
        params["prompt"] = "select_account"
    toolkit = (channel.composio_toolkit or "gmail").lower()
    return f"{AUTH_SERVICE_URL}/composio/{toolkit}/start?{urlencode({k: v for k, v in params.items() if v})}"


async def _test_imap_config(email: str, cfg: dict[str, Any]):
    """Build a transient adapter wrapper around an in-memory channel and
    run ``test_connection``. Used at create / reauth time before the
    credentials are persisted."""
    from types import SimpleNamespace
    from app.channels.imap_smtp import ImapSmtpEmailAdapter
    fake = SimpleNamespace(
        kind="email",
        auth_method="imap_smtp",
        mailbox_address=email,
        imap_config_encrypted=None,
    )
    adapter = ImapSmtpEmailAdapter(fake)
    # Skip the encryption layer — feed the cfg directly.
    adapter._cfg = cfg  # type: ignore[attr-defined]
    return await adapter.test_connection()


async def _test_twilio_config(whatsapp_number: str, cfg: dict[str, Any]):
    """Same transient-adapter trick as ``_test_imap_config`` but for
    the WhatsApp (Twilio) backend. Used at create / reauth time before
    the credentials are persisted."""
    from types import SimpleNamespace
    from app.channels.base import TestResult
    from app.channels.whatsapp_twilio import WhatsAppTwilioAdapter
    if not cfg.get("account_sid") or not cfg.get("auth_token"):
        return TestResult(
            ok=False,
            message="account_sid and auth_token are required",
        )
    fake = SimpleNamespace(
        kind="whatsapp",
        auth_method="twilio",
        mailbox_address=whatsapp_number,
        imap_config_encrypted=None,
    )
    adapter = WhatsAppTwilioAdapter(fake)
    adapter._cfg = dict(cfg, whatsapp_number=whatsapp_number)  # type: ignore[attr-defined]
    return await adapter.test_connection()


async def _test_slack_config(channel_id: str, cfg: dict[str, Any]):
    """Same transient-adapter trick as ``_test_imap_config`` but for the
    Slack (bot token) backend. Used at create / reauth time before the
    credentials are persisted."""
    from types import SimpleNamespace
    from app.channels.base import TestResult
    from app.channels.slack_bot import SlackBotAdapter
    if not cfg.get("bot_token") or not (cfg.get("channel_id") or channel_id):
        return TestResult(
            ok=False, message="bot_token and channel_id are required",
        )
    fake = SimpleNamespace(
        kind="slack",
        auth_method="bot_token",
        mailbox_address=channel_id,
        imap_config_encrypted=None,
    )
    adapter = SlackBotAdapter(fake)
    adapter._cfg = dict(cfg, channel_id=cfg.get("channel_id") or channel_id)  # type: ignore[attr-defined]
    return await adapter.test_connection()


async def _test_telegram_config(cfg: dict[str, Any]):
    """Transient-adapter test for the Telegram (bot token) backend: ``getMe``
    verifies the token and returns the bot's @username. Used at create / reauth."""
    from types import SimpleNamespace
    from app.channels.base import TestResult
    from app.channels.telegram_bot import TelegramBotAdapter
    if not cfg.get("bot_token"):
        return TestResult(ok=False, message="bot_token is required")
    fake = SimpleNamespace(
        kind="telegram",
        auth_method="telegram_bot",
        mailbox_address=None,
        imap_config_encrypted=None,
    )
    adapter = TelegramBotAdapter(fake)
    adapter._cfg = dict(cfg)  # type: ignore[attr-defined]
    return await adapter.test_connection()
