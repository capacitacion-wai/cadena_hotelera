from playwright.sync_api import expect


def test_comparar_y_proponer(cliente, pagina, paso):
    pagina.goto("/reubicaciones")
    expect(pagina.get_by_role("heading", name="Reubicaciones")).to_be_visible()
    expect(pagina.get_by_text("Hotel Aeropuerto", exact=True).first).to_be_visible()
    expect(pagina.get_by_text("Hotel Congresos", exact=True).last).to_be_visible()
    paso("Alternativas de la cadena con sitio, distancia y tarifa")

    pagina.get_by_role("button", name="Proponer reubicación").click()
    pagina.get_by_role("combobox", name="Huésped por reubicar").click()
    pagina.get_by_role("option", name="Jordi Roca · Gran Hotel Centro").click()
    pagina.get_by_role("combobox", name="Hotel de destino").click()
    pagina.get_by_role("option", name="Hotel Aeropuerto", exact=False).click()
    pagina.get_by_role("textbox", name="Motivo").fill("Habitación fuera de servicio por una fuga de agua.")
    pagina.get_by_role("button", name="Guardar propuesta").click()
    expect(pagina.get_by_text("Propuesta registrada. El huésped no se moverá hasta que Dirección la apruebe.", exact=True)).to_be_visible()
    expect(pagina.get_by_role("heading", name="Jordi Roca")).to_be_visible()
    paso("La propuesta queda pendiente; el huésped aún no se ha movido")


def test_recepcion_no_aprueba(cliente):
    propuestas = cliente.get("/api/reubicaciones?limit=20&offset=0").json()["propuestas"]["items"]
    propuesta_id = propuestas[0]["id"]
    response = cliente.post(f"/api/reubicaciones/{propuesta_id}/aprobar", json={})
    assert response.status_code == 403
    assert response.headers.get("X-WAI-Codigo") == "PERFILADO_SIN_PERMISO"


def test_direccion_aprueba_y_registra(cliente):
    propuestas = cliente.get("/api/reubicaciones?limit=20&offset=0").json()["propuestas"]["items"]
    proposal = propuestas[0]
    response = cliente.post(f"/api/reubicaciones/{proposal['id']}/aprobar", json={})
    assert response.status_code == 200

    actual = cliente.get("/api/reubicaciones?limit=20&offset=0&historial_limit=20&historial_offset=0").json()
    moved = next(row for row in actual["historial"]["items"] if row["huesped"] == proposal["huesped"])
    assert moved["estado"] == "aprobada"
    assert moved["propuesto_por"]
    assert moved["aprobado_por"]
    assert moved["propuesto_en"]
    assert moved["aprobado_en"]
    moved_reservations = cliente.get("/api/hoteles/5/reservas?limit=50&offset=0").json()["items"]
    assert any(row["huesped"] == proposal["huesped"] and row["habitacion"] != "Pendiente de asignación" for row in moved_reservations)


def test_historial_auditable(cliente):
    actual = cliente.get("/api/reubicaciones?limit=20&offset=0&historial_limit=20&historial_offset=0").json()
    assert actual["historial"]["total"] >= 1
    movement = actual["historial"]["items"][0]
    assert movement["propuesto_por"] == "recepcion@hoteles.example"
    assert movement["aprobado_por"] == "direccion@hoteles.example"
    assert movement["propuesto_en"]
    assert movement["aprobado_en"]


def test_estado_vacio_y_conflicto_de_estancia_inactiva(cliente, pagina, paso):
    datos = cliente.get("/api/reubicaciones?limit=20&offset=0").json()
    pagina.route(
        "**/api/reubicaciones/*/aprobar",
        lambda route: route.fulfill(status=409, json={"detail": "La estancia ya no está activa esta noche."}),
    )
    pagina.goto("/reubicaciones")

    pagina.get_by_role("button", name="Proponer reubicación").click()
    pagina.get_by_role("combobox", name="Huésped por reubicar").click()
    pagina.get_by_role("option", name="Jordi Roca · Gran Hotel Centro").click()
    pagina.get_by_role("combobox", name="Hotel de destino").click()
    pagina.get_by_role("option", name="Hotel Aeropuerto", exact=False).click()
    pagina.get_by_role("textbox", name="Motivo").fill("La propuesta se actualizó en otra sesión.")

    disponibles = datos["huespedes_por_reubicar"]["items"]
    destinos = [hotel for hotel in datos["destinos"]["items"] if hotel["libres"] > 0]
    for index, huesped in enumerate(disponibles):
        destino = next(hotel for hotel in destinos if hotel["id"] != huesped["origen_id"])
        response = cliente.post("/api/reubicaciones", json={
            "reserva_id": huesped["reserva_id"],
            "hotel_destino_id": destino["id"],
            "motivo": "Propuesta creada desde otra sesión de prueba.",
        })
        assert response.status_code == 201, response.text

    with pagina.expect_request(
        lambda request: request.method == "GET" and "/api/reubicaciones?" in request.url,
    ):
        pagina.get_by_role("button", name="Guardar propuesta").click()
    expect(pagina.get_by_role("alert")).to_contain_text("Ya hay una propuesta pendiente para este huésped.")
    expect(pagina.get_by_role("heading", name="Proponer una reubicación")).to_have_count(0)
    paso("El conflicto al proponer muestra el motivo y actualiza la información")

    pagina.get_by_role("button", name="Proponer reubicación").click()
    expect(pagina.get_by_text(
        "No hay huéspedes con una estancia activa y aptos para reubicar esta noche. "
        "Quienes ya tienen una propuesta pendiente aparecen en Pendientes de aprobación "
        "y no se pueden proponer otra vez.",
        exact=True,
    )).to_be_visible()
    pagina.get_by_role("combobox", name="Huésped por reubicar").click()
    expect(pagina.get_by_role("option", name="Amparo Vidal", exact=False)).to_have_count(0)
    pagina.keyboard.press("Escape")
    pagina.get_by_role("button", name="Cancelar").click()
    paso("El selector vacío explica la causa y no repite al huésped con propuesta pendiente")

    with pagina.expect_request(
        lambda request: request.method == "GET" and "/api/reubicaciones?" in request.url,
    ):
        pagina.get_by_role("button", name="Aprobar y mover huésped").first.click()
    expect(pagina.get_by_role("alert")).to_contain_text("La estancia ya no está activa esta noche.")
    paso("El conflicto de una estancia inactiva explica el rechazo y actualiza la información")
