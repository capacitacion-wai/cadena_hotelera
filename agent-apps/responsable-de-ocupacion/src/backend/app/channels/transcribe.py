"""Best-effort speech-to-text for inbound voice notes (WhatsApp, …).

Transcribes an audio blob via an OpenAI-compatible ``/audio/transcriptions``
endpoint using the tenant's own key — the deployer injects it as
``TRANSCRIPTION_API_KEY`` (the SAME OpenAI secret the agent pod already uses),
so no new credential is introduced.

NEVER raises: a missing key, an oversized file or an API error simply yields
``None`` and the caller falls back to "voice note received but not
transcribed", so channel ingestion is never blocked by transcription.
"""
from __future__ import annotations

import logging

import httpx

from app.config import (
    TRANSCRIPTION_API_KEY,
    TRANSCRIPTION_BASE_URL,
    TRANSCRIPTION_MODEL,
)

logger = logging.getLogger(__name__)

# OpenAI's transcription endpoint caps uploads at 25 MB; skip anything larger
# rather than getting a 413 (a WhatsApp voice note is far smaller anyway).
_MAX_AUDIO_BYTES = 25 * 1024 * 1024


async def transcribe_audio(
    data: bytes, filename: str = "audio.ogg", mime: str | None = None,
) -> str | None:
    """Return the transcript of ``data`` (audio bytes), or ``None`` when
    transcription is unavailable/failed. Best-effort by design."""
    if not TRANSCRIPTION_API_KEY or not data:
        return None
    if len(data) > _MAX_AUDIO_BYTES:
        logger.info("transcribe: audio too large (%d bytes) — skipping", len(data))
        return None
    url = f"{TRANSCRIPTION_BASE_URL.rstrip('/')}/audio/transcriptions"
    files = {"file": (filename or "audio.ogg", data, mime or "application/octet-stream")}
    form = {"model": TRANSCRIPTION_MODEL}
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {TRANSCRIPTION_API_KEY}"},
                data=form,
                files=files,
            )
        if resp.status_code >= 400:
            logger.warning("transcribe: HTTP %s — %s", resp.status_code, resp.text[:200])
            return None
        text = (resp.json().get("text") or "").strip()
        return text or None
    except Exception as exc:  # pragma: no cover — best effort, never block ingestion
        logger.warning("transcribe failed: %s", exc)
        return None
