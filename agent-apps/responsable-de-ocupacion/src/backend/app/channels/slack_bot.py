"""Slack channel adapter backed by the Slack Web API (bot token).

Why a bot token + polling (and not the Events API or Socket Mode): the
channels engine is deliberately *pull-based* — the app pod polls, nothing
on the public internet pushes into the cluster (see
``whatsapp_twilio.py`` and ``patterns/external-channels.md``). Slack
exposes inbound messages through ``conversations.history`` /
``conversations.replies``, so the same poller / executor / routes that
drive email and WhatsApp work unchanged. The Events API would require a
public receiver and Socket Mode a persistent websocket — both out of
scope for this skill.

Rate limits: the May-2025 throttling of ``conversations.history`` to
1 req/min applies only to apps *commercially distributed outside the
Slack Marketplace*. A per-customer **internal** app (created inside the
customer's own workspace, not distributed) keeps the standard Tier 3
limits (50+/min), so a 30–60 s poll is perfectly responsive. Document
this in the wizard so operators create an internal app.

Credentials (``bot_token`` = ``xoxb-…`` plus the watched ``channel_id``)
are stored encrypted (Fernet) in ``channels.imap_config_encrypted`` — the
column is a generic credential blob despite its historical name; only the
adapter interprets the payload. ``channels.mailbox_address`` holds the
Slack channel id being watched (e.g. ``C0123ABCD``). The bot must be a
member of that channel (``/invite @bot``).

Scopes the operator's bot token needs:
  * ``channels:history`` + ``channels:read`` (public channels) and/or
    ``groups:history`` + ``groups:read`` (private channels),
  * ``chat:write`` (post replies),
  * ``files:read`` (download inbound attachments),
  * ``files:write`` (upload outbound attachments),
  * ``users:read`` (optional, nicer sender labels),
  * ``im:history`` + ``im:write`` (optional, DM notifications).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from app.channels.base import (
    ChannelAdapter, ChannelConfigError, ChannelError,
    ChannelUnauthorizedError, InboundAttachment, MAX_INBOUND_ATTACHMENTS,
    MAX_INBOUND_ATTACHMENT_BYTES, Message, Reply, TestResult,
    channel_process_since, resolve_attachment_bytes,
)
from app.channels.encryption import decrypt_blob

logger = logging.getLogger(__name__)

SLACK_API = "https://slack.com/api"

# Hard ceilings per poll tick so a busy channel can't stall the poller
# or OOM the pod.
MAX_FETCH_MESSAGES = 200
MAX_THREADS_PER_TICK = 20
# Slack hard-caps a message body at 40000 chars; stay safely under.
MAX_TEXT_CHARS = 39000

# Auth errors that mean "the token is dead" → flip the channel to
# ``unauthorized`` and stop polling until the operator rotates it.
_UNAUTHORIZED_ERRORS = frozenset({
    "invalid_auth", "not_authed", "token_revoked", "token_expired",
    "account_inactive", "no_permission", "missing_scope",
    "not_in_channel", "channel_not_found",
})

# Slack ``subtype`` values that are system/noise events, not human
# messages we should react to. ``None`` (a plain message) and
# ``file_share`` / ``thread_broadcast`` / ``me_message`` are allowed.
_SKIP_SUBTYPES = frozenset({
    "channel_join", "channel_leave", "channel_topic", "channel_purpose",
    "channel_name", "channel_archive", "channel_unarchive", "bot_add",
    "bot_remove", "bot_message", "pinned_item", "unpinned_item",
    "reminder_add", "tombstone", "group_join", "group_leave",
})


def _ts_to_dt(ts: Optional[str]) -> Optional[datetime]:
    """Slack ``ts`` (``"1718713200.123456"``) → tz-aware UTC datetime."""
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc)
    except (TypeError, ValueError):
        return None


# ─────────────────────────────────────────────────────────────────────
# Markdown → Slack mrkdwn
# ─────────────────────────────────────────────────────────────────────
#
# Slack does NOT render standard Markdown: ``**bold**`` shows the
# asterisks, ``[text](url)`` shows literally and pipe tables arrive as a
# wall of ``|``. This converts the agent's Markdown into Slack mrkdwn
# (``*bold*``, ``_italic_``, ``• `` bullets, ``<url|text>`` links) and
# renders tables as a fixed-width code block. Dependency-free, covering
# the subset agents actually produce. Code spans/fences are protected so
# their contents are never rewritten.

_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)+\|?\s*$")


def _is_table_sep(line: str) -> bool:
    return bool(_TABLE_SEP_RE.match(line)) and "|" in line


def _mrkdwn_inline(s: str) -> str:
    """Inline Markdown → mrkdwn on a single (code-free) text fragment."""
    # Links [text](url) → <url|text> BEFORE emphasis so the URL is safe.
    s = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r"<\2|\1>", s)
    # Bold (**x** / __x__) → *x*. Use a NUL placeholder so the italic pass
    # below doesn't see the single asterisks we just produced. A callable
    # replacement avoids re's template parser (which rejects "\x00").
    _bold = lambda m: "\x00" + m.group(1) + "\x00"
    s = re.sub(r"\*\*([^*]+)\*\*", _bold, s)
    s = re.sub(r"__([^_]+)__", _bold, s)
    # Italic (*x* / _x_) → _x_.
    s = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"_\1_", s)
    s = s.replace("\x00", "*")
    return s


def _slack_table(header: list[str], rows: list[list[str]]) -> str:
    cols = len(header)
    widths = [len(h) for h in header]
    for r in rows:
        for j in range(cols):
            cell = r[j] if j < len(r) else ""
            widths[j] = max(widths[j], len(cell))

    def fmt(cells: list[str]) -> str:
        return " | ".join(
            (cells[j] if j < len(cells) else "").ljust(widths[j])
            for j in range(cols)
        )

    sep = "-+-".join("-" * w for w in widths)
    body = "\n".join(fmt(r) for r in rows)
    return "```\n" + fmt(header) + "\n" + sep + ("\n" + body if body else "") + "\n```"


def render_markdown_slack(md: str) -> str:
    """Render the agent's Markdown reply to Slack mrkdwn text."""
    text = (md or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    n = len(lines)
    out: list[str] = []
    i = 0
    in_fence = False
    while i < n:
        line = lines[i]
        stripped = line.strip()
        # Fenced code block — pass through untouched.
        if stripped.startswith("```"):
            in_fence = not in_fence
            out.append(line)
            i += 1
            continue
        if in_fence:
            out.append(line)
            i += 1
            continue
        # Table (current row has pipes and next row is a separator).
        if "|" in stripped and i + 1 < n and _is_table_sep(lines[i + 1]):
            header = [c.strip() for c in stripped.strip("|").split("|")]
            i += 2
            rows: list[list[str]] = []
            while i < n and lines[i].strip() and "|" in lines[i]:
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            out.append(_slack_table(
                [_mrkdwn_inline(h) for h in header],
                [[_mrkdwn_inline(c) for c in r] for r in rows],
            ))
            continue
        # Heading → bold line.
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            out.append("*" + _mrkdwn_inline(m.group(2).strip()) + "*")
            i += 1
            continue
        # Bullet list item.
        mb = re.match(r"^(\s*)[-*+]\s+(.*)$", line)
        if mb:
            indent = mb.group(1)
            out.append(f"{indent}• " + _mrkdwn_inline(mb.group(2).strip()))
            i += 1
            continue
        # Numbered list item — keep the marker, convert the text.
        mn = re.match(r"^(\s*\d+[.)])\s+(.*)$", line)
        if mn:
            out.append(f"{mn.group(1)} " + _mrkdwn_inline(mn.group(2).strip()))
            i += 1
            continue
        # Plain line.
        out.append(_mrkdwn_inline(line))
        i += 1
    rendered = "\n".join(out).strip()
    if len(rendered) > MAX_TEXT_CHARS:
        rendered = rendered[: MAX_TEXT_CHARS - 1] + "…"
    return rendered


class SlackBotAdapter(ChannelAdapter):
    """Concrete adapter for ``kind='slack' auth_method='bot_token'``."""

    kind = "slack"
    auth_method = "bot_token"

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
                "Slack channel has no stored credentials. Run the wizard "
                "again with the bot token (xoxb-…) and the channel id."
            )
        try:
            cfg = decrypt_blob(token)
        except Exception as exc:
            raise ChannelConfigError(str(exc)) from exc
        if not cfg.get("bot_token"):
            raise ChannelConfigError("Slack credentials are missing the bot_token")
        if not cfg.get("channel_id"):
            cfg["channel_id"] = (self.channel.mailbox_address or "").strip()
        if not cfg.get("channel_id"):
            raise ChannelConfigError("Slack channel has no channel_id configured")
        self._cfg = cfg
        return cfg

    async def _api(
        self, method: str,
        data: Optional[dict[str, Any]] = None,
        http: str = "POST",
    ) -> dict[str, Any]:
        """One authenticated Slack Web API call. Maps Slack's ``ok=false``
        error codes onto the channel exception taxonomy so the poller can
        flip ``status`` correctly."""
        import httpx
        cfg = self._config()
        url = f"{SLACK_API}/{method}"
        headers = {"Authorization": f"Bearer {cfg['bot_token']}"}
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                if http == "GET":
                    resp = await client.get(url, params=data or {}, headers=headers)
                else:
                    resp = await client.post(url, data=data or {}, headers=headers)
        except httpx.HTTPError as exc:
            raise ChannelError(f"Slack request failed: {exc}") from exc
        if resp.status_code == 429:
            retry = resp.headers.get("Retry-After") or "?"
            raise ChannelError(f"Slack rate limited (retry after {retry}s)")
        if resp.status_code in (401, 403):
            raise ChannelUnauthorizedError(
                f"Slack rejected the credentials (HTTP {resp.status_code}). "
                "Rotate the bot token from the channel detail page."
            )
        if resp.status_code >= 400:
            raise ChannelError(f"Slack HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            payload = resp.json()
        except Exception as exc:
            raise ChannelError(f"Slack returned non-JSON payload: {exc}") from exc
        if not payload.get("ok"):
            err = (payload.get("error") or "unknown_error").strip()
            if err in _UNAUTHORIZED_ERRORS:
                raise ChannelUnauthorizedError(
                    f"Slack error {err!r}: the bot token is invalid, lacks a "
                    "scope, or the bot is not in the channel. Reinstall the "
                    "app / rotate the token / invite the bot with /invite."
                )
            raise ChannelError(f"Slack error: {err}")
        return payload

    # ── ChannelAdapter contract ───────────────────────────────────────
    async def test_connection(self) -> TestResult:
        try:
            cfg = self._config()
        except ChannelConfigError as exc:
            return TestResult(ok=False, message=str(exc))
        try:
            auth = await self._api("auth.test")
            info = await self._api(
                "conversations.info", {"channel": cfg["channel_id"]}, http="GET",
            )
        except ChannelUnauthorizedError as exc:
            return TestResult(ok=False, message=str(exc))
        except ChannelError as exc:
            return TestResult(ok=False, message=str(exc))
        chan = info.get("channel") or {}
        if not chan.get("is_member"):
            return TestResult(
                ok=False,
                message=(
                    f"The bot is not a member of #{chan.get('name') or cfg['channel_id']}. "
                    "Invite it with `/invite @<bot>` and test again."
                ),
            )
        # Cache identity so fetch_new can drop the bot's own messages.
        self._cfg = dict(cfg, bot_user_id=auth.get("user_id") or "",
                         team_id=auth.get("team_id") or "")
        return TestResult(
            ok=True, message="Slack connection OK",
            detail={
                "team": auth.get("team"),
                "bot_user": auth.get("user"),
                "channel": chan.get("name"),
                "channel_id": cfg["channel_id"],
            },
        )

    def _message_from_slack(
        self, row: dict[str, Any], channel_id: str, bot_user_id: str,
    ) -> Optional[Message]:
        """Normalise a Slack message dict, or ``None`` to skip it (system
        event, our own bot message, empty)."""
        if row.get("bot_id"):
            return None  # any bot's message, including our own replies
        subtype = row.get("subtype")
        if subtype and subtype not in ("file_share", "thread_broadcast", "me_message"):
            return None
        user = row.get("user") or ""
        if bot_user_id and user == bot_user_id:
            return None
        ts = row.get("ts") or ""
        if not ts:
            return None
        thread_ts = row.get("thread_ts") or ts
        attachments: list[InboundAttachment] = []
        for f in (row.get("files") or [])[:MAX_INBOUND_ATTACHMENTS]:
            if not isinstance(f, dict):
                continue
            ref = f.get("url_private_download") or f.get("url_private")
            if not ref:
                continue
            attachments.append(InboundAttachment(
                filename=f.get("name") or f.get("title") or f.get("id") or "attachment",
                mime_type=f.get("mimetype"),
                size=f.get("size"),
                provider_ref={"url": ref, "id": f.get("id")},
            ))
        return Message(
            provider_message_id=ts,
            provider_thread_id=thread_ts,
            sender=user or None,
            subject=None,
            body_text=row.get("text") or "",
            body_html=None,
            received_at=_ts_to_dt(ts),
            headers={
                "slack_channel": channel_id,
                "thread_ts": thread_ts,
                "is_thread_reply": bool(row.get("thread_ts")
                                        and row.get("thread_ts") != ts),
            },
            attachments=attachments,
        )

    async def fetch_new(
        self, cursor: Optional[str],
    ) -> tuple[list[Message], Optional[str]]:
        cfg = self._config()
        channel_id = cfg["channel_id"]
        bot_user_id = cfg.get("bot_user_id") or ""
        if not bot_user_id:
            # Resolve our identity once so we can drop our own replies.
            try:
                auth = await self._api("auth.test")
                bot_user_id = auth.get("user_id") or ""
                self._cfg = dict(cfg, bot_user_id=bot_user_id)
            except ChannelError:
                bot_user_id = ""

        # ``oldest`` is the timestamp floor (exclusive when inclusive=0).
        # First poll (no cursor): honour the operator's "process since"
        # floor, else only look at the most recent history page.
        oldest = (cursor or "").strip()
        if not oldest:
            since = channel_process_since(self.channel)
            if since is not None:
                oldest = f"{since.timestamp():.6f}"

        rows: list[dict[str, Any]] = []
        page_cursor = ""
        while len(rows) < MAX_FETCH_MESSAGES:
            params: dict[str, Any] = {
                "channel": channel_id,
                "limit": 100,
                "inclusive": "false",
            }
            if oldest:
                params["oldest"] = oldest
            if page_cursor:
                params["cursor"] = page_cursor
            payload = await self._api("conversations.history", params, http="GET")
            rows.extend(payload.get("messages") or [])
            page_cursor = ((payload.get("response_metadata") or {})
                           .get("next_cursor") or "")
            if not payload.get("has_more") or not page_cursor:
                break

        messages: list[Message] = []
        thread_ts_seen: set[str] = set()
        for row in rows:
            msg = self._message_from_slack(row, channel_id, bot_user_id)
            if msg is None:
                continue
            messages.append(msg)
            # Track threads with activity so we can pull in-thread replies
            # that conversations.history does not return on its own.
            if row.get("reply_count") or row.get("thread_ts"):
                thread_ts_seen.add(row.get("thread_ts") or row.get("ts") or "")

        # Pull replies for the threads surfaced in this batch (bounded).
        for tts in list(thread_ts_seen)[:MAX_THREADS_PER_TICK]:
            if not tts:
                continue
            try:
                params = {"channel": channel_id, "ts": tts, "limit": 100}
                if oldest:
                    params["oldest"] = oldest
                payload = await self._api("conversations.replies", params, http="GET")
            except ChannelError as exc:
                logger.warning("slack channel: replies fetch failed (%s): %s", tts, exc)
                continue
            for row in (payload.get("messages") or []):
                ts = row.get("ts") or ""
                # Skip the parent / anything we already collected.
                if ts == tts or any(m.provider_message_id == ts for m in messages):
                    continue
                if oldest and ts <= oldest:
                    continue
                msg = self._message_from_slack(row, channel_id, bot_user_id)
                if msg is not None:
                    messages.append(msg)

        # Advance the cursor to the newest ts we observed (strictly
        # monotonic so the next poll never replays).
        newest = oldest
        for m in messages:
            if not newest or m.provider_message_id > newest:
                newest = m.provider_message_id
        messages.sort(key=lambda m: m.provider_message_id)
        new_cursor = newest or cursor
        return messages, new_cursor

    async def download_attachment(
        self, provider_ref: dict[str, Any],
    ) -> Optional[bytes]:
        url = (provider_ref or {}).get("url")
        if not url:
            return None
        import httpx
        cfg = self._config()
        headers = {"Authorization": f"Bearer {cfg['bot_token']}"}
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                resp = await client.get(url, headers=headers)
            if resp.status_code >= 400:
                logger.warning("slack attachment download HTTP %s", resp.status_code)
                return None
            data = resp.content
            # A token without files:read scope returns an HTML login page
            # (200) instead of the bytes — guard against staging that.
            ct = (resp.headers.get("content-type") or "").lower()
            if "text/html" in ct and data[:15].lstrip().lower().startswith(b"<!doctype"):
                logger.warning("slack attachment download returned HTML (missing files:read?)")
                return None
            return data or None
        except Exception as exc:
            logger.warning("slack attachment download failed: %s", exc)
            return None

    async def _upload_file(
        self, channel_id: str, thread_ts: Optional[str],
        filename: str, data: bytes, title: Optional[str] = None,
    ) -> None:
        """Upload one file via the external-upload flow
        (getUploadURLExternal → PUT bytes → completeUploadExternal)."""
        import httpx
        up = await self._api("files.getUploadURLExternal", {
            "filename": filename, "length": len(data),
        }, http="GET")
        upload_url = up.get("upload_url")
        file_id = up.get("file_id")
        if not upload_url or not file_id:
            raise ChannelError("Slack getUploadURLExternal returned no upload_url/file_id")
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                r = await client.post(upload_url, files={"file": (filename, data)})
            if r.status_code >= 400:
                raise ChannelError(f"Slack file upload HTTP {r.status_code}")
        except httpx.HTTPError as exc:
            raise ChannelError(f"Slack file upload failed: {exc}") from exc
        import json as _json
        complete: dict[str, Any] = {
            "files": _json.dumps([{"id": file_id, "title": title or filename}]),
            "channel_id": channel_id,
        }
        if thread_ts:
            complete["thread_ts"] = thread_ts
        await self._api("files.completeUploadExternal", complete)

    async def _open_dm(self, user_id: str) -> Optional[str]:
        """Open (or fetch) the DM channel id for a Slack user id so a
        notification addressed to ``U…``/``W…`` lands in a direct message
        instead of the watched channel."""
        try:
            payload = await self._api("conversations.open", {"users": user_id})
        except ChannelError as exc:
            logger.warning("slack conversations.open failed for %s: %s", user_id, exc)
            return None
        return ((payload.get("channel") or {}).get("id")) or None

    async def send_reply(self, original: Message, reply: Reply) -> str:
        cfg = self._config()
        # Destination resolution:
        #   * reactive reply → headers carry the inbound channel + thread,
        #   * notification   → a synthetic Message whose ``sender`` is the
        #     operator-configured recipient (a channel id C…/G… or a user
        #     id U…/W… → open a DM),
        #   * fallback       → the channel the adapter watches.
        channel_id = (original.headers or {}).get("slack_channel") or ""
        if not channel_id:
            recipient = (original.sender or "").strip()
            if recipient.startswith(("U", "W")):
                channel_id = await self._open_dm(recipient) or ""
            elif recipient:
                channel_id = recipient
        if not channel_id:
            channel_id = cfg["channel_id"]
        # Reply IN the thread of the inbound message; notifications (a
        # synthetic outbound Message) carry no thread and post top-level.
        thread_ts = (original.headers or {}).get("thread_ts") \
            or original.provider_thread_id

        body = render_markdown_slack(reply.body or "")

        # Materialise attachments BEFORE posting so a failure degrades to
        # a note in the body instead of blocking the reply.
        resolved: list[tuple[bytes, str]] = []  # (data, filename)
        failed_links: list[str] = []
        for att in (reply.attachments or []):
            got = await resolve_attachment_bytes(att)
            if got is None:
                if att.url:
                    failed_links.append(f"• <{att.url}|{att.filename}>")
                continue
            file_bytes, _mime = got
            resolved.append((file_bytes, att.filename))
        if failed_links:
            body = (body + "\n\nArchivos disponibles:\n" + "\n".join(failed_links)).strip()

        posted_ts = ""
        # Post the text body. Skip an empty post when we only have files
        # (the file upload itself carries the message into the thread).
        if body or not resolved:
            post: dict[str, Any] = {
                "channel": channel_id,
                "text": body or " ",
            }
            if thread_ts:
                post["thread_ts"] = thread_ts
            result = await self._api("chat.postMessage", post)
            posted_ts = result.get("ts") or ""

        # Upload files under the same thread (or under the message we just
        # posted, so they group together for a top-level notification).
        file_thread = thread_ts or posted_ts or None
        for fdata, fname in resolved:
            try:
                await self._upload_file(channel_id, file_thread, fname, fdata)
            except ChannelError as exc:
                logger.warning("slack file upload skipped (%s): %s", fname, exc)

        if not posted_ts and not resolved:
            raise ChannelError("Slack reply produced no message and no files")
        return posted_ts or f"file-upload:{channel_id}"

    async def disconnect(self) -> None:
        # Nothing to revoke upstream — the bot token lives only in the
        # encrypted blob, destroyed when the channel row is deleted.
        return None
