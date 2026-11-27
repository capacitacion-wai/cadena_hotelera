import logging
import os
from pathlib import Path

from fastapi import FastAPI

from app.app_database import AppBase, app_engine, app_session
from app import modules, pruebas_de_serie
from app.wai import migraciones
from app.modules.ocupacion.routes import router as ocupacion_router
from app.modules.reubicaciones.routes import router as reubicaciones_router
from app.routes.auth_routes import router as auth_router
from app.routes.spa_routes import montar_spa

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Gestor de ocupación", docs_url="/api/docs", openapi_url="/api/openapi.json")

# Platform modules (installed by wai-app-builder/assets/modulos.py) own their routes.
from app.wai import montar_modulos  # noqa: E402
montar_modulos(app)

app.include_router(auth_router, prefix="/api/auth")
app.include_router(ocupacion_router, prefix="/api")
app.include_router(reubicaciones_router, prefix="/api")
pruebas_de_serie.instalar(app)

if os.getenv("WAI_MODO_PRUEBAS") == "1":
    from app import pruebas_router

    app.include_router(pruebas_router.router)


@app.on_event("startup")
async def preparar_aplicacion() -> None:
    async with app_engine.begin() as connection:
        await connection.run_sync(AppBase.metadata.create_all)
    await migraciones.migrar(
        app_engine,
        "ocupacion_reservas",
        Path(__file__).resolve().parent / "modules" / "ocupacion" / "migraciones",
    )
    if os.getenv("WAI_DATOS_DE_EJEMPLO") == "1":
        from app import datos_de_ejemplo

        async with app_session() as session:
            await datos_de_ejemplo.cargar_si_vacia(session)
            await session.commit()


@app.get("/api/health")
async def health():
    return {"status": "ok"}


montar_spa(app)
