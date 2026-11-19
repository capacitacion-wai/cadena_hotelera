"""v7.76.0 — unified Communications FEED for the "Comunicaciones" page.

Comunicaciones must show EVERY message the employee exchanged — IN and OUT, across
every channel — not just the agent's outbox staging. There are two underlying
tables; this endpoint presents them as ONE feed with ONE source PER LANE, so a
message is never shown twice:

  * ``received``  ← ``channel_messages(direction='inbound')``      — messages RECEIVED.
  * ``sent``      ← ``channel_messages(direction='notification')`` — everything actually
                    DELIVERED: alert→WhatsApp notify-rules, ``notify`` blocks, delivered
                    replies… (``_dispatch_intent`` writes one per channel it sent on).
  * ``pending``   ← ``communications(status in draft,ready)``, non-internal — staged,
                    not yet delivered (actionable: approve / send).
  * ``failed``    ← ``communications(status='failed')``, non-internal — delivery failed
                    (actionable: retry).
  * ``internal``  ← ``communications(channel='internal')`` — in-app notes, never sent to
                    a channel (shown as "Nota interna", no recipient/"sent" framing).

A delivered communication becomes ``status='sent'`` AND produces a
``channel_messages(notification)``. We source ``sent`` from the notification, so the
delivered message appears ONCE — ``communications`` with ``status='sent'`` is
intentionally NOT a lane. Project-scoped + read-only (actions run through the
existing ``communications`` entity endpoint).
"""
from __future__ import annotations

import html as _html
import json
import logging
import re
from email.header import decode_header, make_header

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select

from app.app_database import app_session
from app.app_models.business import Communication
from app.app_models.channels import Channel, ChannelMessage
from app.auth import TokenPayload, require_project_access
from app.timeutil import iso_utc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent/comms", tags=["communications"])

LANES = ("received", "sent", "pending", "failed", "internal")
_DRAFT_READY = ("draft", "ready")

# Inbound email is stored RAW: the subject/sender can be a MIME encoded-word
# (``=?UTF-8?q?…?=``) and the body can be full HTML. The feed must show it
# HUMAN-READABLE, not the raw source.
_STYLE_RE = re.compile(r"<(style|script|head|title)\b[^>]*>.*?(</\1\s*>|\Z)", re.S | re.I)
_ATRULE_RE = re.compile(r"@[a-z-]+[^{}]*\{(?:[^{}]|\{[^{}]*\})*\}", re.S | re.I)
# CSS-SHAPED only (inner has a ``prop: value;`` — a colon AND a semicolon), so a
# leaked-CSS body is stripped but real prose like "Hola {nombre}" or a JSON
# ``{"a":"b"}`` (no ``;``) is NOT deleted.
_CSSRULE_RE = re.compile(r"[^{}<>@]*\{[^{}]*:[^{}]*;[^{}]*\}", re.S)
_BLOCK_TAG_RE = re.compile(r"<\s*(br|/p|/div|/tr|/h[1-6]|/li)\s*/?>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def _decode_mime(s: str | None) -> str:
    """The real text of a header, to show it in the feed.

    Delegates to `channels.base.texto_de_cabecera`: the rule for how a header is read
    (decode and UNFOLD) lives in one place. Written twice, this copy only decoded and a
    long subject reached the feed split over two lines — the same value that, when
    replying, brought down the whole send.
    """
    from app.channels.base import texto_de_cabecera
    return texto_de_cabecera(s)


def _clean_body(s: str | None) -> str:
    """A readable text preview from an inbound body that may be full HTML OR —
    as some senders (e.g. Kommo) do — the RAW CSS of a ``<style>`` block stored
    WITHOUT its tags. Drops style/script blocks and CSS rules, turns block tags
    into newlines, strips the rest, unescapes entities and collapses whitespace."""
    if not s:
        return ""
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = _STYLE_RE.sub(" ", s)      # <style>…</style> (even unclosed → to end)
    s = _ATRULE_RE.sub(" ", s)     # @media { … } — leaked CSS with no tags
    s = _CSSRULE_RE.sub(" ", s)    # selector { … } — leaked CSS with no tags
    s = _BLOCK_TAG_RE.sub("\n", s)
    s = _TAG_RE.sub(" ", s)
    s = _html.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n[ \t]*", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _atts(raw) -> list[dict]:
    if not raw:
        return []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    out: list[dict] = []
    if isinstance(data, list):
        for a in data:
            if isinstance(a, dict) and (a.get("filename") or a.get("url")):
                out.append({"filename": a.get("filename") or a.get("url")})
    return out


def _lane_filters(lane: str):
    """The single-source WHERE condition(s) for a lane, as (model, condition)."""
    if lane == "received":
        return ChannelMessage, ChannelMessage.direction == "inbound"
    if lane == "sent":
        # Everything actually DELIVERED outbound, shown ONCE:
        #  - a proactive notification that WAS sent (status='replied'); NOT the
        #    in-flight ('processing') or failed ('error') rows — those must not
        #    read as "enviado", and a failed communication already shows in the
        #    'failed' lane, so this avoids the double-appearance.
        #  - the agent's REPLY to an inbound message (written back onto the inbound
        #    row with reply_provider_message_id) — otherwise the answer to a
        #    received email/WhatsApp shows nowhere.
        return ChannelMessage, (
            ((ChannelMessage.direction == "notification") & (ChannelMessage.status == "replied"))
            | ((ChannelMessage.direction == "inbound") & (ChannelMessage.reply_provider_message_id.isnot(None)))
        )
    if lane == "internal":
        return Communication, Communication.channel == "internal"
    if lane == "failed":
        return Communication, (Communication.channel != "internal") & (Communication.status == "failed")
    # pending — draft + ready, excluding in-app internal notes
    return Communication, (Communication.channel != "internal") & (Communication.status.in_(_DRAFT_READY))


def _order(model, lane: str):
    if model is ChannelMessage:
        # 'sent' is keyed on when the message went out (reply/notification time);
        # 'received' on arrival — so the sort matches the timestamp shown.
        if lane == "sent":
            return func.coalesce(ChannelMessage.replied_at, ChannelMessage.received_at).desc()
        return ChannelMessage.received_at.desc()
    return Communication.created_at.desc()


def _norm_channel_msg(m: ChannelMessage, kind: str, lane: str) -> dict:
    if lane == "received":
        return {
            "id": m.id, "lane": "received", "direction": "in",
            "channel": kind or "email", "correspondent": _decode_mime(m.sender),
            "subject": _decode_mime(m.subject),
            "body": _clean_body(m.body_excerpt or m.body_full or ""),
            "status": None, "created_at": iso_utc(m.received_at),
            "attachments": _atts(m.inbound_attachments_json),
            "delivery_id": None, "error": m.error, "actionable": False,
        }
    return {  # sent (notification): content lives in reply_*, sender = recipient(s)
        "id": m.id, "lane": "sent", "direction": "out",
        "channel": kind or "email", "correspondent": _decode_mime(m.sender),
        "subject": _decode_mime(m.reply_subject), "body": _clean_body(m.reply_body or ""),
        "status": "sent", "created_at": iso_utc(m.replied_at or m.received_at),
        "attachments": _atts(m.reply_attachments_json),
        "delivery_id": m.reply_provider_message_id, "error": m.error, "actionable": False,
    }


def _norm_comm(c: Communication, lane: str) -> dict:
    return {
        "id": c.id, "lane": lane, "direction": "internal" if lane == "internal" else "out",
        "channel": c.channel, "correspondent": None if lane == "internal" else c.recipient,
        "subject": c.subject, "body": c.body_md or "",
        "status": c.status, "created_at": iso_utc(c.created_at),
        "attachments": _atts(c.attachments_json),
        "delivery_id": c.delivery_id, "error": c.error,
        "actionable": lane in ("pending", "failed"),
    }


async def _channel_kinds(s) -> dict[int, str]:
    rows = (await s.execute(select(Channel.id, Channel.kind))).all()
    return {cid: (kind or "").lower() for cid, kind in rows}


@router.get("/producers")
async def comm_producers(_: TokenPayload = Depends(require_project_access)):
    from app import automation_provenance as prov

    return {"items": await prov.producers_of("communications")}


@router.get("/counts")
async def counts(request: Request, user: TokenPayload = Depends(require_project_access)):
    from app import automation_provenance as prov

    aid, only_open = prov.parse_provenance_query(request)
    # SECURITY: the CHANNEL MESSAGE lanes are other people's conversations with the
    # employee (sender + body). Only an admin sees them; for a member they count 0.
    from app.users_directory import is_admin
    admin = await is_admin(user.email)
    out: dict[str, int] = {}
    async with app_session() as s:
        for lane in LANES:
            model, cond = _lane_filters(lane)
            if model is ChannelMessage and not admin:
                out[lane] = 0
                continue
            if aid is not None and model is ChannelMessage:
                out[lane] = 0
                continue
            if model is Communication and aid is not None:
                clause = prov.sqlalchemy_provenance_filter(
                    Communication, aid, only_open=only_open)
                if clause is not None:
                    cond = cond & clause
                else:
                    out[lane] = 0
                    continue
            out[lane] = int((await s.execute(
                select(func.count()).select_from(model).where(cond)
            )).scalar() or 0)
    return {"counts": out}


@router.get("/feed")
async def feed(
    request: Request,
    lane: str = Query("received"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    user: TokenPayload = Depends(require_project_access),
):
    from app import automation_provenance as prov

    if lane not in LANES:
        lane = "received"
    model, cond = _lane_filters(lane)
    aid, only_open = prov.parse_provenance_query(request)
    # SECURITY: channel messages are OTHER people's conversations with the employee.
    # A member does not see them (empty lane, not an error: it is navigation, not a fault).
    if model is ChannelMessage:
        from app.users_directory import is_admin
        if not await is_admin(user.email):
            return {"items": [], "total": 0, "page": page, "page_size": page_size,
                    "filtered_by_automation": None}
    prov_ctx = None
    async with app_session() as s:
        if model is Communication and aid is not None:
            clause = prov.sqlalchemy_provenance_filter(
                Communication, aid, only_open=only_open)
            if clause is not None:
                cond = cond & clause
                prov_ctx = await prov.provenance_context(aid, only_open=only_open)
            else:
                return {"items": [], "total": 0, "page": page,
                        "page_size": page_size, "filtered_by_automation": None}
        elif model is ChannelMessage and aid is not None:
            return {"items": [], "total": 0, "page": page, "page_size": page_size,
                    "filtered_by_automation": await prov.provenance_context(
                        aid, only_open=only_open)}
        total = int((await s.execute(
            select(func.count()).select_from(model).where(cond)
        )).scalar() or 0)
        rows = (await s.execute(
            select(model).where(cond).order_by(_order(model, lane))
            .offset((page - 1) * page_size).limit(page_size)
        )).scalars().all()
        if model is ChannelMessage:
            kinds = await _channel_kinds(s)
            items = [_norm_channel_msg(m, kinds.get(m.channel_id, ""), lane) for m in rows]
        else:
            items = [_norm_comm(c, lane) for c in rows]
            items = await prov.attach_origin("communications", items)
    return {"items": items, "total": total, "page": page, "page_size": page_size,
            "filtered_by_automation": prov_ctx}
