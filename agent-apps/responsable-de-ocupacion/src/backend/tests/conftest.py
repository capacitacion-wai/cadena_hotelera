"""Pytest bootstrap for the backend tests.

``app.config`` raises at *import* time if ``wai-app.json`` cannot be
found in any of the well-known paths. That's the right behaviour
inside the container (we want the pod to crash if the manifest is
missing) but it makes ``pytest tests/`` from a clean checkout fail
during collection.

This conftest:

  1. Writes a minimal valid ``wai-app.json`` to a temp file and
     points ``WAI_APP_JSON_PATH`` at it. Each test still gets a
     pristine SQLite via its own monkeypatch — the fixture only
     unblocks the import chain.
  2. Overrides ``APP_DB_PATH`` / ``APP_DATABASE_URL`` to an in-memory
     SQLite by default so test files that don't bother setting their
     own DB still get an isolated one.
  3. Sets ``OPENCODE_DEFAULT_MODEL=""`` so the model-resolver test
     starts from a known-empty world.

It runs as a pytest plugin hook (``pytest_configure``) which fires
BEFORE the first test module is collected, so we can safely shim
``os.environ`` before any ``app.*`` import.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import text as _text
import json
import os
import tempfile
from pathlib import Path

import pytest


_FIXTURE_MANIFEST: dict = {
    "agent_app": {
        "name": "test_app",
        "version": "0.0.1",
        "watchers": [],
        "event_type_specs": [],
        "actions": [],
        "tools": [],
    }
}


def pytest_configure(config) -> None:  # noqa: D401 — pytest hook
    # 1. Manifest fixture. Only set if the operator hasn't provided
    # one already (so the verifier can point us at a generated app's
    # real manifest if it wants to).
    if not os.environ.get("WAI_APP_JSON_PATH"):
        fd, path = tempfile.mkstemp(prefix="wai-app-", suffix=".json")
        os.close(fd)
        Path(path).write_text(
            json.dumps(_FIXTURE_MANIFEST, ensure_ascii=False),
            encoding="utf-8",
        )
        os.environ["WAI_APP_JSON_PATH"] = path

    # 2. Default SQLite — individual tests still override this, but
    # the import chain (which opens the engine module-level) needs
    # *something*.
    if not os.environ.get("APP_DATABASE_URL"):
        fd, path = tempfile.mkstemp(prefix="app-", suffix=".sqlite")
        os.close(fd)
        os.environ["APP_DATABASE_URL"] = f"sqlite+aiosqlite:///{path}"
        os.environ["APP_DB_PATH"] = path

    # 2.bis Project read-only Postgres — ``app.database`` builds its
    # engine at module-load time and SQLAlchemy refuses an empty URL.
    # ``create_async_engine`` does NOT connect, so a syntactically
    # valid Postgres URL with no live server is enough; the tested
    # contracts never touch the project DB.
    if not os.environ.get("DATABASE_URL"):
        os.environ["DATABASE_URL"] = (
            "postgresql+asyncpg://user:pass@127.0.0.1:5432/test_db"
        )
    # Mirror the individual DB_* fields too — config.py uses them
    # when ``DATABASE_URL`` is unset elsewhere in the codebase.
    os.environ.setdefault("DB_HOST", "127.0.0.1")
    os.environ.setdefault("DB_PORT", "5432")
    os.environ.setdefault("DB_NAME", "test_db")
    os.environ.setdefault("DB_USER", "user")
    os.environ.setdefault("DB_PASSWORD", "pass")
    os.environ.setdefault("DB_SCHEMA", "public")

    # 3. Reset the model resolver's view of the world so tests start
    # from clean defaults. Tests that need a specific value use
    # ``monkeypatch.setenv``.
    os.environ.pop("OPENCODE_DEFAULT_MODEL", None)
    os.environ.pop("AGENT_AUTONOMOUS_MODEL", None)
    os.environ.pop("AGENT_INTERACTIVE_MODEL", None)


#: The schema is created once per session; see `_reset`.
_ESQUEMA_LISTO = False
#: Does `sqlite_sequence` exist? It only does if some table uses AUTOINCREMENT.
_HAY_SEQUENCE = False


@pytest.fixture(autouse=True)
def _isolate_app_db():
    """Give EVERY test a pristine local DB.

    ``app.app_database`` opens ``app_engine`` at MODULE-IMPORT time from
    ``APP_DATABASE_URL``, so a test setting ``os.environ["APP_DATABASE_URL"]`` AFTER
    import does NOT get a new engine — all tests share the one engine bound to the
    conftest default file. Left unchecked, rows leak across tests and the full
    suite hits unique/FK collisions ("passes alone, fails together"). This
    autouse fixture DROPs+CREATEs all tables on that shared engine before each test
    (and disposes the pool so no connection is bound to this fixture's loop),
    restoring true per-test isolation without reworking the global engine.

    Sync fixture on purpose (runs for async AND sync tests); it drives the async
    reset on its own throwaway loop, then disposes so the test's own event loop
    opens fresh connections.
    """
    try:
        from app.app_database import AppBase, app_engine
        import app.app_models  # noqa: F401 — register every model on the metadata
        # EVERY module of the package, not only the ones `app_models/__init__.py` lists: that
        # file is the employee's SCAFFOLD, so an employee created before a skill-owned model
        # existed does not import it, and its tables were missing from the test schema (14.14:
        # `push_devices`, five unrelated tests failed in an evolved employee). In production
        # `main.py` imports them; here the schema is built before anyone does.
        import importlib, pkgutil
        for _m in pkgutil.iter_modules(app.app_models.__path__):
            importlib.import_module(f"app.app_models.{_m.name}")
    except Exception:
        # A test that doesn't need the app DB (or an import-time failure unrelated
        # to isolation) must not be blocked by this helper.
        yield
        return

    async def _reset() -> None:
        # THE SCHEMA IS CREATED ONCE; each test only EMPTIES the tables.
        #
        # This used to do `drop_all` + `create_all` in EVERY test. With ~30 tables that is
        # some sixty DDL statements per test, and the fixture is `autouse`: 641
        # tests × that. Measured over the whole suite: 92 % of the total time went
        # into `setup` and only 8 % into running the tests. Deleting the rows gives
        # exactly the same isolation —a clean database— without rebuilding the
        # schema, which does not change between tests.
        global _ESQUEMA_LISTO, _HAY_SEQUENCE
        async with app_engine.begin() as conn:
            if not _ESQUEMA_LISTO:
                await conn.run_sync(AppBase.metadata.drop_all)
                await conn.run_sync(AppBase.metadata.create_all)
                _ESQUEMA_LISTO = True
                _HAY_SEQUENCE = bool((await conn.execute(_text(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='sqlite_sequence'"))).first())
            else:
                # In reverse dependency order, because of the foreign keys.
                for tabla in reversed(AppBase.metadata.sorted_tables):
                    await conn.execute(_text(f'DELETE FROM "{tabla.name}"'))
                # `drop_all` also reset the AUTOINCREMENT counters;
                # `DELETE` does not. Without this, a test expecting id 1 passes alone and
                # fails in company — the same "passes alone, fails together"
                # this fixture exists to prevent.
                await conn.execute(_text(
                    "DELETE FROM sqlite_sequence"
                )) if _HAY_SEQUENCE else None
        await app_engine.dispose()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_reset())
    finally:
        loop.close()
    yield
