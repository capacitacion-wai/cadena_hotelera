from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from conftest import cabeceras


def fecha(dias: int) -> str:
    hoy = datetime.now(ZoneInfo("Europe/Madrid")).date()
    return (hoy + timedelta(days=dias)).isoformat()


async def habitacion_para(client, hotel_id: int, entrada: str, salida: str, headers: dict) -> int:
    rooms = (await client.get(
        f"/api/hoteles/{hotel_id}/habitaciones?limit=100&offset=0", headers=headers,
    )).json()["items"]
    reservations = (await client.get(
        f"/api/hoteles/{hotel_id}/reservas?periodo=todas&limit=100&offset=0", headers=headers,
    )).json()["items"]
    used = {
        reservation["habitacion_id"] for reservation in reservations
        if reservation["estado"] == "confirmada"
        and reservation["entrada"] < salida and reservation["salida"] > entrada
    }
    return next(room["id"] for room in rooms if room["estado"] != "fuera_servicio" and room["id"] not in used)


async def crear(client, headers, huesped, room_id, entrada, salida):
    return await client.post("/api/hoteles/3/reservas", headers=headers, json={
        "huesped": huesped,
        "habitacion_id": room_id,
        "entrada": entrada,
        "salida": salida,
    })


@pytest.mark.asyncio
async def test_recepcion_crea_en_habitacion_operativa_y_disponible(client, recepcion_headers):
    entrada, salida = fecha(4), fecha(6)
    room_id = await habitacion_para(client, 3, entrada, salida, recepcion_headers)
    response = await crear(client, recepcion_headers, "Prueba de alta", room_id, entrada, salida)
    assert response.status_code == 201, response.text
    assert response.json()["habitacion_id"] == room_id
    assert response.json()["estado"] == "confirmada"

    overlap = await crear(client, recepcion_headers, "Prueba duplicada", room_id, fecha(5), fecha(7))
    assert overlap.status_code == 409


@pytest.mark.asyncio
async def test_editar_fechas_y_habitacion_revalida_solapamientos(client, direccion_headers):
    entrada, salida = fecha(8), fecha(10)
    room_a = await habitacion_para(client, 3, entrada, salida, direccion_headers)
    first = await crear(client, direccion_headers, "Reserva A", room_a, entrada, salida)
    assert first.status_code == 201
    second = await crear(client, direccion_headers, "Reserva contigua", room_a, salida, fecha(12))
    assert second.status_code == 201
    conflict = await client.patch(
        f"/api/reservas/{second.json()['id']}",
        headers=direccion_headers,
        json={"entrada": fecha(9)},
    )
    assert conflict.status_code == 409

    room_b = await habitacion_para(client, 3, entrada, salida, direccion_headers)
    other = await crear(client, direccion_headers, "Reserva B", room_b, entrada, salida)
    assert other.status_code == 201
    conflict = await client.patch(
        f"/api/reservas/{first.json()['id']}",
        headers=direccion_headers,
        json={"habitacion_id": room_b},
    )
    assert conflict.status_code == 409
    valid = await client.patch(
        f"/api/reservas/{first.json()['id']}",
        headers=direccion_headers,
        json={"entrada": fecha(13), "salida": fecha(15)},
    )
    assert valid.status_code == 200, valid.text


@pytest.mark.asyncio
async def test_cancelacion_obligatoria_auditable_y_no_borra(client, recepcion_headers):
    entrada, salida = fecha(17), fecha(19)
    room_id = await habitacion_para(client, 3, entrada, salida, recepcion_headers)
    response = await crear(client, recepcion_headers, "Reserva con auditoría", room_id, entrada, salida)
    reservation_id = response.json()["id"]
    missing = await client.post(
        f"/api/reservas/{reservation_id}/cancelar", headers=recepcion_headers, json={"motivo": "  "},
    )
    assert missing.status_code == 422
    spoof = await client.post(
        f"/api/reservas/{reservation_id}/cancelar",
        headers=recepcion_headers,
        json={"motivo": "Cambio de planes", "cancelada_por": "falso@example.com"},
    )
    assert spoof.status_code == 422
    cancelled = await client.post(
        f"/api/reservas/{reservation_id}/cancelar",
        headers=recepcion_headers,
        json={"motivo": "Cambio de planes"},
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["estado"] == "cancelada"
    assert cancelled.json()["cancelada_por"] == "recepcion@pruebas.wai"
    assert cancelled.json()["cancelada_en"]
    listed = await client.get(
        "/api/hoteles/3/reservas?periodo=canceladas&limit=100&offset=0", headers=recepcion_headers,
    )
    row = next(row for row in listed.json()["items"] if row["id"] == reservation_id)
    assert row["motivo_cancelacion"] == "Cambio de planes"


@pytest.mark.asyncio
async def test_consulta_no_escribe_reservas(client):
    consultation = cabeceras("consulta@pruebas.wai")
    assert (await client.get("/api/ocupacion", headers=consultation)).status_code == 200
    current = (await client.get(
        "/api/hoteles/3/reservas?periodo=actuales&limit=100&offset=0", headers=consultation,
    )).json()["items"][0]
    body = {"huesped": "No autorizado", "habitacion_id": current["habitacion_id"],
            "entrada": fecha(25), "salida": fecha(27)}
    response = await client.post("/api/hoteles/3/reservas", headers=consultation, json=body)
    assert response.status_code == 403
    response = await client.patch(
        f"/api/reservas/{current['id']}", headers=consultation, json={"entrada": fecha(26)},
    )
    assert response.status_code == 403
    response = await client.post(
        f"/api/reservas/{current['id']}/cancelar", headers=consultation, json={"motivo": "No permitido"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_admin_fijo_puede_crear_editar_y_cancelar_reservas(client):
    admin = cabeceras("pruebas@pruebas.wai")
    entrada, salida = fecha(25), fecha(27)
    room_id = await habitacion_para(client, 3, entrada, salida, admin)
    created = await crear(client, admin, "Reserva de Administrador", room_id, entrada, salida)
    assert created.status_code == 201, created.text

    changed = await client.patch(
        f"/api/reservas/{created.json()['id']}", headers=admin,
        json={"entrada": fecha(28), "salida": fecha(30)},
    )
    assert changed.status_code == 200, changed.text

    cancelled = await client.post(
        f"/api/reservas/{created.json()['id']}/cancelar", headers=admin,
        json={"motivo": "Ajuste realizado por administración"},
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["estado"] == "cancelada"
    assert cancelled.json()["cancelada_por"] == "pruebas@pruebas.wai"
    assert cancelled.json()["cancelada_en"]


@pytest.mark.asyncio
async def test_reserva_creada_en_prueba_se_elimina_por_su_marca(client, recepcion_headers):
    mark = "prueba-a12v3-9"
    headers = {**recepcion_headers, "X-WAI-Test-Run": mark}
    entrada, salida = fecha(28), fecha(30)
    room_id = await habitacion_para(client, 3, entrada, salida, recepcion_headers)
    response = await crear(client, headers, "Reserva temporal", room_id, entrada, salida)
    assert response.status_code == 201, response.text
    reservation_id = response.json()["id"]

    swept = await client.delete(f"/api/pruebas?origen={mark}", headers=recepcion_headers)
    assert swept.status_code == 200, swept.text
    assert swept.json()["borrados"]["reservas"] == 1
    remaining = await client.get("/api/hoteles/3/reservas?periodo=todas&limit=100&offset=0",
                                 headers=recepcion_headers)
    assert all(row["id"] != reservation_id for row in remaining.json()["items"])


@pytest.mark.asyncio
async def test_lectura_inmediata_actualiza_vendidas_y_sobreventa(client, direccion_headers):
    current = (await client.get(
        "/api/hoteles/1/reservas?periodo=actuales&limit=100&offset=0", headers=direccion_headers,
    )).json()["items"]
    unassigned = next(row for row in current if row["habitacion_id"] is None)
    before = (await client.get("/api/ocupacion", headers=direccion_headers)).json()
    response = await client.post(
        f"/api/reservas/{unassigned['id']}/cancelar",
        headers=direccion_headers,
        json={"motivo": "Registro duplicado"},
    )
    assert response.status_code == 200
    hotel = await client.get("/api/hoteles/1", headers=direccion_headers)
    after = await client.get("/api/ocupacion", headers=direccion_headers)
    assert hotel.json()["vendidas"] == 29
    assert after.json()["sobreventa"] == before["sobreventa"] - 1


@pytest.mark.asyncio
async def test_reservas_se_filtran_por_periodo_y_canceladas_conservan_auditoria(client, direccion_headers):
    entrada, salida = fecha(35), fecha(37)
    room_id = await habitacion_para(client, 3, entrada, salida, direccion_headers)
    response = await crear(client, direccion_headers, "Reserva futura filtrada", room_id, entrada, salida)
    assert response.status_code == 201
    future = await client.get("/api/hoteles/3/reservas?periodo=futuras&limit=100&offset=0",
                              headers=direccion_headers)
    assert any(row["huesped"] == "Reserva futura filtrada" for row in future.json()["items"])
    cancelled = await client.post(
        f"/api/reservas/{response.json()['id']}/cancelar",
        headers=direccion_headers,
        json={"motivo": "Prueba de filtro"},
    )
    assert cancelled.status_code == 200
    historic = await client.get("/api/hoteles/5/reservas?periodo=pasadas&limit=100&offset=0",
                                headers=direccion_headers)
    assert historic.json()["total"] >= 1
    cancelled_list = await client.get("/api/hoteles/3/reservas?periodo=canceladas&limit=100&offset=0",
                                      headers=direccion_headers)
    row = next(row for row in cancelled_list.json()["items"] if row["id"] == response.json()["id"])
    assert row["motivo_cancelacion"] == "Prueba de filtro"
    assert row["cancelada_por"] == "direccion@pruebas.wai" and row["cancelada_en"]
