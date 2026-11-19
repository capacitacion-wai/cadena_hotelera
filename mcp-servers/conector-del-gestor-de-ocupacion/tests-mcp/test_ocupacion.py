AUTH = {"Authorization": "Bearer test"}


def test_get_occupancy_summary(mcp, modo, request):
    expected = {"hoteles": 2, "vendidas": 8, "fuera_servicio": 1, "sobreventa": 1}
    if modo == "simulado":
        request.getfixturevalue("destino").responder("GET", "/api/ocupacion", respuesta=expected)

    result = mcp.llamar("get_occupancy_summary", cabeceras=AUTH)
    assert result == expected


def test_list_hotels(mcp, modo, request):
    expected = {
        "items": [{
            "id": 4,
            "nombre": "Hotel de prueba",
            "tarifa_noche_eur": 120.0,
            "distancia_centro_valencia_km": 1.5,
            "habitaciones_totales": 20,
            "fuera_servicio": 1,
            "vendidas": 18,
            "disponibles": 1,
            "libres": 1,
            "ocupacion_porcentaje": 90.0,
            "motivo_fuera_servicio": None,
        }],
        "total": 1,
        "limit": 10,
        "offset": 0,
    }
    if modo == "simulado":
        request.getfixturevalue("destino").responder("GET", "/api/hoteles", respuesta=expected)

    result = mcp.llamar("list_hotels", limit=10, offset=0, cabeceras=AUTH)
    assert result == expected
