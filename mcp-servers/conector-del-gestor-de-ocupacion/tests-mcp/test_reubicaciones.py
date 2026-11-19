AUTH = {"Authorization": "Bearer test"}


def test_get_relocation_options(mcp, modo, request):
    expected = {
        "propuestas": {"items": [], "total": 0, "limit": 10, "offset": 0},
        "historial": {"items": [], "total": 0, "limit": 10, "offset": 0},
        "huespedes_por_reubicar": {
            "items": [{"reserva_id": 71, "huesped": "Huésped de prueba", "origen_id": 4, "origen": "Hotel de origen"}],
            "total": 1,
            "limit": 10,
            "offset": 0,
        },
        "destinos": {
            "items": [{
                "id": 9,
                "nombre": "Hotel de destino",
                "tarifa_noche_eur": 130.0,
                "distancia_centro_valencia_km": 2.0,
                "habitaciones_totales": 25,
                "fuera_servicio": 0,
                "vendidas": 20,
                "disponibles": 5,
                "libres": 5,
                "ocupacion_porcentaje": 80.0,
                "motivo_fuera_servicio": None,
            }],
            "total": 1,
            "limit": 10,
            "offset": 0,
        },
    }
    if modo == "simulado":
        request.getfixturevalue("destino").responder("GET", "/api/reubicaciones", respuesta=expected)

    result = mcp.llamar(
        "get_relocation_options",
        limit=10,
        offset=0,
        historial_limit=10,
        historial_offset=0,
        cabeceras=AUTH,
    )
    assert result == expected


def test_propose_relocation(mcp, modo, request):
    options = {
        "propuestas": {"items": [], "total": 0, "limit": 10, "offset": 0},
        "historial": {"items": [], "total": 0, "limit": 10, "offset": 0},
        "huespedes_por_reubicar": {
            "items": [{"reserva_id": 71, "huesped": "Huésped de prueba", "origen_id": 4, "origen": "Hotel de origen"}],
            "total": 1,
            "limit": 10,
            "offset": 0,
        },
        "destinos": {
            "items": [{
                "id": 9,
                "nombre": "Hotel de destino",
                "tarifa_noche_eur": 130.0,
                "distancia_centro_valencia_km": 2.0,
                "habitaciones_totales": 25,
                "fuera_servicio": 0,
                "vendidas": 20,
                "disponibles": 5,
                "libres": 5,
                "ocupacion_porcentaje": 80.0,
                "motivo_fuera_servicio": None,
            }],
            "total": 1,
            "limit": 10,
            "offset": 0,
        },
    }
    if modo == "simulado":
        destino = request.getfixturevalue("destino")
        destino.responder("GET", "/api/reubicaciones", respuesta=options)
        destino.responder("POST", "/api/reubicaciones", respuesta={"id": 203, "estado": "pendiente"})

    current = mcp.llamar(
        "get_relocation_options",
        limit=10,
        offset=0,
        historial_limit=10,
        historial_offset=0,
        cabeceras=AUTH,
    )
    guest = current["huespedes_por_reubicar"]["items"][0]
    hotel = current["destinos"]["items"][0]
    result = mcp.llamar(
        "propose_relocation",
        reserva_id=guest["reserva_id"],
        hotel_destino_id=hotel["id"],
        motivo="Reubicación de prueba por sobreventa.",
        cabeceras=AUTH,
    )

    assert isinstance(result["id"], int)
    assert isinstance(result["estado"], str) and result["estado"]
    if modo == "simulado":
        assert not any(p["ruta"].endswith("/aprobar") for p in destino.peticiones)
