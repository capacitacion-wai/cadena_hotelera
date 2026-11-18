"""The profiling module's store: the app's OWN SQLite (`app.app_database.app_engine`).

INVARIANT FILE — installed by `wai-app-builder/assets/modulos.py`; do not edit it.

The module's tables are NOT SQLAlchemy models of the app (`AppBase`): they are created and evolved
by the module's numbered migrations, so the test routes that drop and recreate the app's tables to
load a data set (`pruebas_router.py`) never wipe who can use the app.

Writes go through ONE lock per process (SQLite has a single writer; the app's engine sets WAL and
busy_timeout where it follows patterns/local-app-db.md).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import text

logger = logging.getLogger(__name__)

MODULO = "perfilado"
CARPETA_MIGRACIONES = Path(__file__).resolve().with_name("migraciones")

_motor: Any = None
_listo = False
_preparando = asyncio.Lock()
ESCRITURA = asyncio.Lock()


def usar_motor(motor: Any) -> None:
    """Uses another engine (tests; None = the app's again). Resets the «prepared» state and the locks
    (an asyncio lock that once waited stays bound to that test's event loop)."""
    global _motor, _listo, _preparando, ESCRITURA
    from .. import migraciones
    _motor, _listo = motor, False
    _preparando, ESCRITURA = asyncio.Lock(), asyncio.Lock()
    migraciones._CERROJO = asyncio.Lock()


def motor() -> Any:
    global _motor
    if _motor is None:
        from app.app_database import app_engine  # the app's own SQLite (storage.local_db)
        _motor = app_engine
    return _motor


def ahora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dueno() -> str:
    """The app owner's email (`APP_OWNER_EMAIL`, set by the deployer): ALWAYS an administrator."""
    return (os.environ.get("APP_OWNER_EMAIL") or "").strip().lower()


def en_modo_de_pruebas() -> bool:
    """The test runner (`WAI_MODO_PRUEBAS=1`) and the live view (`WAI_DATOS_DE_EJEMPLO=1`)."""
    return os.environ.get("WAI_MODO_PRUEBAS") == "1" or os.environ.get("WAI_DATOS_DE_EJEMPLO") == "1"


async def preparar() -> None:
    """Migrations + the rows the module guarantees: the owner's record and, in test mode, the test
    users (`<profile>@pruebas.wai`). Idempotent; cheap after the first call."""
    global _listo
    if _listo:
        return
    async with _preparando:
        if _listo:
            return
        from ..migraciones import migrar
        await migrar(motor(), MODULO, CARPETA_MIGRACIONES)
        await asegurar_columnas_de_dueno()
        await importar_adopcion()
        if dueno():
            await asegurar_usuario(dueno(), ["admin"], origen="despliegue", reactivar=True)
        if en_modo_de_pruebas():
            await restaurar_usuarios_de_prueba()
        _listo = True


RX_TABLA = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


async def asegurar_columnas_de_dueno() -> None:
    """Level 3: every table «with an owner» that ALREADY exists gets its `dueno` and `equipo` columns
    (nullable, indexed) if it lacks them — `create_all` never adds a column to an existing table, and
    an app raised from level 2 has its rows there. Level 4: every table «of the organisation» gets its
    `organizacion` column the same way. A new table gets them from its model. Rows left without an
    owner / organisation are logged, never assigned (an administrator does it,
    `servicio.asignar_sin_dueno` / `servicio.asignar_sin_organizacion`)."""
    from . import permisos
    try:
        if permisos.nivel() < 3:
            return
        columnas: Dict[str, List[str]] = {}
        for x in permisos.datos_con_dueno():
            columnas.setdefault(str(x["tabla"]), []).extend([permisos.columna(str(x["tabla"])), permisos.COLUMNA_EQUIPO])
        if permisos.nivel() >= 4:
            for x in permisos.datos_de_la_organizacion():
                columnas.setdefault(str(x["tabla"]), []).append(permisos.COLUMNA_ORGANIZACION)
    except permisos.ConfiguracionInvalida:
        return   # the routes say it (permisos.json missing); nothing to evolve without it
    for tabla, cs in columnas.items():
        if not RX_TABLA.match(tabla):
            continue
        sin: Dict[str, int] = {}
        async with ESCRITURA:
            async with motor().begin() as con:
                cols = {r[1] for r in (await con.exec_driver_sql(f'PRAGMA table_info("{tabla}")')).all()}
                if not cols:
                    continue   # not created yet: its model brings the columns
                for c in dict.fromkeys(cs):
                    if c not in cols:
                        await con.exec_driver_sql(f'ALTER TABLE "{tabla}" ADD COLUMN {c} TEXT')
                        logger.info("profiling: column %s added to %s", c, tabla)
                    await con.exec_driver_sql(f'CREATE INDEX IF NOT EXISTS "ix_{tabla}_{c}" ON "{tabla}" ({c})')
                for clave, c in ((permisos.COLUMNA_DUENO, permisos.columna(tabla)),
                                 (permisos.COLUMNA_ORGANIZACION, permisos.COLUMNA_ORGANIZACION)):
                    if c in cs:
                        sin[clave] = (await con.exec_driver_sql(
                            f"SELECT COUNT(*) FROM \"{tabla}\" WHERE \"{c}\" IS NULL OR trim(\"{c}\") = ''")).scalar() or 0
        if sin.get(permisos.COLUMNA_DUENO):
            logger.warning("profiling: %s rows of %s have no owner (visible only with scope «todo»): an "
                           "administrator assigns them (GET /api/perfilado/sin-dueno)", sin[permisos.COLUMNA_DUENO], tabla)
        if sin.get(permisos.COLUMNA_ORGANIZACION):
            logger.warning("profiling: %s rows of %s have no organisation (visible only to internal people who reach "
                           "every organisation): an administrator assigns them (GET /api/perfilado/sin-organizacion)",
                           sin[permisos.COLUMNA_ORGANIZACION], tabla)


async def importar_adopcion() -> int:
    """An app that had its OWN users and roles adopted them (`modulos.py adoptar … --aplicar`,
    permisos.json → `adopcion`): its old user table is imported into `perfilado_usuarios` ONCE (recorded
    in `perfilado_adopciones`), each row with the module role its old value maps to. Idempotent and
    additive: someone already registered is left as they are (INSERT OR IGNORE), the old table is never
    touched or dropped (it keeps the business data), a value without a mapping is skipped and logged —
    never given a role by guess. Until the old table exists (a fresh database) nothing is recorded, so
    the import happens on the first start-up that finds it. Returns the rows imported now."""
    from . import permisos
    try:
        ad = permisos.adopcion()
        roles_validos = set(permisos.ids_de_roles())
    except permisos.ConfiguracionInvalida:
        return 0
    if not ad:
        return 0
    tabla, c_email, c_nombre, c_rol = ad.get("tabla"), ad.get("email"), ad.get("nombre"), ad.get("rol")
    if not all(isinstance(x, str) and RX_TABLA.match(x) for x in (tabla, c_email)):
        return 0
    async with ESCRITURA:
        async with motor().begin() as con:
            hecho = (await con.exec_driver_sql("SELECT 1 FROM perfilado_adopciones WHERE tabla = ?", (tabla,))).first()
            if hecho:
                return 0
            cols = {r[1] for r in (await con.exec_driver_sql(f'PRAGMA table_info("{tabla}")')).all()}
            if c_email not in cols:
                return 0   # not created yet (a fresh database): try again on the next start-up
            sel = ", ".join(f'"{c}"' if isinstance(c, str) and RX_TABLA.match(c) and c in cols else "NULL"
                            for c in (c_email, c_nombre, c_rol))
            filas = (await con.exec_driver_sql(f'SELECT {sel} FROM "{tabla}"')).all()
            t = ahora()
            n, sin_rol = 0, []
            for email, nombre, valor in filas:
                e = str(email or "").strip().lower()
                if not e:
                    continue
                v = str(valor).lower() if isinstance(valor, (bool, int)) else (str(valor) if valor is not None else "")
                rol = ad["roles"].get(v) if v else ad.get("por_defecto")
                if rol not in roles_validos:
                    sin_rol.append(e)
                    continue
                r = await con.exec_driver_sql(
                    "INSERT OR IGNORE INTO perfilado_usuarios (email, nombre, roles, activo, origen, creado_por, creado_en, "
                    "actualizado_en) VALUES (?, ?, ?, 1, 'adopcion', 'wai', ?, ?)", (e, nombre, json.dumps([rol]), t, t))
                n += r.rowcount or 0
            await con.exec_driver_sql("INSERT INTO perfilado_adopciones (tabla, filas, importada_en) VALUES (?, ?, ?)",
                                      (tabla, n, t))
    logger.info("profiling: adopted %s user(s) from %s", n, tabla)
    if sin_rol:
        logger.warning("profiling: %s user(s) of %s were not imported: their role has no mapping (%s…); an "
                       "administrator adds them in «Users»", len(sin_rol), tabla, ", ".join(sin_rol[:5]))
    return n


async def asegurar_usuario(email: str, roles: List[str], *, origen: str, reactivar: bool,
                           nombre: Optional[str] = None, sesion: Any = None, equipo: Any = None,
                           organizacion: Any = None) -> None:
    """Creates the record if it does not exist (never duplicates under concurrency: INSERT OR IGNORE)
    and, with `reactivar`, leaves it active with EXACTLY those roles (and, if given, that team and
    that organisation — "" = internal)."""
    e = email.strip().lower()
    t = ahora()
    sentencias = [(
        "INSERT OR IGNORE INTO perfilado_usuarios (email, nombre, roles, activo, origen, creado_por, creado_en, "
        "actualizado_en) VALUES (:e, :n, :r, 1, :o, 'wai', :t, :t)",
        {"e": e, "n": nombre, "r": json.dumps(roles), "o": origen, "t": t})]
    if reactivar:
        sentencias.append(("UPDATE perfilado_usuarios SET roles = :r, activo = 1, actualizado_en = :t WHERE email = :e",
                           {"e": e, "r": json.dumps(roles), "t": t}))
    if reactivar and equipo is not None:
        sentencias.append(("UPDATE perfilado_usuarios SET equipo = :q WHERE email = :e", {"e": e, "q": equipo or None}))
    if reactivar and organizacion is not None:
        sentencias.append(("UPDATE perfilado_usuarios SET organizacion = :o WHERE email = :e", {"e": e, "o": organizacion or None}))
    await escribir(sentencias, sesion=sesion)


async def restaurar_usuarios_de_prueba(sesion: Any = None) -> int:
    """Leaves every test user of `permisos.json` active with its declared roles. Called at start-up in
    test mode, and by each data set of `datos_de_ejemplo.py` (pass its session, so it goes in the
    same transaction): a scenario that deactivated one does not leak into the next."""
    from . import permisos
    if sesion is not None:
        await preparar()
    t = ahora()
    # Level 3: the test teams first (a test user points at one), each active again.
    await escribir([("INSERT OR IGNORE INTO perfilado_equipos (id, nombre, activo, creado_por, creado_en, actualizado_en) "
                     "VALUES (:i, :n, 1, 'wai', :t, :t)", {"i": q["id"], "n": q.get("nombre") or q["id"], "t": t})
                    for q in permisos.equipos_de_prueba()]
                   + [("UPDATE perfilado_equipos SET activo = 1 WHERE id = :i", {"i": q["id"]})
                      for q in permisos.equipos_de_prueba()], sesion=sesion)
    # Level 4: the test organisations, each active again (a test user of an organisation points at one).
    await escribir([("INSERT OR IGNORE INTO perfilado_organizaciones (id, nombre, activo, creado_por, creado_en, "
                     "actualizado_en) VALUES (:i, :n, 1, 'wai', :t, :t)", {"i": o["id"], "n": o.get("nombre") or o["id"], "t": t})
                    for o in permisos.organizaciones_de_prueba()]
                   + [("UPDATE perfilado_organizaciones SET activo = 1 WHERE id = :i", {"i": o["id"]})
                      for o in permisos.organizaciones_de_prueba()], sesion=sesion)
    usuarios = permisos.usuarios_de_prueba()
    for u in usuarios:
        await asegurar_usuario(u["email"], list(u.get("roles") or []), origen="pruebas", reactivar=True,
                               sesion=sesion, equipo=u.get("equipo") or "",
                               organizacion=(u.get("organizacion") or "") if permisos.nivel() >= 4 else None)
    return len(usuarios)


async def leer(sql: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    async with motor().connect() as con:
        return [dict(f) for f in (await con.execute(text(sql), params or {})).mappings().all()]


async def escribir(sentencias: Iterable[tuple], sesion: Any = None, con_cerrojo: bool = True) -> None:
    """Runs the statements in ONE transaction, committed before returning. With `sesion` (an
    `AsyncSession` of the caller) they join the caller's transaction and the caller commits.
    `con_cerrojo=False` only for a caller that ALREADY holds `ESCRITURA` (a check-then-write that
    must not interleave with another writer: the last administrator)."""
    if sesion is not None:
        for sql, params in sentencias:
            await sesion.execute(text(sql), params)
        return
    if not con_cerrojo:
        async with motor().begin() as con:
            for sql, params in sentencias:
                await con.execute(text(sql), params)
        return
    async with ESCRITURA:
        async with motor().begin() as con:
            for sql, params in sentencias:
                await con.execute(text(sql), params)
