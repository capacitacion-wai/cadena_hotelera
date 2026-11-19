"""v7.62.0 — SignalNotifyRule evaluation + delivery.

When an alert / recommendation / decision is CREATED, fire every operator rule
whose intensity thresholds it meets: message the rule's directory user via the
rule's channel. Evaluated inline at creation (no polling) and best-effort — a
notification miss must NEVER break signal creation.

Governance: the OPERATOR authors the rule (recipient + channel), so delivering to
it does not break the "the agent never picks recipients" rule — we reuse the same
``recipients_override`` trusted path as an operator-pinned automation recipient.
"""
from __future__ import annotations

import json
import logging

from sqlalchemy import select

from app.timeutil import now_utc

logger = logging.getLogger(__name__)

# Intensity ranks for the "X o más" thresholds (higher = more intense).
_IMPACT_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}
_URGENCY_RANK = {"low": 1, "medium": 2, "high": 3, "immediate": 4, "critical": 4}
_CONFIDENCE_RANK = {"low": 1, "medium": 2, "high": 3}

# v7.80.0 — an INTRO line so a channel notification reads as a message ("Se ha
# creado una nueva alerta: …") instead of a bare title+body dump, in the
# employee's configured language (``resolve_agent_language``).
_NOTIFY_INTRO = {
    "alerts": {"es": "🔔 Se ha creado una nueva alerta:", "en": "🔔 A new alert has been created:"},
    "recommendations": {"es": "💡 Nueva recomendación del empleado:", "en": "💡 New recommendation from the employee:"},
    "decisions": {"es": "🗒️ Nueva decisión registrada:", "en": "🗒️ New decision logged:"},
}


def _notify_intro(table: str, lang: str) -> str:
    per_table = _NOTIFY_INTRO.get(table) or {}
    return per_table.get(lang) or per_table.get("es") or ""


def _meets(value, minimum, rank: dict) -> bool:
    """True if the row's band value is at least the threshold band ("X o más")."""
    if not minimum:
        return True  # no threshold on this axis
    v = rank.get(str(value or "").lower(), 0)
    m = rank.get(str(minimum).lower(), 0)
    return v > 0 and v >= m


def _rule_matches(cond: dict, row: dict) -> bool:
    """Evaluate a rule's thresholds against a signal row. A rule with NO threshold
    never fires (avoid notifying on every single signal)."""
    checks: list[bool] = []
    if cond.get("impact_min"):
        checks.append(_meets(row.get("impact"), cond["impact_min"], _IMPACT_RANK))
    if cond.get("urgency_min"):
        checks.append(_meets(row.get("urgency"), cond["urgency_min"], _URGENCY_RANK))
    if cond.get("confidence_min"):
        checks.append(_meets(row.get("confidence"), cond["confidence_min"], _CONFIDENCE_RANK))
    if not checks:
        return False
    return any(checks) if (cond.get("match") or "all") == "any" else all(checks)


async def _resolve_address(session, kind: str, user_email: str):
    """Resolve the directory user's address for the channel KIND: their email for
    an email channel, their first directory phone (E.164) for WhatsApp. Slack has
    no directory handle yet → None (delivery records a visible failure)."""
    k = (kind or "").lower()
    if k == "email":
        return user_email or None
    if k == "whatsapp":
        try:
            from app.app_models.users import AppUser
            from app.users_directory import loads_list
            u = (await session.execute(
                select(AppUser).where(AppUser.email == user_email)
            )).scalar_one_or_none()
            phones = loads_list(getattr(u, "phones_json", None)) if u else []
            return phones[0] if phones else None
        except Exception:  # pragma: no cover
            return None
    if k == "telegram":
        # 7.155.0 — Telegram was MISSING here, so a notification rule towards a
        # Telegram channel was left with no recipient and silently sent NOTHING
        # (measured: rule active since 27/07, 34 matching alerts, 0 sends).
        #
        # And the address is NOT the @username: a Telegram bot cannot open a
        # conversation with someone by name — it can only write to a `chat_id`
        # of a conversation the person has started. That id arrives with every
        # inbound message (`provider_thread_id`), so it is resolved like this:
        #   email → directory handle → their last message → their chat_id.
        try:
            from app.app_models.channels import ChannelMessage
            from app.app_models.users import AppUser
            u = (await session.execute(
                select(AppUser).where(AppUser.email == user_email)
            )).scalar_one_or_none()
            handle = (getattr(u, "telegram", None) or "").strip().lstrip("@").lower()
            if not handle:
                return None
            return (await session.execute(
                select(ChannelMessage.provider_thread_id)
                .where(ChannelMessage.sender == handle,
                       ChannelMessage.provider_thread_id.isnot(None))
                .order_by(ChannelMessage.id.desc()).limit(1)
            )).scalar_one_or_none()
        except Exception:  # pragma: no cover
            return None
    return None


async def evaluate_signal_rules(session, table: str, row: dict, run_id) -> int:
    """Fire matching notify rules for a just-created signal. Returns #delivered.
    Never raises."""
    from app.app_models.notify_rules import SignalNotifyRule
    from app.app_models.channels import Channel
    from app.channels.notifier import NotifyIntent, _dispatch_intent
    from app.agent_locale import resolve_agent_language

    delivered = 0
    try:
        rules = (await session.execute(
            select(SignalNotifyRule).where(
                SignalNotifyRule.signal_table == table,
                SignalNotifyRule.enabled.is_(True),
            )
        )).scalars().all()
        if not rules:
            return 0
        intro = _notify_intro(table, await resolve_agent_language())
        for rule in rules:
            try:
                cond = json.loads(rule.conditions_json or "{}")
            except Exception:
                cond = {}
            if not isinstance(cond, dict) or not _rule_matches(cond, row):
                continue
            channel = (await session.execute(
                select(Channel).where(Channel.id == int(rule.channel_id))
            )).scalar_one_or_none()
            if channel is None:
                logger.info("notify rule %s: channel %s missing — skipped", rule.id, rule.channel_id)
                continue
            address = await _resolve_address(session, channel.kind, rule.recipient_user)
            if not address:
                logger.info("notify rule %s: no %s address for %s — skipped",
                            rule.id, channel.kind, rule.recipient_user)
                continue
            title = str(row.get("title") or row.get("reason_md") or "").strip()
            desc = str(row.get("description") or "").strip()
            # Introduce the notification as a message in the employee's language,
            # e.g. "🔔 A new alert has been created: <title>" (in the configured language) + the description.
            # The subject becomes an email Subject header → collapse newlines/tabs
            # (defense against header injection) and fall back to a generic subject
            # when the title is empty (avoid an intro-only "…:" subject).
            safe_title = " ".join(title.split())
            subject = ((f"{intro} {safe_title}".strip() if intro else safe_title)
                       if safe_title else "Aviso del empleado")[:180]
            core = (f"{title}\n\n{desc}" if desc else title).strip()
            body = (f"{intro}\n\n{core}" if intro else core)[:4000]
            intent = NotifyIntent(
                severity=str(row.get("severity") or "info").lower(),
                subject=subject, body=body, attachments=[], channels=[channel.kind],
            )
            try:
                outcome = await _dispatch_intent(
                    int(run_id or 0), intent, provider_prefix="signal_rule",
                    provider_suffix=f"rule{rule.id}-{row.get('id')}",
                    recipients_override=[address],
                )
                if getattr(outcome, "delivered", False):
                    delivered += 1
                    rule.last_fired_at = now_utc().replace(tzinfo=None)
            except Exception:  # pragma: no cover — delivery must not break creation
                logger.debug("notify rule %s dispatch failed", rule.id, exc_info=True)
        if delivered:
            await session.commit()
    except Exception:  # pragma: no cover
        logger.debug("evaluate_signal_rules failed for %s", table, exc_info=True)
    return delivered
