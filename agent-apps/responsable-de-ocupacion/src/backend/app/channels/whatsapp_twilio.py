"""WhatsApp channel adapter backed by the Twilio REST API.

Why Twilio (and not Meta's Cloud API): the channels engine is
deliberately *pull-based* — the app pod polls, nothing on the public
internet pushes into the cluster. Twilio exposes inbound messages
through ``GET /2010-04-01/Accounts/{sid}/Messages.json`` so the same
poller / executor / routes that drive email work unchanged. Meta's
Cloud API only delivers inbound via webhooks, which would force a
public receiver — out of scope for this skill (see
``patterns/external-channels.md``).

Credentials (``account_sid``, ``auth_token``, ``whatsapp_number``) are
stored encrypted (Fernet) in ``channels.imap_config_encrypted`` — the
column is a generic credential blob despite its historical name; only
the adapter interprets the payload. ``channels.mailbox_address`` holds
the WhatsApp business number in E.164 (``+34600111222``).

Reply window: WhatsApp only allows freeform business replies within
24 h of the last inbound message. Since this adapter only ever replies
to a just-received inbound, the window is always open. Outside-window
notifications (templates) are NOT a channel concern — use
``log_communication`` + a downstream worker for those.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional

from app.channels.base import (
    ChannelAdapter, ChannelConfigError, ChannelError,
    ChannelUnauthorizedError, InboundAttachment, Message, Reply, TestResult,
    channel_process_since,
)
from app.channels.encryption import decrypt_blob

logger = logging.getLogger(__name__)

TWILIO_API = "https://api.twilio.com/2010-04-01"

# WhatsApp body hard cap (Twilio). WhatsApp itself allows ~4096 chars but
# Twilio's API rejects bodies over 1600 for WhatsApp, so we clamp there.
WA_BODY_MAX = 1600

# Inbound audio (voice notes): NOT staged as a file — instead transcribed to
# text (best-effort, ``channels/transcribe``) and injected into the message
# body so the employee UNDERSTANDS the spoken request. Falls back to a "voice
# note received but not transcribed" note when transcription is unavailable.
_AUDIO_MEDIA_PREFIXES = ("audio/",)

# E.164 — used to validate the configured business number.
E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")


def _strip_wa(number: str) -> str:
    """``whatsapp:+34600111222`` → ``+34600111222``."""
    n = (number or "").strip()
    return n[len("whatsapp:"):] if n.startswith("whatsapp:") else n


def _to_wa(number: str) -> str:
    """``+34600111222`` → ``whatsapp:+34600111222`` (idempotent)."""
    n = (number or "").strip()
    return n if n.startswith("whatsapp:") else f"whatsapp:{n}"


def _parse_twilio_date(raw: Optional[str]) -> Optional[datetime]:
    """Twilio timestamps are RFC 2822 (``Tue, 10 Jun 2026 14:01:02 +0000``)."""
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _parse_cursor(cursor: Optional[str]) -> Optional[datetime]:
    """Cursor format: ISO-8601 UTC datetime of the newest inbound
    message already processed. ``None`` on first poll."""
    if not cursor:
        return None
    try:
        dt = datetime.fromisoformat(cursor)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


class WhatsAppTwilioAdapter(ChannelAdapter):
    """Concrete adapter for ``kind='whatsapp' auth_method='twilio'``."""

    kind = "whatsapp"
    auth_method = "twilio"

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
                "WhatsApp channel has no stored credentials. Run the "
                "wizard again with the Twilio Account SID + Auth Token."
            )
        try:
            cfg = decrypt_blob(token)
        except Exception as exc:
            raise ChannelConfigError(str(exc)) from exc
        if not cfg.get("account_sid") or not cfg.get("auth_token"):
            raise ChannelConfigError(
                "Twilio credentials are incomplete (account_sid / "
                "auth_token missing)."
            )
        if not cfg.get("whatsapp_number"):
            cfg["whatsapp_number"] = _strip_wa(self.channel.mailbox_address or "")
        if not E164_RE.match(_strip_wa(cfg["whatsapp_number"])):
            raise ChannelConfigError(
                f"WhatsApp number {cfg['whatsapp_number']!r} is not E.164 "
                "(+<country><number>)."
            )
        self._cfg = cfg
        return cfg

    async def _request(
        self, method: str, path: str,
        params: Optional[dict[str, Any]] = None,
        data: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """One authenticated call against the Twilio REST API. Maps the
        provider's status codes onto the channel exception taxonomy so
        the poller can flip ``status`` correctly."""
        import httpx
        cfg = self._config()
        url = f"{TWILIO_API}/Accounts/{cfg['account_sid']}{path}"
        auth = (cfg["account_sid"], cfg["auth_token"])
        try:
            async with httpx.AsyncClient(timeout=30.0, auth=auth) as client:
                resp = await client.request(method, url, params=params, data=data)
        except httpx.HTTPError as exc:
            raise ChannelError(f"Twilio request failed: {exc}") from exc
        if resp.status_code in (401, 403):
            raise ChannelUnauthorizedError(
                f"Twilio rejected the credentials (HTTP {resp.status_code}). "
                "Rotate the Auth Token from the channel detail page."
            )
        if resp.status_code >= 400:
            detail = ""
            try:
                detail = (resp.json() or {}).get("message") or ""
            except Exception:
                detail = resp.text[:200]
            raise ChannelError(f"Twilio HTTP {resp.status_code}: {detail}")
        try:
            return resp.json()
        except Exception as exc:
            raise ChannelError(f"Twilio returned non-JSON payload: {exc}") from exc

    # ── ChannelAdapter contract ───────────────────────────────────────
    async def test_connection(self) -> TestResult:
        try:
            cfg = self._config()
        except ChannelConfigError as exc:
            return TestResult(ok=False, message=str(exc))
        try:
            account = await self._request("GET", ".json")
        except ChannelUnauthorizedError as exc:
            return TestResult(ok=False, message=str(exc))
        except ChannelError as exc:
            return TestResult(ok=False, message=str(exc))
        status = (account.get("status") or "").lower()
        if status and status != "active":
            return TestResult(
                ok=False,
                message=f"Twilio account status is {status!r} (expected 'active')",
            )
        return TestResult(
            ok=True, message="Twilio connection OK",
            detail={
                "account_name": account.get("friendly_name"),
                "whatsapp_number": cfg["whatsapp_number"],
            },
        )

    # Hard ceiling per poll tick: pagination follows ``next_page_uri``
    # until exhausted or until this many rows have been scanned, so a
    # burst bigger than one page (50) is not silently dropped while a
    # pathological backlog can't stall the poller either.
    MAX_FETCH_ROWS = 500

    async def fetch_new(
        self, cursor: Optional[str],
    ) -> tuple[list[Message], Optional[str]]:
        cfg = self._config()
        since = _parse_cursor(cursor)
        params: dict[str, Any] = {
            "To": _to_wa(cfg["whatsapp_number"]),
            "PageSize": 50,
        }
        # Twilio's DateSent filter is day-granular; we narrow server-side
        # to the cursor's date and filter exact datetimes client-side.
        # First poll (no cursor): honour the operator's "process since"
        # floor when set, else only look back 24 h so an old busy number
        # doesn't flood the executor with stale conversations.
        if since is None:
            since = channel_process_since(self.channel)
        floor = since or (datetime.now(timezone.utc) - timedelta(hours=24))
        params["DateSent>"] = (floor - timedelta(days=1)).strftime("%Y-%m-%d")

        rows: list[dict[str, Any]] = []
        payload = await self._request("GET", "/Messages.json", params=params)
        rows.extend(payload.get("messages") or [])
        while len(rows) < self.MAX_FETCH_ROWS:
            next_uri = payload.get("next_page_uri") or ""
            if not next_uri:
                break
            # next_page_uri is rooted at the API host and already carries
            # the account prefix + query string.
            path = next_uri
            prefix = f"/2010-04-01/Accounts/{cfg['account_sid']}"
            if path.startswith(prefix):
                path = path[len(prefix):]
            payload = await self._request("GET", path)
            page = payload.get("messages") or []
            if not page:
                break
            rows.extend(page)
        if len(rows) >= self.MAX_FETCH_ROWS:
            logger.warning(
                "twilio channel %s: fetch capped at %d rows this tick; "
                "remaining messages will be picked up on the next poll",
                getattr(self.channel, "id", "?"), self.MAX_FETCH_ROWS,
            )

        messages: list[Message] = []
        newest: Optional[datetime] = since
        for row in rows:
            direction = (row.get("direction") or "").lower()
            if not direction.startswith("inbound"):
                continue  # our own outbound replies also appear in the list
            sent_at = _parse_twilio_date(row.get("date_sent") or row.get("date_created"))
            if sent_at is None:
                continue
            if since is not None and sent_at <= since:
                continue  # already processed (cursor is strict)
            if since is None and sent_at < floor:
                continue  # first poll look-back window
            sender = _strip_wa(row.get("from") or "")
            msg_sid = row.get("sid") or ""
            body_text = row.get("body") or ""
            try:
                num_media = int(row.get("num_media") or 0)
            except (TypeError, ValueError):
                num_media = 0
            attachments: list[InboundAttachment] = []
            if num_media > 0 and msg_sid:
                attachments, audio_note = await self._build_inbound_attachments(msg_sid)
                if audio_note and not body_text.strip():
                    body_text = audio_note
                elif audio_note:
                    body_text = f"{body_text}\n\n{audio_note}"
            messages.append(Message(
                provider_message_id=msg_sid,
                # WhatsApp has no thread id; the conversation IS the
                # sender's number. Threading replies keys off this.
                provider_thread_id=sender or None,
                sender=sender or None,
                subject=None,
                body_text=body_text,
                body_html=None,
                received_at=sent_at,
                headers={
                    "wa_from": row.get("from") or "",
                    "wa_to": row.get("to") or "",
                    "num_media": str(num_media),
                },
                attachments=attachments,
            ))
            if newest is None or sent_at > newest:
                newest = sent_at

        messages.sort(key=lambda m: m.received_at or datetime.now(timezone.utc))
        new_cursor = newest.isoformat() if newest else cursor
        return messages, new_cursor

    async def _build_inbound_attachments(
        self, message_sid: str,
    ) -> tuple[list[InboundAttachment], str | None]:
        """List a message's media (Twilio Media subresource) and turn each
        into a deferred ``InboundAttachment`` (downloaded later by the
        poller via ``download_attachment``). Audio is skipped (no audio
        support yet) and reported back as a human note. Best-effort: any
        failure yields no attachments rather than blocking ingestion."""
        import mimetypes
        try:
            payload = await self._request(
                "GET", f"/Messages/{message_sid}/Media.json",
            )
        except ChannelError as exc:
            logger.warning("twilio media list failed for %s: %s", message_sid, exc)
            return [], None
        media_list = payload.get("media_list") or []
        out: list[InboundAttachment] = []
        transcripts: list[str] = []
        had_untranscribed_audio = False
        for i, m in enumerate(media_list):
            content_type = (m.get("content_type") or "").lower()
            media_sid = m.get("sid") or ""
            if not media_sid:
                continue
            if content_type.startswith(_AUDIO_MEDIA_PREFIXES):
                # Voice note → download the bytes now and transcribe so the
                # employee understands it (Phase 1: understand inbound audio).
                ref = {"message_sid": message_sid, "media_sid": media_sid}
                text = None
                try:
                    data = await self.download_attachment(ref)
                    if data:
                        from app.channels.transcribe import transcribe_audio
                        ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ".ogg"
                        text = await transcribe_audio(
                            data, f"voice-{media_sid[:10]}{ext}", content_type,
                        )
                except Exception as exc:  # pragma: no cover — never block ingestion
                    logger.warning("voice-note transcription failed (%s): %s", media_sid, exc)
                if text:
                    transcripts.append(text)
                else:
                    had_untranscribed_audio = True
                continue
            ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ".bin"
            base = content_type.split("/")[0] or "file"
            out.append(InboundAttachment(
                filename=f"{base}-{media_sid[:10]}{ext}",
                mime_type=content_type or None,
                provider_ref={"message_sid": message_sid, "media_sid": media_sid},
            ))
        if transcripts:
            joined = "\n\n".join(t.strip() for t in transcripts if t.strip())
            note = (
                "🎤 Nota de voz recibida (transcrita automáticamente):\n"
                f"\"{joined}\""
            )
        elif had_untranscribed_audio:
            note = (
                "(El remitente envió una nota de voz que no se pudo transcribir. "
                "Si necesitas su contenido, pídele que lo escriba.)"
            )
        else:
            note = None
        return out, note

    async def download_attachment(self, provider_ref: dict[str, Any]):
        """Resolve a deferred inbound media item into raw bytes. Twilio media
        bytes live at the Media instance URL (Basic-auth, follows a redirect
        to the media host). Returns ``None`` on any failure."""
        import httpx
        msg_sid = (provider_ref or {}).get("message_sid")
        media_sid = (provider_ref or {}).get("media_sid")
        if not msg_sid or not media_sid:
            return None
        cfg = self._config()
        url = (
            f"{TWILIO_API}/Accounts/{cfg['account_sid']}"
            f"/Messages/{msg_sid}/Media/{media_sid}"
        )
        try:
            async with httpx.AsyncClient(
                timeout=30.0, auth=(cfg["account_sid"], cfg["auth_token"]),
                follow_redirects=True,
            ) as client:
                resp = await client.get(url)
            if resp.status_code >= 400:
                logger.warning("twilio media download %s: HTTP %s", media_sid, resp.status_code)
                return None
            return resp.content or None
        except Exception as exc:  # pragma: no cover — best-effort
            logger.warning("twilio media download failed (%s): %s", media_sid, exc)
            return None

    def _media_url_for(self, att) -> str | None:
        """Public, Twilio-fetchable URL for one outbound attachment.

        - ``url`` (already public) → used as-is.
        - ``workspace_path`` → a short-lived SIGNED URL served by this app's
          public ingress (Twilio fetches it in seconds, then it expires).
        Returns ``None`` for ``content_base64``-only files (no path to serve)
        or when no public base URL is configured — the caller names those in
        the body instead."""
        if att.url and att.url.lower().startswith(("http://", "https://")):
            return att.url
        if att.workspace_path:
            from app.signed_media import signed_media_url
            return signed_media_url(att.workspace_path, channel=self.channel)
        return None

    async def send_reply(self, original: Message, reply: Reply) -> str:
        cfg = self._config()
        to_number = _strip_wa(original.sender or "")
        if not to_number:
            raise ChannelError("original message has no sender number to reply to")
        body = (reply.body or "").strip()

        # Build a Twilio-fetchable URL per attachment (signed workspace link
        # or public url). Files we can't turn into a URL are named in body.
        sendable: list[str] = []
        unsendable: list[str] = []
        for att in (reply.attachments or []):
            u = self._media_url_for(att)
            if u:
                sendable.append(u)
            elif att.workspace_path or att.content_base64:
                unsendable.append(att.filename or "archivo")
        if unsendable:
            tail = (
                "\n\nNo se pudo adjuntar por WhatsApp: " + ", ".join(unsendable)
                + ". Pídemelo por correo si lo necesitas."
            )
            body = (body + tail)

        # WhatsApp via Twilio = 1 media per message. First message carries
        # the text + first file; any extra files go in follow-up messages.
        first_sid = ""
        head_body = body[:WA_BODY_MAX - 3] + "..." if len(body) > WA_BODY_MAX else body
        data: dict[str, Any] = {
            "From": _to_wa(cfg["whatsapp_number"]),
            "To": _to_wa(to_number),
            "Body": head_body,
        }
        if sendable:
            data["MediaUrl"] = sendable[0]
        result = await self._request("POST", "/Messages.json", data=data)
        first_sid = result.get("sid") or ""
        if not first_sid:
            raise ChannelError("Twilio accepted the message but returned no SID")

        # Follow-up messages, one per remaining attachment (best-effort: a
        # failed extra send must not undo the main reply already delivered).
        for extra_url in sendable[1:]:
            try:
                await self._request("POST", "/Messages.json", data={
                    "From": _to_wa(cfg["whatsapp_number"]),
                    "To": _to_wa(to_number),
                    "MediaUrl": extra_url,
                })
            except ChannelError as exc:
                logger.warning("twilio extra media send failed: %s", exc)
        return first_sid

    async def disconnect(self) -> None:
        # Nothing to revoke upstream — the Auth Token lives only in the
        # encrypted blob, destroyed when the channel row is deleted.
        return None
