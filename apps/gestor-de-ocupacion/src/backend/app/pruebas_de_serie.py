"""BUILT-IN TESTING: every WAI application can be tested against itself, in production, and leaves nothing behind.

A BASE FILE (`base_files.txt`): copied as is to `src/backend/app/pruebas_de_serie.py`, never edited. `main.py` calls
`pruebas_de_serie.instalar(app)` ALWAYS (not only in test mode), right after creating `app` and including the routers.

Why it exists: an agent (or a connector) is tested against the DEPLOYED application, which has real data. Its tests
need data they know — «review the call CONV-1» — and it must not stay there afterwards. The same three steps for
every application, with nothing to write per app:

1. **Seed on the fly**: `POST /api/pruebas/conjuntos/<conjunto>` with the header `X-WAI-Test-Run: <mark>` loads one of
   the app's sample datasets (`datos_de_ejemplo.CONJUNTOS`) ON TOP of what is there — it never empties anything —.
   The ids the dataset writes by hand get NEW ones (ints after the table's last one; texts with a suffix) so two runs
   never collide, and the answer says which became which: `equivalencias: {"convocatorias": {"CONV-1": "CONV-1-3f9a2c"}}`.
2. **Everything created under a mark carries it**: any request with `X-WAI-Test-Run` (the seed, and every write the
   agent makes through the connector) stamps that mark in the `origen` column of each row it creates. Automatic: no
   model declares anything, the column is added here to every local table that does not have it.
3. **Sweep**: `DELETE /api/pruebas?origen=<mark>` deletes the rows with THAT mark and the rows that depend on them
   (their foreign keys point to a swept row), children first, and returns how many per table.

Guards (a sweep is a delete one argument away from disaster):
* Without a valid mark (`prueba-a<agent>v<version>-<n>`) nothing is seeded and nothing is deleted: 400.
* A row a person created has no mark: it is left out by construction, not by a filter someone may forget.
* The mark only LABELS: it never skips validations, permissions or business rules.
* Same auth as any route of the app (`require_project_access`): whoever tests sees what that person sees.

The ids of a seeded dataset reach a test as `{{<table>.<id in the dataset>}}`: the platform replaces it with the
`equivalencias` of the seed before sending the case (wai-agent-platform, plan-de-pruebas «Built-in test data»).
"""
from __future__ import annotations

import asyncio
import re
import secrets
from contextvars import ContextVar
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query
from sqlalchemy import Column, String, delete, event, inspect, or_, select, text, update
from sqlalchemy.orm import Session

from app.app_database import AppBase, app_engine, app_write
from app.auth import require_project_access

CABECERA = "X-WAI-Test-Run"
CAMPO = "origen"
_MARCA = re.compile(r"prueba-a\d+v\d+-\d+")

#: The mark of the request in progress ("" = not a test).
_marca: ContextVar[str] = ContextVar("wai_marca_de_pruebas", default="")
#: While seeding: the renaming of the dataset's hand-written ids ({table: {old: new}}) and the table bases.
_siembra: ContextVar[Any] = ContextVar("wai_siembra", default=None)

router = APIRouter(prefix="/api/pruebas", tags=["pruebas"])


def marca_valida(valor: Any) -> str:
    marca = str(valor or "").strip()
    if not _MARCA.fullmatch(marca):
        raise HTTPException(400, "A test run mark is required (prueba-a<agent>v<version>-<n>): nothing was done.")
    return marca


# ── 2 · the mark on everything created ─────────────────────────────────────────────────────────────────────────────

def _anadir_columna() -> None:
    """`origen` on every local table that lacks it. Added to the TABLE (not the model): inserts leave it NULL and
    only the mark fills it, after the flush."""
    for tabla in AppBase.metadata.tables.values():
        if CAMPO not in tabla.c:
            tabla.append_column(Column(CAMPO, String(80), nullable=True))


async def _columna_en_la_base() -> None:
    """The column in the DATABASE too (an app deployed before this had its tables already created): idempotent."""
    async with app_engine.begin() as conn:
        def _hacer(sync):
            insp = inspect(sync)
            for tabla in AppBase.metadata.sorted_tables:
                if not insp.has_table(tabla.name):
                    continue
                if CAMPO not in {c["name"] for c in insp.get_columns(tabla.name)}:
                    sync.execute(text(f'ALTER TABLE "{tabla.name}" ADD COLUMN {CAMPO} VARCHAR(80)'))
                sync.execute(text(f'CREATE INDEX IF NOT EXISTS "ix_{tabla.name}_{CAMPO}" ON "{tabla.name}" ({CAMPO})'))
        await conn.run_sync(_hacer)


def _pk(mapper, obj) -> List[Any]:
    return [getattr(obj, mapper.get_property_by_column(c).key) for c in mapper.primary_key]


def _renombrar(session: Session) -> None:
    """While seeding: give the dataset's hand-written ids new ones BEFORE they are inserted, and point the foreign
    keys that used the old ones to the new ones. Ints go after the table's last one; texts get the seed's suffix."""
    s = _siembra.get()
    if s is None:
        return
    nuevos = [o for o in session.new if inspect(o).mapper.local_table in AppBase.metadata.tables.values()]
    for obj in nuevos:
        mapper = inspect(obj).mapper
        if len(mapper.primary_key) != 1:
            continue
        col = mapper.primary_key[0]
        clave = mapper.get_property_by_column(col).key
        viejo = getattr(obj, clave)
        tabla = mapper.local_table.name
        s["hechos"].setdefault(tabla, {})
        if viejo is None or id(obj) in s["vistos"]:      # an object renamed in an earlier flush keeps its new id
            continue
        s["vistos"].add(id(obj))
        if viejo in s["hechos"][tabla]:
            nuevo = s["hechos"][tabla][viejo]
        elif isinstance(viejo, int):
            if tabla not in s["bases"]:
                s["bases"][tabla] = session.execute(select(col).order_by(col.desc()).limit(1)).scalar() or 0
            nuevo = s["bases"][tabla] + viejo
        else:
            nuevo = f"{viejo}-{s['sufijo']}"
        s["hechos"][tabla][viejo] = nuevo
        setattr(obj, clave, nuevo)
    for obj in nuevos:
        mapper = inspect(obj).mapper
        for col in mapper.local_table.columns:
            for fk in col.foreign_keys:
                cambios = s["hechos"].get(fk.column.table.name) or {}
                try:
                    clave = mapper.get_property_by_column(col).key
                except Exception:  # noqa: BLE001 — a column the model does not map
                    continue
                valor = getattr(obj, clave)
                if valor in cambios:
                    setattr(obj, clave, cambios[valor])


def _antes(session: Session, _ctx, _instancias) -> None:
    _renombrar(session)
    marca = _marca.get()
    if marca:
        session.info.setdefault("wai_nuevos", []).extend(
            o for o in session.new if inspect(o).mapper.local_table in AppBase.metadata.tables.values())


def _despues(session: Session, _ctx) -> None:
    """Stamp the mark on the rows this flush created (they have their ids now)."""
    marca = _marca.get()
    nuevos = session.info.pop("wai_nuevos", [])
    if not marca or not nuevos:
        return
    s = _siembra.get()
    for obj in nuevos:
        mapper = inspect(obj).mapper
        pk = _pk(mapper, obj)
        for tabla in mapper.tables:
            if CAMPO not in tabla.c or len(tabla.primary_key.columns) != len(pk):
                continue
            filtro = [c == v for c, v in zip(tabla.primary_key.columns, pk)]
            session.connection().execute(update(tabla).where(*filtro).values({CAMPO: marca}))
        if s is not None:
            s["creados"].setdefault(mapper.local_table.name, []).append(pk[0] if len(pk) == 1 else pk)


event.listen(Session, "before_flush", _antes)
event.listen(Session, "after_flush", _despues)


_COLUMNA_LISTA: List[bool] = []
_COLUMNA_CERROJO = asyncio.Lock()


class _ConLaMarca:
    """Pure ASGI middleware: the request's `X-WAI-Test-Run`, if valid, is the mark of everything it creates.

    It also makes sure, on the FIRST request, that the database has the `origen` column (an app deployed before this
    had its tables created without it). At the first request and not at startup: by then the app's own startup has
    created its tables, whatever way it does it (startup event or lifespan)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        if not _COLUMNA_LISTA:
            async with _COLUMNA_CERROJO:
                if not _COLUMNA_LISTA:
                    await _columna_en_la_base()
                    _COLUMNA_LISTA.append(True)
        valor = next((v.decode("latin-1") for k, v in scope.get("headers") or []
                      if k.decode("latin-1").lower() == CABECERA.lower()), "")
        token = _marca.set(valor if _MARCA.fullmatch(valor.strip()) else "")
        try:
            await self.app(scope, receive, send)
        finally:
            _marca.reset(token)


# ── 1 · seed on the fly · 3 · sweep ────────────────────────────────────────────────────────────────────────────────

@router.get("/conjuntos")
async def conjuntos(_user=Depends(require_project_access)) -> list:
    """The sample datasets this app can seed for a test run."""
    from app import datos_de_ejemplo
    return [{"id": k, "descripcion": v[0]} for k, v in datos_de_ejemplo.CONJUNTOS.items() if v[1] is not None]


@router.post("/conjuntos/{nombre}")
async def sembrar(nombre: str, x_wai_test_run: str = Header(""),
                  _user=Depends(require_project_access)) -> Dict[str, Any]:
    """Loads the dataset ON TOP of what is there, every row marked; says what it created and which ids it renamed."""
    from app import datos_de_ejemplo
    marca = marca_valida(x_wai_test_run)
    if nombre not in datos_de_ejemplo.CONJUNTOS or datos_de_ejemplo.CONJUNTOS[nombre][1] is None:
        raise HTTPException(404, f"Unknown dataset «{nombre}». Available: "
                                 + ", ".join(k for k, v in datos_de_ejemplo.CONJUNTOS.items() if v[1] is not None))
    estado = {"hechos": {}, "bases": {}, "creados": {}, "vistos": set(), "sufijo": secrets.token_hex(3)}
    t_marca, t_siembra = _marca.set(marca), _siembra.set(estado)
    try:
        async with app_write() as session:
            try:
                filas = await datos_de_ejemplo.CONJUNTOS[nombre][1](session) or 0
                await session.commit()
            except HTTPException:
                raise
            except Exception as exc:  # noqa: BLE001 — a dataset that cannot be seeded twice is said, not hidden
                await session.rollback()
                raise HTTPException(409, f"The dataset «{nombre}» could not be seeded on top of the existing data "
                                         f"({type(exc).__name__}: {str(exc)[:300]}). A dataset must be loadable "
                                         "more than once: no fixed values in UNIQUE fields other than the ids.")
    finally:
        _marca.reset(t_marca)
        _siembra.reset(t_siembra)
    return {"conjunto": nombre, "marca": marca, "filas": filas,
            "creados": estado["creados"],
            "equivalencias": {t: {str(k): v for k, v in m.items()} for t, m in estado["hechos"].items() if m}}


@router.delete("")
async def barrer(origen: str = Query(""), _user=Depends(require_project_access)) -> Dict[str, Any]:
    """Deletes the rows marked `origen` and those that depend on them (children first). Never without a mark."""
    marca = marca_valida(origen)
    borrados: Dict[str, int] = {}
    async with app_write() as session:
        def _hacer(sync):
            marcadas: Dict[str, set] = {}
            plan = []
            for tabla in AppBase.metadata.sorted_tables:
                conds = [tabla.c[CAMPO] == marca] if CAMPO in tabla.c else []
                for fk in tabla.foreign_keys:
                    padre = fk.column.table
                    pks = list(padre.primary_key.columns)
                    if marcadas.get(padre.name) and len(pks) == 1 and fk.column is pks[0]:
                        conds.append(fk.parent.in_(marcadas[padre.name]))
                if not conds:
                    continue
                filtro = or_(*conds)
                pks = list(tabla.primary_key.columns)
                if len(pks) == 1:
                    marcadas[tabla.name] = set(sync.execute(select(pks[0]).where(filtro)).scalars())
                plan.append((tabla, filtro))
            for tabla, filtro in reversed(plan):
                n = sync.execute(delete(tabla).where(filtro)).rowcount or 0
                if n:
                    borrados[tabla.name] = n
        await session.run_sync(_hacer)
        await session.commit()
    return {"origen": marca, "borrados": borrados, "total": sum(borrados.values())}


def instalar(app: FastAPI) -> None:
    """ALWAYS, in main.py, after importing the models: the mark middleware, the routes and the `origen` column."""
    _anadir_columna()
    app.add_middleware(_ConLaMarca)
    app.include_router(router)
