"""The application's TEST routes: load a dataset and dump its tables.

Copy this file as-is to `src/backend/app/pruebas_router.py` and hook it into `main.py`
ONLY in test mode:

    if os.getenv("WAI_MODO_PRUEBAS") == "1":
        from app import pruebas_router
        app.include_router(pruebas_router.router)

They only exist when the application is started by the test runner or the live view (which
set `WAI_MODO_PRUEBAS=1` and a key of that run's own in `WAI_PRUEBAS_CLAVE`). In
production they do not exist: nobody can wipe a deployed application's data from here.
And even in test mode they demand the key: without it, 404 (not even their existence is revealed).

The datasets are the data of the Definition's TEST PACKAGES, loaded by `app/datos_de_ejemplo.py` (`CONJUNTOS`, one
per package id, patterns/datos-de-ejemplo.md). Loading one also tells the AI employee module (if installed) which
package answers the employees' calls (`pruebas.paquetes[].interacciones`, patterns/llamar-a-un-empleado.md §6).
"""
import hmac
import os
from typing import Any, Dict

from fastapi import APIRouter, Header, HTTPException
from sqlalchemy import func, select

from app.app_database import AppBase, app_engine, app_session

router = APIRouter(prefix="/api/_pruebas", include_in_schema=False)

MUESTRA = 20


def _exigir_clave(clave: str) -> None:
    esperada = os.getenv("WAI_PRUEBAS_CLAVE", "")
    if os.getenv("WAI_MODO_PRUEBAS") != "1" or not esperada or not hmac.compare_digest(clave or "", esperada):
        raise HTTPException(404)


@router.get("/conjuntos")
async def conjuntos(x_wai_pruebas: str = Header("")) -> list:
    _exigir_clave(x_wai_pruebas)
    from app import datos_de_ejemplo
    return [{"id": k, "descripcion": v[0]} for k, v in datos_de_ejemplo.CONJUNTOS.items()]


@router.post("/conjunto/{nombre}")
async def cargar(nombre: str, x_wai_pruebas: str = Header("")) -> Dict[str, Any]:
    """Leaves the local database with THAT dataset and only that one: it is emptied and loaded."""
    _exigir_clave(x_wai_pruebas)
    from app import datos_de_ejemplo
    if nombre not in datos_de_ejemplo.CONJUNTOS:
        raise HTTPException(404, f"conjunto desconocido: {nombre}")
    async with app_engine.begin() as conn:
        await conn.run_sync(AppBase.metadata.drop_all)
        await conn.run_sync(AppBase.metadata.create_all)
    _, cargar_fn = datos_de_ejemplo.CONJUNTOS[nombre]
    filas = 0
    if cargar_fn is not None:
        async with app_session() as session:
            filas = await cargar_fn(session) or 0
            await session.commit()
    try:
        from app.wai.agente import simulacion
    except ImportError:   # the app calls no AI employee
        simulacion = None
    if simulacion is not None:
        simulacion.usar_conjunto(nombre)
    return {"conjunto": nombre, "filas": filas}


@router.get("/volcado")
async def volcado(x_wai_pruebas: str = Header("")) -> Dict[str, Any]:
    """The local tables with their row count and a sample: what the Definition panel's
    "Datos" tab shows."""
    _exigir_clave(x_wai_pruebas)
    out: Dict[str, Any] = {}
    async with app_session() as session:
        for tabla in AppBase.metadata.sorted_tables:
            total = (await session.execute(select(func.count()).select_from(tabla))).scalar() or 0
            filas = (await session.execute(select(tabla).limit(MUESTRA))).mappings().all()
            out[tabla.name] = {"filas": total,
                               "muestra": [{k: (v if isinstance(v, (int, float, str, bool)) or v is None else str(v))
                                            for k, v in f.items()} for f in filas]}
    return out
