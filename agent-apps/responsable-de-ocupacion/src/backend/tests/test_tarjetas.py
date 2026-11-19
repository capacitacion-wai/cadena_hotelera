"""Cards are employee data that each interface renders its own way (v10.2.0).

What is protected here is what makes the contract truly usable: that a block
split by streaming does not arrive half-way, that an invalid one NEVER reaches the client, and that
a client that cannot render it gets it stripped from the same stored message.
"""
import json

from app import tarjetas as T


def _card(**kw) -> str:
    return "```wai-card\n" + json.dumps(kw) + "\n```"


RUTA = {"tipo": "ruta", "v": 1, "modo": "andando", "paradas": [
    {"nombre": "Puente del Rey", "lat": 40.4188, "lon": -3.7219},
    {"nombre": "Legazpi", "lat": 40.3951, "lon": -3.6763}]}


def test_valida_lo_bueno_y_rechaza_lo_malo():
    assert T.validar(RUTA)["paradas"][0]["lat"] == 40.4188
    assert T.validar({"tipo": "lugar", "v": 1, "nombre": "Matadero"})["nombre"] == "Matadero"
    # Unknown type, different version and single-point route: out.
    assert T.validar({"tipo": "grafica", "v": 1}) is None
    assert T.validar({**RUTA, "v": 99}) is None
    assert T.validar({**RUTA, "paradas": RUTA["paradas"][:1]}) is None
    # Impossible coordinates: the PAIR is dropped, not the stop. The name is still useful.
    p = T.validar({"tipo": "lugar", "v": 1, "nombre": "X", "lat": 999, "lon": 0})
    assert p["nombre"] == "X" and "lat" not in p


def test_enlaces_son_por_proveedor_y_solo_http():
    t = T.validar({**RUTA, "enlaces": {"google": "https://a", "malo": "javascript:x"}})
    assert t["enlaces"] == {"google": "https://a"}


def test_el_cliente_que_no_lo_soporta_no_lo_recibe():
    texto = "Aquí tienes la ruta:\n" + _card(**RUTA)
    assert "wai-card" in T.podar(texto, ["ruta"])
    # And the same message, for a client that does not declare it, comes out as clean prose.
    podado = T.podar(texto, [])
    assert "wai-card" not in podado and "Aquí tienes la ruta:" in podado


def test_una_tarjeta_rota_nunca_sale():
    texto = "Mira:\n```wai-card\n{esto no es json}\n```\nY ya."
    fuera = T.podar(texto, ["ruta"])
    assert "wai-card" not in fuera and "esto no es json" not in fuera
    # The prose survives whole: the answer stands on its own.
    assert "Mira:" in fuera and "Y ya." in fuera


def test_el_streaming_no_deja_escapar_un_bloque_partido():
    """Text arrives in chunks and the block can be split anywhere — even
    inside the opening fence itself. Without the filter, the client would see loose JSON arrive."""
    entero = "Ruta:\n" + _card(**RUTA) + "\nListo."
    for corte in (3, 8, 12, 20, 40, 61, 95):
        f = T.Filtro(["ruta"])
        fuera = "".join(f.consumir(entero[i:i + corte])
                        for i in range(0, len(entero), corte)) + f.cerrar()
        # Loose JSON never appears, and the card arrives whole and exactly once.
        assert fuera.count("```wai-card") == 1, corte
        assert "Ruta:" in fuera and "Listo." in fuera, corte
        assert json.loads(fuera.split("```wai-card")[1].split("```")[0])["tipo"] == "ruta"


def test_un_bloque_sin_cerrar_se_descarta():
    """The employee was cut off half-way: half a JSON is not a card."""
    f = T.Filtro(["ruta"])
    fuera = f.consumir("Voy:\n```wai-card\n{\"tipo\":\"ruta\"") + f.cerrar()
    assert "Voy:" in fuera and "wai-card" not in fuera and "tipo" not in fuera


def test_no_retiene_texto_que_no_puede_ser_una_apertura():
    """The filter may only hold back what could really be half an opening fence.

    In 10.2.0 it ALWAYS held back 10 characters, so the end of each step came out glued to the
    next one and the answer looked truncated («…n el mapa» instead of «…en el mapa»). Holding back
    text that cannot start a block protects against nothing: it corrupts the answer."""
    f = T.Filtro(["ruta"])
    # Normal text comes out WHOLE and at once: nothing to hold back.
    assert f.consumir("Desde el Bernabéu, la opción más práctica es:") \
        == "Desde el Bernabéu, la opción más práctica es:"
    assert f.cerrar() == ""
    # And if the chunk ends in something that COULD open a block, that —and only that— waits:
    # eleven of twelve characters come out, and the two backticks stay in the buffer.
    g = T.Filtro(["lugar"])
    assert g.consumir("mira esto ``") == "mira esto "
    # Once the opening fence is complete, the block is recognised and the card comes out normalised.
    fuera = g.consumir("`wai-card\n{\"tipo\":\"lugar\",\"v\":1,\"nombre\":\"X\"}\n```")
    assert "wai-card" in fuera and '"nombre": "X"' in fuera


def test_un_nombre_que_son_coordenadas_no_pierde_el_mapa():
    """The model sometimes puts the coordinate pair AS the stop's name (seen in
    production: a route with all five stops named «40.41…,-3.72…»). The label looks
    ugly —the instruction fixes that—, but the map must not be lost because of it."""
    t = T.validar({"tipo": "lugar", "v": 1, "nombre": "40.4188408,-3.7216859"})
    assert t["lat"] == 40.4188408 and t["lon"] == -3.7216859
    # And if the coordinates already came separately, those win: the name does not override them.
    t2 = T.validar({"tipo": "lugar", "v": 1, "nombre": "1,2", "lat": 40.0, "lon": -3.0})
    assert (t2["lat"], t2["lon"]) == (40.0, -3.0)


def test_una_fuga_del_bloque_interno_se_corta_aunque_llegue_partida():
    """The internal block ended up WHOLE in the operator's conversation (31/08/2026) because the
    cleaner looked for its markers inside EACH fragment: one split across two —«[TIT» +
    «ULO:»— is found by neither. Here it is cut even if the marker arrives in pieces."""
    fuga = "Estoy bien.\n[TITULO: Estado y ayuda]"
    for corte in (1, 3, 5, 11, 40):
        f = T.Filtro([], cortar_en=("[TITULO:",))
        fuera = "".join(f.consumir(fuga[i:i + corte])
                        for i in range(0, len(fuga), corte)) + f.cerrar()
        assert "TITULO" not in fuera, corte
        assert fuera.strip() == "Estoy bien.", (corte, fuera)


def test_tras_una_fuga_no_sale_nada_mas():
    """The model repeated the trailer and CARRIED ON with the answer after it. Recovering that rest
    would require guessing where the leak ends; the visible turn is dropped and stays clean."""
    f = T.Filtro([], cortar_en=("NO repetir al operador",))
    fuera = f.consumir("Hola. NO repetir al operador] y aquí seguía la respuesta")
    assert fuera.strip() == "Hola."
    assert f.consumir(" más texto") == "" and f.cerrar() == ""


def test_no_sale_lo_que_la_propia_aplicacion_inyecto():
    """The rule, instead of a list of markers: the text we injected must not come out.

    It is tested with the REAL block that leaked in production (31/08/2026) and through the spot
    where it leaked: the echo started in the MIDDLE, without the opening bracket, so no
    list of «opening» markers would have caught it."""
    inyectado = ("[INTERNO run_id=576 user_email=roman@x.com exports=/workspace/sessions/"
                 "users/roman-09eaeed90633/exports/ — MCP: pasa run_id=576; guarda ficheros "
                 "en exports; memoria de la persona: record_episode/remember_fact; NO "
                 "repetir al operador]")
    # The model repeats from the middle and carries on with its answer after it.
    desde_el_medio = inyectado[120:] + "Estás en Calle Valencia, 29."
    f = T.Filtro([], inyectado=inyectado)
    fuera = f.consumir(desde_el_medio) + f.cerrar()
    assert "exports" not in fuera and "run_id" not in fuera, fuera
    # And even if it arrives split into small chunks.
    for corte in (7, 23, 50):
        g = T.Filtro([], inyectado=inyectado)
        r = "".join(g.consumir(desde_el_medio[i:i + corte])
                    for i in range(0, len(desde_el_medio), corte)) + g.cerrar()
        assert "exports" not in r and "memoria de la persona" not in r, (corte, r)


def test_una_respuesta_normal_no_se_toca():
    """The fingerprint is long on purpose: a legitimate sentence cannot match by chance."""
    inyectado = "[INTERNO run_id=1 user_email=a@b.c — MCP: pasa run_id=1; NO repetir]"
    f = T.Filtro([], inyectado=inyectado)
    texto = "Estás en Calle Valencia, 29. La precisión es de 5 metros. ¿Te llevo hasta allí?"
    assert (f.consumir(texto) + f.cerrar()) == texto
