"""IMAP + SMTP channel adapter.

Use this when the operator's mailbox provider blocks Composio OAuth
(corporate Exchange, custom IMAP, etc.) and they can issue an *App
Password* instead. The password is stored encrypted (Fernet) in
``channels.imap_config_encrypted``. The adapter decrypts in RAM only.

Provider auto-detection (host / port / TLS) keys off the local part of
the email — see ``IMAP_DEFAULTS``. Operators can always override.
"""

from __future__ import annotations

import email
import email.utils
import logging
import re
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Any, Optional

from app.channels.base import (
    ChannelAdapter, ChannelConfigError, ChannelError,
    ChannelUnauthorizedError, InboundAttachment, MAX_INBOUND_ATTACHMENTS,
    MAX_INBOUND_ATTACHMENT_BYTES, MAX_INBOUND_ATTACHMENTS_TOTAL_BYTES,
    Message, Reply, TestResult, channel_process_since,
    texto_de_cabecera, valor_de_cabecera,
)
from app.channels.encryption import decrypt_blob

logger = logging.getLogger(__name__)

# Messages above this size are skipped (cursor still advances). Large
# enough to carry a typical set of attachments (the app stages them into
# the workspace); pathological mails beyond this are skipped, not loaded.
MAX_MESSAGE_BYTES = 48 * 1024 * 1024


# domain → ({imap host, imap port, use_ssl}, {smtp host, smtp port, use_tls})
IMAP_DEFAULTS: dict[str, dict[str, Any]] = {
    "gmail.com":   {"imap": ("imap.gmail.com", 993, True),
                    "smtp": ("smtp.gmail.com", 587, True)},
    "googlemail.com": {"imap": ("imap.gmail.com", 993, True),
                       "smtp": ("smtp.gmail.com", 587, True)},
    "outlook.com": {"imap": ("outlook.office365.com", 993, True),
                    "smtp": ("smtp.office365.com", 587, True)},
    "hotmail.com": {"imap": ("outlook.office365.com", 993, True),
                    "smtp": ("smtp.office365.com", 587, True)},
    "office365.com": {"imap": ("outlook.office365.com", 993, True),
                      "smtp": ("smtp.office365.com", 587, True)},
    "icloud.com":  {"imap": ("imap.mail.me.com", 993, True),
                    "smtp": ("smtp.mail.me.com", 587, True)},
    "yahoo.com":   {"imap": ("imap.mail.yahoo.com", 993, True),
                    "smtp": ("smtp.mail.yahoo.com", 587, True)},
}


def autodetect_servers(email_address: str) -> dict[str, Any] | None:
    """Return the default IMAP/SMTP coordinates for the email's domain,
    or ``None`` when the domain is not in the autodetect table."""
    if not email_address or "@" not in email_address:
        return None
    domain = email_address.rsplit("@", 1)[-1].strip().lower()
    return IMAP_DEFAULTS.get(domain)


def _next_uidvalidity_cursor(uidvalidity: int, last_uid: int) -> str:
    """Build a string cursor that survives UIDVALIDITY rotation. Format
    ``"<uidvalidity>:<uid>"``. When the server rotates UIDVALIDITY (an
    INBOX recreate / migration), we reset the uid side and re-scan
    once. Without this guard we'd miss every message after a server
    migration."""
    return f"{int(uidvalidity)}:{int(last_uid)}"


def _parse_cursor(cursor: Optional[str]) -> tuple[int, int]:
    if not cursor or ":" not in cursor:
        return (0, 0)
    try:
        a, b = cursor.split(":", 1)
        return int(a), int(b)
    except Exception:
        return (0, 0)


def _strip_html(html: str) -> str:
    """Very small HTML → plaintext for body_excerpt. We don't want a
    full lxml/bs4 dependency for what is essentially an excerpt."""
    text = re.sub(r"<\s*br[^>]*>", "\n", html, flags=re.I)
    text = re.sub(r"<\s*/?p[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def _decode_payload(part) -> str:
    """Decode a MIME part payload to text."""
    try:
        raw = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace")
    except Exception:
        return ""


def _extract_bodies(msg) -> tuple[str, Optional[str]]:
    """Return ``(plain, html)`` from a python ``email.message.Message``.

    Tries to walk multipart messages picking the first text/plain and
    the first text/html. Falls back to whatever the message carries.
    """
    plain = ""
    html = None
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            if not plain and ctype == "text/plain":
                plain = _decode_payload(part)
            elif html is None and ctype == "text/html":
                html = _decode_payload(part)
    else:
        ctype = msg.get_content_type()
        body = _decode_payload(msg)
        if ctype == "text/html":
            html = body
            plain = _strip_html(body)
        else:
            plain = body
    if not plain and html:
        plain = _strip_html(html)
    return plain, html


def _extract_attachments(msg) -> list[InboundAttachment]:
    """Collect attachment parts (bytes inline) from a parsed email.

    Walks the MIME tree picking parts marked ``attachment`` (or inline
    parts that carry a filename). Enforces per-file / count / total caps
    so a hostile mail can't exhaust memory or disk; oversized or
    over-count parts are skipped, never fatal.
    """
    if not msg.is_multipart():
        return []
    out: list[InboundAttachment] = []
    total = 0
    for part in msg.walk():
        if part.is_multipart():
            continue
        disp = (part.get("Content-Disposition") or "").lower()
        filename = part.get_filename()
        is_attach = "attachment" in disp or (filename and "inline" in disp)
        if not is_attach and not (filename and not disp):
            # Skip plain body parts (handled by _extract_bodies).
            continue
        if not filename:
            continue
        try:
            data = part.get_payload(decode=True) or b""
        except Exception:
            continue
        if not data or len(data) > MAX_INBOUND_ATTACHMENT_BYTES:
            continue
        if total + len(data) > MAX_INBOUND_ATTACHMENTS_TOTAL_BYTES:
            break
        total += len(data)
        out.append(InboundAttachment(
            filename=filename,
            mime_type=part.get_content_type() or None,
            size=len(data),
            data=data,
        ))
        if len(out) >= MAX_INBOUND_ATTACHMENTS:
            break
    return out



# Which IMAP server replies really mean "that credential is not valid". The rest
# —including no reply at all— is transient and deserves a retry, not a permanent removal.
_RECHAZO_DE_CREDENCIAL = (
    "AUTHENTICATIONFAILED", "AUTHORIZATIONFAILED", "INVALID CREDENTIALS",
    "USERNAME AND PASSWORD NOT ACCEPTED", "LOGIN FAILED", "APPLICATION-SPECIFIC PASSWORD",
    "WEBLOGIN REQUIRED", "PRIVACY REQUIRED", "ACCOUNT DISABLED",
)


def _texto_de_respuesta(r) -> str:
    """The lines the server returned, in one string. `aioimaplib` gives them as a list of
    bytes and without this you cannot look at WHAT it said, only whether it said OK."""
    try:
        return " ".join(
            x.decode("utf-8", "replace") if isinstance(x, (bytes, bytearray)) else str(x)
            for x in (r.lines or [])
        )
    except Exception:  # noqa: BLE001 — reading the reason can never bring the polling down
        return ""


def _es_rechazo_de_credencial(r) -> bool:
    """`NO` + an authentication reason. A bare (or empty) `NO` does NOT count: servers
    also answer `NO` when they are overloaded, and that is retried."""
    if (r.result or "").upper() != "NO":
        return False
    texto = _texto_de_respuesta(r).upper()
    return any(m in texto for m in _RECHAZO_DE_CREDENCIAL)


class ImapSmtpEmailAdapter(ChannelAdapter):
    """Concrete adapter for ``kind='email' auth_method='imap_smtp'``."""

    kind = "email"
    auth_method = "imap_smtp"

    def __init__(self, channel) -> None:
        self.channel = channel
        self._cfg: dict[str, Any] | None = None

    # ── credential plumbing ───────────────────────────────────────────
    def _config(self) -> dict[str, Any]:
        if self._cfg is not None:
            return self._cfg
        token = self.channel.imap_config_encrypted
        if not token:
            raise ChannelConfigError(
                "IMAP channel has no stored credentials. Run the wizard "
                "again with email + App Password."
            )
        try:
            cfg = decrypt_blob(token)
        except Exception as exc:
            raise ChannelConfigError(str(exc)) from exc
        # Defensive defaults — the wizard already auto-fills these but a
        # hand-edited manifest could leave them empty.
        auto = autodetect_servers(cfg.get("user") or self.channel.mailbox_address) or {}
        if not cfg.get("host"):
            cfg["host"], cfg["port"], cfg["use_ssl"] = auto.get("imap", ("", 0, True))
        if not cfg.get("smtp_host"):
            cfg["smtp_host"], cfg["smtp_port"], cfg["smtp_use_tls"] = auto.get("smtp", ("", 0, True))
        self._cfg = cfg
        return cfg

    # ── ChannelAdapter contract ───────────────────────────────────────
    async def test_connection(self) -> TestResult:
        try:
            cfg = self._config()
        except ChannelConfigError as exc:
            return TestResult(ok=False, message=str(exc))
        from aioimaplib import aioimaplib
        host, port, use_ssl = cfg["host"], int(cfg["port"]), bool(cfg.get("use_ssl", True))
        try:
            cli = aioimaplib.IMAP4_SSL(host=host, port=port) if use_ssl else \
                  aioimaplib.IMAP4(host=host, port=port)
            await cli.wait_hello_from_server()
            r = await cli.login(cfg["user"], cfg["password"])
            if (r.result or "").upper() != "OK":
                return TestResult(ok=False, message=f"IMAP LOGIN rejected: {r.result}")
            r = await cli.select("INBOX")
            if (r.result or "").upper() != "OK":
                await cli.logout()
                return TestResult(ok=False, message=f"IMAP SELECT INBOX failed: {r.result}")
            await cli.logout()
        except Exception as exc:
            return TestResult(ok=False, message=f"IMAP connection failed: {exc}")
        return TestResult(
            ok=True, message="IMAP connection OK",
            detail={"host": host, "port": port, "smtp_host": cfg.get("smtp_host")},
        )

    async def fetch_new(
        self, cursor: Optional[str],
    ) -> tuple[list[Message], Optional[str]]:
        cfg = self._config()
        from aioimaplib import aioimaplib
        host, port, use_ssl = cfg["host"], int(cfg["port"]), bool(cfg.get("use_ssl", True))
        cli = aioimaplib.IMAP4_SSL(host=host, port=port) if use_ssl else \
              aioimaplib.IMAP4(host=host, port=port)
        try:
            await cli.wait_hello_from_server()
            r = await cli.login(cfg["user"], cfg["password"])
            if (r.result or "").upper() != "OK":
                # "Not OK" does NOT mean "bad credential". A mail server says no for many
                # transient reasons —too many simultaneous connections, a temporary system
                # error, a timeout that leaves `result` empty— and calling them
                # "unauthorized" has a disproportionate consequence: the polling STOPS
                # looking at that channel forever (`_active_channels` only takes the
                # `active` ones) and the employee is left without inbound email until a
                # person notices and presses "test". It really happened: the Odoo
                # employee's channel had been down without anything saying so, and it was
                # "fixed" just by pressing test — the password was always fine.
                if _es_rechazo_de_credencial(r):
                    raise ChannelUnauthorizedError(f"IMAP LOGIN rejected: {r.result}")
                raise ChannelError(
                    f"IMAP LOGIN no concluyó ({r.result or 'sin respuesta'}): "
                    f"{_texto_de_respuesta(r)[:200]}")
            r = await cli.select("INBOX")
            if (r.result or "").upper() != "OK":
                raise ChannelError(f"IMAP SELECT INBOX failed: {r.result}")
            # UIDVALIDITY: parse from the SELECT response so we can detect
            # an INBOX recreation (Gmail almost never rotates; Exchange
            # sometimes does after migration).
            uidvalidity = _select_uidvalidity(r)
            last_uidv, last_uid = _parse_cursor(cursor)
            if last_uidv != uidvalidity:
                last_uid = 0  # treat as fresh start

            # UID > last_uid; limit to a defensive page so a busy inbox
            # doesn't OOM the pod. On a FRESH start (no cursor yet) with a
            # "process since" floor, narrow the server-side search by date
            # (IMAP SINCE is day-granular) so we jump straight to recent
            # mail instead of paging through years of history; the poller
            # still enforces the exact datetime floor per message.
            since_dt = channel_process_since(self.channel)
            if last_uid == 0 and since_dt is not None:
                since_str = since_dt.strftime("%d-%b-%Y")
                search = await cli.uid_search(f"SINCE {since_str}")
            else:
                search = await cli.uid_search(f"UID {last_uid + 1}:*")
            if (search.result or "").upper() != "OK":
                raise ChannelError(f"IMAP UID SEARCH failed: {search.result}")
            uids_line = b" ".join(search.lines or []) if isinstance(search.lines, list) else b""
            uids: list[int] = []
            for tok in uids_line.split():
                try:
                    n = int(tok)
                    if n > last_uid:
                        uids.append(n)
                except ValueError:
                    continue
            uids = sorted(set(uids))[:50]

            # Batch RFC822.SIZE first so oversized messages (huge
            # attachments) are skipped WITHOUT ever loading them in RAM
            # — 50 full RFC822 bodies with attachments could otherwise
            # blow the pod's memory limit.
            sizes: dict[int, int] = {}
            if uids:
                seq = ",".join(str(u) for u in uids)
                sz = await cli.uid("fetch", seq, "(RFC822.SIZE)")
                if (sz.result or "").upper() == "OK":
                    for line in sz.lines or []:
                        if not isinstance(line, (bytes, bytearray)):
                            continue
                        mu = re.search(rb"UID (\d+)", line)
                        ms = re.search(rb"RFC822\.SIZE (\d+)", line)
                        if mu and ms:
                            sizes[int(mu.group(1))] = int(ms.group(1))

            messages: list[Message] = []
            highest = last_uid
            for uid in uids:
                size = sizes.get(uid)
                if size is not None and size > MAX_MESSAGE_BYTES:
                    logger.warning(
                        "imap channel: skipping uid %s (%d bytes > %d cap)",
                        uid, size, MAX_MESSAGE_BYTES,
                    )
                    # Emit a visible sentinel (no body loaded) so the operator
                    # sees WHY this mail wasn't answered, instead of it
                    # silently disappearing. The cursor still advances past it.
                    mb = size / (1024 * 1024)
                    messages.append(Message(
                        provider_message_id=f"imap-uid-{uid}",
                        subject="(mensaje omitido: demasiado grande)",
                        received_at=datetime.now(timezone.utc),
                        skip_reason=(
                            f"Mensaje demasiado grande ({mb:.1f} MB) — supera "
                            f"el límite de {MAX_MESSAGE_BYTES // (1024 * 1024)} MB; "
                            "no se descargó ni procesó."
                        ),
                    ))
                    highest = max(highest, uid)  # cursor moves past it
                    continue
                fetch = await cli.uid("fetch", str(uid), "(RFC822)")
                if (fetch.result or "").upper() != "OK":
                    continue
                raw = _extract_rfc822(fetch.lines)
                if not raw:
                    continue
                msg = email.message_from_bytes(raw)
                plain, html = _extract_bodies(msg)
                msg_id = (msg.get("Message-ID") or "").strip()
                references = [
                    r.strip() for r in
                    re.findall(r"<[^>]+>", msg.get("References") or "")
                ]
                received = email.utils.parsedate_to_datetime(msg.get("Date") or "")
                if received and received.tzinfo is None:
                    received = received.replace(tzinfo=timezone.utc)
                messages.append(Message(
                    provider_message_id=msg_id or f"imap-uid-{uid}",
                    provider_thread_id=msg_id or None,  # IMAP has no thread id
                    sender=email.utils.parseaddr(msg.get("From") or "")[1] or None,
                    subject=texto_de_cabecera(msg.get("Subject")) or None,
                    body_text=plain,
                    body_html=html,
                    received_at=received,
                    headers={
                        "in_reply_to": msg_id,
                        "references": references,
                        "imap_uid": uid,
                        # Surface the inbound Auto-Submitted header so the
                        # poller's loop guard (== "auto-replied") actually
                        # fires. send_reply self-tags our outbound mail with
                        # this; without parsing it here the guard was dead code
                        # and a self-monitored mailbox could reply to itself in
                        # a loop.
                        "auto_submitted": (msg.get("Auto-Submitted") or "").strip().lower(),
                    },
                    attachments=_extract_attachments(msg),
                ))
                highest = max(highest, uid)
            await cli.logout()
            new_cursor = _next_uidvalidity_cursor(uidvalidity, highest)
            return messages, new_cursor
        except ChannelError:
            try:
                await cli.logout()
            except Exception:
                pass
            raise
        except Exception as exc:
            try:
                await cli.logout()
            except Exception:
                pass
            raise ChannelError(f"IMAP fetch failed: {exc}") from exc

    async def send_reply(self, original: Message, reply: Reply) -> str:
        cfg = self._config()
        smtp_host = cfg.get("smtp_host") or ""
        smtp_port = int(cfg.get("smtp_port") or 587)
        if not smtp_host:
            raise ChannelConfigError("SMTP host is not configured")
        sender = cfg.get("user") or self.channel.mailbox_address
        msg = EmailMessage()
        # A REPLY to an inbound email MUST keep the thread's subject ("Re: …").
        # A fresh subject from the agent forks a NEW conversation in Gmail and
        # most clients EVEN with In-Reply-To set (they also thread by subject).
        # So when this is a real reply (the original carries an In-Reply-To /
        # Message-ID), force the quoted thread subject and ignore the agent's
        # ``reply.subject``. Only honour a custom subject when STARTING a new
        # conversation (the notify path builds a synthetic original with no
        # In-Reply-To).
        is_reply = bool(original.headers.get("in_reply_to"))
        subject = (
            _quote_subject(original.subject)
            if is_reply
            else (reply.subject or _quote_subject(original.subject))
        )
        # EVERY header goes through `valor_de_cabecera`. It is not belt and braces on top
        # of sanitising the input: the subject can come from the AGENT (`reply.subject`) and
        # the recipient from a configuration, and a single line break here does not degrade
        # the reply — it prevents it entirely, with the run dead and the sender unanswered.
        msg["From"] = valor_de_cabecera(sender)
        msg["To"] = valor_de_cabecera(original.sender or "")
        msg["Subject"] = valor_de_cabecera(subject)
        msg["Message-ID"] = make_msgid(domain=sender.split("@", 1)[-1] if "@" in sender else "wai.local")
        if original.headers.get("in_reply_to"):
            msg["In-Reply-To"] = valor_de_cabecera(original.headers["in_reply_to"])
            refs = original.headers.get("references") or []
            refs = list(refs) + [original.headers["in_reply_to"]]
            msg["References"] = valor_de_cabecera(" ".join(refs))
        if reply.cc:
            msg["Cc"] = valor_de_cabecera(", ".join(reply.cc))
        if reply.bcc:
            msg["Bcc"] = valor_de_cabecera(", ".join(reply.bcc))
        # Self-tag so the watcher won't try to reprocess our own message
        # if it ever lands back in INBOX (BCC, mailing list reflection).
        msg["Auto-Submitted"] = "auto-replied"

        # Native MIME attachments. Materialise them BEFORE setting the
        # body: a failed materialisation (download error, oversize)
        # degrades to a download link appended to the body instead of
        # blocking the reply.
        from app.channels.base import resolve_attachment_bytes
        resolved_atts: list[tuple[bytes, str, str]] = []  # (data, mime, filename)
        failed_links: list[str] = []
        missing: list[str] = []
        for att in (reply.attachments or []):
            resolved = await resolve_attachment_bytes(att)
            if resolved is None:
                if att.url:
                    failed_links.append(f"- {att.filename}: {att.url}")
                else:
                    # PRE-FLIGHT: a workspace/inline attachment that can't be
                    # materialised (missing, empty, oversize, or blocked by the
                    # deliverable allowlist). Don't drop it silently — the body
                    # may promise "adjunto el informe"; tell the recipient it
                    # couldn't be attached so the gap is visible, not confusing.
                    missing.append(att.filename or "adjunto")
                continue
            data, mime = resolved
            resolved_atts.append((data, mime, att.filename))

        body = reply.body or ""
        if failed_links:
            body += "\n\nArchivos disponibles para descarga:\n" + "\n".join(failed_links)
        if missing:
            body += (
                "\n\n⚠️ No se pudo adjuntar: " + ", ".join(missing)
                + ". Si lo necesitas, respóndeme y te lo reenvío."
            )
            logger.warning(
                "imap reply: %d attachment(s) could not be materialised: %s",
                len(missing), ", ".join(missing),
            )
        # Plain-text part (Markdown as-is) is the fallback; the HTML
        # alternative renders tables/lists/emphasis in clients that
        # support it (which is all of them). This makes the message
        # multipart/alternative; add_attachment below re-wraps it in
        # multipart/mixed automatically.
        msg.set_content(body)
        from app.channels.base import render_markdown_email_html
        msg.add_alternative(render_markdown_email_html(body), subtype="html")
        for data, mime, filename in resolved_atts:
            maintype, _, subtype = (mime or "application/octet-stream").partition("/")
            msg.add_attachment(
                data,
                maintype=maintype or "application",
                subtype=subtype or "octet-stream",
                filename=filename,
            )

        from aiosmtplib import SMTP
        from aiosmtplib.errors import SMTPAuthenticationError
        async with SMTP(hostname=smtp_host, port=smtp_port,
                        start_tls=bool(cfg.get("smtp_use_tls", True))) as smtp:
            try:
                await smtp.login(cfg["user"], cfg["password"])
            except SMTPAuthenticationError as exc:
                # THIS one IS the credential: the server looked at it and rejected it.
                raise ChannelUnauthorizedError(f"SMTP LOGIN rejected: {exc}") from exc
            except Exception as exc:
                # Everything else —timeout, DNS, TLS, network cut— is transient, and
                # marking it "unauthorized" switched the channel off FOREVER over a hiccup.
                raise ChannelError(f"SMTP LOGIN no se pudo completar: {exc}") from exc
            await smtp.send_message(msg)
        return msg["Message-ID"]

    async def disconnect(self) -> None:
        # IMAP/SMTP has no upstream concept of "disconnect"; the password
        # blob is destroyed by the route layer when the channel is
        # deleted (DELETE channels). Nothing to do here.
        return None


# ─────────────────────────────────────────────────────────────────────
# IMAP response parsing helpers (kept private to this module)
# ─────────────────────────────────────────────────────────────────────


def _select_uidvalidity(select_response) -> int:
    """Parse the UIDVALIDITY out of the IMAP SELECT response."""
    for line in (select_response.lines or []):
        if isinstance(line, (bytes, bytearray)) and b"UIDVALIDITY" in line:
            m = re.search(rb"UIDVALIDITY (\d+)", line)
            if m:
                try:
                    return int(m.group(1))
                except Exception:
                    pass
        elif isinstance(line, str) and "UIDVALIDITY" in line:
            m = re.search(r"UIDVALIDITY (\d+)", line)
            if m:
                try:
                    return int(m.group(1))
                except Exception:
                    pass
    return 0


def _extract_rfc822(lines) -> bytes:
    """The FETCH response is a list of either tuples or bytes; pull the
    raw RFC822 blob out of it."""
    if not lines:
        return b""
    for item in lines:
        if isinstance(item, (bytes, bytearray)) and item.startswith(b"\r\n"):
            continue
        if isinstance(item, (bytes, bytearray)) and len(item) > 64:
            return bytes(item)
    # Fall back: glue everything together
    blob = b""
    for item in lines:
        if isinstance(item, (bytes, bytearray)):
            blob += bytes(item) + b"\r\n"
    return blob


def _quote_subject(subject: Optional[str]) -> str:
    s = (subject or "").strip()
    if not s:
        return "Re:"
    if re.match(r"^re\s*:", s, re.I):
        return s
    return f"Re: {s}"
