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
