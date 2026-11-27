def test_recepcion_ve_todos_los_hoteles(cliente):
    response = cliente.get("/api/hoteles?limit=50&offset=0")
    assert response.status_code == 200
    result = response.json()
    assert result["total"] == 5
    names = {hotel["nombre"] for hotel in result["items"]}
    assert names == {
        "Gran Hotel Centro",
        "Hotel Playa Malvarrosa",
        "Hotel Aeropuerto",
        "Hotel Sierra Calderona",
        "Hotel Congresos",
    }


def test_consulta_no_puede_proponer_reubicacion(cliente):
    response = cliente.post("/api/reubicaciones", json={
        "reserva_id": 10001,
        "hotel_destino_id": 3,
        "motivo": "Consulta no puede proponer movimientos.",
    })
    assert response.status_code == 403


def test_consulta_no_puede_aprobar_reubicacion(cliente):
    response = cliente.post("/api/reubicaciones/1/aprobar", json={})
    assert response.status_code == 403


def test_consulta_no_puede_cargar_datos_de_ejemplo(cliente):
    response = cliente.post("/api/ocupacion/datos-de-ejemplo")
    assert response.status_code == 403
    occupancy = cliente.get("/api/ocupacion")
    assert occupancy.status_code == 200 and occupancy.json()["hoteles"] == 0
