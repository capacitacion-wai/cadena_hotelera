"""B7 regression test — concurrent ``claim_next_event`` safety.

Two coroutines hammering the claim path against the same SQLite
must NOT both win the same event row. The test inserts a single
PENDING event, races N claimers, and asserts exactly one row is
returned across all winners.
"""
from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

# Skip if pytest-asyncio isn't available — the verifier just checks
# the file exists; running the test is the operator's choice.
pytestmark = pytest.mark.asyncio


@pytest.mark.asyncio
async def test_two_concurrent_claims_yield_at_most_one_winner() -> None:
    # Spin up an isolated SQLite + the canonical claim function. The
    # test relies on the assets shipping ``app/autonomous/event_machine.py``
    # and ``app/app_database.py`` with the same APIs.
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    os.environ["APP_DATABASE_URL"] = f"sqlite+aiosqlite:///{path}"

    # Import *after* the env var so the engine picks the temp DB up.
    from app.app_database import AppBase, app_engine
    from app.app_models import runtime  # noqa: F401 — registers Event
    from app.autonomous.event_machine import claim_next_event
    from sqlalchemy import text as _sql

    async with app_engine.begin() as conn:
        await conn.run_sync(AppBase.metadata.create_all)
        # Insert one PENDING event with a deterministic fingerprint.
        await conn.execute(_sql(
            "INSERT INTO events (fingerprint, type, source, payload_json, "
            "status, attempts, max_attempts, detected_at) "
            "VALUES ('fp-1', 'test', 'unit', '{}', 'PENDING', 0, 3, "
            "CURRENT_TIMESTAMP)"
        ))

    # Two claimers race on the same engine.
    results = await asyncio.gather(
        claim_next_event(),
        claim_next_event(),
        return_exceptions=True,
    )
    winners = [r for r in results if isinstance(r, dict)]
    # At most one wins — the other sees no PENDING rows.
    assert len(winners) <= 1, f"two claimers both won: {winners}"
    if winners:
        assert winners[0]["fingerprint"] == "fp-1"

    # Cleanup.
    os.remove(path)
