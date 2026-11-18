"""Numbered migrations of the platform modules, in the app's own SQLite.

INVARIANT FILE — installed by `wai-app-builder/assets/modulos.py`; do not edit it.

Why not `create_all`: without an applied version number nobody can know which state an
installation is in, and a module that grows (a column for teams, a table for organisations) needs
to evolve the tables of apps that are already running with data.

Contract:
  * each module keeps its migrations in `app/wai/<id>/migraciones/NNNN_<name>.sql`
    (four digits, applied in order, never renumbered, never edited once released);
  * statements are separated by `;` at the end of a line;
  * every migration is IDEMPOTENT (`CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`,
    `INSERT OR IGNORE`). SQLite has no `ADD COLUMN IF NOT EXISTS`: an `ALTER TABLE … ADD COLUMN`
    whose column already exists is tolerated here (a run that died after the DDL and before
    recording the version is re-run safely);
  * what was applied is recorded in `wai_modulos_migraciones (modulo, version, nombre, aplicada)`.

`migrar` is safe to call on every start-up and from several requests at once (one lock per
process; the DDL is idempotent across processes).
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Tuple

from sqlalchemy import text

logger = logging.getLogger(__name__)

TABLA = "wai_modulos_migraciones"
_CREAR_TABLA = ("CREATE TABLE IF NOT EXISTS wai_modulos_migraciones (modulo TEXT NOT NULL, version INTEGER NOT NULL, "
                "nombre TEXT NOT NULL, aplicada TEXT NOT NULL, PRIMARY KEY (modulo, version))")
RX_FICHERO = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
_CERROJO = asyncio.Lock()


def pendientes_de(carpeta: Path) -> List[Tuple[int, str, Path]]:
    """(version, name, file) of every migration file of a module, in order."""
    out = []
    for f in sorted(Path(carpeta).glob("*.sql")):
        m = RX_FICHERO.match(f.name)
        if m:
            out.append((int(m.group(1)), f.stem, f))
    return out


def sentencias(sql: str) -> List[str]:
    """The statements of a migration file: split by `;` at the end of a line, comments removed."""
    limpio = "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--"))
    return [s.strip() for s in re.split(r";\s*$", limpio, flags=re.M) if s.strip()]


async def migrar(motor: Any, modulo: str, carpeta: Path) -> List[int]:
    """Applies the pending migrations of `modulo` (from `carpeta`) on the engine `motor`.
    Returns the versions applied in this call (empty = already up to date)."""
    aplicadas: List[int] = []
    async with _CERROJO:
        async with motor.begin() as con:
            await con.execute(text(_CREAR_TABLA))
            hechas = {r[0] for r in (await con.execute(
                text("SELECT version FROM wai_modulos_migraciones WHERE modulo = :m"), {"m": modulo})).all()}
        for version, nombre, fichero in pendientes_de(carpeta):
            if version in hechas:
                continue
            async with motor.begin() as con:
                for s in sentencias(fichero.read_text(encoding="utf-8")):
                    try:
                        await con.exec_driver_sql(s)
                    except Exception as exc:  # noqa: BLE001 — only the re-run ADD COLUMN is tolerated
                        if "duplicate column name" in str(exc).lower():
                            continue
                        raise
                await con.execute(text(
                    "INSERT OR IGNORE INTO wai_modulos_migraciones (modulo, version, nombre, aplicada) "
                    "VALUES (:m, :v, :n, :a)"),
                    {"m": modulo, "v": version, "n": nombre, "a": datetime.now(timezone.utc).isoformat()})
            aplicadas.append(version)
            logger.info("module %s: migration %s applied", modulo, nombre)
    return aplicadas


async def version_aplicada(motor: Any, modulo: str) -> int:
    """The highest applied version of `modulo` (0 = none)."""
    async with motor.begin() as con:
        await con.execute(text(_CREAR_TABLA))
        fila = (await con.execute(text("SELECT MAX(version) FROM wai_modulos_migraciones WHERE modulo = :m"),
                                  {"m": modulo})).first()
    return int(fila[0] or 0) if fila else 0
