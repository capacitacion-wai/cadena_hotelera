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
