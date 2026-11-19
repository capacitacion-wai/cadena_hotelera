"""PUBLIC media endpoint for outbound channel attachments.

WhatsApp/Twilio fetches a deliverable by URL, so this route serves a single
workspace file granted by a short-lived signed token. It is **intentionally
unauthenticated** (Twilio sends no bearer): security comes from the HMAC
signature + expiry in the token (see ``app.signed_media``) and from the
``realpath`` confinement of ``_workspace_file`` — never from app auth.

It is the ONLY public, auth-exempt route this feature adds; it cannot read
anything outside the workspace and only what a valid, unexpired token names.
"""

from __future__ import annotations

import logging
import mimetypes
import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from app.channels.base import MAX_ATTACHMENT_BYTES, _workspace_file
from app.signed_media import verify_media_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/channels", tags=["channels"])

# Python's ``mimetypes`` DB is incomplete in slim containers — e.g.
# ``guess_type("x.pptx")`` returns ``None`` — so office/PDF files were served as
# ``application/octet-stream``. WhatsApp/Twilio REJECTS media whose Content-Type
# it doesn't recognise as a supported document/image (delivery fails with Twilio
# error 63019 "media failed to download"), even though the bytes download fine.
# Map the extensions WhatsApp supports to their correct type explicitly.
_EXT_MIME = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".txt": "text/plain",
    ".csv": "text/csv",
}


@router.get("/media/{token}")
async def get_signed_media(token: str):
    """Stream the workspace file named by a valid signed token. 404 on any
    invalid/expired/tampered token or missing file (never leak why)."""
    rel = verify_media_token(token)
    if not rel:
        raise HTTPException(status_code=404, detail="not found")
    path = _workspace_file(rel)  # realpath-confined to the workspace
    if not path:
        raise HTTPException(status_code=404, detail="not found")
    try:
        if os.path.getsize(path) > MAX_ATTACHMENT_BYTES:
            raise HTTPException(status_code=413, detail="file too large")
        with open(path, "rb") as fh:
            data = fh.read()
    except HTTPException:
        raise
    except OSError:
        raise HTTPException(status_code=404, detail="not found")
    filename = os.path.basename(path)
    ext = os.path.splitext(filename)[1].lower()
    mime = _EXT_MIME.get(ext) or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return Response(
        content=data,
        media_type=mime,
        headers={
            # inline so WhatsApp previews; filename kept for clients that save.
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "private, no-store",
        },
    )
