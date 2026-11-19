from playwright.sync_api import expect


def test_aviso_sobreventa(cliente, pagina, paso):
    inicio = cliente.get("/")
    assert inicio.status_code == 200
    assert "text/html" in inicio.headers.get("content-type", "")
    resumen = cliente.get("/api/ocupacion")
    assert resumen.status_code == 200
    assert resumen.json()["sobreventa"] == 3

    pagina.goto("/")
    expect(pagina.get_by_role("heading", name="Ocupación de esta noche")).to_be_visible()
    paso("Resumen de ocupación de esta noche")
    hotel = pagina.get_by_role("link", name="Gran Hotel Centro")
    expect(hotel).to_contain_text("30 vendidas")
    expect(hotel).to_contain_text("3 por encima de capacidad")
    aviso = pagina.get_by_role("alert").filter(has_text="Gran Hotel Centro")
    expect(aviso).to_contain_text("3 habitaciones vendidas por encima de su capacidad disponible")
    paso("Gran Hotel Centro aparece sobrevendido por tres habitaciones")


def test_direccion_carga_datos_de_ejemplo(cliente, pagina, paso):
    pagina.goto("/")
    expect(pagina.get_by_role("heading", name="Ocupación de esta noche")).to_be_visible()
    boton = pagina.get_by_role("button", name="Cargar datos de ejemplo")
    expect(boton).to_be_visible()
    boton.click()

    expect(pagina.get_by_text("Se cargaron los datos de ejemplo de los cinco hoteles.", exact=True)).to_be_visible()
    for nombre in (
        "Gran Hotel Centro",
        "Hotel Playa Malvarrosa",
        "Hotel Aeropuerto",
        "Hotel Sierra Calderona",
        "Hotel Congresos",
    ):
        expect(pagina.get_by_text(nombre, exact=True)).to_be_visible()
    centro = pagina.get_by_role("link", name="Gran Hotel Centro")
    expect(centro).to_contain_text("30 vendidas")
    expect(centro).to_contain_text("3 fuera de servicio")
    expect(centro).to_contain_text("3 por encima de capacidad")
    expect(pagina.get_by_role("button", name="Cargar datos de ejemplo")).to_be_hidden()

    resumen = cliente.get("/api/ocupacion")
    assert resumen.status_code == 200
    assert resumen.json() == {"hoteles": 5, "vendidas": 101, "fuera_servicio": 3, "sobreventa": 3}
    hoteles = cliente.get("/api/hoteles?limit=50&offset=0").json()
    assert hoteles["total"] == 5
    habitaciones_totales = 0
    fuera_servicio = 0
    for hotel in hoteles["items"]:
        habitaciones = cliente.get(f"/api/hoteles/{hotel['id']}/habitaciones?limit=100&offset=0").json()
        habitaciones_totales += habitaciones["total"]
        fuera_servicio += sum(fila["estado"] == "fuera_servicio" for fila in habitaciones["items"])
    assert habitaciones_totales == 149
    assert fuera_servicio == 3
    reservas = cliente.get("/api/hoteles/1/reservas?limit=100&offset=0")
    assert reservas.status_code == 200
    assert reservas.json()["total"] == 30
    paso("Dirección carga los cinco hoteles y ve la sobreventa del Gran Hotel Centro")


def test_boton_carga_no_aparece_si_hay_datos(cliente, pagina, paso):
    pagina.goto("/")
    expect(pagina.get_by_role("heading", name="Ocupación de esta noche")).to_be_visible()
    expect(pagina.get_by_role("button", name="Cargar datos de ejemplo")).to_be_hidden()

    respuesta = cliente.post("/api/ocupacion/datos-de-ejemplo")
    assert respuesta.status_code == 409
    resumen = cliente.get("/api/ocupacion")
    assert resumen.status_code == 200
    assert resumen.json()["sobreventa"] == 3
    paso("La carga no aparece ni sustituye datos que ya existen")


def test_recepcion_no_puede_cargar_datos_de_ejemplo(cliente):
    respuesta = cliente.post("/api/ocupacion/datos-de-ejemplo")
    assert respuesta.status_code == 403
    assert respuesta.headers.get("X-WAI-Codigo") == "PERFILADO_SIN_PERMISO"
    resumen = cliente.get("/api/ocupacion")
    assert resumen.status_code == 200
    assert resumen.json()["hoteles"] == 0


def test_recepcion_no_ve_boton_carga_datos(pagina, paso):
    pagina.goto("/")
    expect(pagina.get_by_role("heading", name="Ocupación de esta noche")).to_be_visible()
    expect(pagina.get_by_text("Todavía no hay datos de los hoteles cargados.", exact=True)).to_be_visible()
    expect(pagina.get_by_role("button", name="Cargar datos de ejemplo")).to_be_hidden()
    paso("Recepción ve el estado vacío sin opción de cargar datos")
