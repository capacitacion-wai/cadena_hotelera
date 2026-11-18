import os
import tempfile

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from jose import jwt

_db_file = os.path.join(tempfile.mkdtemp(), "app.sqlite")
os.environ["APP_DB_PATH"] = _db_file
os.environ["APP_DATABASE_URL"] = f"sqlite+aiosqlite:///{_db_file}"
os.environ.setdefault("DB_SCHEMA", "demo_wai")
os.environ.setdefault("PROJECT_ID", "43")
os.environ.setdefault("WAI_BACKEND_URL", "http://wai-backend.test")
os.environ.setdefault("WAI_INTROSPECCION_URL", "http://introspection.test")
os.environ.setdefault("APP_NAME", "gestor-de-ocupacion")
os.environ["WAI_DATOS_DE_EJEMPLO"] = "0"
os.environ["WAI_MODO_PRUEBAS"] = "0"

from app import auth, datos_de_ejemplo  # noqa: E402
from app.app_database import AppBase, app_engine, app_session  # noqa: E402
from app.config import DB_SCHEMA, PROJECT_ID  # noqa: E402
from app.main import app  # noqa: E402

CLAVE_DE_PRUEBA = "solo-pruebas"


@pytest.fixture(autouse=True)
def plataforma_de_prueba(monkeypatch):
    async def introspeccion(token: str) -> dict:
        try:
            return {"activo": True, "claims": jwt.get_unverified_claims(token)}
        except Exception:
            return {"activo": False, "motivo": "formato"}

    monkeypatch.setattr(auth, "preguntar_a_la_plataforma", introspeccion)
    auth._cache.clear()
    yield
    auth._cache.clear()


def cabeceras(email: str, project_ids: list[int] | None = None) -> dict[str, str]:
    token = jwt.encode({
        "sub": email,
        "is_superuser": False,
        "project_ids": project_ids if project_ids is not None else [PROJECT_ID],
        "data_areas": [DB_SCHEMA],
        "exp": 9999999999,
    }, CLAVE_DE_PRUEBA, algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def direccion_headers():
    return cabeceras("direccion@pruebas.wai")


@pytest.fixture
def recepcion_headers():
    return cabeceras("recepcion@pruebas.wai")


@pytest.fixture
def sin_acceso_headers():
    return cabeceras("otra@pruebas.wai", [99999])


@pytest_asyncio.fixture(autouse=True)
async def base_de_datos():
    await app_engine.dispose()
    from app.wai.perfilado import preparar

    await preparar()
    async with app_engine.begin() as connection:
        await connection.run_sync(AppBase.metadata.drop_all)
        await connection.run_sync(AppBase.metadata.create_all)
    async with app_session() as session:
        await datos_de_ejemplo.CONJUNTOS["base"][1](session)
        await session.commit()
    yield
    await app_engine.dispose()


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as test_client:
        yield test_client
