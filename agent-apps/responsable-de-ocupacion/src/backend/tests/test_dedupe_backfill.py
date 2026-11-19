"""The canonical-key backfill converges and merges duplicates — on SQLITE (the app's real
engine) and with the REAL SCHEMA of each table.

Two chained real regressions, both from not exercising the backfill against the real
app:

  * v7.107.0 carried the `if dialect != "sqlite": return` guard inherited from the old
    migrations — harmless here, but I misdiagnosed the engine.
  * The SELECT asked for `root_cause` in BOTH tables… and `root_cause` ONLY exists in
    `alerts` (the `Recommendation` model does not have it). On recommendations the SELECT
    blew up, the `except` swallowed it as "table not available" and the backfill skipped
    **silently exactly the table that had the duplicates**. The app was deployed, said
    `skill_version 7.107.0`… and the duplicates were still there.

This test uses the REAL schema (recommendations WITHOUT root_cause, alerts WITH it) and the
TWO real rows from the demo tenant.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path


def test_backfill_converges_and_merges_on_sqlite():
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def _run(db_path: str):
        eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        async with eng.begin() as conn:
            # REAL SCHEMA: recommendations has NO root_cause; alerts DOES.
            await conn.execute(text(
                "CREATE TABLE recommendations (id INTEGER PRIMARY KEY, title TEXT, "
                "status TEXT, dedupe_key TEXT, target_entity_kind TEXT, "
                "target_entity_id TEXT, occurrences INTEGER DEFAULT 1, created_at TEXT, "
                "metadata_json TEXT)"))
            await conn.execute(text(
                "CREATE TABLE alerts (id INTEGER PRIMARY KEY, title TEXT, status TEXT, "
                "dedupe_key TEXT, root_cause TEXT, target_entity_kind TEXT, "
                "target_entity_id TEXT, occurrences INTEGER DEFAULT 1, created_at TEXT, "
                "metadata_json TEXT)"))
            # The two REAL rows from the demo tenant (INC-AGO-137).
            await conn.execute(text(
                "INSERT INTO recommendations (id,title,status,dedupe_key,target_entity_kind,"
                "target_entity_id,occurrences,created_at) VALUES "
                "(6,'Revisar reposición','open','reposicion:product.product:21:warehouse:1',"
                "'product.product','21',2,'2026-07-13T13:03'),"
                "(5,'Definir la reposición','open',"
                "'reabastecimiento-sin-regla:warehouse:1:product:21','product.product','21',1,"
                "'2026-07-13T09:00'),"
                "(1,'Presupuesto S00003','open','sale.order:5','sale.order','5',1,"
                "'2026-07-11T19:29')"))
            # An alert with root_cause: two different risks on the SAME target
            # must NOT be merged (only recommendations collapse by target).
            await conn.execute(text(
                "INSERT INTO alerts (id,title,status,dedupe_key,root_cause,"
                "target_entity_kind,target_entity_id,occurrences,created_at) VALUES "
                "(1,'Stock bajo','open','stock-bajo',NULL,'product.product','21',1,'2026-07-13T10:00'),"
                "(2,'Margen negativo','open','margen-negativo',NULL,'product.product','21',1,"
                "'2026-07-13T11:00')"))

        from app.main import _backfill_canonical_dedupe_keys
        async with eng.begin() as conn:
            await _backfill_canonical_dedupe_keys(conn)
            await _backfill_canonical_dedupe_keys(conn)   # idempotent

        async with eng.begin() as conn:
            recs = (await conn.execute(text(
                "SELECT id, status, dedupe_key, occurrences, metadata_json "
                "FROM recommendations ORDER BY id"))).mappings().all()
            alerts = (await conn.execute(text(
                "SELECT id, status, dedupe_key FROM alerts ORDER BY id"))).mappings().all()
        await eng.dispose()
        return {r["id"]: dict(r) for r in recs}, {a["id"]: dict(a) for a in alerts}

    with tempfile.TemporaryDirectory() as tmp:
        recs, alerts = asyncio.run(_run(str(Path(tmp) / "app.sqlite")))

    # Recommendations: the OLDEST (id=5) survives with the reappearances added up…
    assert recs[5]["status"] == "open"
    assert recs[5]["dedupe_key"] == "product.product:21"
    assert recs[5]["occurrences"] == 3            # 1 + 2
    # …and the other is absorbed, recording into which one.
    assert recs[6]["status"] == "dismissed"
    assert json.loads(recs[6]["metadata_json"])["merged_into"] == 5
    # Another target: untouched.
    assert recs[1]["status"] == "open" and recs[1]["dedupe_key"] == "sale.order:5"
    # The AGO-137 invariant: ONE single open recommendation per target.
    assert len([r for r in recs.values()
                if r["status"] == "open" and r["dedupe_key"] == "product.product:21"]) == 1

    # Alerts: two DIFFERENT risks on the same target are NOT merged, and both
    # gain the canonical target (AGO-138 invariant).
    assert alerts[1]["status"] == "open" and alerts[1]["dedupe_key"] == "stock-bajo:product.product:21"
    assert alerts[2]["status"] == "open" and alerts[2]["dedupe_key"] == "margen-negativo:product.product:21"
