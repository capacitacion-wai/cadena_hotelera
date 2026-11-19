"""Proactive notifications for autonomous (non-channel) runs.

The reactive flow (a message arrives → the agent answers the sender) is
handled by ``app/channels/executor.py``. This module is the OUTBOUND
counterpart: when a watcher / scheduled run finds something worth
telling a human, the agent can ask the platform to push a notification
through the configured channels.

Separation of duties (this is the security model):

* The OPERATOR configures **to whom** on each channel
  (``Channel.notify_config.notify_to`` + ``policy``). A channel with no
  recipients (or ``policy='never'``) never notifies.
* The AGENT only decides **whether** and **what** through the optional
  ``STRUCTURED_RESPONSE.notify`` block ``{decision, severity, subject,
  body, attachments[], channels?[]}``. It can NEVER choose the
  recipients, so a prompt injection in the analysed data cannot
  exfiltrate anything to an arbitrary address.
* The PLATFORM applies the policy, sends through the existing adapters
  (reusing ``send_reply`` with a synthetic outbound ``Message``) and
  records every notification as a ``ChannelMessage`` row
  (``direction='notification'``) so it shows up in the channel's
  message thread in the SPA.

It is best-effort: the run is already COMPLETED by the time this runs,
so any failure is logged and audited but never re-raised — a broken
mailbox must not fail the analytical run nor trigger a retry.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.app_database import app_session, app_write
from app.app_models.business import Communication
from app.app_models.channels import Channel, ChannelMessage
from app.channels.base import (
    Attachment, ChannelError, Message, Reply, get_adapter,
    format_channel_exception,
)

logger = logging.getLogger(__name__)

VALID_SEVERITIES = ("info", "warning", "critical")


@dataclass
class DeliveryOutcome:
    """Result of trying to deliver one agent intent (a ``notify`` block
    or a ``ready`` communication) through the configured channels.

    ``intent`` is True when the agent actually asked to deliver
    something. ``delivered`` is True when at least one channel sent it.
    ``blocked_reason`` carries an OPERATOR-actionable explanation when
    the agent wanted to deliver but the platform could not (e.g. no
    recipients configured) — this is what turns the previous SILENT drop
    into something visible in the SPA and on the communication row."""
    intent: bool = False
    delivered: bool = False
    delivery_ids: list[str] = field(default_factory=list)
    # v7.44.0 — the ACTUAL recipients each delivering channel sent to, so a
    # communication row can store WHO really received it (instead of a vague
    # "destinatario configurado" placeholder).
    delivered_to: list[str] = field(default_factory=list)
    blocked_reason: str | None = None
    send_errors: list[str] = field(default_factory=list)

    @property
    def delivery_id_str(self) -> str | None:
        return ",".join(p for p in self.delivery_ids if p)[:500] or None

    @property
    def delivered_to_str(self) -> str | None:
        seen: list[str] = []
        for r in self.delivered_to:
            r = (r or "").strip()
            if r and r not in seen:
                seen.append(r)
        return ", ".join(seen)[:500] or None


@dataclass
class NotifyIntent:
    """Parsed ``STRUCTURED_RESPONSE.notify`` block."""
    severity: str
    subject: str | None
    body: str
    attachments: list[Attachment]
    channels: list[str]  # optional kind allow-list ([] = all eligible)


def _parse_notify(structured: dict[str, Any] | None) -> NotifyIntent | None:
    """Pull the ``notify`` block. Returns ``None`` (no-op) unless the
    agent explicitly asked to send and provided a non-empty body."""
    if not isinstance(structured, dict):
        return None
    raw = structured.get("notify")
    if not isinstance(raw, dict):
        return None
    decision = str(raw.get("decision") or "").strip().lower()
    # Tolerate a bare ``{body: ...}`` as an implicit send, but a present
    # decision that is not "send"/"notify"/"yes" is an explicit skip.
    if decision and decision not in ("send", "notify", "yes", "true"):
        return None
    body = str(raw.get("body") or "").strip()
    if not body:
        return None
    severity = str(raw.get("severity") or "info").strip().lower()
    if severity not in VALID_SEVERITIES:
        severity = "info"
    channels_raw = raw.get("channels")
    channels = [
        str(k).strip().lower()
        for k in channels_raw if str(k).strip()
    ] if isinstance(channels_raw, list) else []

    # Reuse the channel executor's defensive attachment parser so the
    # notification path obeys the exact same workspace_path / url /
    # base64 rules and caps as a reply.
    from app.channels.executor import _extract_attachments
    attachments = _extract_attachments(raw.get("attachments"))

    return NotifyIntent(
        severity=severity,
        subject=(str(raw.get("subject")).strip() or None) if raw.get("subject") else None,
        body=body,
        attachments=attachments,
        channels=channels,
    )


def _channel_notify_cfg(channel: Channel) -> tuple[list[str], str, int]:
    """Return ``(notify_to, policy, max_per_day)`` from the channel's
    ``notify_config`` with safe defaults."""
    try:
        cfg = json.loads(channel.notify_config or "{}")
    except (TypeError, ValueError):
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    notify_to = [
        str(x).strip() for x in (cfg.get("notify_to") or [])
        if str(x).strip()
    ]
    policy = str(cfg.get("policy") or "").strip().lower()
    if policy not in ("agent", "critical_only", "never"):
        policy = "agent" if notify_to else "never"
    try:
        max_per_day = int(cfg.get("max_per_day") or 0)
    except (TypeError, ValueError):
        max_per_day = 0
    return notify_to, policy, max_per_day


async def _sent_today(session, channel_id: int) -> int:
    """Count notification rows emitted on this channel since UTC
    midnight (the ``max_per_day`` anti-spam gate)."""
    start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    return int((await session.execute(
        select(func.count(ChannelMessage.id)).where(
            ChannelMessage.channel_id == channel_id,
            ChannelMessage.direction == "notification",
            ChannelMessage.received_at >= start,
        )
    )).scalar() or 0)


def _notification_retry_allowed(
    status: str | None,
    reply_provider_message_id: str | None,
    received_at: datetime | None,
    *,
    stale_sec: int,
    now: datetime | None = None,
) -> bool:
    """Return True when a conflicting notification row should be (re)sent.

    * ``replied`` / ``skipped`` / provider id set → never
    * ``error`` → always retry (legitimate failure path)
    * ``processing`` → retry only when older than ``stale_sec`` (crash
      reclaim); fresh rows may belong to an in-flight send on this pod
    """
    st = (status or "").strip().lower()
    if st in ("replied", "skipped"):
        return False
    if (reply_provider_message_id or "").strip():
        return False
    if st == "error":
        return True
    if st == "processing":
        if received_at is None:
            return True
        now = now or datetime.now(timezone.utc)
        ra = received_at
        if ra.tzinfo is None:
            ra = ra.replace(tzinfo=timezone.utc)
        return (now - ra).total_seconds() >= float(stale_sec)
    return False


def _synthetic_message(recipient: str, subject: str | None) -> Message:
    """An outbound-only ``Message`` that makes ``adapter.send_reply``
    start a NEW conversation (no thread / no In-Reply-To headers)
    addressed to ``recipient``."""
    return Message(
        provider_message_id="",
        provider_thread_id=None,
        sender=recipient,
        subject=subject,
        body_text="",
        body_html=None,
        received_at=datetime.now(timezone.utc),
        headers={},
    )


async def dispatch_notifications(
    run_id: int, structured: dict[str, Any] | None,
) -> DeliveryOutcome:
    """Send the agent's proactive ``notify`` block (if any) through every
    eligible channel. Never raises — failures are logged + audited.

    Returns a :class:`DeliveryOutcome` so the caller can reconcile the
    run's ``communications`` rows against what was ACTUALLY delivered
    (instead of leaving them stuck at ``ready`` forever) and so a
    misconfiguration (no recipients) becomes visible rather than a
    silent drop."""
    try:
        intent = _parse_notify(structured)
    except Exception:
        logger.debug("notify parse failed (ignored)", exc_info=True)
        return DeliveryOutcome(intent=False)
    if intent is None:
        return DeliveryOutcome(intent=False)
    return await _dispatch_intent(run_id, intent, provider_prefix="notify")


async def _automation_recipient_for_run(run_id: int) -> tuple[list[str], str | None, int | None]:
    """If this run was spawned by an automation whose ``notify`` reaction pins
    an OPERATOR-set recipient, return ``(recipients, channel_kind, channel_id)``.

    This recipient comes from the automation's STORED config — the boss chose it
    when authoring the standing order — NOT from the agent at runtime, so
    honouring it does not break the "the agent never picks recipients" rule (the
    agent-relayed ``recipient`` on a Communication stays audit-only). Lets a
    notify automation deliver to its configured address even when the channel's
    own ``notify_to`` is empty. Returns ``([], None, None)`` for non-automation
    runs or when the rule left the recipient blank (→ channel default).

    The ``create_automation`` contract stores the chosen channel as
    ``reaction_spec.channel_id`` (notify → {channel_id, recipient}). We resolve
    it to that channel's kind AND return its id so delivery is scoped to exactly
    that channel — previously only the legacy ``channel_kind`` was read, so a
    spec-authored automation got ``None`` and the recipient was broadcast to
    EVERY active channel of every kind.
    """
    try:
        from app.app_models.runtime import AgentRun, Event
        from app.app_models.automations import Automation
        from app import automations_service as asvc
        kind: str | None = None
        channel_id: int | None = None
        async with app_session() as s:
            run = (await s.execute(
                select(AgentRun).where(AgentRun.id == run_id)
            )).scalar_one_or_none()
            if run is None or not run.event_id:
                return [], None, None
            ev = (await s.execute(
                select(Event).where(Event.id == run.event_id)
            )).scalar_one_or_none()
            if ev is None or ev.type != "automation_fired":
                return [], None, None
            try:
                payload = json.loads(ev.payload_json or "{}")
            except Exception:
                payload = {}
            aid = payload.get("automation_id")
            if not aid:
                return [], None, None
            a = (await s.execute(
                select(Automation).where(Automation.id == int(aid))
            )).scalar_one_or_none()
            if a is None or (a.reaction_type or "") != "notify":
                return [], None, None
            rspec = asvc._json(a.reaction_spec)
            # Resolve the pinned channel_id → kind + id (scope to that channel).
            ch_id = rspec.get("channel_id")
            if ch_id not in (None, "", 0):
                try:
                    ch = (await s.execute(
                        select(Channel).where(Channel.id == int(ch_id))
                    )).scalar_one_or_none()
                    if ch is not None:
                        channel_id = ch.id
                        kind = (ch.kind or "").strip().lower() or None
                except Exception:
                    pass
        raw = rspec.get("recipient") or rspec.get("recipients") or ""
        if isinstance(raw, str):
            recips = [r.strip() for r in raw.replace(";", ",").split(",") if r.strip()]
        elif isinstance(raw, list):
            recips = [str(r).strip() for r in raw if str(r).strip()]
        else:
            recips = []
        # Fall back to the legacy channel_kind/channel keys when no channel_id.
        if kind is None:
            kind = (rspec.get("channel_kind") or rspec.get("channel") or "").strip().lower() or None
        return recips, kind, channel_id
    except Exception:  # pragma: no cover — never break delivery
        logger.debug("automation recipient resolve failed for run %s", run_id, exc_info=True)
        return [], None, None


async def _dispatch_intent(
    run_id: int, intent: NotifyIntent, *, provider_prefix: str,
    provider_suffix: str = "", recipients_override: list[str] | None = None,
) -> DeliveryOutcome:
    """Core delivery routine shared by the ``notify`` block and the
    ``ready`` communications paths. Resolves eligible channels, sends,
    records a ``ChannelMessage(direction='notification')`` per channel
    and — crucially — SURFACES the reason when nothing could be sent.

    ``recipients_override`` (used by boss-directed Communications) sends to an
    EXPLICIT recipient the caller vouches for, on the intent's channels — same
    trusted path as an operator-pinned automation recipient. Autonomous notify
    leaves it None and keeps the operator-configured-only safety model."""
    out = DeliveryOutcome(intent=True)
    # An OPERATOR-set recipient on the firing automation (trusted, from its
    # stored config) overrides the channel's notify_to for this delivery.
    override_recips, override_kind, override_channel_id = await _automation_recipient_for_run(run_id)
    # An EXPLICIT recipient on a boss-directed Communication takes precedence:
    # scoped to the intent's channel kind (no channel-id/kind pin), it delivers
    # to exactly the address the operator asked the employee to write to.
    if recipients_override:
        override_recips = [r for r in recipients_override if r]
        override_kind = None
        override_channel_id = None
    async with app_session() as s:
        channels = (await s.execute(
            select(Channel).where(
                Channel.status == "active",
                Channel.is_active == 1,
            )
        )).scalars().all()

    matched: list[Channel] = []
    blocked: list[tuple[Channel, str]] = []  # (channel, reason_code)
    eligible: list[tuple[Channel, list[str], int]] = []
    for channel in channels:
        if intent.channels and channel.kind not in intent.channels:
            continue
        notify_to, policy, max_per_day = _channel_notify_cfg(channel)
        # Operator pinned a recipient on the firing automation → deliver to it on
        # the chosen channel, regardless of the channel's own notify_to / policy.
        # Still operator-set (not agent-chosen), so the recipient-safety rule
        # holds; this is how a notify automation reaches its configured address
        # when the channel default is empty.
        if override_recips:
            # Scope to the pinned channel: by id when known (the operator chose a
            # SPECIFIC channel — don't fan out to its siblings of the same kind),
            # else by kind (legacy), else all eligible (no pin). A channel that
            # doesn't match the pin is SKIPPED entirely — the operator did not
            # select it, so it must not be notified via its own notify_to either.
            if override_channel_id is not None:
                if channel.id != override_channel_id:
                    continue
            elif override_kind is not None:
                if channel.kind != override_kind:
                    continue
            matched.append(channel)
            eligible.append((channel, override_recips, max_per_day))
            continue
        matched.append(channel)
        if not notify_to:
            blocked.append((channel, "no_recipients"))
            continue
        if policy == "never":
            blocked.append((channel, "paused"))
            continue
        if policy == "critical_only" and intent.severity != "critical":
            blocked.append((channel, "critical_only"))
            continue
        eligible.append((channel, notify_to, max_per_day))

    for channel, notify_to, max_per_day in eligible:
        tag = provider_suffix or str(run_id)
        provider_id = f"{provider_prefix}:{tag}:{channel.id}"
        try:
            sent_ids, send_errors = await _notify_one_channel(
                channel, notify_to, max_per_day, intent, run_id, provider_id,
            )
            if send_errors:
                out.send_errors.extend(send_errors)
            if sent_ids:
                out.delivered = True
                out.delivery_ids.extend(sent_ids)
                out.delivered_to.extend([r for r in (notify_to or []) if r])
        except Exception:
            logger.exception(
                "notification dispatch failed for channel %s (ignored)",
                channel.id,
            )

    if not out.delivered:
        if out.send_errors:
            out.blocked_reason = "; ".join(out.send_errors)[:2000]
        else:
            out.blocked_reason = _explain_blocked(matched, blocked)
        # Turn the silent drop into something the operator can SEE and
        # act on: write a visible error notification row for every
        # channel blocked by a genuine MISCONFIG (no recipients). We do
        # NOT spam rows for channels the operator intentionally paused
        # (policy='never' with recipients) or restricted to critical.
        await _record_blocked(
            run_id, intent, blocked, provider_prefix,
            provider_suffix or str(run_id),
        )
    return out


def _explain_blocked(
    matched: list[Channel], blocked: list[tuple[Channel, str]],
) -> str:
    """Build an operator-actionable reason for why nothing was sent."""
    if not matched:
        return (
            "No hay ningún canal activo que pueda entregar este aviso. "
            "Crea/activa un canal en Configuración › Canales."
        )
    reasons = {code for _, code in blocked}
    if "no_recipients" in reasons:
        chans = ", ".join(
            c.display_name or c.mailbox_address
            for c, code in blocked if code == "no_recipients"
        )
        return (
            "No se entregó: el canal no tiene destinatarios configurados "
            f"({chans}). Configúralos en Configuración › Canales "
            "(Notificaciones proactivas) para que el agente pueda enviar."
        )
    if reasons == {"paused"}:
        return (
            "No se entregó: las notificaciones proactivas del canal están "
            "en pausa (política 'never'). Actívalas en Configuración › Canales."
        )
    if reasons == {"critical_only"}:
        return (
            "No se entregó: el canal solo envía avisos de severidad "
            "'critical' y este no lo era."
        )
    return "No se entregó por la configuración de notificaciones del canal."


async def _record_blocked(
    run_id: int, intent: NotifyIntent,
    blocked: list[tuple[Channel, str]], provider_prefix: str, tag: str,
) -> None:
    """Persist a VISIBLE error notification row for channels blocked by a
    real misconfiguration (no recipients), so the operator sees the
    failed delivery in the channel thread instead of nothing."""
    now = datetime.now(timezone.utc)
    async with app_write() as s:
        for channel, code in blocked:
            if code != "no_recipients":
                continue
            provider_id = f"{provider_prefix}:{tag}:{channel.id}"
            ins = sqlite_insert(ChannelMessage).values(
                channel_id=channel.id,
                provider_message_id=provider_id,
                direction="notification",
                sender="(sin destinatarios)",
                subject=intent.subject,
                body_excerpt=None,
                received_at=now,
                status="error",
                run_id=run_id,
                reply_subject=(intent.subject or None),
                reply_body=(intent.body[:10000] or None),
                error=(
                    "No entregado: el canal no tiene destinatarios "
                    "configurados (notify_to vacío). Configúralos en "
                    "Configuración › Canales."
                ),
            ).on_conflict_do_nothing(
                index_elements=[
                    ChannelMessage.channel_id,
                    ChannelMessage.provider_message_id,
                ]
            )
            await s.execute(ins)
        await s.commit()


async def _resolve_recipients(
    channel: Channel, recipients: list[str],
) -> tuple[list[str], list[str]]:
    """Materialise operator-configured recipients into the handle the
    channel kind needs (v7.24.0). Recipients are DIRECTORY references
    (canonical emails picked with the UserPicker), so a WhatsApp channel
    resolves each email to the person's current directory phone — a phone
    edit in Usuarios propagates with no channel change. Raw handles
    (legacy configs, Slack ids) pass through untouched. Unresolvable
    references come back as errors instead of being silently dropped."""
    kind = (channel.kind or "email").lower()
    out: list[str] = []
    errs: list[str] = []
    for r in recipients:
        v = (r or "").strip()
        if not v:
            continue
        if kind == "whatsapp" and "@" in v:
            from app.users_directory import phone_for_email
            p = await phone_for_email(v)
            if p:
                out.append(p)
            else:
                errs.append(
                    f"{v}: la persona no tiene teléfono en el directorio (Usuarios)"
                )
            continue
        if kind == "email" and v.startswith("+"):
            from app.users_directory import email_for_phone
            e = await email_for_phone(v)
            if e:
                out.append(e)
            else:
                errs.append(
                    f"{v}: el teléfono no corresponde a nadie del directorio (Usuarios)"
                )
            continue
        out.append(v)
    seen: set[str] = set()
    uniq = [x for x in out if not (x.lower() in seen or seen.add(x.lower()))]
    return uniq, errs


async def _notify_one_channel(
    channel: Channel, notify_to: list[str], max_per_day: int,
    intent: NotifyIntent, run_id: int, provider_id: str,
) -> tuple[list[str], list[str]]:
    """Send one intent through one channel. Returns
    ``(provider_message_ids_sent, delivery_errors)``."""
    notify_to, resolve_errors = await _resolve_recipients(channel, notify_to)
    if not notify_to:
        return [], (resolve_errors or [
            "ningún destinatario configurado resuelve a un contacto del directorio",
        ])
    # Anti-spam + idempotent claim in one IMMEDIATE transaction. The
    # unique constraint (channel_id, provider_message_id) makes the row a
    # natural dedup key: a re-run with the same run_id will hit the
    # ON CONFLICT DO NOTHING and we skip the send.
    from app.app_database import app_engine, app_write

    async with app_engine.connect() as conn:
        if conn.dialect.name == "sqlite":
            try:
                await conn.exec_driver_sql("BEGIN IMMEDIATE")
            except Exception:
                pass
        if max_per_day > 0:
            start = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0,
            )
            sent = int((await conn.execute(
                select(func.count(ChannelMessage.id)).where(
                    ChannelMessage.channel_id == channel.id,
                    ChannelMessage.direction == "notification",
                    ChannelMessage.received_at >= start,
                )
            )).scalar() or 0)
            if sent >= max_per_day:
                logger.info(
                    "channel %s hit max_per_day=%s — notification dropped",
                    channel.id, max_per_day,
                )
                await conn.commit()
                return [], []
        ins = sqlite_insert(ChannelMessage).values(
            channel_id=channel.id,
            provider_message_id=provider_id,
            direction="notification",
            sender=", ".join(notify_to)[:500],
            subject=intent.subject,
            body_excerpt=None,
            received_at=datetime.now(timezone.utc),
            status="processing",
            run_id=run_id,
            reply_subject=(intent.subject or None),
            reply_body=(intent.body[:10000] or None),
            reply_attachments_json=(
                json.dumps([
                    {"filename": a.filename, "url": a.url or ""}
                    for a in intent.attachments
                ]) if intent.attachments else None
            ),
        ).on_conflict_do_nothing(
            index_elements=[
                ChannelMessage.channel_id,
                ChannelMessage.provider_message_id,
            ]
        )
        result = await conn.execute(ins)
        if (result.rowcount or 0) == 0:
            existing = (await conn.execute(
                select(
                    ChannelMessage.status,
                    ChannelMessage.reply_provider_message_id,
                    ChannelMessage.received_at,
                ).where(
                    ChannelMessage.channel_id == channel.id,
                    ChannelMessage.provider_message_id == provider_id,
                )
            )).first()
            if existing is None:
                await conn.commit()
                return [], []
            from app.config import NOTIFY_STALE_PROCESSING_SEC
            if not _notification_retry_allowed(
                existing[0],
                existing[1],
                existing[2],
                stale_sec=NOTIFY_STALE_PROCESSING_SEC,
            ):
                await conn.commit()
                logger.info(
                    "notification %s already exists (%s) — skipping",
                    provider_id, existing[0],
                )
                return [], []
            reclaim = await conn.execute(
                update(ChannelMessage)
                .where(
                    ChannelMessage.channel_id == channel.id,
                    ChannelMessage.provider_message_id == provider_id,
                    ChannelMessage.status.in_(["error", "processing"]),
                    ChannelMessage.reply_provider_message_id.is_(None),
                )
                .values(
                    status="processing",
                    run_id=run_id,
                    reply_subject=(intent.subject or None),
                    reply_body=(intent.body[:10000] or None),
                    reply_attachments_json=(
                        json.dumps([
                            {"filename": a.filename, "url": a.url or ""}
                            for a in intent.attachments
                        ]) if intent.attachments else None
                    ),
                    error=None,
                )
            )
            await conn.commit()
            if (reclaim.rowcount or 0) == 0:
                logger.info(
                    "notification %s lost reclaim race — skipping",
                    provider_id,
                )
                return [], []
            logger.info(
                "notification %s reclaimed row stuck in %s",
                provider_id, existing[0],
            )
        else:
            await conn.commit()

    reply = Reply(
        body=intent.body,
        subject=intent.subject,
        attachments=intent.attachments,
    )
    adapter = get_adapter(channel)
    sent_ids: list[str] = []
    errors: list[str] = list(resolve_errors)
    for recipient in notify_to:
        try:
            pid = await adapter.send_reply(
                _synthetic_message(recipient, intent.subject), reply,
            )
            if pid and str(pid).strip():
                sent_ids.append(str(pid).strip())
            else:
                errors.append(
                    f"{recipient}: el proveedor no devolvió id de mensaje "
                    "(envío incierto)"
                )
        except (ChannelError, Exception) as exc:  # noqa: BLE001
            detail = format_channel_exception(exc)
            errors.append(f"{recipient}: {detail}")
            logger.warning(
                "notification to %s on channel %s failed: %s",
                recipient, channel.id, detail,
                exc_info=not isinstance(exc, ChannelError),
            )

    async with app_write() as s:
        values: dict[str, Any] = {}
        if sent_ids:
            values["status"] = "replied"
            values["reply_provider_message_id"] = ",".join(sent_ids)[:500]
            values["replied_at"] = datetime.now(timezone.utc)
            if errors:
                values["error"] = ("; ".join(errors))[:2000]
        else:
            values["status"] = "error"
            values["error"] = ("; ".join(errors) or "send failed")[:2000]
        msg_res = await s.execute(
            update(ChannelMessage)
            .where(
                ChannelMessage.channel_id == channel.id,
                ChannelMessage.provider_message_id == provider_id,
                ChannelMessage.status.in_(["processing", "error"]),
            )
            .values(**values)
        )
        if sent_ids and (msg_res.rowcount or 0) > 0:
            await s.execute(
                update(Channel)
                .where(Channel.id == channel.id)
                .values(messages_replied=Channel.messages_replied + 1)
            )
        await s.commit()
    return sent_ids, errors


# ─────────────────────────────────────────────────────────────────────
# Communications reconciliation + dispatch
#
# ``log_communication`` (the app-tools MCP) stages a row in the
# ``communications`` table. Historically NOTHING ever moved those rows
# past ``ready``, so every "communication" looked stuck at LISTA PARA
# ENVIAR even after the agent had actually delivered the message via a
# ``notify`` block or a channel reply. These helpers close that loop.
# ─────────────────────────────────────────────────────────────────────


async def reconcile_communications(
    run_id: int, outcome: DeliveryOutcome, only_kind: str | None = None,
) -> None:
    """After a run delivered (or failed to deliver) through the ``notify``
    block, bring this run's ``ready`` communications in line with what
    actually happened, so the SPA shows ``sent``/``failed`` instead of a
    permanent ``ready``. Best-effort; never raises.

    ``only_kind`` restricts reconciliation to communications of THAT channel
    kind — used by a channel-origin reply so it does NOT mark a communication
    on a DIFFERENT medium (e.g. an email) as "sent" using the reply's own
    (WhatsApp) delivery id. Those are delivered separately via their own
    channel by ``dispatch_ready_communications``."""
    try:
        async with app_write() as s:
            # v7.107.3 — includes the rows the agent already recorded AS `sent` but without
            # a trace (`sent_at`/`delivery_id` NULL): they used to be left out of the reconcile
            # (it only looked at draft/ready) and never got the delivery receipt, so the
            # outbox piled up sends without proof (AGO-83).
            rows = (await s.execute(
                select(Communication).where(
                    Communication.created_by_run_id == run_id,
                    or_(
                        Communication.status.in_(("ready", "draft")),
                        and_(Communication.status == "sent",
                             Communication.sent_at.is_(None)),
                    ),
                )
            )).scalars().all()
            if not rows:
                return
            now = datetime.now(timezone.utc)
            for c in rows:
                # A draft is an explicit "do not send" — leave it alone.
                if c.status == "draft":
                    continue
                # Already `sent` (self-reported by the agent) and without a trace: the channel's
                # receipt is stamped on it; its state does not change even if the outcome does not mark it.
                if c.status == "sent" and c.sent_at is None and outcome.delivered:
                    await s.execute(
                        update(Communication).where(Communication.id == c.id)
                        .values(sent_at=now, delivery_id=outcome.delivery_id_str
                                or c.delivery_id or "agent-reported", error=None))
                    continue
                # Only reconcile comms of the delivered medium (when scoped):
                # a WhatsApp reply must not flip an email comm to "sent".
                if only_kind is not None and _comm_channel_kind(c.channel) != only_kind:
                    continue
                if outcome.delivered:
                    await s.execute(
                        update(Communication)
                        .where(Communication.id == c.id)
                        .values(
                            status="sent",
                            sent_at=now,
                            delivery_id=outcome.delivery_id_str,
                            error=None,
                        )
                    )
                elif outcome.blocked_reason:
                    await s.execute(
                        update(Communication)
                        .where(Communication.id == c.id)
                        .values(status="failed", error=outcome.blocked_reason[:2000])
                    )
            await s.commit()
    except Exception:
        logger.exception("communications reconcile failed for run %s", run_id)


async def dispatch_ready_communications(
    run_id: int, honor_recipient: bool = False,
) -> None:
    """Deliver this run's ``ready`` communications through the matching
    channel and update the row to ``sent``/``failed``. Best-effort; never
    raises.

    ``honor_recipient`` — for BOSS-DIRECTED runs (a chat/channel request where
    the operator explicitly asked the employee to write to someone) send to the
    communication's own ``recipient``. Autonomous/watcher runs leave it False
    and keep the operator-configured-only safety model (no agent-chosen
    address)."""
    try:
        async with app_session() as s:
            rows = (await s.execute(
                select(Communication).where(
                    Communication.created_by_run_id == run_id,
                    Communication.status == "ready",
                )
            )).scalars().all()
        for c in rows:
            await _deliver_communication(c, run_id, honor_recipient)
    except Exception:
        logger.exception(
            "ready-communications dispatch failed for run %s", run_id,
        )


async def _deliver_communication(c, run_id: int, honor_recipient: bool) -> None:
    """Deliver ONE ``ready`` communication through its channel and stamp the row
    ``sent``/``failed``. Shared by the run-scoped drain and the operator's manual
    release.

    v7.38.0 — non-deliverable channels no longer sit at ``ready`` FOREVER (the
    silent-stuck bug behind "pending communications are not being sent"):
      * ``internal`` is an in-app notice — there is no external channel to hit,
        so it is "delivered" as the record the operator already sees → mark
        ``sent`` (delivery_id=``internal``).
      * any other kind WITHOUT an adapter (``sms``/``ticket``/``webhook``/…) can
        NOT be delivered by the platform → mark ``failed`` with an actionable
        reason, so it is visible instead of stuck.
    """
    kind = _comm_channel_kind(c.channel)
    if kind is None:
        raw = (c.channel or "").strip().lower()
        now = datetime.now(timezone.utc)
        async with app_write() as s:
            if raw == "internal":
                # An in-app notice to the operator — homogenise the recipient so
                # the Outbox never shows a blank/placeholder for internal notes.
                ivals = dict(status="sent", sent_at=now, delivery_id="internal", error=None)
                if not (c.recipient or "").strip():
                    ivals["recipient"] = "operador"
                await s.execute(
                    update(Communication).where(Communication.id == c.id).values(**ivals)
                )
            else:
                await s.execute(
                    update(Communication).where(Communication.id == c.id).values(
                        status="failed",
                        error=(
                            f"El canal '{raw or '—'}' no admite envío desde la plataforma "
                            "(solo email, whatsapp, telegram o slack). Cambia el canal de la "
                            "comunicación o crea un canal compatible en Configuración › Canales."
                        )[:2000],
                    )
                )
            await s.commit()
        return
    intent = NotifyIntent(
        severity="info",
        subject=c.subject,
        body=c.body_md or "",
        attachments=_comm_attachments(c),
        channels=[kind],
    )
    if not intent.body:
        return
    recips = (
        [c.recipient] if (honor_recipient and (c.recipient or "").strip())
        else None
    )
    outcome = await _dispatch_intent(
        run_id, intent,
        provider_prefix="comm", provider_suffix=f"c{c.id}",
        recipients_override=recips,
    )
    now = datetime.now(timezone.utc)
    async with app_write() as s:
        if outcome.delivered:
            values = dict(status="sent", sent_at=now,
                          delivery_id=outcome.delivery_id_str, error=None)
            # v7.44.0 — store the REAL recipient(s) it was delivered to, so the
            # Outbox shows a concrete address/person instead of the placeholder
            # the agent may have written ("destinatario configurado").
            real = outcome.delivered_to_str
            if real:
                values["recipient"] = real
            await s.execute(
                update(Communication).where(Communication.id == c.id).values(**values)
            )
        else:
            await s.execute(
                update(Communication).where(Communication.id == c.id).values(
                    status="failed",
                    error=(outcome.blocked_reason
                           or "No se pudo entregar la comunicación.")[:2000],
                )
            )
        await s.commit()


async def dispatch_communication(comm_id: int, honor_recipient: bool = True) -> dict:
    """Deliver ONE communication NOW by id — the operator RELEASED it from the
    Outbox (draft→ready). ``honor_recipient`` defaults True because the operator
    explicitly approved sending to the row's recipient. Returns
    ``{status, error?}`` (the row's status after the attempt). Never raises."""
    try:
        async with app_session() as s:
            c = (await s.execute(select(Communication).where(
                Communication.id == comm_id,
                Communication.status == "ready",
            ))).scalars().first()
        if c is None:
            return {"status": "skipped", "error": "communication not ready or not found"}
        await _deliver_communication(c, c.created_by_run_id or 0, honor_recipient)
        async with app_session() as s:
            row = (await s.execute(select(
                Communication.status, Communication.error,
            ).where(Communication.id == comm_id))).first()
        return {"status": row[0] if row else "unknown", "error": row[1] if row else None}
    except Exception:  # noqa: BLE001 — never let a manual release blow up the API
        logger.exception("dispatch_communication failed for %s", comm_id)
        return {"status": "error", "error": "dispatch failed"}


async def _user_preferred_channel_kind(email: str, active_kinds: set[str]) -> str | None:
    """The channel to reach a PERSON on. Explicit ``metadata_json.preferred_channel``
    wins; else WhatsApp when the person has a phone and a WhatsApp channel is
    active; else email. Returns None when none can reach them."""
    pref = ""
    try:
        from app.app_models.users import AppUser
        async with app_session() as s:
            u = (await s.execute(
                select(AppUser).where(AppUser.email == email)
            )).scalar_one_or_none()
        if u is not None:
            try:
                pref = str((json.loads(u.metadata_json or "{}") or {}).get("preferred_channel") or "").strip().lower()
            except Exception:
                pref = ""
    except Exception:
        pref = ""
    from app.users_directory import REACHABLE_CHANNELS
    if pref in active_kinds and pref in REACHABLE_CHANNELS:
        return pref
    if "whatsapp" in active_kinds:
        from app.users_directory import phone_for_email
        if await phone_for_email(email):
            return "whatsapp"
    # v7.155.0 — Telegram as a real route: if the person has a handle in their record and the
    # channel is active, they can be written to. It was never chosen before, so an
    # employee with ONLY Telegram configured had no way to notify anyone.
    if "telegram" in active_kinds:
        from app.users_directory import telegram_for_email
        if await telegram_for_email(email):
            return "telegram"
    if "email" in active_kinds and "@" in email:
        return "email"
    return None


async def dispatch_user_reminder(item) -> bool:
    """A calendar item assigned to a PERSON came due → message that person on
    their preferred channel with what they have to do. Returns True when
    delivered (the watcher then marks the item done). Best-effort; never raises."""
    try:
        who = (getattr(item, "assigned_to", None) or "").strip()
        if not who:
            return False
        async with app_session() as s:
            channels = (await s.execute(
                select(Channel).where(Channel.status == "active", Channel.is_active == 1)
            )).scalars().all()
        active_kinds = {(c.kind or "").strip().lower() for c in channels}
        kind = await _user_preferred_channel_kind(who, active_kinds)
        if kind is None:
            return False
        title = str(getattr(item, "title", "") or "Recordatorio")
        desc = str(getattr(item, "description", "") or "").strip()
        body = f"Recordatorio: {title}"
        if desc:
            body += f"\n\n{desc}"
        body += "\n\n— Tu empleado de IA"
        intent = NotifyIntent(severity="info", subject=title, body=body, attachments=[], channels=[kind])
        outcome = await _dispatch_intent(
            int(getattr(item, "created_by_run_id", 0) or 0), intent,
            provider_prefix="reminder", provider_suffix=f"si{getattr(item, 'id', '')}",
            recipients_override=[who],
        )
        return bool(outcome.delivered)
    except Exception:  # noqa: BLE001 — a reminder must never break the watcher
        logger.exception("user reminder dispatch failed for item %s", getattr(item, "id", "?"))
        return False


def _comm_channel_kind(channel: str | None) -> str | None:
    """Map a communication ``channel`` to a deliverable channel KIND.
    email/whatsapp/telegram/slack have adapters and can be sent; ticket/etc. have
    no adapter, so they stay staged (returns None).
    v7.155.0 — «telegram» was missing: it had an adapter and messages came in through it
    fine, but any OUTBOUND communication towards Telegram stayed «staged»."""
    c = (channel or "").strip().lower()
    if c in ("email", "whatsapp", "telegram", "slack"):
        return c
    return None


def _comm_attachments(c: Communication) -> list[Attachment]:
    """Parse optional attachments staged on a communication row."""
    raw = getattr(c, "attachments_json", None)
    if not raw:
        return []
    try:
        from app.channels.executor import _extract_attachments
        return _extract_attachments(json.loads(raw))
    except Exception:
        return []
