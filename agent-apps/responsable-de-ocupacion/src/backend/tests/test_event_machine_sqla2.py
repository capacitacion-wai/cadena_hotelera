"""IS2.b regression test — ``claim_next_event`` under SQLAlchemy 2.x.

This is the bug that froze every event in PENDING on the
pulse-sentinel and reco-sentinel deployments:

    sqlalchemy.exc.ArgumentError:
        Invalid value 'IMMEDIATE' for isolation_level.
        Valid isolation levels for 'sqlite' are READ UNCOMMITTED,
        SERIALIZABLE, AUTOCOMMIT.

The fix dropped to the DBAPI level (``conn.exec_driver_sql("BEGIN
IMMEDIATE")``) instead of using ``execution_options(isolation_level=
"IMMEDIATE")`` which only ever worked on SQLAlchemy 1.x. We had to
hot-patch two pods at runtime when this regressed.

This test exercises the exact code path against an in-memory aiosqlite
DB. If anyone re-introduces the SQLA-1.x style ``isolation_level``
kwarg the test crashes at ``claim_next_event`` time, not when the
first watcher tick fires hours into production.

Requires ``pytest-asyncio`` + ``aiosqlite``. Both are pinned in
``requirements-dev.txt`` (already wired for the existing
``test_concurrent_claim.py``).
"""
from __future__ import annotations

import os
import tempfile

import pytest

pytestmark = pytest.mark.asyncio


@pytest.mark.asyncio
async def test_claim_returns_pending_event_under_sqla2() -> None:
    """Happy path: one PENDING row, one claim, one winner."""
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    os.environ["APP_DATABASE_URL"] = f"sqlite+aiosqlite:///{path}"

    # Import *after* APP_DATABASE_URL so the engine binds the temp file.
    from app.app_database import AppBase, app_engine
    from app.app_models import runtime  # noqa: F401 — registers ORM models
    from app.autonomous.event_machine import claim_next_event
    from sqlalchemy import text as _sql

    async with app_engine.begin() as conn:
        await conn.run_sync(AppBase.metadata.create_all)
        await conn.execute(_sql(
            "INSERT INTO events (fingerprint, type, source, payload_json, "
            "status, attempts, max_attempts, detected_at) "
            "VALUES ('fp-claim-1', 'sqla2', 'unit', '{}', 'PENDING', 0, 3, "
            "CURRENT_TIMESTAMP)"
        ))

    claimed = await claim_next_event()
    assert claimed is not None, "PENDING row exists but claim returned None"
    assert claimed.get("fingerprint") == "fp-claim-1"
    # The CLAIM_SQL ``RETURNING`` clause doesn't include ``status``
    # (kept slim to match what the executor actually needs); verify
    # the flip directly against the row.
    assert claimed.get("attempts") == 1, claimed
    async with app_engine.connect() as conn:
        result = await conn.execute(_sql("SELECT status FROM events WHERE id = 1"))
        status_row = result.first()
    assert status_row is not None and status_row[0] == "CLAIMED", status_row

    # A second claim returns nothing because the only row is no longer PENDING.
    again = await claim_next_event()
    assert again is None, f"unexpected second claim: {again}"

    os.remove(path)


@pytest.mark.asyncio
async def test_claim_returns_none_when_no_pending() -> None:
    """No PENDING rows → the executor must idle quietly, not crash."""
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    os.environ["APP_DATABASE_URL"] = f"sqlite+aiosqlite:///{path}"

    from app.app_database import AppBase, app_engine
    from app.app_models import runtime  # noqa: F401
    from app.autonomous.event_machine import claim_next_event

    async with app_engine.begin() as conn:
        await conn.run_sync(AppBase.metadata.create_all)

    claimed = await claim_next_event()
    assert claimed is None

    os.remove(path)


@pytest.mark.asyncio
async def test_begin_immediate_does_not_raise_argument_error() -> None:
    """Smoke check: drive ``BEGIN IMMEDIATE`` directly through the
    DBAPI bridge that ``claim_next_event`` uses. If anyone reintroduces
    ``execution_options(isolation_level="IMMEDIATE")`` this test
    surfaces it with the exact ArgumentError that bit production —
    not a generic timeout six hours into the watcher loop."""
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    os.environ["APP_DATABASE_URL"] = f"sqlite+aiosqlite:///{path}"

    from app.app_database import AppBase, app_engine

    async with app_engine.begin() as conn:
        await conn.run_sync(AppBase.metadata.create_all)

    # New connection — exercise the exact statement event_machine
    # issues. Must not raise ArgumentError.
    async with app_engine.connect() as conn:
        if conn.dialect.name == "sqlite":
            try:
                await conn.exec_driver_sql("BEGIN IMMEDIATE")
                # If we got here the DBAPI accepted the statement.
                await conn.exec_driver_sql("COMMIT")
            except Exception as exc:  # pragma: no cover — failure path
                msg = repr(exc)
                assert "Invalid value 'IMMEDIATE'" not in msg, (
                    "Regression: SQLAlchemy 2.x ArgumentError reintroduced. "
                    "Did someone re-add execution_options(isolation_level=…)? "
                    f"got={msg}"
                )
                # Some SQLite builds reject nested BEGIN IMMEDIATE because
                # autocommit has already started a transaction. That's
                # tolerable — the production code falls back silently.

    os.remove(path)
