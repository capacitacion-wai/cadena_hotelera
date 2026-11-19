"""Telegram channel adapter — Bot API long-polling (``getUpdates``).

Why long-polling (not ``setWebhook``): the channels engine is deliberately
*pull-based* — the app pod polls, nothing on the public internet pushes in (see
``whatsapp_twilio.py`` and ``patterns/external-channels.md``). Telegram's
``getUpdates`` fits exactly: the ``update_id`` OFFSET is the cursor, monotonic
and idempotent (re-calling with it returns ``[]``). ``setWebhook`` would need a
public receiver the deployment does not provide.

Credentials (``bot_token``) are stored encrypted (Fernet) in
``channels.imap_config_encrypted`` (a generic credential blob despite the name),
exactly like Slack's ``bot_token`` and Twilio's ``auth_token``.
``channels.mailbox_address`` holds the bot's ``@username`` (display only).

Sender identity: the inbound ``from.username`` (lowercased, no ``@``) is used as
``Message.sender`` so the poller's allow-list can resolve it to a DIRECTORY person
(``AppUser.telegram``), mirroring how WhatsApp resolves a phone number.
``Message.provider_thread_id`` carries the numeric ``chat.id`` — the reply target.

Scope (v1): 1:1 private chats only (like WhatsApp). Groups (privacy mode + mentions)
are a later iteration.
"""
from __future__ import annotations

import logging
import mimetypes
from datetime import datetime, timezone
from typing import Any, Optional

from app.channels.base import (
    ChannelAdapter, ChannelConfigError, ChannelError, ChannelUnauthorizedError,
    InboundAttachment, Message, Reply, TestResult, channel_process_since,
    resolve_attachment_bytes,
)
from app.channels.encryption import decrypt_blob

logger = logging.getLogger(__name__)

TG_API = "https://api.telegram.org"
# Telegram sendMessage hard cap (4096 chars); longer replies are split.
TG_BODY_MAX = 4096
# getUpdates rows per tick — a burst bigger than this is picked up next poll.
MAX_FETCH_UPDATES = 100
# Inbound audio (voice notes / audio files): NOT staged as a file — transcribed
# to text (best-effort, channels/transcribe) and injected into the body so the
# employee UNDERSTANDS the spoken request, exactly like the WhatsApp adapter.
_AUDIO_MIME_PREFIXES = ("audio/",)


class TelegramBotAdapter(ChannelAdapter):
    """Concrete adapter for ``kind='telegram' auth_method='telegram_bot'``."""

    kind = "telegram"
    auth_method = "telegram_bot"

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
                "Telegram channel has no stored credentials. Run the wizard "
                "again with the bot token from @BotFather."
            )
        try:
            cfg = decrypt_blob(token)
        except Exception as exc:
            raise ChannelConfigError(str(exc)) from exc
        if not cfg.get("bot_token"):
            raise ChannelConfigError("Telegram credentials are incomplete (bot_token missing).")
        self._cfg = cfg
        return cfg

    async def _request(
        self, api_method: str, http_method: str = "GET",
        params: Optional[dict[str, Any]] = None,
        data: Optional[dict[str, Any]] = None,
        files: Optional[dict[str, Any]] = None,
    ) -> Any:
        """One call against the Telegram Bot API. Maps provider errors onto the
        channel exception taxonomy so the poller flips ``status`` correctly."""
        import httpx
        cfg = self._config()
        url = f"{TG_API}/bot{cfg['bot_token']}/{api_method}"
        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                resp = await client.request(http_method, url, params=params, data=data, files=files)
        except httpx.HTTPError as exc:
            raise ChannelError(f"Telegram request failed: {exc}") from exc
        if resp.status_code in (401, 403):
            raise ChannelUnauthorizedError(
                f"Telegram rejected the bot token (HTTP {resp.status_code}). "
                "Recreate or rotate it with @BotFather."
            )
        try:
            body = resp.json()
        except Exception as exc:
            raise ChannelError(f"Telegram returned non-JSON payload: {exc}") from exc
        if not body.get("ok"):
            code = body.get("error_code")
            desc = body.get("description") or "unknown error"
            if code in (401, 403):
                raise ChannelUnauthorizedError(f"Telegram: {desc}")
            raise ChannelError(f"Telegram error {code}: {desc}")
        return body.get("result")

    # ── ChannelAdapter contract ───────────────────────────────────────
    async def test_connection(self) -> TestResult:
        try:
            self._config()
        except ChannelConfigError as exc:
            return TestResult(ok=False, message=str(exc))
        try:
            me = await self._request("getMe")
        except (ChannelUnauthorizedError, ChannelError) as exc:
            return TestResult(ok=False, message=str(exc))
        return TestResult(
            ok=True, message="Telegram bot connection OK",
            detail={"bot_username": (me or {}).get("username"),
                    "bot_name": (me or {}).get("first_name")},
        )

    async def fetch_new(
        self, cursor: Optional[str],
    ) -> tuple[list[Message], Optional[str]]:
        offset: Optional[int] = None
        if cursor:
            try:
                offset = int(cursor)
            except (TypeError, ValueError):
                offset = None
        params: dict[str, Any] = {
            "timeout": 0, "limit": MAX_FETCH_UPDATES,
            "allowed_updates": '["message"]',
        }
        if offset is not None:
            params["offset"] = offset
        updates = await self._request("getUpdates", params=params) or []

        # First poll (no cursor): honour the operator's "process since" floor.
        floor = None if cursor else channel_process_since(self.channel)
        messages: list[Message] = []
        highest: Optional[int] = (offset - 1) if offset is not None else None
        for u in updates:
            uid = u.get("update_id")
            if uid is not None:
                highest = uid if highest is None else max(highest, uid)
            msg = u.get("message")
            if not msg:
                continue
            chat = msg.get("chat") or {}
            if chat.get("type") != "private":
                continue  # v1: 1:1 DMs only (groups are a later iteration)
            ts = msg.get("date")
            sent_at = (datetime.fromtimestamp(ts, tz=timezone.utc)
                       if ts else datetime.now(timezone.utc))
            if floor and sent_at < floor:
                continue
            frm = msg.get("from") or {}
            username = (frm.get("username") or "").strip().lstrip("@").lower()
            body_text = msg.get("text") or msg.get("caption") or ""
            attachments, audio_note = await self._build_inbound_attachments(msg)
            if audio_note and not body_text.strip():
                body_text = audio_note
            elif audio_note:
                body_text = f"{body_text}\n\n{audio_note}"
            messages.append(Message(
                provider_message_id=str(msg.get("message_id")),
                provider_thread_id=str(chat.get("id")),   # chat id = reply target
                sender=username or str(frm.get("id") or ""),
                subject=None,
                body_text=body_text,
                body_html=None,
                received_at=sent_at,
                headers={
                    "tg_chat_id": str(chat.get("id")),
                    "tg_user_id": str(frm.get("id") or ""),
                    "tg_username": username,
                    "tg_first_name": frm.get("first_name") or "",
                },
                attachments=attachments,
            ))
        messages.sort(key=lambda m: m.received_at or datetime.now(timezone.utc))
        new_cursor = str(highest + 1) if highest is not None else cursor
        return messages, new_cursor

    async def _build_inbound_attachments(
        self, msg: dict[str, Any],
    ) -> tuple[list[InboundAttachment], str | None]:
        """Map a message's media: voice/audio → transcribed inline (note into the
        body); documents/photos → deferred ``InboundAttachment`` downloaded later
        via ``download_attachment``. Best-effort: a failure never blocks ingestion."""
        out: list[InboundAttachment] = []
        transcripts: list[str] = []
        had_untranscribed_audio = False

        media: list[tuple[str, dict, str]] = []
        if msg.get("voice"):
            media.append(("voice", msg["voice"], (msg["voice"].get("mime_type") or "audio/ogg")))
        if msg.get("audio"):
            media.append(("audio", msg["audio"], (msg["audio"].get("mime_type") or "audio/mpeg")))
        if msg.get("document"):
            d = msg["document"]
            media.append(("document", d, (d.get("mime_type") or "application/octet-stream")))
        if msg.get("photo"):
            sizes = msg["photo"] or []
            if sizes:
                media.append(("photo", sizes[-1], "image/jpeg"))  # largest size

        for kind, obj, mime in media:
            file_id = obj.get("file_id")
            if not file_id:
                continue
            if kind in ("voice", "audio") or (mime or "").lower().startswith(_AUDIO_MIME_PREFIXES):
                try:
                    data = await self._download_file(file_id)
                    if data:
                        from app.channels.transcribe import transcribe_audio
                        ext = mimetypes.guess_extension((mime or "").split(";")[0].strip()) or ".ogg"
                        text = await transcribe_audio(data, f"voice-{str(file_id)[:10]}{ext}", mime)
                        if text:
                            transcripts.append(text)
                        else:
                            had_untranscribed_audio = True
                    else:
                        had_untranscribed_audio = True
                except Exception as exc:  # pragma: no cover — never block ingestion
                    logger.warning("telegram voice transcription failed (%s): %s", file_id, exc)
                    had_untranscribed_audio = True
                continue
            ext = mimetypes.guess_extension((mime or "").split(";")[0].strip()) or ".bin"
            fname = obj.get("file_name") or f"{kind}-{str(file_id)[:10]}{ext}"
            out.append(InboundAttachment(
                filename=fname, mime_type=mime or None,
                provider_ref={"file_id": file_id},
            ))

        if transcripts:
            joined = "\n\n".join(t.strip() for t in transcripts if t.strip())
            note = ("🎤 Nota de voz recibida (transcrita automáticamente):\n"
                    f"\"{joined}\"")
        elif had_untranscribed_audio:
            note = ("(El remitente envió una nota de voz que no se pudo transcribir. "
                    "Si necesitas su contenido, pídele que lo escriba.)")
        else:
            note = None
        return out, note

    async def _download_file(self, file_id: str) -> Optional[bytes]:
        """Resolve a Telegram file_id to bytes: ``getFile`` → download URL."""
        import httpx
        cfg = self._config()
        info = await self._request("getFile", params={"file_id": file_id})
        file_path = (info or {}).get("file_path")
        if not file_path:
            return None
        url = f"{TG_API}/file/bot{cfg['bot_token']}/{file_path}"
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.get(url)
            if resp.status_code >= 400:
                logger.warning("telegram file download %s: HTTP %s", file_id, resp.status_code)
                return None
            return resp.content or None
        except Exception as exc:  # pragma: no cover — best-effort
            logger.warning("telegram file download failed (%s): %s", file_id, exc)
            return None

    async def download_attachment(self, provider_ref: dict[str, Any]) -> Optional[bytes]:
        file_id = (provider_ref or {}).get("file_id")
        if not file_id:
            return None
        return await self._download_file(file_id)

    async def send_reply(self, original: Message, reply: Reply) -> str:
        # The third fallback is what makes a PROACTIVE notification possible: the notifier
        # builds a synthetic message (no thread, no headers) whose `sender` IS the
        # recipient. Without it, any notice that was not a reply died with
        # «no chat id» — which is why a notification rule to Telegram sent nothing.
        chat_id = (original.provider_thread_id
                   or (original.headers or {}).get("tg_chat_id")
                   or (original.sender or "").strip() or None)
        if not chat_id:
            raise ChannelError("original message has no chat id to reply to")
        body = (reply.body or "").strip()
        first_id = ""
        # Plain text (no parse_mode): avoids MarkdownV2's fragile escaping. Long
        # replies are split into 4096-char chunks. (Richer formatting: later.)
        for start in range(0, len(body), TG_BODY_MAX) if body else []:
            chunk = body[start:start + TG_BODY_MAX]
            result = await self._request(
                "sendMessage", "POST", data={"chat_id": chat_id, "text": chunk})
            if not first_id:
                first_id = str((result or {}).get("message_id") or "")

        # Attachments: Telegram accepts DIRECT bytes upload (multipart) — no public
        # signed-URL needed (unlike Twilio). One sendDocument per file.
        for att in (reply.attachments or []):
            try:
                resolved = await resolve_attachment_bytes(att)
                if not resolved:
                    continue
                data, mime = resolved
                files = {"document": (att.filename or "archivo", data, mime or "application/octet-stream")}
                r = await self._request("sendDocument", "POST",
                                        data={"chat_id": chat_id}, files=files)
                if not first_id:
                    first_id = str((r or {}).get("message_id") or "")
            except ChannelError as exc:
                logger.warning("telegram sendDocument failed: %s", exc)

        if not first_id:
            raise ChannelError("Telegram accepted nothing (empty reply and no attachments)")
        return first_id

    async def disconnect(self) -> None:
        # Nothing to revoke upstream — the bot token lives only in the encrypted
        # blob, destroyed when the channel row is deleted.
        return None
