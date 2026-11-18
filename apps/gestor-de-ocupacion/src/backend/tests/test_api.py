import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError


@pytest.mark.asyncio
async def test_ocupacion_y_listas_muestran_totales_coherentes(client, recepcion_headers):
    overview = await client.get("/api/ocupacion", headers=recepcion_headers)
    assert overview.status_code == 200
    assert overview.json() == {"hoteles": 5, "vendidas": 101, "fuera_servicio": 3, "sobreventa": 3}

    hotels = await client.get("/api/hoteles?limit=2&offset=0", headers=recepcion_headers)
    assert hotels.status_code == 200
    assert hotels.json()["total"] == 5
    assert len(hotels.json()["items"]) == 2


@pytest.mark.asyncio
async def test_detalle_del_centro_muestra_habitaciones_y_reservas_activas(client, recepcion_headers):
    hotel = await client.get("/api/hoteles/1", headers=recepcion_headers)
    rooms = await client.get("/api/hoteles/1/habitaciones?limit=50&offset=0", headers=recepcion_headers)
    reservations = await client.get("/api/hoteles/1/reservas?limit=50&offset=0", headers=recepcion_headers)
    assert hotel.status_code == rooms.status_code == reservations.status_code == 200
    assert hotel.json()["disponibles"] == 27
    assert hotel.json()["vendidas"] == 30
    assert rooms.json()["total"] == 30
    assert sum(room["estado"] == "fuera_servicio" for room in rooms.json()["items"]) == 3
    assert reservations.json()["total"] == 30


@pytest.mark.asyncio
async def test_recepcion_puede_proponer_y_la_propuesta_se_lee_al_momento(client, recepcion_headers):
    initial = (await client.get("/api/reubicaciones", headers=recepcion_headers)).json()
    guest = initial["huespedes_por_reubicar"]["items"][0]
    response = await client.post("/api/reubicaciones", headers=recepcion_headers, json={
        "reserva_id": guest["reserva_id"],
        "hotel_destino_id": 3,
        "motivo": "La habitación asignada está fuera de servicio.",
    })
    assert response.status_code == 201
    saved = (await client.get("/api/reubicaciones", headers=recepcion_headers)).json()
    assert saved["propuestas"]["total"] == 2
    assert any(item["huesped"] == guest["huesped"] for item in saved["propuestas"]["items"])


@pytest.mark.asyncio
async def test_propuesta_requiere_un_motivo(client, recepcion_headers):
    data = (await client.get("/api/reubicaciones", headers=recepcion_headers)).json()
    guest = data["huespedes_por_reubicar"]["items"][0]
    response = await client.post("/api/reubicaciones", headers=recepcion_headers, json={
        "reserva_id": guest["reserva_id"],
        "hotel_destino_id": 3,
        "motivo": "   ",
    })
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_recepcion_no_puede_aprobar_ni_mover_al_huesped(client, recepcion_headers):
    data = (await client.get("/api/reubicaciones", headers=recepcion_headers)).json()
    proposal_id = data["propuestas"]["items"][0]["id"]
    response = await client.post(f"/api/reubicaciones/{proposal_id}/aprobar", headers=recepcion_headers, json={})
    assert response.status_code == 403
    assert response.headers.get("X-WAI-Codigo") == "PERFILADO_SIN_PERMISO"
    overview = (await client.get("/api/ocupacion", headers=recepcion_headers)).json()
    assert overview["sobreventa"] == 3


@pytest.mark.asyncio
async def test_direccion_aprueba_mueve_y_deja_autoria_y_fecha(client, direccion_headers):
    before = (await client.get("/api/reubicaciones", headers=direccion_headers)).json()
    pending = before["propuestas"]["items"][0]
    response = await client.post(f"/api/reubicaciones/{pending['id']}/aprobar", headers=direccion_headers, json={})
    assert response.status_code == 200

    after = (await client.get("/api/reubicaciones", headers=direccion_headers)).json()
    moved = next(item for item in after["historial"]["items"] if item["huesped"] == pending["huesped"])
    assert moved["propuesto_por"]
    assert moved["aprobado_por"] == "direccion@pruebas.wai"
    assert moved["propuesto_en"]
    assert moved["aprobado_en"]
    assert after["historial"]["total"] == 2
    overview = (await client.get("/api/ocupacion", headers=direccion_headers)).json()
    assert overview["sobreventa"] == 2


@pytest.mark.asyncio
async def test_fallo_al_guardar_no_responde_como_si_se_hubiera_guardado(client, recepcion_headers, monkeypatch):
    async def commit_failed(_self):
        raise IntegrityError("INSERT", {}, Exception("test-only commit failure"))

    monkeypatch.setattr(AsyncSession, "commit", commit_failed)
    data = (await client.get("/api/reubicaciones", headers=recepcion_headers)).json()
    guest = data["huespedes_por_reubicar"]["items"][0]
    response = await client.post("/api/reubicaciones", headers=recepcion_headers, json={
        "reserva_id": guest["reserva_id"],
        "hotel_destino_id": 3,
        "motivo": "Habitación fuera de servicio.",
    })
    assert response.status_code == 409
