"""Channel adapter contract + dispatcher.

Every channel ``kind`` (``email``, ``whatsapp``, ``slack``) provides an
*auth backend* (``imap_smtp``, ``twilio``, ``bot_token``). They all
implement the same small protocol so the watcher and the executor are
auth-method-agnostic. (The Composio/OAuth email adapter ``oauth_composio``
was retired — email is IMAP/SMTP only; see ``get_adapter`` below.)

    backend = get_adapter(channel)
    await backend.test_connection()
    msgs, cursor = await backend.fetch_new(channel.cursor)
    provider_id = await backend.send_reply(original, reply)
    await backend.disconnect()

Adding a new channel kind is one new module that subclasses
``ChannelAdapter``. The watcher / executor never branch on ``kind``.
"""

from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Protocol, runtime_checkable


def texto_de_cabecera(crudo: str | None) -> str:
    """The REAL text of an email header: decoded and on ONE single line.

    Two things a real email's header carries that the code above does not
    expect:

    · **It comes FOLDED.** RFC 5322 splits a long header over several lines (CRLF + a
      space). `msg.get("Subject")` returns that value with the breaks inside, and `.strip()`
      only removes the ones at the ends. When building the reply, `msg["Subject"] = …`
      rejects it with `ValueError: Header values may not contain linefeed…`, the run dies as
      "Unrecoverable agent error" and **whoever wrote gets no reply**. Measured on
      18/08/2026 in the Odoo employee: a subject longer than 78 characters was enough.

    · **It comes ENCODED** if it has accents (`=?UTF-8?Q?Facturaci=C3=B3n?=`). Without
      decoding, the "Re: …" comes out with the encoding visible.

    It never raises: a broken header degrades to plain text without breaks. What must not
    happen is an inbound email preventing a reply.
    """
    if not crudo:
        return ""
    texto = crudo
    if "=?" in texto:
        try:
            from email.header import decode_header, make_header
            texto = str(make_header(decode_header(texto)))
        except Exception:  # noqa: BLE001 — malformed header: used as is
            pass
    # Unfold: the break and the spaces that follow it are ONE separator, not content.
    return re.sub(r"\s+", " ", texto.replace("\r", " ").replace("\n", " ")).strip()


def valor_de_cabecera(valor: str | None) -> str:
    """What can be written into an outbound header, without exceptions.

    Last barrier before `msg[...] = …`: it does not matter whether the text comes from the
    inbound email, the agent or the configuration — if it has a line break, the library
    blows up and the whole reply is lost. Here it is flattened and we carry on.
    It also stops a header injection through the subject (`Subject\r\nBcc: …`).
    """
    return re.sub(r"\s+", " ", (valor or "").replace("\r", " ").replace("\n", " ")).strip()


def channel_process_since(channel) -> Optional[datetime]:
    """Return the channel's ``process_since`` floor as a tz-aware UTC
    datetime, or ``None`` when the channel processes all history.

    Adapters use this on first connect (empty cursor) to narrow the
    provider query — IMAP ``SINCE``, Gmail ``after:``, Twilio
    ``DateSent`` — so they don't page through the whole mailbox. The
    poller enforces the same floor as a hard safety net on every
    message, so an adapter that ignores this still behaves correctly
    (just less efficiently on the first poll).
    """
    raw = getattr(channel, "process_since", None)
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if raw.tzinfo is None:
        raw = raw.replace(tzinfo=timezone.utc)
    return raw.astimezone(timezone.utc)


# ─────────────────────────────────────────────────────────────────────
# Markdown → email-safe HTML (v6.31.0)
# ─────────────────────────────────────────────────────────────────────
#
# Agents draft replies in Markdown (headings, **bold**, bullet/numbered
# lists and — crucially — pipe tables). Email clients (Gmail, Outlook…)
# render NONE of that: a Markdown table arrives as a wall of ``|`` pipes.
# This converts the agent's Markdown into HTML with **inline** CSS only
# (Gmail strips <style>/<head>), so tables, lists and emphasis lay out
# properly. It is intentionally dependency-free (no `markdown`/`bleach`
# package to add to the image) and covers the subset agents actually
# produce. EMAIL adapters call this; WhatsApp stays plain text.

_MD_HEADING = re.compile(r"(#{1,6})\s+(.*)")
_MD_BULLET = re.compile(r"\s*[-*+]\s+")
_MD_NUMBER = re.compile(r"\s*\d+[.)]\s+")
_MD_TABLE_SEP = re.compile(r":?-{1,}:?")
_MD_BLOCK_START = re.compile(r"(#{1,6})\s+|\s*[-*+]\s+|\s*\d+[.)]\s+")


def _md_inline(s: str) -> str:
    """Inline Markdown → HTML on already-block-split text."""
    s = _html.escape(s, quote=False)
    s = re.sub(
        r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
        r'<a href="\2" style="color:#1a73e8;text-decoration:underline">\1</a>',
        s,
    )
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"__([^_]+)__", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", s)
    s = re.sub(
        r"`([^`]+)`",
        r'<code style="background:#f0f0f0;padding:1px 4px;'
        r'border-radius:3px;font-family:monospace;font-size:90%">\1</code>',
        s,
    )
    return s


def _md_is_table_sep(line: str) -> bool:
    st = line.strip()
    if "|" not in st:
        return False
    cells = [c.strip() for c in st.strip("|").split("|")]
    return bool(cells) and all(_MD_TABLE_SEP.fullmatch(c or "") for c in cells)


def _md_aligns(sep_line: str) -> list[str]:
    out: list[str] = []
    for a in (c.strip() for c in sep_line.strip().strip("|").split("|")):
        left, right = a.startswith(":"), a.endswith(":")
        out.append("center" if left and right else "right" if right else "left")
    return out


def render_markdown_email_html(md: str) -> str:
    """Render the agent's Markdown reply to a self-contained HTML fragment
    with inline styles, safe to drop into a Gmail/Composio HTML body or an
    SMTP ``text/html`` alternative. Returns a single wrapping ``<div>``."""
    text = (md or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    n = len(lines)
    blocks: list[str] = []
    i = 0
    while i < n:
        st = lines[i].strip()
        if not st:
            i += 1
            continue
        # Table (current row has pipes and the NEXT row is a separator).
        if "|" in st and i + 1 < n and _md_is_table_sep(lines[i + 1]):
            header = [c.strip() for c in st.strip("|").split("|")]
            aligns = _md_aligns(lines[i + 1])
            i += 2
            rows: list[list[str]] = []
            while i < n and lines[i].strip() and "|" in lines[i]:
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1

            def _al(j: int) -> str:
                return aligns[j] if j < len(aligns) else "left"

            ths = "".join(
                f'<th style="background:#1f4e5f;color:#ffffff;text-align:{_al(j)};'
                f'padding:8px 10px;border:1px solid #cfd8dc;font-weight:bold">'
                f"{_md_inline(h)}</th>"
                for j, h in enumerate(header)
            )
            trs = []
            for r_idx, row in enumerate(rows):
                bg = "#f6f8fa" if r_idx % 2 else "#ffffff"
                tds = "".join(
                    f'<td style="text-align:{_al(j)};padding:8px 10px;'
                    f'border:1px solid #e0e0e0;background:{bg}">'
                    f"{_md_inline(row[j] if j < len(row) else '')}</td>"
                    for j in range(len(header))
                )
                trs.append(f"<tr>{tds}</tr>")
            blocks.append(
                '<table cellspacing="0" cellpadding="0" role="presentation" '
                'style="border-collapse:collapse;width:100%;margin:12px 0;'
                'font-size:13px">'
                f"<thead><tr>{ths}</tr></thead>"
                f"<tbody>{''.join(trs)}</tbody></table>"
            )
            continue
        # Heading.
        m = _MD_HEADING.match(st)
        if m:
            lvl = len(m.group(1))
            size = {1: "20px", 2: "18px", 3: "16px", 4: "15px", 5: "14px", 6: "13px"}.get(lvl, "14px")
            blocks.append(
                f'<h{lvl} style="margin:16px 0 8px;font-size:{size};'
                f'font-weight:bold;color:#111111">{_md_inline(m.group(2))}</h{lvl}>'
            )
            i += 1
            continue
        # Bullet list.
        if _MD_BULLET.match(lines[i]) and lines[i].lstrip().startswith(("-", "*", "+")):
            items = []
            while i < n and _MD_BULLET.match(lines[i]) and lines[i].lstrip().startswith(("-", "*", "+")):
                items.append(re.sub(r"^\s*[-*+]\s+", "", lines[i]).strip())
                i += 1
            lis = "".join(f'<li style="margin:2px 0">{_md_inline(x)}</li>' for x in items)
            blocks.append(f'<ul style="margin:8px 0;padding-left:22px">{lis}</ul>')
            continue
        # Numbered list.
        if _MD_NUMBER.match(lines[i]):
            items = []
            while i < n and _MD_NUMBER.match(lines[i]):
                items.append(re.sub(r"^\s*\d+[.)]\s+", "", lines[i]).strip())
                i += 1
            lis = "".join(f'<li style="margin:2px 0">{_md_inline(x)}</li>' for x in items)
            blocks.append(f'<ol style="margin:8px 0;padding-left:22px">{lis}</ol>')
            continue
        # Paragraph (gather consecutive non-blank, non-block lines).
        para = [st]
        i += 1
        while i < n and lines[i].strip() and not _MD_BLOCK_START.match(lines[i]) and not (
            "|" in lines[i] and i + 1 < n and _md_is_table_sep(lines[i + 1])
        ):
            para.append(lines[i].strip())
            i += 1
        blocks.append(
            '<p style="margin:8px 0">'
            + "<br>".join(_md_inline(p) for p in para)
            + "</p>"
        )
    return (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;'
        'color:#1a1a1a;line-height:1.5;max-width:760px">'
        + "\n".join(blocks)
        + "</div>"
    )


# ─────────────────────────────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────────────────────────────


class ChannelError(RuntimeError):
    """Generic channel failure. The watcher records ``errors_count++``."""


class ChannelUnauthorizedError(ChannelError):
    """Provider rejected the credentials. The watcher flips ``status``
    to ``unauthorized`` and stops polling until the operator reauths."""


class ChannelConfigError(ChannelError):
    """The channel configuration is unusable (missing IMAP host, no
    Composio user_id, …). The watcher records ``status='error'`` with
    the diagnostic on ``status_reason`` and skips polling."""


def format_channel_exception(exc: BaseException) -> str:
    """Human-readable error text for audit rows and the channel SPA.

    Several httpx/network exceptions expose an empty ``str()`` — without
    this helper the UI shows ``demo@x.com: `` with no explanation."""
    msg = str(exc).strip()
    if msg:
        return msg
    name = type(exc).__name__
    if name == "ReadTimeout":
        return (
            "Tiempo de espera agotado esperando respuesta del proveedor "
            "(auth-service / Composio / Gmail). Frecuente al enviar "
            "adjuntos; reintenta o reduce el tamaño del fichero."
        )
    if name in ("ConnectTimeout", "WriteTimeout", "PoolTimeout"):
        return f"Tiempo de espera agotado ({name}) al contactar el proveedor."
    return name or "Error desconocido"


# ─────────────────────────────────────────────────────────────────────
# Normalised payloads (kind-agnostic)
# ─────────────────────────────────────────────────────────────────────


@dataclass
class InboundAttachment:
    """One attachment that arrived ON an inbound message.

    The adapter fills exactly ONE source so the poller can materialise
    the bytes into the workspace:

    * ``data`` — raw bytes already in hand (IMAP reads the whole RFC822
      so the payload is decoded inline);
    * ``provider_ref`` — an opaque descriptor for DEFERRED download
      (Gmail/Composio: ``{"message_id", "attachment_id"}``) resolved by
      ``ChannelAdapter.download_attachment``.

    ``size`` is best-effort (provider-reported or ``len(data)``); the
    poller enforces the real caps when it writes the file.
    """
    filename: str
    mime_type: Optional[str] = None
    size: Optional[int] = None
    data: Optional[bytes] = None
    provider_ref: Optional[dict[str, Any]] = None


@dataclass
class Message:
    """One inbound message in normalised form."""
    provider_message_id: str
    provider_thread_id: Optional[str] = None
    sender: Optional[str] = None
    subject: Optional[str] = None
    body_text: str = ""
    body_html: Optional[str] = None
    received_at: Optional[datetime] = None
    # Provider-specific headers the adapter needs back on send_reply.
    # For email: { "in_reply_to": <Message-Id>, "references": [..] }
    headers: dict[str, Any] = field(default_factory=dict)
    # Attachments that arrived on this message (email only for now).
    attachments: list["InboundAttachment"] = field(default_factory=list)
    # When set, this is a SENTINEL for a message the adapter deliberately did
    # NOT load (e.g. too large to fetch safely). The poller persists a visible
    # row with status='skipped' and this reason instead of letting the message
    # vanish — and never spawns an event for it.
    skip_reason: Optional[str] = None


@dataclass
class Attachment:
    """One outbound attachment. The agent provides ONE of:

    * ``workspace_path`` — path relative to the agent's ``/workspace``
      (preferred: the agent exports files to ``/workspace/exports/``
      and the app reads the bytes from its read-only mount, see
      ``AGENT_WORKSPACE_DIR``);
    * ``url`` — a download URL (e.g. a document generated in WAI);
    * ``content_base64`` — inline content for small generated files.
    """
    filename: str
    url: Optional[str] = None
    content_base64: Optional[str] = None
    mime_type: Optional[str] = None
    workspace_path: Optional[str] = None


@dataclass
class Reply:
    """The outbound payload built from ``STRUCTURED_RESPONSE.reply``."""
    body: str            # markdown that the adapter converts (plain / HTML)
    subject: Optional[str] = None
    cc: list[str] = field(default_factory=list)
    bcc: list[str] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)


# Caps shared by every adapter when materialising attachments.
MAX_ATTACHMENTS = 5
MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024  # 15 MB per file
ATTACHMENT_DOWNLOAD_TIMEOUT = 30.0       # seconds

# Caps applied to INBOUND attachments the app stages into the workspace.
MAX_INBOUND_ATTACHMENTS = 10             # per message
MAX_INBOUND_ATTACHMENT_BYTES = 15 * 1024 * 1024   # 15 MB per file
MAX_INBOUND_ATTACHMENTS_TOTAL_BYTES = 40 * 1024 * 1024  # per message


def _workspace_file(path: str) -> Optional[str]:
    """Resolve ``workspace_path`` against the read-only agent-workspace
    mount. Returns the absolute path or ``None`` when the feature is
    off (no mount), the path escapes the workspace, or the file does
    not exist. Confinement is enforced with ``realpath`` so symlinks
    cannot escape either.
    """
    import os
    root = (os.environ.get("AGENT_WORKSPACE_DIR") or "").strip()
    if not root or not os.path.isdir(root):
        return None
    rel = (path or "").strip().lstrip("/")
    if not rel or ".." in rel.split("/"):
        return None
    real_root = os.path.realpath(root)
    candidate = os.path.realpath(os.path.join(real_root, rel))
    if candidate != real_root and not candidate.startswith(real_root + os.sep):
        return None
    if not os.path.isfile(candidate):
        return None
    return candidate


def _sanitize_filename(name: str) -> str:
    """Strip any path components and unsafe characters from a provider
    filename so it can be written safely under the inbox folder."""
    import os
    import re as _re
    base = os.path.basename((name or "").strip().replace("\\", "/"))
    base = base.lstrip(".") or "attachment"
    base = _re.sub(r"[^A-Za-z0-9._ -]", "_", base)
    return base[:180] or "attachment"


def workspace_write_inbound(
    dest_dir: str, filename: str, data: bytes,
) -> Optional[str]:
    """Write an inbound attachment under the shared workspace mount.

    ``dest_dir`` is relative to ``/workspace`` (e.g.
    ``sessions/channels/3/john-…/inbox``). Returns the workspace-relative
    path actually written (collisions get a ``-1``/``-2`` suffix) or
    ``None`` when the feature is off (no writable mount), the path
    escapes the workspace, or the write fails. Confinement mirrors
    ``_workspace_file`` with ``realpath`` so symlinks cannot escape.
    """
    import os
    root = (os.environ.get("AGENT_WORKSPACE_DIR") or "").strip()
    if not root or not os.path.isdir(root):
        return None
    rel_dir = (dest_dir or "").strip().lstrip("/")
    if not rel_dir or ".." in rel_dir.split("/"):
        return None
    real_root = os.path.realpath(root)
    target_dir = os.path.realpath(os.path.join(real_root, rel_dir))
    if target_dir != real_root and not target_dir.startswith(real_root + os.sep):
        return None
    if not data:
        return None

    safe = _sanitize_filename(filename)
    stem, ext = os.path.splitext(safe)
    try:
        os.makedirs(target_dir, exist_ok=True)
        candidate = os.path.join(target_dir, safe)
        i = 1
        while os.path.exists(candidate):
            candidate = os.path.join(target_dir, f"{stem}-{i}{ext}")
            i += 1
        # Re-check confinement on the final resolved path.
        final = os.path.realpath(candidate)
        if not final.startswith(real_root + os.sep):
            return None
        with open(final, "wb") as fh:
            fh.write(data)
    except OSError:
        return None
    return f"{rel_dir}/{os.path.basename(candidate)}"


async def resolve_attachment_bytes(att: Attachment) -> Optional[tuple[bytes, str]]:
    """Materialise an attachment into ``(data, mime_type)``.

    Reads ``workspace_path`` from the shared agent-workspace mount,
    downloads ``url`` (size-capped) or decodes ``content_base64``.
    Returns ``None`` when the attachment cannot be materialised — the
    caller decides whether to degrade (skip / link in body) so one bad
    attachment never blocks the reply itself.
    """
    import base64
    import mimetypes

    mime = att.mime_type or mimetypes.guess_type(att.filename or "")[0] \
        or "application/octet-stream"

    if att.workspace_path:
        import os
        path = _workspace_file(att.workspace_path)
        if not path:
            return None
        try:
            if os.path.getsize(path) > MAX_ATTACHMENT_BYTES:
                return None
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            return None
        if not data:
            return None
        return data, (
            att.mime_type
            or mimetypes.guess_type(path)[0]
            or mime
        )

    if att.content_base64:
        try:
            data = base64.b64decode(att.content_base64, validate=True)
        except Exception:
            return None
        if not data or len(data) > MAX_ATTACHMENT_BYTES:
            return None
        return data, mime

    # v7.88.0 — URL attachments are PLAIN LINKS the recipient fetches (see
    # deliverables.sanitize_attachment_dict). We deliberately do NOT fetch them
    # server-side: the URL is agent-controlled, so a prompt-injected reply could
    # point at an internal service / loopback / cloud-metadata
    # (169.254.169.254) and — following redirects — exfiltrate the response to the
    # channel recipient (SSRF). The channel adapters already degrade a URL
    # attachment to a visible download link, so nothing is lost. If a server-side
    # fetch is ever truly needed, gate it on a strict public-IP allowlist with
    # redirects disabled and per-hop re-validation — never a bare client.get.
    return None


@dataclass
class TestResult:
    ok: bool
    message: str = ""
    # Diagnostics that the SPA shows to the operator (host probed, OAuth
    # account email returned by the provider, …).
    detail: dict[str, Any] = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────
# Adapter protocol
# ─────────────────────────────────────────────────────────────────────


@runtime_checkable
class ChannelAdapter(Protocol):
    """Contract every concrete adapter MUST implement."""

    kind: str         # 'email' | 'whatsapp' | 'slack' (outlook/teams later)
    auth_method: str  # 'imap_smtp' | 'twilio' | 'bot_token'

    async def test_connection(self) -> TestResult:
        """Quick sanity check used by the wizard. Must not raise on
        bad credentials — returns ``TestResult(ok=False, message=...)``
        instead so the SPA can render the error inline."""

    async def fetch_new(
        self, cursor: Optional[str],
    ) -> tuple[list[Message], Optional[str]]:
        """Return ``(messages, new_cursor)``. ``new_cursor`` MUST be
        idempotent — calling ``fetch_new(new_cursor)`` immediately after
        MUST return an empty list. Raise ``ChannelUnauthorizedError``
        when the provider rejects the credentials."""

    async def send_reply(self, original: Message, reply: Reply) -> str:
        """Deliver ``reply`` as an answer to ``original``. Returns the
        provider-assigned id of the sent message (for audit / threading
        of subsequent replies)."""

    async def download_attachment(
        self, provider_ref: dict[str, Any],
    ) -> Optional[bytes]:
        """Resolve a DEFERRED inbound attachment into raw bytes.

        Called by the poller for attachments the adapter returned with a
        ``provider_ref`` instead of inline ``data`` (e.g. Gmail). Adapters
        whose ``fetch_new`` already returns inline bytes (IMAP) don't need
        to override this. Returns ``None`` on any failure so one bad
        attachment never blocks message ingestion."""
        return None

    async def disconnect(self) -> None:
        """Clean up the upstream credential (e.g. revoke the Composio
        connected_account). Safe to call multiple times."""


# ─────────────────────────────────────────────────────────────────────
# Dispatcher
# ─────────────────────────────────────────────────────────────────────


def get_adapter(channel) -> ChannelAdapter:
    """Resolve the adapter for a ``Channel`` row.

    The actual adapters are imported lazily so a deploy that doesn't
    use IMAP doesn't pay the ``cryptography`` import at module load
    time. ``ChannelConfigError`` is raised when the combination of
    ``kind`` + ``auth_method`` is not supported.
    """
    kind = (channel.kind or "").strip().lower()
    auth = (channel.auth_method or "").strip().lower()

    if kind == "email":
        # Composio is NOT supported as an email channel (only as MCP tooling).
        # The email channel is always IMAP/SMTP.
        if auth == "imap_smtp":
            from app.channels.imap_smtp import ImapSmtpEmailAdapter
            return ImapSmtpEmailAdapter(channel)

    if kind == "whatsapp":
        if auth == "twilio":
            from app.channels.whatsapp_twilio import WhatsAppTwilioAdapter
            return WhatsAppTwilioAdapter(channel)

    if kind == "slack":
        if auth == "bot_token":
            from app.channels.slack_bot import SlackBotAdapter
            return SlackBotAdapter(channel)

    if kind == "telegram":
        if auth == "telegram_bot":
            from app.channels.telegram_bot import TelegramBotAdapter
            return TelegramBotAdapter(channel)

    raise ChannelConfigError(
        f"No adapter for kind={kind!r} auth_method={auth!r}. "
        "Supported today: email + imap_smtp, whatsapp + twilio, "
        "slack + bot_token, telegram + telegram_bot."
    )
