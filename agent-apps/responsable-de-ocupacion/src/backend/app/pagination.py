"""Shared paginated list response shape for operator HTTP APIs."""

from __future__ import annotations

from typing import Any


def paginated(total: int, limit: int, offset: int, rows: list[Any]) -> dict[str, Any]:
    """Return ``{total, limit, offset, rows}`` — the canonical list envelope."""
    return {
        "total": int(total or 0),
        "limit": int(limit),
        "offset": int(offset),
        "rows": rows,
    }
