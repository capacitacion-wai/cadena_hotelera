from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from playwright.sync_api import expect


def _fecha(dias):
    return (datetime.now(ZoneInfo("Europe/Madrid")).date() + timedelta(days=dias)).isoformat()


def _habitacion_libre(cliente, hotel_id, entrada, salida, headers=None, excluir=None):
    params = "limit=100&offset=0"
    habitaciones = cliente.get(f"/api/hoteles/{hotel_id}/habitaciones?{params}", headers=headers)
    assert habitaciones.status_code == 200, habitaciones.text
    reservas = cliente.get(
        f"/api/hoteles/{hotel_id}/reservas?periodo=todas&{params}", headers=headers,
    )
    assert reservas.status_code == 200, reservas.text
    ocupadas = {
        reserva["habitacion_id"]
        for reserva in reservas.json()["items"]
        if reserva["estado"] == "confirmada"
        and reserva["id"] != excluir
        and reserva["entrada"] < salida
        and reserva["salida"] > entrada
    }
    return next(
        room["id"] for room in habitaciones.json()["items"]
        if room["estado"] != "fuera_servicio" and room["id"] not in ocupadas
    )


def _crear(cliente, headers, hotel_id, huesped, habitacion_id, entrada, salida):
    response = cliente.post(
        f"/api/hoteles/{hotel_id}/reservas",
        headers=headers,
        json={"huesped": huesped, "habitacion_id": habitacion_id, "entrada": entrada, "salida": salida},
    )
    return response


def test_recepcion_crea_reserva_disponible(cliente):
    entrada, salida = _fecha(12), _fecha(14)
    room_id = _habitacion_libre(cliente, 3, entrada, salida)
    response = _crear(cliente, None, 3, "Huésped de prueba", room_id, entrada, salida)
    assert response.status_code == 201, response.text
    reserva = response.json()
    assert reserva["estado"] == "confirmada"
    assert reserva["habitacion_id"] == room_id
    assert reserva["entrada"] == entrada and reserva["salida"] == salida
    listado = cliente.get(
        "/api/hoteles/3/reservas?periodo=futuras&limit=100&offset=0",
    )
    assert listado.status_code == 200, listado.text
    guardada = next(item for item in listado.json()["items"] if item["id"] == reserva["id"])
    assert guardada["huesped"] == "Huésped de prueba"
    assert guardada["habitacion_id"] == room_id


def test_no_admite_reservas_solapadas(cliente):
    entrada, salida = _fecha(16), _fecha(19)
    room_id = _habitacion_libre(cliente, 3, entrada, salida)
    first = _crear(cliente, None, 3, "Primera estancia", room_id, entrada, salida)
    assert first.status_code == 201, first.text
    conflict = _crear(cliente, None, 3, "Estancia solapada", room_id, _fecha(18), _fecha(20))
    assert conflict.status_code == 409


def test_editar_reserva_revalida_disponibilidad(cliente):
    entrada, salida = _fecha(22), _fecha(24)
    room_a = _habitacion_libre(cliente, 3, entrada, salida)
    first = _crear(cliente, None, 3, "Reserva para editar", room_a, entrada, salida)
    assert first.status_code == 201, first.text
    second_start, second_end = _fecha(24), _fecha(26)
    touching = _crear(cliente, None, 3, "Estancia contigua", room_a, second_start, second_end)
    assert touching.status_code == 201, touching.text
    date_conflict = cliente.patch(
        f"/api/reservas/{touching.json()['id']}",
        json={"entrada": _fecha(23), "salida": _fecha(25)},
    )
    assert date_conflict.status_code == 409

    room_b = _habitacion_libre(cliente, 3, entrada, salida)
    occupying = _crear(cliente, None, 3, "Reserva de destino", room_b, entrada, salida)
    assert occupying.status_code == 201, occupying.text
    room_conflict = cliente.patch(
        f"/api/reservas/{first.json()['id']}", json={"habitacion_id": room_b},
    )
    assert room_conflict.status_code == 409
    valid = cliente.patch(
        f"/api/reservas/{first.json()['id']}", json={"entrada": _fecha(27), "salida": _fecha(29)},
    )
    assert valid.status_code == 200, valid.text


def test_cancelacion_exige_motivo_y_guarda_auditoria(cliente):
    entrada, salida = _fecha(30), _fecha(32)
    room_id = _habitacion_libre(cliente, 3, entrada, salida)
    reserva = _crear(cliente, None, 3, "Huésped para cancelar", room_id, entrada, salida)
    assert reserva.status_code == 201, reserva.text
    missing_reason = cliente.post(f"/api/reservas/{reserva.json()['id']}/cancelar", json={"motivo": "  "})
    assert missing_reason.status_code == 422

    yo = cliente.get("/api/perfilado/yo")
    assert yo.status_code == 200, yo.text
    cancelled = cliente.post(
        f"/api/reservas/{reserva.json()['id']}/cancelar", json={"motivo": "Cambio de planes"},
    )
    assert cancelled.status_code == 200, cancelled.text
    result = cancelled.json()
    assert result["estado"] == "cancelada"
    assert result["motivo_cancelacion"] == "Cambio de planes"
    assert result["cancelada_por"] == yo.json()["email"]
    assert result["cancelada_en"]
    listed = cliente.get("/api/hoteles/3/reservas?periodo=canceladas&limit=100&offset=0")
    assert any(row["id"] == result["id"] for row in listed.json()["items"])


def test_direccion_puede_escribir(cliente):
    entrada, salida = _fecha(34), _fecha(36)
    room_id = _habitacion_libre(cliente, 3, entrada, salida)
    reservation = _crear(cliente, None, 3, "Estancia de Dirección", room_id, entrada, salida)
    assert reservation.status_code == 201, reservation.text
    changed = cliente.patch(
        f"/api/reservas/{reservation.json()['id']}",
        json={"entrada": _fecha(37), "salida": _fecha(39)},
    )
    assert changed.status_code == 200, changed.text
    cancelled = cliente.post(
        f"/api/reservas/{reservation.json()['id']}/cancelar", json={"motivo": "Ajuste de itinerario"},
    )
    assert cancelled.status_code == 200 and cancelled.json()["estado"] == "cancelada"


def test_consulta_no_puede_escribir_reservas(cliente):
    current = cliente.get("/api/hoteles/3/reservas?periodo=actuales&limit=100&offset=0")
    assert current.status_code == 200
    reservation = current.json()["items"][0]
    before = cliente.get("/api/hoteles/3/reservas?periodo=todas&limit=100&offset=0").json()["total"]
    create = cliente.post("/api/hoteles/3/reservas", json={
        "huesped": "Consulta no autorizada",
        "habitacion_id": reservation["habitacion_id"],
        "entrada": _fecha(40),
        "salida": _fecha(42),
    })
    edit = cliente.patch(f"/api/reservas/{reservation['id']}", json={"entrada": _fecha(41)})
    cancel = cliente.post(f"/api/reservas/{reservation['id']}/cancelar", json={"motivo": "No autorizado"})
    assert create.status_code == edit.status_code == cancel.status_code == 403
    after = cliente.get("/api/hoteles/3/reservas?periodo=todas&limit=100&offset=0").json()["total"]
    assert after == before


def test_ocupacion_refleja_cambio_confirmado_en_siguiente_lectura(cliente):
    before = cliente.get("/api/hoteles/1/reservas?periodo=actuales&limit=100&offset=0").json()
    unassigned = next(row for row in before["items"] if row["habitacion_id"] is None)
    overview_before = cliente.get("/api/ocupacion").json()
    cancelled = cliente.post(
        f"/api/reservas/{unassigned['id']}/cancelar", json={"motivo": "Reserva duplicada"},
    )
    assert cancelled.status_code == 200, cancelled.text
    hotel = cliente.get("/api/hoteles/1")
    overview_after = cliente.get("/api/ocupacion")
    assert hotel.status_code == overview_after.status_code == 200
    assert hotel.json()["vendidas"] == 29
    assert overview_after.json()["sobreventa"] == overview_before["sobreventa"] - 1


def test_listado_muestra_periodos_y_auditoria_de_cancelacion(cliente, pagina, paso, como):
    headers = como("direccion")
    future_in, future_out = _fecha(45), _fecha(47)
    future_room = _habitacion_libre(cliente, 3, future_in, future_out, headers)
    future = _crear(cliente, headers, 3, "Huésped futuro", future_room, future_in, future_out)
    assert future.status_code == 201, future.text

    past_in, past_out = _fecha(-8), _fecha(-6)
    past_room = _habitacion_libre(cliente, 3, past_in, past_out, headers)
    past = _crear(cliente, headers, 3, "Huésped pasado", past_room, past_in, past_out)
    assert past.status_code == 201, past.text

    cancelled_in, cancelled_out = _fecha(50), _fecha(52)
    cancelled_room = _habitacion_libre(cliente, 3, cancelled_in, cancelled_out, headers)
    cancelled = _crear(cliente, headers, 3, "Huésped cancelado", cancelled_room, cancelled_in, cancelled_out)
    assert cancelled.status_code == 201, cancelled.text
    audit = cliente.post(
        f"/api/reservas/{cancelled.json()['id']}/cancelar",
        headers=headers,
        json={"motivo": "El huésped cambió sus planes"},
    )
    assert audit.status_code == 200, audit.text

    pagina.goto("/hoteles/3")
    pagina.get_by_role("tab", name="Reservas").click()
    expect(pagina.get_by_role("tab", name="Actuales")).to_be_visible()
    expect(pagina.get_by_role("tab", name="Futuras")).to_be_visible()
    expect(pagina.get_by_role("tab", name="Pasadas")).to_be_visible()
    expect(pagina.get_by_role("tab", name="Canceladas")).to_be_visible()
    expect(pagina.get_by_role("columnheader", name="Huésped")).to_be_visible()
    expect(pagina.get_by_role("columnheader", name="Habitación")).to_be_visible()
    expect(pagina.get_by_role("columnheader", name="Entrada")).to_be_visible()
    expect(pagina.get_by_role("columnheader", name="Salida")).to_be_visible()
    expect(pagina.get_by_role("button", name="Nueva reserva")).to_be_hidden()
    paso("Consulta ve las categorías de reservas sin controles de escritura")

    pagina.get_by_role("tab", name="Futuras").click()
    expect(pagina.get_by_text("Huésped futuro", exact=True)).to_be_visible()
    pagina.get_by_role("tab", name="Pasadas").click()
    expect(pagina.get_by_text("Huésped pasado", exact=True)).to_be_visible()
    pagina.get_by_role("tab", name="Canceladas").click()
    expect(pagina.get_by_text("Huésped cancelado", exact=True)).to_be_visible()
    expect(pagina.get_by_text("El huésped cambió sus planes", exact=True)).to_be_visible()
    expect(pagina.get_by_text("Cancelada por direccion@pruebas.wai", exact=True)).to_be_visible()
    paso("Las cancelaciones conservan su motivo, autoría y momento")


def test_cancelacion_es_cambio_auditado_no_borrado(cliente):
    entrada, salida = _fecha(55), _fecha(57)
    room_id = _habitacion_libre(cliente, 3, entrada, salida)
    reservation = _crear(cliente, None, 3, "Reserva que permanece", room_id, entrada, salida)
    assert reservation.status_code == 201, reservation.text
    response = cliente.post(
        f"/api/reservas/{reservation.json()['id']}/cancelar", json={"motivo": "Se conserva el registro"},
    )
    assert response.status_code == 200, response.text
    listed = cliente.get("/api/hoteles/3/reservas?periodo=canceladas&limit=100&offset=0")
    assert listed.status_code == 200
    record = next(row for row in listed.json()["items"] if row["id"] == reservation.json()["id"])
    assert record["estado"] == "cancelada"
    assert record["motivo_cancelacion"] == "Se conserva el registro"
    assert record["cancelada_por"] and record["cancelada_en"]


def test_recepcion_puede_editar_y_cancelar(cliente):
    entrada, salida = _fecha(60), _fecha(62)
    room_id = _habitacion_libre(cliente, 3, entrada, salida)
    reservation = _crear(cliente, None, 3, "Reserva de Recepción", room_id, entrada, salida)
    assert reservation.status_code == 201, reservation.text
    changed = cliente.patch(
        f"/api/reservas/{reservation.json()['id']}",
        json={"entrada": _fecha(63), "salida": _fecha(65)},
    )
    assert changed.status_code == 200, changed.text
    cancelled = cliente.post(
        f"/api/reservas/{reservation.json()['id']}/cancelar", json={"motivo": "Cambio confirmado"},
    )
    assert cancelled.status_code == 200 and cancelled.json()["estado"] == "cancelada"


def test_administrador_puede_gestionar_reservas(cliente):
    entrada, salida = _fecha(70), _fecha(72)
    room_id = _habitacion_libre(cliente, 3, entrada, salida)
    reservation = _crear(cliente, None, 3, "Reserva de Administrador", room_id, entrada, salida)
    assert reservation.status_code == 201, reservation.text

    yo = cliente.get("/api/perfilado/yo")
    assert yo.status_code == 200, yo.text
    changed = cliente.patch(
        f"/api/reservas/{reservation.json()['id']}",
        json={"entrada": _fecha(73), "salida": _fecha(75)},
    )
    assert changed.status_code == 200, changed.text
    cancelled = cliente.post(
        f"/api/reservas/{reservation.json()['id']}/cancelar",
        json={"motivo": "Ajuste realizado por administración"},
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["estado"] == "cancelada"
    assert cancelled.json()["cancelada_por"] == yo.json()["email"]
    assert cancelled.json()["cancelada_en"]
