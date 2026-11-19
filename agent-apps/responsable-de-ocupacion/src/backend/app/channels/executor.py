"""Executor branch for ``type='external_message'`` events.

Called from ``app/autonomous/executor.py:run_agent_session`` when the
claimed event was emitted by the channel poller. The flow is:

  1. Load the ``channel_messages`` row referenced by the event payload.
  2. Build a channel-aware prompt (subject + sender + body excerpt).
  3. Call the simple-API sidecar exactly like a normal autonomous run.
  4. Parse ``structured.reply.body`` (the agent's draft reply).
  5. Hand the reply to the adapter so the platform sends it back.
  6. Update ``channel_messages.status`` (``replied`` / ``skipped`` /
     ``error``) and bump ``channels.messages_replied`` accordingly.

The simple-API sidecar still appends/parses the STRUCTURED_RESPONSE
suffix so the agent always returns ``{summary, metrics, [next_step,
reply]}``. Reply is documented in ``patterns/external-channels.md`` as
an OPTIONAL block — when absent or empty, the executor marks the
message ``skipped``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update

from app.agent_proxy import FatalAgentError, get_agent_proxy
from app.agent_sessions import (
    build_memory_block, get_session_manager, resolve_inbound_scope,
    workspace_block,
)
from app.memory_prompt import memory_capture_hint
from app.timeutil import local_stamp
from app.app_database import app_session, app_write
from app.app_models.channels import Channel, ChannelMessage
from app.channels.base import (
    MAX_ATTACHMENTS, Attachment, ChannelConfigError, ChannelError,
    ChannelUnauthorizedError, Message, Reply, get_adapter,
)
from app.config import PROMPT_BODY_BUDGET_CHARS
from app.deliverables import sanitize_attachment_list

logger = logging.getLogger(__name__)


async def _inbound_processing_stale(
    msg_row: ChannelMessage,
    *,
    stale_sec: int,
    now: datetime | None = None,
) -> bool:
    """True when a ``processing`` inbound row looks abandoned (crash /
    orphan) and a retry may reclaim it. Uses the linked run's age when
    ``run_id`` is set; otherwise falls back to ``received_at``."""
    from app.app_models.runtime import AgentRun
    from app.channels.notifier import _notification_retry_allowed

    now = now or datetime.now(timezone.utc)
    if msg_row.run_id:
        async with app_session() as s:
            run = (await s.execute(
                select(AgentRun.status, AgentRun.started_at)
                .where(AgentRun.id == msg_row.run_id)
            )).first()
        if run is not None:
            status, started = run
            if (status or "").upper() == "RUNNING" and started is not None:
                st = started
                if st.tzinfo is None:
                    st = st.replace(tzinfo=timezone.utc)
                if (now - st).total_seconds() < float(stale_sec):
                    return False
            return True
    return _notification_retry_allowed(
        msg_row.status,
        msg_row.reply_provider_message_id,
        msg_row.received_at,
        stale_sec=stale_sec,
        now=now,
    )


def _clamp_body_for_window(text: str, budget: int = PROMPT_BODY_BUDGET_CHARS) -> str:
    """Protect the context window on oversized inbound mails.

    Replies carry the whole quoted chain, which we want to keep. Only
    when a single mail exceeds ``budget`` do we trim it: email clients
    put the NEW message on top and the older quoted history at the
    bottom, so we keep the most recent ``budget`` characters (cutting at
    a nearby line break to avoid splitting mid-sentence) and replace the
    oldest tail with a visible marker. Under budget ⇒ returned intact.
    """
    body = text or ""
    if budget <= 0 or len(body) <= budget:
        return body
    head = body[:budget]
    # Prefer cutting at the last line break within the budget so we don't
    # slice a line in half; fall back to the raw cut if there is none.
    nl = head.rfind("\n")
    if nl > budget // 2:
        head = head[:nl]
    omitted = len(body) - len(head)
    return (
        head.rstrip()
        + f"\n\n[--- histórico anterior recortado por límite de tamaño: "
        f"{omitted} caracteres omitidos ---]"
    )


# File classes for inbound-attachment handling guidance. BINARY office /
# PDF formats must NOT be ``cat``-ed as text — doing so floods the context
# with megabytes of zip/XML noise and makes the agent stall (this caused a
# 900 s×3 gateway-timeout hang on a .docx, v6.36.6). Text formats can be
# read directly.
_TEXTLIKE_EXTS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".xml", ".html", ".htm", ".log", ".rst", ".ini", ".cfg", ".toml",
}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
# zip-based office containers — text is extractable with ``unzip -p`` even
# without python libs.
_ZIP_OFFICE_EXTS = {".docx", ".pptx", ".xlsx"}
_BINARY_DOC_EXTS = _ZIP_OFFICE_EXTS | {
    ".pdf", ".doc", ".ppt", ".xls", ".odt", ".ods", ".odp", ".rtf",
}


def _file_kind(name: str) -> str:
    import os
    ext = os.path.splitext((name or "").lower())[1]
    if ext in _TEXTLIKE_EXTS:
        return "text"
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _BINARY_DOC_EXTS:
        return "doc"
    return "binary"


def _inbound_files_block(msg: ChannelMessage) -> str:
    """Render an 'ATTACHMENTS RECEIVED' block when the inbound
    message carried attachments the app staged into the workspace. Lists
    each file with its `/workspace`-relative path and mime, and — crucially
    — tells the agent HOW to read each KIND so it never stalls on a binary
    office/PDF file (v6.36.6). Returns "" when there are none."""
    raw = getattr(msg, "inbound_attachments_json", None)
    if not raw:
        return ""
    try:
        files = json.loads(raw)
    except Exception:
        return ""
    if not isinstance(files, list) or not files:
        return ""
    lines = []
    kinds: set[str] = set()
    for f in files:
        if not isinstance(f, dict):
            continue
        path = f.get("workspace_path")
        if not path:
            continue
        name = f.get("filename") or path.rsplit("/", 1)[-1]
        mime = f.get("mime")
        size = f.get("size")
        kind = _file_kind(name)
        kinds.add(kind)
        tag = {
            "text": "TEXT",
            "image": "IMAGE",
            "doc": "BINARY (document)",
            "binary": "BINARY",
        }[kind]
        extra = ", ".join(
            x for x in (mime, (f"{size} bytes" if size else None)) if x
        )
        suffix = f" ({extra})" if extra else ""
        lines.append(f"- /workspace/{path}{suffix}  [{name}] — {tag}")
    if not lines:
        return ""
    how: list[str] = [
        "HOW TO READ EACH ATTACHMENT (important so you do not get stuck!):",
    ]
    if "text" in kinds:
        how.append(
            "- TEXT (csv, json, txt, md…): you can read it directly."
        )
    if "doc" in kinds:
        how.append(
            "- BINARY (document) (.docx/.pptx/.xlsx/.pdf…): do NOT read it "
            "as raw text (do NOT use `cat`/direct reading: they are zip/XML "
            "or binary and flood the context). Extract its CONTENT: for "
            "`.docx/.pptx/.xlsx` use `unzip -p <path> word/document.xml` "
            "(or `ppt/slides/*.xml`, `xl/sharedStrings.xml`) and strip the "
            "XML tags; for `.pdf` use `pdftotext <path> -` if it is "
            "available. If you cannot extract it after ONE reasonable attempt, "
            "do NOT retry in a loop: carry on with what you have and say so "
            "in `reply.body`."
        )
    if "image" in kinds or "binary" in kinds:
        how.append(
            "- IMAGE/other binaries: do not dump them as text; "
            "describe them by name/type if they are relevant."
        )
    how.append(
        "If you are asked for 'the same format' or 'an identical format' to a "
        "binary document, do NOT try to clone the file byte by byte: "
        "generate a clean EQUIVALENT (e.g. PDF/CSV/PPTX) based on its "
        "content and deliver it as an attachment."
    )
    return (
        "\n--- ATTACHMENTS RECEIVED ---\n"
        "The sender attached these files; the system has already saved them in "
        "your workspace:\n"
        + "\n".join(lines)
        + "\n"
        + "\n".join(how)
        + "\n--- end of attachments received ---\n"
    )


def _build_prompt(channel: Channel, msg: ChannelMessage, exports_dir: str = "exports") -> str:
    """Render the prompt the agent sees. Channel-aware: emails carry
    subject + sender + thread context. Slack / Teams will plug in here
    with their own template once we add those adapters.

    ``exports_dir`` is the per-ORIGIN exports folder (relative to
    ``/workspace``, e.g. ``sessions/channels/3/john-…/exports``) so each
    sender's attachments land in their own folder; defaults to the
    legacy shared ``exports`` for back-compat."""
    # Prefer the full quoted chain (body_full); fall back to the short
    # preview for legacy rows. Clamp only when it threatens the window.
    body = _clamp_body_for_window(msg.body_full or msg.body_excerpt or "")
    inbound = _inbound_files_block(msg)
    if channel.kind == "email":
        return (
            "You have received a new email that the system "
            "can answer on your behalf.\n\n"
            f"From: {msg.sender or 'unknown'}\n"
            f"Subject: {msg.subject or '(no subject)'}\n"
            f"Received: {local_stamp(msg.received_at or datetime.now(timezone.utc))}\n"
            f"Monitored mailbox: {channel.mailbox_address}\n\n"
            "--- Message body ---\n"
            f"{body or '(empty)'}\n"
            "--- end of message ---\n"
            f"{inbound}\n"
            "Analyse the request and write a professional, useful "
            "reply, in the language the sender writes in. If the reply "
            "requires querying business data, do it through the available "
            "MCP tools. IMPORTANT: the "
            "sender will ONLY see what you put in the `reply.body` block "
            "of the STRUCTURED_RESPONSE — they have no access to this session or to "
            "`summary`. Therefore: (a) if you have the answer, put it in "
            "`reply.body`; (b) if you need a clarification from the sender "
            "to continue, THAT QUESTION is your reply — write it in "
            "`reply.body` so it reaches them by email; (c) only leave "
            "`reply.body` empty when it is NOT appropriate to answer at all "
            "(spam, out of scope, risk of a loop), recording the "
            "reason in `summary`.\n\n"
            "EMAIL FORMAT: write `reply.body` in Markdown — the "
            "system converts it automatically to formatted HTML before "
            "sending it, so it will look right in Gmail/Outlook. Use "
            "Markdown TABLES (`| Col | Col |` with their separator row "
            "`|---|---:|`) for any tabular data (e.g. sales per "
            "month), headings (`##`), **bold** and lists with `-`. "
            "Right-align numeric columns with `---:`. Do NOT "
            "write HTML by hand or paste tables as plain text with "
            "spaces; use Markdown and let the system render it.\n\n"
            "ATTACHMENTS: if the reply includes files (a presentation, "
            "a report, a CSV…), generate them and save them in "
            f"`/workspace/{exports_dir}/` (create it if it does not exist) and add them "
            "to `reply.attachments` as a list of objects "
            '{"filename": "informe.pdf", "workspace_path": '
            f'"{exports_dir}/informe.pdf"}}. The system will read the file and '
            "send it as a real email attachment (you can attach "
            "several). Alternatives: {\"filename\": \"...\", \"url\": "
            "\"...\"} with an accessible download URL, or {\"filename\": "
            "\"...\", \"content_base64\": \"...\", \"mime_type\": \"...\"} "
            "for small files generated inline. "
            "Do NOT ask for confirmation about the delivery format: deliver "
            "directly through this channel.\n"
            "HARD RULE: if the sender asks to receive a file through "
            "this channel ('send it to me by email', 'send me the report'…), "
            "your reply MUST include it as an ATTACHMENT: generate the "
            "export (PPTX/PDF/CSV…), write it PHYSICALLY in "
            f"`/workspace/{exports_dir}/` (check with ls that it exists and "
            "weighs >0) and reference it in `reply.attachments` with "
            "`workspace_path`. NEVER deliver links (to WAI or "
            "anywhere else) instead of the attachment. Replying with a "
            "link, saying the file 'is available in the "
            "workspace' or offering to send it later are "
            "INVALID replies. If you truly cannot produce the "
            "file, explain in `reply.body` what failed and record "
            "`log_decision`. Follow the skill `wai-autonomous-delivery`."
        )
    if channel.kind == "whatsapp":
        return (
            "You have received a new WhatsApp message that the system "
            "can answer on your behalf.\n\n"
            f"From: {msg.sender or 'unknown'}\n"
            f"Received: {local_stamp(msg.received_at or datetime.now(timezone.utc))}\n"
            f"Monitored number: {channel.mailbox_address}\n\n"
            "--- Message ---\n"
            f"{body or '(empty)'}\n"
            "--- end of message ---\n"
            f"{inbound}\n"
            "Analyse the request and write a reply, in the language the "
            "sender writes in. It is a "
            "WhatsApp chat: answer briefly and directly (at most ~1000 "
            "characters), in plain text without markdown, without a subject and without "
            "an email signature. If the reply requires querying business "
            "data, do it through the available MCP tools. "
            "IMPORTANT: the sender will ONLY see what you put in the "
            "`reply.body` block of the STRUCTURED_RESPONSE. If you need "
            "a clarification to continue, THAT QUESTION is your reply "
            "— write it in `reply.body`. Only leave `reply.body` empty "
            "when it is NOT appropriate to answer at all (spam, out of "
            "scope, risk of a loop), recording the reason in "
            "`summary`.\n"
            "ATTACHMENTS: if the reply includes files (PDF, CSV, image, "
            f"PPTX…), generate them and save them in `/workspace/{exports_dir}/` and "
            "add them to `reply.attachments` as "
            '{"filename": "informe.pdf", "workspace_path": '
            f'"{exports_dir}/informe.pdf"}}. The system sends them as '
            "REAL attachments of the WhatsApp message (the first goes with the "
            "text and the rest in following messages). "
            '{"filename": "...", "url": "..."} with a public URL also works. Do NOT '
            "deliver audio (not supported for now). Do NOT ask for confirmation "
            "about the delivery format: deliver directly through this channel."
        )
    if channel.kind == "telegram":
        return (
            "You have received a new Telegram message that the system "
            "can answer on your behalf.\n\n"
            f"From: {msg.sender or 'unknown'}\n"
            f"Received: {local_stamp(msg.received_at or datetime.now(timezone.utc))}\n"
            f"Monitored bot: {channel.mailbox_address}\n\n"
            "--- Message ---\n"
            f"{body or '(empty)'}\n"
            "--- end of message ---\n"
            f"{inbound}\n"
            "Analyse the request and write a reply, in the language the "
            "sender writes in. It is a "
            "Telegram chat: answer briefly and directly, in plain text without "
            "markdown, without a subject and without an email signature. If the reply "
            "requires querying business data, do it through the available MCP "
            "tools. IMPORTANT: the sender will ONLY see what you put in the "
            "`reply.body` block of the STRUCTURED_RESPONSE. If you need a "
            "clarification to continue, THAT QUESTION is your reply — write it "
            "in `reply.body`. Only leave `reply.body` empty when it is NOT "
            "appropriate to answer at all (spam, out of scope, risk of a loop), "
            "recording the reason in `summary`.\n"
            "ATTACHMENTS: if the reply includes files (PDF, CSV, image, "
            f"PPTX…), generate them and save them in `/workspace/{exports_dir}/` and "
            "add them to `reply.attachments` as "
            '{"filename": "informe.pdf", "workspace_path": '
            f'"{exports_dir}/informe.pdf"}}. The system uploads them as '
            "REAL attachments of the Telegram message (direct byte upload). Deliver "
            "directly through this channel, without asking for confirmation about the format."
        )
    return (
        f"Inbound message on channel {channel.kind!r}.\n"
        f"From: {msg.sender or 'unknown'}\n"
        f"Content:\n{body or ''}\n\n"
        "If appropriate, return the reply in `reply.body`, in the language the "
        "sender writes in."
    )


async def _load_message(payload: dict[str, Any]) -> tuple[ChannelMessage, Channel] | None:
    """Return ``(msg_row, channel_row)`` or ``None`` when the channel /
    message has been deleted between the event emission and the executor
    pick-up."""
    channel_id = int(payload.get("channel_id") or 0)
    provider_id = (payload.get("provider_message_id") or "").strip()
    if not channel_id or not provider_id:
        return None
    async with app_session() as s:
        msg = (await s.execute(
            select(ChannelMessage).where(
                ChannelMessage.channel_id == channel_id,
                ChannelMessage.provider_message_id == provider_id,
            )
        )).scalar_one_or_none()
        if msg is None:
            return None
        ch = (await s.execute(
            select(Channel).where(Channel.id == channel_id)
        )).scalar_one_or_none()
        if ch is None:
            return None
    return msg, ch


def _to_adapter_message(channel: Channel, msg: ChannelMessage) -> Message:
    return Message(
        provider_message_id=msg.provider_message_id,
        provider_thread_id=msg.provider_thread_id,
        sender=msg.sender,
        subject=msg.subject,
        body_text=msg.body_full or msg.body_excerpt or "",
        body_html=None,
        received_at=msg.received_at,
        # Threading headers: we did not persist them on the
        # channel_message row to keep the table small. They are
        # rebuilt from what the provider returns; the adapter knows
        # what to do.
        headers={
            "in_reply_to": msg.provider_message_id,
            "references": [msg.provider_message_id],
            "gmail_thread_id": msg.provider_thread_id or "",
        },
    )


def _extract_reply(structured: dict[str, Any] | None) -> Reply | None:
    """Pull ``reply`` from the STRUCTURED_RESPONSE tail. Tolerant of
    omissions — every field is optional and we only act if ``body`` is
    non-empty."""
    if not isinstance(structured, dict):
        return None
    raw = structured.get("reply")
    if not isinstance(raw, dict):
        return None
    body = (raw.get("body") or "").strip()
    if not body:
        return None
    return Reply(
        body=body,
        subject=(raw.get("subject") or None),
        cc=list(raw.get("cc") or []),
        bcc=list(raw.get("bcc") or []),
        attachments=_extract_attachments(raw.get("attachments")),
    )


def _extract_attachments(raw: Any) -> list[Attachment]:
    """Parse ``reply.attachments`` defensively. Malformed entries are
    dropped (never block the reply). Visual-QA intermediates (``slide-NN.jpg``)
    are stripped by the agent-app deliverable policy. Capped at ``MAX_ATTACHMENTS``."""
    raw = sanitize_attachment_list(raw)
    if not raw:
        return []
    out: list[Attachment] = []
    for item in raw[:MAX_ATTACHMENTS]:
        if not isinstance(item, dict):
            continue
        filename = str(item.get("filename") or item.get("name") or "").strip()
        url = str(item.get("url") or "").strip() or None
        content_b64 = str(item.get("content_base64") or "").strip() or None
        ws_path = str(
            item.get("workspace_path") or item.get("path") or ""
        ).strip() or None
        if not (url or content_b64 or ws_path):
            continue
        if url and not url.lower().startswith(("http://", "https://")):
            continue
        if not filename:
            # Derive something usable from the path / URL tail.
            src = ws_path or url or "adjunto"
            filename = src.rstrip("/").rsplit("/", 1)[-1][:120] or "adjunto"
        out.append(Attachment(
            filename=filename[:200],
            url=url,
            content_base64=content_b64,
            mime_type=str(item.get("mime_type") or item.get("mimetype") or "").strip() or None,
            workspace_path=ws_path,
        ))
    return out


async def run_channel_message(event: dict[str, Any], run_id: int) -> dict[str, Any]:
    """Run one ``external_message`` event end-to-end.

    Returns the normalised result dict consumed by ``_finish_run`` (same
    shape as ``autonomous.executor.run_agent_session``). Sending failures
    are surfaced through the result dict (status='error') rather than as
    exceptions so the executor's state machine doesn't trigger a retry
    that would re-send to the user.
    """
    payload = json.loads(event.get("payload_json") or "{}")
    pair = await _load_message(payload)
    if pair is None:
        return {
            "text": "",
            "structured": {"summary": "channel message disappeared"},
            "tokens_in": 0, "tokens_out": 0, "model": "",
            "duration_sec": 0, "cost_usd": "", "session_id": None,
        }
    msg_row, channel_row = pair

    # Idempotency guard — `replied` and `skipped` are TERMINAL. A
    # recovery re-claim (pod died between send_reply and mark_completed),
    # an event retry or a manual reprocess must NEVER invoke the agent
    # again nor re-send the reply to the user.
    if msg_row.status in ("replied", "skipped"):
        logger.info(
            "channel message %s already %s — skipping re-run (idempotency)",
            msg_row.id, msg_row.status,
        )
        return {
            "text": "",
            "structured": {
                "summary": f"message already {msg_row.status}; no re-send",
            },
            "tokens_in": 0, "tokens_out": 0, "model": "",
            "duration_sec": 0, "cost_usd": "", "session_id": None,
        }

    # Recovery path: reply went out (provider id set) but status was not
    # marked terminal — self-heal and never re-send.
    if (msg_row.reply_provider_message_id or "").strip():
        if msg_row.status not in ("replied", "skipped"):
            async with app_write() as s:
                healed = await s.execute(
                    update(ChannelMessage)
                    .where(
                        ChannelMessage.id == msg_row.id,
                        ChannelMessage.status.notin_(["replied", "skipped"]),
                    )
                    .values(status="replied")
                )
                if (healed.rowcount or 0) > 0:
                    await s.execute(
                        update(Channel)
                        .where(Channel.id == channel_row.id)
                        .values(messages_replied=Channel.messages_replied + 1)
                    )
                await s.commit()
        logger.info(
            "channel message %s already has reply_provider_message_id "
            "— skipping re-run (idempotency)",
            msg_row.id,
        )
        return {
            "text": "",
            "structured": {
                "summary": "message already replied; no re-send",
            },
            "tokens_in": 0, "tokens_out": 0, "model": "",
            "duration_sec": 0, "cost_usd": "", "session_id": None,
        }

    # Mark the channel_message as processing + link it to the run row.
    from app.config import NOTIFY_STALE_PROCESSING_SEC
    async with app_write() as s:
        claimed = await s.execute(
            update(ChannelMessage)
            .where(
                ChannelMessage.id == msg_row.id,
                ChannelMessage.status.in_(["pending", "error"]),
            )
            .values(
                status="processing",
                event_id=event.get("id"),
                run_id=run_id,
            )
        )
        if (claimed.rowcount or 0) == 0:
            fresh = (await s.execute(
                select(ChannelMessage).where(ChannelMessage.id == msg_row.id)
            )).scalar_one_or_none()
            if fresh is not None:
                if fresh.status in ("replied", "skipped"):
                    await s.commit()
                    return {
                        "text": "",
                        "structured": {
                            "summary": f"message already {fresh.status}; no re-send",
                        },
                        "tokens_in": 0, "tokens_out": 0, "model": "",
                        "duration_sec": 0, "cost_usd": "", "session_id": None,
                    }
                if (fresh.reply_provider_message_id or "").strip():
                    await s.commit()
                    return {
                        "text": "",
                        "structured": {
                            "summary": "message already replied; no re-send",
                        },
                        "tokens_in": 0, "tokens_out": 0, "model": "",
                        "duration_sec": 0, "cost_usd": "", "session_id": None,
                    }
                if fresh.status == "processing":
                    if not await _inbound_processing_stale(
                        fresh, stale_sec=NOTIFY_STALE_PROCESSING_SEC,
                    ):
                        await s.commit()
                        logger.info(
                            "channel message %s still processing in-flight "
                            "— skipping duplicate run",
                            msg_row.id,
                        )
                        return {
                            "text": "",
                            "structured": {
                                "summary": "message already processing; no re-send",
                            },
                            "tokens_in": 0, "tokens_out": 0, "model": "",
                            "duration_sec": 0, "cost_usd": "", "session_id": None,
                        }
                    reclaimed = await s.execute(
                        update(ChannelMessage)
                        .where(
                            ChannelMessage.id == msg_row.id,
                            ChannelMessage.status == "processing",
                            ChannelMessage.reply_provider_message_id.is_(None),
                        )
                        .values(
                            event_id=event.get("id"),
                            run_id=run_id,
                        )
                    )
                    if (reclaimed.rowcount or 0) == 0:
                        await s.commit()
                        return {
                            "text": "",
                            "structured": {
                                "summary": "message already processing; no re-send",
                            },
                            "tokens_in": 0, "tokens_out": 0, "model": "",
                            "duration_sec": 0, "cost_usd": "", "session_id": None,
                        }
        await s.commit()

    # Per-ORIGIN session + workspace folder. A thread with the same
    # sender reuses one OpenCode session (keeps context across messages)
    # and writes its artefacts under its own folder, so two senders'
    # exports never collide. Watcher/system runs never reach here.
    from app.autonomous.executor import plan_verify_block, traceability_block
    # Per-ORIGIN scope, UNIFIED onto the sender's chat session when they
    # are a known operator (so chat + email share session, folder and
    # memory). Falls back to the channel origin scope otherwise.
    scope = await resolve_inbound_scope(channel_row.id, msg_row.sender or "unknown")
    from app.memory_utils import persist_run_memory_user
    await persist_run_memory_user(run_id, scope.memory_user)
    exports_dir = f"{scope.workspace_subdir}/exports"
    from app.scoped_knowledge import build_scoped_knowledge_block
    # The employee replies AS itself (persona), in business voice — not as a
    # generic assistant. Identity goes first.
    try:
        from app.persona import employee_identity_preamble
        from app.agent_locale import resolve_agent_language
        _identity = await employee_identity_preamble(await resolve_agent_language())
    except Exception:
        _identity = ""

    async def _write_policy_block_safe() -> str:
        try:
            from app.mcp_policy import write_policy_block
            return await write_policy_block()
        except Exception:  # pragma: no cover — a hint must never break a reply
            return ""

    async def _agent_memory_block_safe(query: str) -> str:
        """The environment learnings + how to record new ones. Fail-open: a reply
        to a human must never break because a memory block could not be built."""
        try:
            from app.agent_memory_prompt import (
                agent_memory_capture_hint, build_agent_memory_block_detail,
                saturation_warning,
            )
            from app.memory_contract import MODE_CHANNEL, two_memories_contract
            # 7.164.0 — serving a channel is REAL work with a real outcome, so this
            # path closes the loop too: it records which learnings go inside and
            # `_finish_run` —the same one the autonomous path uses— later distributes
            # the outcome. Without this, `used_ok` only grew through the autonomous
            # branch and the signal was biased towards what is used at night.
            _mem = await build_agent_memory_block_detail(query=query)
            try:
                from app.agent_memory_utils import record_injection
                await record_injection(run_id, _mem.get("entry_ids") or [])
            except Exception:  # pragma: no cover
                pass
            return ((_mem.get("text") or "")
                    + agent_memory_capture_hint()
                    + await saturation_warning()
                    # The boundary (v7.143.0). In a channel there is a person AND an
                    # environment at the same time, so it is the path where they are
                    # easiest to mix up: the writer's preference goes to THEIR memory,
                    # how the system behaves goes to its own.
                    + two_memories_contract(
                        MODE_CHANNEL, memory_user=scope.memory_user or ""))
        except Exception:  # pragma: no cover
            return ""

    from app.mcp_runtime import composio_tools_hint
    prompt = (
        (_identity + "\n\n" if _identity else "")
        + _build_prompt(channel_row, msg_row, exports_dir)
        + plan_verify_block()
        + traceability_block(run_id)
        + workspace_block(scope.workspace_subdir)
        + await build_memory_block(
            scope.memory_user,
            query=(msg_row.body_full or msg_row.body_excerpt or msg_row.subject or ""),
        )
        # How to drive the Composio connectors (router meta-tools vs direct) —
        # the agent replies to email/WhatsApp consulting the CRM, so it needs it.
        + composio_tools_hint()
        # Write-policy rules (v7.26.0): read-only / approval-gated connectors
        # and how to park a write for the operator's approval.
        + await _write_policy_block_safe()
        # Memory distillation (v7.29.0): emit the structured `memory` block.
        + memory_capture_hint()
        # v7.140.0 — the two memories are ORTHOGONAL and both belong here: the
        # per-person block above says who this human is, this one says how the
        # environment behaves. Answering "is that slow?" needs the second one
        # even when it is a person asking.
        + await _agent_memory_block_safe(
            msg_row.body_full or msg_row.body_excerpt or msg_row.subject or "")
        # Inbound channel message = serving a human → "attending" scope (+ the
        # sender's per-user knowledge).
        + await build_scoped_knowledge_block(
            attending=True, memory_user=scope.memory_user,
        )
    )

    # Persist the REAL prompt on the run row. The autonomous executor
    # pre-fills it with app.prompts (which may carry an unfilled
    # generic template for external_message) — the operator must see
    # in /runs exactly what the agent received.
    async with app_write() as s:
        from app.app_models.runtime import AgentRun
        await s.execute(
            update(AgentRun).where(AgentRun.id == run_id).values(
                prompt=prompt,
                # v12.7.0 — WHO asked for this. In a channel it is not a WAI user: it is
                # the address of whoever wrote, which often matches none of our
                # accounts. It is recorded here because it is the first point where it is
                # known —the autonomous executor creates the row before loading the
                # message— and because `channel_messages` may be pruned while the run is kept.
                solicitante=(msg_row.sender or None),
                canal_kind=(channel_row.kind or None),
                canal_id=channel_row.id,
            )
        )
        await s.commit()

    # Ask the agent — same simple-API sidecar contract as any other
    # autonomous run. The sidecar appends STRUCTURED_RESPONSE itself.
    # If the agent call fails, the message must NOT stay in
    # `processing`: it is flagged `error` (a retryable, non-terminal
    # state) so the event retry can pick it up again and the SPA shows
    # an honest status instead of an orphan row.
    # Wall-clock floor so post-run memory indexing only picks up files this
    # run actually wrote to the origin's exports folder.
    import time as _time
    started_ts = _time.time()

    proxy = get_agent_proxy()
    mgr = get_session_manager()
    session_id: str | None = None
    try:
        handle = await mgr.resolve(scope)
        session_id = handle.session_id
        try:
            raw = await proxy.send_autonomous(
                session_id, prompt, require_structured=True,
            )
        except FatalAgentError as exc:
            # Self-heal: the registry pointed at a session the sidecar
            # has already GC'd (idle). Mint a fresh one for this origin
            # and retry ONCE; any other fatal error propagates.
            if " 404:" not in str(exc):
                raise
            logger.info(
                "channel session for scope=%s went stale — rotating",
                scope.scope_key,
            )
            handle = await mgr.resolve(scope, force_new=True)
            session_id = handle.session_id
            raw = await proxy.send_autonomous(
                session_id, prompt, require_structured=True,
            )
    except BaseException as exc:
        # Abort the in-flight generation on the (persistent) channel session
        # so a timed-out / step-ceiling run is actually stopped before the
        # event is retried — otherwise the runaway run keeps burning compute
        # on the reused session. We abort (not delete): the session is shared
        # across this sender's messages and must survive.
        if session_id:
            try:
                await proxy.abort_session(session_id)
            except Exception:  # pragma: no cover — best-effort cleanup
                logger.debug("abort on error failed for session %s", session_id)
        async with app_write() as s:
            await s.execute(
                update(ChannelMessage)
                .where(ChannelMessage.id == msg_row.id)
                .values(status="error", error=str(exc)[:2000])
            )
            await s.commit()
        raise
    await mgr.touch(
        scope.scope_key,
        context_tokens=(
            int(raw.get("tokens_input") or 0) + int(raw.get("tokens_output") or 0)
        ) or None,
    )

    structured = raw.get("structured") or {}
    reply = _extract_reply(structured)

    # Fallback — the model did the work but ignored the `reply` block
    # (it answered in the analytical text instead). A DELIBERATE skip
    # is signalled by an explicit `reply` object with an empty body;
    # when the key is missing entirely and the agent produced text,
    # that text IS the answer and the sender must receive it.
    if (
        reply is None
        and "reply" not in structured
        and (raw.get("text") or "").strip()
        and channel_row.reply_mode != "never"
    ):
        logger.warning(
            "channel %s msg %s: agent omitted reply block — falling "
            "back to the run text as reply body", channel_row.id, msg_row.id,
        )
        reply = Reply(body=(raw.get("text") or "").strip())

    # Resolve final disposition.
    disposition: str
    reply_provider_id: str | None = None
    error: str | None = None

    if channel_row.reply_mode == "never" or reply is None:
        disposition = "skipped"
    else:
        try:
            adapter = get_adapter(channel_row)
            reply_provider_id = await adapter.send_reply(
                _to_adapter_message(channel_row, msg_row),
                reply,
            )
            disposition = "replied"
            # Write-ahead: persist the outbound id before the full
            # finalize so a crash after send never triggers a re-send.
            async with app_write() as s:
                await s.execute(
                    update(ChannelMessage)
                    .where(
                        ChannelMessage.id == msg_row.id,
                        ChannelMessage.status == "processing",
                        ChannelMessage.reply_provider_message_id.is_(None),
                    )
                    .values(
                        reply_provider_message_id=reply_provider_id,
                        replied_at=datetime.now(timezone.utc),
                    )
                )
                await s.commit()
        except (ChannelConfigError, ChannelUnauthorizedError, ChannelError) as exc:
            disposition = "error"
            error = str(exc)
            logger.warning(
                "channel %s reply failed for msg %s: %s",
                channel_row.id, msg_row.id, exc,
            )

    # Persist the disposition + telemetry. The outbound content is
    # stored on the message row (even on a failed send) so the SPA can
    # show the full thread: inbound mail + the answer that went out.
    async with app_write() as s:
        values: dict[str, Any] = {"status": disposition}
        if reply is not None:
            values["reply_subject"] = (reply.subject or "")[:500] or None
            values["reply_body"] = (reply.body or "")[:10000] or None
            if reply.attachments:
                values["reply_attachments_json"] = json.dumps([
                    {"filename": a.filename, "url": a.url or ""}
                    for a in reply.attachments
                ])
        if reply_provider_id:
            values["reply_provider_message_id"] = reply_provider_id
            values["replied_at"] = datetime.now(timezone.utc)
        if error:
            values["error"] = error[:2000]
        msg_res = await s.execute(
            update(ChannelMessage)
            .where(
                ChannelMessage.id == msg_row.id,
                ChannelMessage.status == "processing",
            )
            .values(**values)
        )
        if disposition == "replied" and (msg_res.rowcount or 0) > 0:
            await s.execute(
                update(Channel)
                .where(Channel.id == channel_row.id)
                .values(messages_replied=Channel.messages_replied + 1)
            )
        await s.commit()

    # Reconcile this run's communications log against the real delivery:
    # a reply that went out marks any staged 'ready' communication as
    # 'sent' (a failed send marks it 'failed'), so the SPA never shows a
    # message stuck at LISTA PARA ENVIAR after it was actually answered.
    if disposition in ("replied", "error"):
        try:
            from app.channels.notifier import (
                DeliveryOutcome, dispatch_ready_communications,
                reconcile_communications,
            )
            # Reconcile ONLY comms of the ORIGIN medium with this reply — a
            # WhatsApp reply must not flag an email comm as "sent" with the
            # WhatsApp delivery id (that made emails show ENVIADA while SMTP was
            # never called).
            await reconcile_communications(
                run_id,
                DeliveryOutcome(
                    intent=True,
                    delivered=disposition == "replied",
                    delivery_ids=[reply_provider_id] if reply_provider_id else [],
                    blocked_reason=(error if disposition == "error" else None),
                ),
                only_kind=(channel_row.kind or None),
            )
            # Now actually DELIVER any staged comms on OTHER media (e.g. the
            # email the boss asked for) via their own channel adapter. This is a
            # boss-directed run, so honour the communication's explicit
            # recipient. Marks each sent/failed honestly — no more false sent.
            await dispatch_ready_communications(run_id, honor_recipient=True)
        except Exception:
            logger.exception(
                "communications reconcile/dispatch failed for channel run %s", run_id,
            )

    # Per-user memory: record the interaction as an episode and index any
    # deliverables the agent produced — mirrors the chat path
    # (``_finish_chat_run``) so an exchange held over email/WhatsApp shows
    # up in the Memoria panel exactly like a chat turn. Best-effort: never
    # block the run or alter the message disposition. Inbound attachments
    # are already indexed by the poller at ingestion time.
    if disposition in ("replied", "error"):
        try:
            from app.memory_utils import (
                auto_index_generated_files, auto_record_chat_episode,
            )
            reply_body = (reply.body if reply else "") or ""
            run_text = raw.get("text") or ""
            artifact_paths = [
                a.workspace_path
                for a in (reply.attachments if reply else [])
                if a.workspace_path
            ]
            detect_text = "\n".join(
                x for x in (reply_body, run_text, *artifact_paths) if x
            )
            inbound_for_files = " ".join(
                x for x in (
                    msg_row.subject or "",
                    msg_row.body_excerpt or "",
                ) if x
            ).strip()
            indexed = await auto_index_generated_files(
                run_id,
                scope.memory_user,
                detect_text,
                workspace_subdir=scope.workspace_subdir,
                since_ts=started_ts,
                source=channel_row.kind or "email",
                description=f"Entregado por {channel_row.kind or 'email'}",
                user_message=inbound_for_files,
            )
            inbound_text = "\n".join(
                x for x in (
                    msg_row.subject or "",
                    msg_row.body_full or msg_row.body_excerpt or "",
                ) if x
            ).strip()
            await auto_record_chat_episode(
                run_id,
                scope.memory_user,
                user_message=inbound_text,
                response_text=reply_body or run_text,
                artifacts=indexed,
                source=channel_row.kind or "email",
            )
        except Exception:  # pragma: no cover — memory must never block delivery
            logger.debug(
                "channel memory recording failed for run %s", run_id,
                exc_info=True,
            )

    return {
        "text": raw.get("text") or "",
        "structured": structured,
        "tokens_in": int(raw.get("tokens_input") or 0),
        "tokens_out": int(raw.get("tokens_output") or 0),
        "model": raw.get("model") or "",
        "duration_sec": int(raw.get("duration_sec") or 0),
        "cost_usd": str(raw.get("cost") or ""),
        "session_id": session_id,
    }
