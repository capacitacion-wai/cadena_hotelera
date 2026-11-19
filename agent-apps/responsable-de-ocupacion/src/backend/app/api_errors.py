"""Stable API error codes for operator-facing HTTP responses.

FastAPI ``detail`` may be a plain string (legacy) or
``{"code": "<dotted.code>", "message": "<english>"}``. The SPA maps
``code`` through i18n; ``message`` is the English fallback.
"""

from __future__ import annotations

from fastapi import HTTPException


def api_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )
