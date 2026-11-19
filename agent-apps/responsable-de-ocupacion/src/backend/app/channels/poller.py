"""Background poller for external channels.

Spawned by ``app.main:lifespan`` alongside the manifest watcher and
the executor. Every tick it:

  1. queries ``channels`` for rows with ``is_active=1 AND status='active'``,
  2. skips those polled within ``poll_interval_sec``,
  3. asks the adapter for new messages,
  4. dedupes by ``(channel_id, provider_message_id)``,
  5. inserts ``channel_messages`` rows and a matching ``events`` row
     with ``type='external_message'`` so the executor picks them up.

This module is independent from ``app/autonomous/watcher.py`` because
channels are NOT manifest-declared — they live in the local SQLite and
get CRUD'd by the operator through the SPA's Canales section. Mixing
them into the manifest watcher dispatch would force every channel
change to be a manifest edit.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.app_database import app_session, app_write
from app.app_models.channels import Channel, ChannelMessage
from app.app_models.runtime import Event
from app.agent_sessions import resolve_inbound_scope
from app.channels.base import (
    ChannelConfigError, ChannelError, ChannelUnauthorizedError,
    MAX_INBOUND_ATTACHMENTS, MAX_INBOUND_ATTACHMENT_BYTES,
    MAX_INBOUND_ATTACHMENTS_TOTAL_BYTES, Message, get_adapter,
    workspace_write_inbound,
)
from app.config import CHANNELS_MIN_POLL_INTERVAL_SEC, MAX_INBOUND_BODY_CHARS

logger = logging.getLogger(__name__)

LOOP_SLEEP = 5  # seconds — main tick cadence
EXTERNAL_MESSAGE_EVENT_TYPE = "external_message"


def _fingerprint(channel_id: int, provider_message_id: str) -> str:
    """Stable fingerprint for the event row. Same provider_message_id
    on the same channel == same event, even across restarts."""
    raw = f"channel::{channel_id}::msg::{provider_message_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _passes_filters(
    msg: Message, filter_cfg: dict[str, Any], kind: str = "email",
) -> tuple[bool, str]:
    """Apply the operator's filter config. Returns ``(passed, reason)``
    where ``reason`` is used for diagnostics when the message is
    filtered out."""
    sender = (msg.sender or "").lower()

    # 1. Sender whitelist — REQUIRED (the route layer refuses to save a
    #    channel without it). Without this an open inbox would expose
    #    the agent to every spammer on the net. Two entry classes since
    #    v7.24.0: GLOBAL rules (email: bare domain "acme.com" suffix
    #    match or full address exact match; whatsapp: E.164 exact or
    #    prefix like "+34") and DIRECTORY people
    #    (``allow_directory_users``, canonical emails: email channels
    #    match the sender address; whatsapp channels resolve the sender
    #    number to its directory owner via ``email_for_phone``). Passing
    #    EITHER class admits the message.
    allow_people = [
        (u or "").strip().lower()
        for u in (filter_cfg.get("allow_directory_users") or [])
        if (u or "").strip()
    ]
    if kind == "whatsapp":
        allow_numbers = filter_cfg.get("from_numbers_allow") or []
        if not allow_numbers and not allow_people:
            return False, "sender whitelist empty (rules or directory people required)"
        by_rule = any(sender == n.lower() or sender.startswith(n.lower())
                      for n in allow_numbers)
        by_person = False
        if not by_rule and allow_people:
            from app.users_directory import email_for_phone
            owner = await email_for_phone(sender)
            by_person = bool(owner) and owner in allow_people
        if not (by_rule or by_person):
            return False, f"sender {sender!r} not in number/directory allow-list"
    elif kind == "slack":
        # Membership of the watched Slack channel IS the trust boundary,
        # so the user whitelist is OPTIONAL. When the operator set one,
        # enforce it (exact Slack user id match, case-insensitive).
        allow_users = filter_cfg.get("from_users_allow") or []
        if allow_users and not any(sender == u.lower() for u in allow_users):
            return False, f"sender {sender!r} not in user allow-list"
    elif kind == "telegram":
        # A bot has NO channel-membership trust boundary (anyone can DM it), so
        # the allow-list is REQUIRED. Sender = the inbound @username (lowercased,
        # no '@') or numeric id. Trust via the DIRECTORY: resolve the username to
        # its owner (``AppUser.telegram``) and require it in ``allow_directory_users``;
        # an explicit username rule (``from_users_allow``) also admits.
        allow_users = [
            (u or "").strip().lstrip("@").lower()
            for u in (filter_cfg.get("from_users_allow") or []) if (u or "").strip()
        ]
        if not allow_users and not allow_people:
            return False, "sender whitelist empty (rules or directory people required)"
        by_rule = any(sender == u for u in allow_users)
        by_person = False
        if not by_rule and allow_people:
            from app.users_directory import email_for_telegram
            owner = await email_for_telegram(sender)
            by_person = bool(owner) and owner in allow_people
        if not (by_rule or by_person):
            return False, f"sender {sender!r} not in user/directory allow-list"
    else:
        allow = filter_cfg.get("from_domain_allow") or []
        if not allow and not allow_people:
            return False, "sender whitelist empty (rules or directory people required)"

        def _sender_allowed(entry: str) -> bool:
            e = (entry or "").strip().lower()
            if not e:
                return False
            if "@" in e:  # full address → exact match
                return sender == e
            return "@" in sender and sender.endswith("@" + e)

        if not (any(_sender_allowed(d) for d in allow) or sender in allow_people):
            return False, f"sender {sender!r} not in domain/directory allow-list"

    # 2. Optional subject regex. The subject is capped so a pathological
    #    pattern can't burn the shared event loop (ReDoS), and a regex
    #    that raises REJECTS the message (fail-closed — consistent with
    #    the route layer, which refuses to save an invalid pattern).
    pattern = filter_cfg.get("subject_regex")
    if pattern:
        import re as _re
        try:
            if not _re.search(pattern, (msg.subject or "")[:512], _re.I):
                return False, f"subject regex {pattern!r} did not match"
        except Exception:
            logger.warning("channel filter: invalid subject_regex %r", pattern)
            return False, f"subject regex {pattern!r} is invalid (rejected)"

    # 3. Loop prevention — never reprocess our own outbound replies.
    if (msg.headers or {}).get("auto_submitted") == "auto-replied":
        return False, "Auto-Submitted: auto-replied (loop prevention)"
    return True, ""


async def _materialize_inbound_attachments(
    adapter, channel: Channel, msg: Message, scope,
) -> list[dict[str, Any]]:
    """Stage a message's inbound attachments into the origin's workspace
    ``inbox/`` folder so the agent can read them. Returns the metadata
    list to persist on the row. Best-effort: per-attachment failures and
    cap violations are logged and skipped — never block ingestion. The
    files are isolated per origin/operator (``<scope.workspace_subdir>/
    inbox``) and confined by ``workspace_write_inbound``."""
    dest_dir = f"{scope.workspace_subdir}/inbox"
    meta: list[dict[str, Any]] = []
    total = 0
    for att in (msg.attachments or [])[:MAX_INBOUND_ATTACHMENTS]:
        try:
            data = att.data
            if data is None and att.provider_ref is not None:
                data = await adapter.download_attachment(att.provider_ref)
        except Exception as exc:  # defensive: a bad attachment is not fatal
            logger.warning(
                "inbound attachment fetch failed (%s): %s", att.filename, exc,
            )
            data = None
        if not data:
            continue
        if len(data) > MAX_INBOUND_ATTACHMENT_BYTES:
            logger.warning(
                "inbound attachment %r skipped: over per-file cap", att.filename,
            )
            continue
        if total + len(data) > MAX_INBOUND_ATTACHMENTS_TOTAL_BYTES:
            logger.warning(
                "inbound attachments total cap reached (channel %s msg %s)",
                channel.id, msg.provider_message_id,
            )
            break
        rel = workspace_write_inbound(dest_dir, att.filename, data)
        if not rel:
            logger.warning(
                "could not stage inbound attachment %r (no writable "
                "workspace mount?)", att.filename,
            )
            continue
        total += len(data)
        meta.append({
            "filename": rel.rsplit("/", 1)[-1],
            "workspace_path": rel,
            "mime": att.mime_type or None,
            "size": len(data),
        })
    return meta


async def _index_inbound_files(
    session, user_email: str, att_meta: list[dict[str, Any]], msg: Message,
) -> None:
    """Index staged inbound attachments into ``user_files`` (artifacts
    memory). Idempotent on ``(user_email, workspace_path)`` so a reprocess
    never duplicates. Best-effort — uses the caller's session so it
    commits atomically with the message row; failures never abort the
    poll."""
    import hashlib as _hashlib
    from app.app_models.memory import UserFile
    e = (user_email or "").strip().lower()
    if not e:
        return
    for f in att_meta:
        path = f.get("workspace_path")
        if not path:
            continue
        key = _hashlib.sha256(f"{e}|{path}".encode()).hexdigest()
        try:
            await session.execute(
                sqlite_insert(UserFile).values(
                    idempotency_key=key,
                    user_email=e,
                    filename=f.get("filename") or str(path).rsplit("/", 1)[-1],
                    workspace_path=str(path),
                    mime=f.get("mime"),
                    size=f.get("size"),
                    source="email",
                    source_ref=str(msg.provider_message_id or ""),
                    description=f"Adjunto recibido: {msg.subject or '(sin asunto)'}",
                ).on_conflict_do_nothing(
                    index_elements=[UserFile.user_email, UserFile.workspace_path],
                )
            )
        except Exception:  # pragma: no cover — never block ingestion
            logger.debug("inbound file index failed for %s (ignored)", path)


async def _process_channel(channel: Channel) -> None:
    """Poll one channel and emit events for the new messages."""
    now = datetime.now(timezone.utc)
    interval = max(
        int(channel.poll_interval_sec or 60),
        CHANNELS_MIN_POLL_INTERVAL_SEC,
    )
    if channel.last_polled_at:
        last = channel.last_polled_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if (now - last) < timedelta(seconds=interval):
            return

    try:
        adapter = get_adapter(channel)
    except ChannelConfigError as exc:
        await _mark_channel_error(channel.id, "error", str(exc))
        return

    try:
        messages, new_cursor = await adapter.fetch_new(channel.cursor)
    except ChannelUnauthorizedError as exc:
        await _mark_channel_error(channel.id, "unauthorized", str(exc))
        logger.warning(
            "channel %s (mailbox=%s) unauthorized: %s",
            channel.id, channel.mailbox_address, exc,
        )
        return
    except ChannelError as exc:
        await _bump_error_count(channel.id, str(exc))
        logger.warning(
            "channel %s (mailbox=%s) fetch failed: %s",
            channel.id, channel.mailbox_address, exc,
        )
        return

    inserted_msgs = 0
    try:
        filter_cfg = json.loads(channel.filter_config or "{}")
    except Exception:
        filter_cfg = {}

    # "Process messages since" floor (operator-chosen at channel
    # creation). NULL ⇒ process all history. A datetime ⇒ ignore anything
    # older. Enforced here so it holds for EVERY adapter regardless of how
    # its cursor works; the cursor still advances past skipped messages so
    # the channel catches up to "now" without replaying old mail.
    process_since = channel.process_since
    if process_since is not None and process_since.tzinfo is None:
        process_since = process_since.replace(tzinfo=timezone.utc)

    # WRITE — serialized queue (INC-VIG-59). This block (message rows + the
    # telemetry UPDATE below) was the visible victim of the unfair SQLite lock:
    # "channel 1 tick failed / database is locked" every time a round was running.
    # The messages were already fetched above, so no network I/O is inside.
    async with app_write() as s:
        for msg in messages:
            received_at = msg.received_at or datetime.now(timezone.utc)
            if received_at.tzinfo is None:
                received_at = received_at.replace(tzinfo=timezone.utc)
            # Before the start date ⇒ skip entirely (no row, no event):
            # these are intentionally out of scope, not "filtered", so we
            # don't clutter the message log with the whole back-history.
            if process_since is not None and received_at < process_since:
                continue
            # Adapter-level skip sentinel (e.g. too large to fetch): persist a
            # visible 'skipped' row with the reason so the operator knows the
            # message exists and why it wasn't processed; never spawn an event.
            skip_reason = getattr(msg, "skip_reason", None)
            if skip_reason:
                ins_skip = sqlite_insert(ChannelMessage).values(
                    channel_id=channel.id,
                    provider_message_id=msg.provider_message_id,
                    provider_thread_id=msg.provider_thread_id,
                    sender=msg.sender,
                    subject=msg.subject,
                    body_excerpt="",
                    body_full=None,
                    received_at=received_at,
                    status="skipped",
                    error=str(skip_reason)[:500],
                ).on_conflict_do_nothing(
                    index_elements=[
                        ChannelMessage.channel_id,
                        ChannelMessage.provider_message_id,
                    ],
                )
                await s.execute(ins_skip)
                continue
            ok, reason = await _passes_filters(msg, filter_cfg, channel.kind or "email")
            # Insert channel_messages (idempotent on UNIQUE constraint).
            # Filtered messages are persisted too (status='filtered') so
            # the cursor can advance safely: the message stays visible in
            # the SPA with the rejection reason instead of vanishing
            # forever when the operator later fixes the allow-list.
            ins_msg = sqlite_insert(ChannelMessage).values(
                channel_id=channel.id,
                provider_message_id=msg.provider_message_id,
                provider_thread_id=msg.provider_thread_id,
                sender=msg.sender,
                subject=msg.subject,
                body_excerpt=(msg.body_text or "")[:2000],
                # Keep the WHOLE quoted chain (capped at a generous safety
                # limit). The executor decides how much of it fits the
                # context window; the SPA list keeps using body_excerpt.
                body_full=(msg.body_text or "")[:MAX_INBOUND_BODY_CHARS],
                received_at=received_at,
                status="pending" if ok else "filtered",
                error=None if ok else reason[:500],
            ).on_conflict_do_nothing(
                index_elements=[
                    ChannelMessage.channel_id,
                    ChannelMessage.provider_message_id,
                ],
            )
            res = await s.execute(ins_msg)
            if not res.rowcount:
                continue  # already processed
            if not ok:
                logger.debug(
                    "channel %s filtered msg %s: %s",
                    channel.id, msg.provider_message_id, reason,
                )
                continue
            # Stage any inbound attachments into the origin's workspace
            # folder and record their metadata on the row. Best-effort:
            # never blocks the message or its event.
            if msg.attachments:
                # Unify onto the operator's scope when the sender is a
                # known chat user, so attachments land in the same folder
                # and index as their chat memory.
                try:
                    scope = await resolve_inbound_scope(
                        channel.id, msg.sender or "unknown",
                    )
                    att_meta = await _materialize_inbound_attachments(
                        adapter, channel, msg, scope,
                    )
                except Exception as exc:  # defensive: never abort the poll
                    logger.warning(
                        "channel %s inbound attachments failed for %s: %s",
                        channel.id, msg.provider_message_id, exc,
                    )
                    scope = None
                    att_meta = []
                if att_meta:
                    await s.execute(
                        update(ChannelMessage)
                        .where(
                            ChannelMessage.channel_id == channel.id,
                            ChannelMessage.provider_message_id == msg.provider_message_id,
                        )
                        .values(inbound_attachments_json=json.dumps(att_meta, default=str))
                    )
                    # Auto-index into the user's ARTIFACTS memory so the
                    # files survive past this message and surface in the
                    # memory block + Operations "Memoria" tab.
                    if scope is not None and scope.memory_user:
                        await _index_inbound_files(
                            s, scope.memory_user, att_meta, msg,
                        )
            # Emit the event so the executor wakes up.
            fp = _fingerprint(channel.id, msg.provider_message_id)
            ins_event = sqlite_insert(Event).values(
                fingerprint=fp,
                type=EXTERNAL_MESSAGE_EVENT_TYPE,
                source=f"channel:{channel.id}",
                payload_json=json.dumps({
                    "channel_id": channel.id,
                    "provider_message_id": msg.provider_message_id,
                }, default=str),
                status="PENDING",
                attempts=0,
                detected_at=datetime.now(timezone.utc),
            ).on_conflict_do_nothing(index_elements=[Event.fingerprint])
            await s.execute(ins_event)
            inserted_msgs += 1

        # Telemetry update
        await s.execute(
            update(Channel)
            .where(Channel.id == channel.id)
            .values(
                last_polled_at=now,
                last_message_at=(now if inserted_msgs else channel.last_message_at),
                messages_received=channel.messages_received + inserted_msgs,
                cursor=new_cursor if new_cursor is not None else channel.cursor,
                errors_count=0,
                status_reason=None,
            )
        )
        await s.commit()


async def _mark_channel_error(channel_id: int, status: str, reason: str) -> None:
    async with app_write() as s:
        await s.execute(
            update(Channel).where(Channel.id == channel_id).values(
                status=status,
                status_reason=reason[:1000],
                last_polled_at=datetime.now(timezone.utc),
            )
        )
        await s.commit()


async def _bump_error_count(channel_id: int, reason: str) -> None:
    async with app_write() as s:
        row = (await s.execute(
            select(Channel).where(Channel.id == channel_id)
        )).scalar_one_or_none()
        if row is None:
            return
        await s.execute(
            update(Channel).where(Channel.id == channel_id).values(
                errors_count=row.errors_count + 1,
                status_reason=reason[:1000],
                last_polled_at=datetime.now(timezone.utc),
            )
        )
        await s.commit()


async def _active_channels() -> list[Channel]:
    async with app_session() as s:
        rows = (await s.execute(
            select(Channel).where(
                Channel.is_active == 1,
                Channel.status == "active",
            )
        )).scalars().all()
    return list(rows)


async def _tick_once() -> None:
    channels = await _active_channels()
    if not channels:
        return
    # Poll sequentially so we don't blast the same SMTP/IMAP server.
    # The cost is dominated by the network round-trip; with N channels
    # the worst case is N * fetch_time < LOOP_SLEEP * 12. If that ever
    # becomes a problem we'll move to gathered tasks with per-host
    # semaphores.
    for ch in channels:
        try:
            await _process_channel(ch)
        except Exception:
            logger.exception("channel %s tick failed", ch.id)


async def run_forever() -> None:
    logger.info("Channel poller starting")
    while True:
        try:
            await _tick_once()
        except Exception:
            logger.exception("channel poller iteration failed")
        await asyncio.sleep(LOOP_SLEEP)
