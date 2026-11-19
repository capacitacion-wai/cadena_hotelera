"""A 'sent' communication ALWAYS leaves a delivery record (AGO-83).

Real regression (demo, 2026-07): the outbox piled up 8 `sent` communications without
`sent_at` or `delivery_id` — "it says it went out, but not when nor with which id". Two
holes that covered for each other:

  * the agent can register a communication ALREADY AS `sent` (it delivered it itself in
    the pass, with a `notify` block), and nothing was stamped on creation;
  * the reconcile that DOES stamp only looked at `draft`/`ready` rows, so those never
    went through it.

Contract: if the status says `sent`, there is a trace. No exceptions.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path


def test_sent_communication_always_carries_delivery_trace():
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def _run(db: str):
        eng = create_async_engine(f"sqlite+aiosqlite:///{db}")
        async with eng.begin() as conn:
            await conn.execute(text(
                "CREATE TABLE communications (id INTEGER PRIMARY KEY, channel TEXT, "
                "recipient TEXT, status TEXT, sent_at TEXT, delivery_id TEXT, error TEXT, "
                "created_at TEXT)"))
            # The tenant's REAL rows: 'sent' without a trace (the ones that make AGO-83 fail).
            await conn.execute(text(
                "INSERT INTO communications (id,channel,recipient,status,created_at) VALUES "
                "(37,'internal','operator','sent','2026-07-13T14:00'),"
                "(36,'email','demo@waiworking.com','sent','2026-07-13T13:00'),"
                "(35,'email','demo@waiworking.com','failed','2026-07-13T12:00'),"
                "(33,'internal','operator','draft','2026-07-13T11:00')"))
        from app.main import _backfill_communication_delivery_trace
        async with eng.begin() as conn:
            await _backfill_communication_delivery_trace(conn)
            await _backfill_communication_delivery_trace(conn)   # idempotent
        async with eng.begin() as conn:
            filas = (await conn.execute(text(
                "SELECT id, status, sent_at, delivery_id FROM communications ORDER BY id"
            ))).mappings().all()
        await eng.dispose()
        return {f["id"]: dict(f) for f in filas}

    with tempfile.TemporaryDirectory() as tmp:
        filas = asyncio.run(_run(str(Path(tmp) / "app.sqlite")))

    # Every 'sent' ends up with a trace…
    for fid in (36, 37):
        assert filas[fid]["status"] == "sent"
        assert filas[fid]["sent_at"], f"id={fid}: 'sent' sin sent_at"
        assert filas[fid]["delivery_id"], f"id={fid}: 'sent' sin delivery_id"
    # …and for what was not sent, no delivery is invented.
    assert filas[35]["sent_at"] is None and filas[35]["delivery_id"] is None
    assert filas[33]["sent_at"] is None and filas[33]["delivery_id"] is None
