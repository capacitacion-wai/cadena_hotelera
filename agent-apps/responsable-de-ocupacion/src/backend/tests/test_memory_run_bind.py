"""R-MEM-5 / v6.40.2 — run-scoped memory bind + strict run_id resolution.

Without legacy agents we enforce:

* ``valid_memory_user`` / ``authorized_memory_user`` on the memory path
* ``persist_run_memory_user`` writes ``request_meta_json.memory_user``
* ``_resolve_run_id`` accepts ONLY an existing ``AgentRun.id`` — no
  fallback to the latest RUNNING run (which could attach tool calls to
  the wrong audit row under concurrency).
"""
from __future__ import annotations

import json
import os
import tempfile

import pytest

pytestmark = pytest.mark.asyncio


@pytest.mark.asyncio
async def test_valid_memory_user_rejects_placeholders_and_autonomous() -> None:
    from unittest.mock import patch
    from app.memory_utils import valid_memory_user

    assert valid_memory_user("ops@acme.com") == "ops@acme.com"
    assert valid_memory_user("  OPS@ACME.COM ") == "ops@acme.com"
    assert valid_memory_user("unknown") is None
    assert valid_memory_user("unknown@unknown") is None
    assert valid_memory_user("not-an-email") is None
    assert valid_memory_user("") is None

    with patch("app.memory_utils.AUTONOMOUS_USER_EMAIL", "bot@wai.internal"):
        assert valid_memory_user("bot@wai.internal") is None
        assert valid_memory_user(
            "bot@wai.internal", allow_autonomous_identity=True,
        ) == "bot@wai.internal"


@pytest.mark.asyncio
async def test_persist_and_authorized_memory_user() -> None:
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    os.environ["APP_DATABASE_URL"] = f"sqlite+aiosqlite:///{path}"

    from app.app_database import AppBase, app_engine, app_session
    from app.app_models import runtime  # noqa: F401
    from app.app_models.runtime import AgentRun
    from app.memory_utils import authorized_memory_user, persist_run_memory_user
    from sqlalchemy import select

    async with app_engine.begin() as conn:
        await conn.run_sync(AppBase.metadata.create_all)

    async with app_session() as s:
        s.add(AgentRun(
            idempotency_key="bind-test",
            status="RUNNING",
            prompt="hi",
        ))
        await s.commit()
        run_id = (await s.execute(
            select(AgentRun.id).where(AgentRun.idempotency_key == "bind-test")
        )).scalar_one()

    await persist_run_memory_user(int(run_id), "human@acme.com")
    assert await authorized_memory_user(int(run_id), "human@acme.com") == "human@acme.com"
    assert await authorized_memory_user(int(run_id), "other@acme.com") is None

    async with app_session() as s:
        row = (await s.execute(
            select(AgentRun).where(AgentRun.id == int(run_id))
        )).scalar_one()
        meta = json.loads(row.request_meta_json or "{}")
    assert meta.get("memory_user") == "human@acme.com"

    os.remove(path)


@pytest.mark.asyncio
async def test_resolve_run_id_exact_only_no_fallback() -> None:
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    os.environ["APP_DATABASE_URL"] = f"sqlite+aiosqlite:///{path}"

    from app.app_database import AppBase, app_engine, app_session
    from app.app_models import runtime  # noqa: F401
    from app.app_models.runtime import AgentRun
    from app.mcp_server import _resolve_run_id

    async with app_engine.begin() as conn:
        await conn.run_sync(AppBase.metadata.create_all)

    async with app_session() as s:
        s.add(AgentRun(
            idempotency_key="old",
            status="COMPLETED",
            prompt="done",
        ))
        s.add(AgentRun(
            idempotency_key="live",
            status="RUNNING",
            prompt="running",
        ))
        await s.commit()

    assert await _resolve_run_id(0) is None
    assert await _resolve_run_id(99999) is None
    resolved_old = await _resolve_run_id(1)
    resolved_live = await _resolve_run_id(2)
    assert resolved_old == 1
    assert resolved_live == 2
    # No fallback: mistyped/zero must not resolve to the RUNNING row.
    assert await _resolve_run_id(0) != 2

    os.remove(path)
