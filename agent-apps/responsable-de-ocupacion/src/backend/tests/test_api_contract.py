"""Contract tests for shared API helpers."""

from __future__ import annotations

import pytest
from fastapi import HTTPException


def test_paginated_envelope() -> None:
    from app.pagination import paginated

    out = paginated(100, 25, 50, [{"id": 1}])
    assert out == {
        "total": 100,
        "limit": 25,
        "offset": 50,
        "rows": [{"id": 1}],
    }


def test_api_error_detail_shape() -> None:
    from app.api_errors import api_error

    exc = api_error(404, "not_found", "Event not found.")
    assert exc.status_code == 404
    assert exc.detail == {
        "code": "not_found",
        "message": "Event not found.",
    }


@pytest.mark.asyncio
async def test_events_list_pagination_shape(monkeypatch) -> None:
    """Smoke: list_events returns the canonical paginated envelope."""
    import tempfile
    import os

    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    os.environ["APP_DATABASE_URL"] = f"sqlite+aiosqlite:///{path}"

    from app.app_database import AppBase, app_engine, app_session
    from app.app_models import runtime  # noqa: F401
    from app.routes.events_routes import list_events

    async with app_engine.begin() as conn:
        await conn.run_sync(AppBase.metadata.create_all)

    from app.auth import TokenPayload

    user = TokenPayload(
        email="ops@test.com",
        is_superuser=True,
        project_ids=[],
        data_areas=[],
    )
    out = await list_events(user=user, limit=10, offset=0)
    assert set(out.keys()) == {"total", "limit", "offset", "rows"}
    assert out["limit"] == 10
    assert out["offset"] == 0
    assert isinstance(out["rows"], list)

    os.remove(path)
