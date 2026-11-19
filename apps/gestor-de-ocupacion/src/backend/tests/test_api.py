import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from app.app_database import app_session
from app.modules.ocupacion.models import Habitacion, Hotel, Reserva


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
async def test_seed_base_devuelve_aliases_de_reservas_y_barrer_respeta_datos_sin_marca(
    client, recepcion_headers,
):
    marca = "prueba-a12v3-1"
    huespedes = {10001: "Amparo Vidal", 10002: "Jordi Roca", 10003: "Lucía Pérez"}

    async with app_session() as session:
        ids_originales_por_tabla = {
            modelo.__tablename__: set((await session.scalars(select(modelo.id))).all())
            for modelo in (Hotel, Habitacion, Reserva)
        }
        originales = (await session.execute(
            select(Reserva.id, Reserva.huesped)
            .where(Reserva.huesped.in_(huespedes.values()), Reserva.__table__.c.origen.is_(None))
        )).all()
    ids_originales = {huesped: reserva_id for reserva_id, huesped in originales}
    assert set(ids_originales) == set(huespedes.values())

    sembrado = await client.post(
        "/api/pruebas/conjuntos/base",
        headers={**recepcion_headers, "X-WAI-Test-Run": marca},
    )
    assert sembrado.status_code == 200, sembrado.text
    equivalencias = sembrado.json()["equivalencias"]["reservas"]
    assert set(equivalencias) >= {str(alias) for alias in huespedes}
    ids_marcados = {int(equivalencias[str(alias)]) for alias in huespedes}

    async with app_session() as session:
        hotel_marcado = await session.scalar(
            select(Hotel).where(
                Hotel.nombre == "Gran Hotel Centro",
                Hotel.__table__.c.origen == marca,
            )
        )
        assert hotel_marcado is not None
        habitaciones_marcadas = (await session.execute(
            select(Habitacion.id, Habitacion.numero, Habitacion.estado)
            .where(
                Habitacion.hotel_id == hotel_marcado.id,
                Habitacion.__table__.c.origen == marca,
            )
        )).all()
        filas_marcadas = (await session.execute(
            select(Reserva.id, Reserva.huesped, Reserva.__table__.c.origen)
            .where(Reserva.id.in_(ids_marcados))
        )).all()
    ids_habitaciones_marcadas = {habitacion_id for habitacion_id, _, _ in habitaciones_marcadas}
    assert {
        numero for _, numero, estado in habitaciones_marcadas if estado == "fuera_servicio"
    } == {str(hotel_marcado.id * 100 + 101 + offset) for offset in range(3)}
    assert {huesped: origen for _, huesped, origen in filas_marcadas} == {
        huespedes[alias]: marca for alias in huespedes
    }

    reubicaciones = await client.get("/api/reubicaciones", headers=recepcion_headers)
    assert reubicaciones.status_code == 200, reubicaciones.text
    candidatos_marcados = {
        (fila["huesped"], fila["reserva_id"])
        for fila in reubicaciones.json()["huespedes_por_reubicar"]["items"]
        if fila["reserva_id"] in ids_marcados
    }
    assert candidatos_marcados == {
        (huespedes[10002], int(equivalencias["10002"])),
        (huespedes[10003], int(equivalencias["10003"])),
    }

    barrido = await client.delete(f"/api/pruebas?origen={marca}", headers=recepcion_headers)
    assert barrido.status_code == 200, barrido.text
    assert barrido.json()["borrados"]["reservas"] >= len(ids_marcados)

    async with app_session() as session:
        ids_restantes = set((await session.scalars(
            select(Reserva.id).where(Reserva.id.in_(ids_marcados | set(ids_originales.values())))
        )).all())
        originales_sin_marca = (await session.execute(
            select(Reserva.id, Reserva.huesped, Reserva.__table__.c.origen)
            .where(Reserva.id.in_(ids_originales.values()))
        )).all()
        hotel_marcado_restante = await session.scalar(
            select(Hotel.id).where(Hotel.id == hotel_marcado.id)
        )
        habitaciones_marcadas_restantes = set((await session.scalars(
            select(Habitacion.id).where(Habitacion.id.in_(ids_habitaciones_marcadas))
        )).all())
        ids_restantes_por_tabla = {
            modelo.__tablename__: set((await session.scalars(select(modelo.id))).all())
            for modelo in (Hotel, Habitacion, Reserva)
        }
    assert ids_restantes == set(ids_originales.values())
    assert hotel_marcado_restante is None
    assert not habitaciones_marcadas_restantes
    assert all(
        ids_restantes_por_tabla[tabla] >= ids_originales
        for tabla, ids_originales in ids_originales_por_tabla.items()
    )
    assert {huesped: origen for _, huesped, origen in originales_sin_marca} == {
        huesped: None for huesped in huespedes.values()
    }


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
