import pytest


@pytest.mark.asyncio
async def test_health_no_pide_sesion(client):
    response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_ocupacion_requiere_sesion(client):
    response = await client.get("/api/ocupacion")
    assert response.status_code in (401, 403)


@pytest.mark.asyncio
async def test_ocupacion_rechaza_a_quien_no_tiene_acceso_al_proyecto(client, sin_acceso_headers):
    response = await client.get("/api/ocupacion", headers=sin_acceso_headers)
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_login_se_envia_a_la_sesion_de_wai(client, monkeypatch):
    from app.routes import auth_routes

    class Respuesta:
        status_code = 200

        @staticmethod
        def json():
            return {"access_token": "token-de-prueba", "token_type": "bearer"}

    class ClienteFalso:
        def __init__(self, **_options):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, data, headers):
            assert url.endswith("/backend/auth/token")
            assert data == {"username": "direccion@hoteles.example", "password": "clave-prueba"}
            assert headers["Content-Type"] == "application/x-www-form-urlencoded"
            return Respuesta()

    monkeypatch.setattr(auth_routes.httpx, "AsyncClient", ClienteFalso)
    response = await client.post("/api/auth/login", json={
        "email": "direccion@hoteles.example",
        "password": "clave-prueba",
    })
    assert response.status_code == 200
    assert response.json()["access_token"] == "token-de-prueba"
