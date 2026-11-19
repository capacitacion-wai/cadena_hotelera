"""What gets resolved in one go, and who decides how much thinking happens (19/09/2026).

Two pieces that, without guards, fail SILENTLY, which is what makes them dangerous:

* a shortcut that fires where it should not answers something else —the trips of the
  wrong person— and nothing screams;
* a conversational model that proposes a level can override a person's choice, and the
  only visible symptom would be somewhat worse answers.

That is why what is pinned here is the ORDER and the brakes, not the happy path.
"""
from __future__ import annotations

import pytest

from app import atajos, razonamiento as r


# ── Level precedence ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_lo_que_elige_la_persona_gana_a_todo():
    nivel, quien = await r.resolver("nadie@x.com", "profundo", propuesto="rapido")
    assert (nivel, quien) == ("profundo", "persona")


@pytest.mark.asyncio
async def test_el_conversador_solo_ocupa_el_hueco_de_nadie():
    """It replaces the blind `medium`, not a choice. If someone chose, it does not even weigh in."""
    nivel, quien = await r.resolver("nadie@x.com", None, propuesto="profundo")
    assert (nivel, quien) == ("profundo", "conversador")


@pytest.mark.asyncio
async def test_el_conversador_no_puede_mandar_no_pensar():
    """«Don't think» can only be said by a declaration about a specific capability. The
    worst case of it being wrong must be a weaker answer, never one that was not even
    attempted."""
    nivel, quien = await r.resolver("nadie@x.com", None, propuesto="directo")
    assert quien == "modelo" and nivel == ""


@pytest.mark.asyncio
async def test_sin_nadie_que_elija_no_se_manda_nada():
    nivel, quien = await r.resolver(None, None)
    assert (nivel, quien) == ("", "modelo")


# ── The variant: both decisions in one id ───────────────────────────────────

def test_las_dos_decisiones_viajan_juntas():
    assert r.variante("profundo", "breve") == "profundo-breve"
    assert r.variante("profundo", "") == "profundo"
    # Asking for «breve» without a level cannot force a reasoning setting nobody asked for.
    assert r.variante("", "breve") == "solo-breve"
    assert r.variante("", "") == ""
    # «normal» means not forcing: it does not exist as a variant.
    assert r.variante("rapido", "normal") == "rapido"


# ── The shortcut ────────────────────────────────────────────────────────────

ATAJO = {"id": "consultar", "que": "Consultar tus viajes",
         "herramienta": "wai-viajes_consultar", "argumentos": ["persona"],
         "salvo_si": None}


def test_sin_todos_los_datos_no_hay_atajo():
    """Fired with a gap, it answers SOMETHING ELSE —the trips of the wrong person— and that
    is worse than taking twenty seconds."""
    assert atajos.faltan_argumentos(ATAJO, {}) == ["persona"]
    assert atajos.faltan_argumentos(ATAJO, {"persona": ""}) == ["persona"]
    assert atajos.faltan_argumentos(ATAJO, {"persona": "ana@x.com"}) == []


def test_un_empleado_sin_atajos_declarados_no_paga_nada():
    """This is the default state and it must cost ZERO: no declaration means no catalog,
    and no catalog means no recognition and no extra call."""
    assert atajos.catalogo() == []
    assert atajos.buscar("lo-que-sea") is None


@pytest.mark.asyncio
async def test_los_interruptores_vienen_encendidos():
    """What they replace is a blind constant, not anybody's choice."""
    assert await atajos.activos() is True
    assert await r.conversador_puede_elegir() is True


# ── Where a shortcut's tool lives (20/09/2026) ──────────────────────────────

@pytest.mark.asyncio
async def test_una_herramienta_de_conector_no_se_busca_entre_las_locales(monkeypatch):
    """THE MEASURED FAILURE: the shortcut only looked at `mcp_server.TOOLS` —the employee's
    70 local tools— and failed with `la herramienta no existe en este empleado` on
    `wai-viajes_consultar_contexto_usuario`, which did exist: behind the MCP proxy. The
    shortcut fell back to the normal path on EVERY question and `¿qué viajes tengo?` still
    took twenty seconds despite its declared shortcut."""
    visto = {}

    async def _llamar(conector, tool, argumentos, acting_user=""):
        visto.update(conector=conector, tool=tool, argumentos=argumentos)
        return {"content": [{"type": "text", "text": "4 viajes"}]}

    async def _sin_veto(*_a, **_k):
        return None

    monkeypatch.setattr("app.routes.mcp_proxy_routes.llamar_herramienta", _llamar)
    monkeypatch.setattr("app.mcp_actor.gate_error", _sin_veto)
    # The connector permissions are the platform's table (14.8.0); this test is about WHERE
    # the tool lives, not about its permission.
    monkeypatch.setattr("app.mcp_policy.veto_for_call", _sin_veto)
    monkeypatch.setattr("app.mcp_actor.run_serving_user", _nadie)
    r = await atajos.ejecutar(
        {"herramienta": "wai-viajes_consultar_contexto_usuario"},
        {"persona": "ana@x.com"}, run_id=1)
    assert r["ok"] is True
    # Split at the FIRST underscore: slugs contain underscores.
    assert visto["conector"] == "wai-viajes"
    assert visto["tool"] == "consultar_contexto_usuario"


async def _nadie(_run_id):
    return None


@pytest.mark.asyncio
async def test_el_atajo_llama_al_conector_COMO_LA_PERSONA_que_pregunta(monkeypatch):
    """THE MEASURED FAILURE (21/09/2026): `¿Qué viajes tengo?` → Direct →
    `No hay información disponible sobre tus viajes`; `no vayas directo, consulta al empleado` → five trips.
    The agent passes `run_token` and the proxy turns it into the person before calling the
    connector; the shortcut called as nobody. The identity comes from the run, through the same gate."""
    visto = {}

    async def _llamar(conector, tool, argumentos, acting_user=""):
        visto.update(acting_user=acting_user, argumentos=argumentos)
        return {"content": [{"type": "text", "text": "5 viajes"}]}

    async def _sin_veto(*_a, **_k):
        return None

    async def _la_persona_del_run(run_id):
        assert run_id == 697
        return "Admin@wai.com"

    monkeypatch.setattr("app.routes.mcp_proxy_routes.llamar_herramienta", _llamar)
    monkeypatch.setattr("app.mcp_policy.veto_for_call", _sin_veto)
    monkeypatch.setattr("app.mcp_actor.run_serving_user", _la_persona_del_run)
    r = await atajos.ejecutar(
        {"herramienta": "wai-viajes_consultar_contexto_usuario"}, {}, run_id=697)
    assert r["ok"] is True
    assert visto["acting_user"] == "admin@wai.com"       # normalized, as in the proxy
    assert "run_token" not in visto["argumentos"]        # identity is not an argument


@pytest.mark.asyncio
async def test_cada_puerta_en_su_sitio(monkeypatch):
    """EACH KIND OF TOOL HAS ITS OWN GATE, AND THEY ARE NOT INTERCHANGEABLE.

    The employee's LOCAL tools are governed by `gate_error` (with `TOOL_MIN_ROLE`); a
    CONNECTOR's, by the proxy's write policy. Passing a connector tool through `gate_error`
    ALWAYS denies it —it is not in that table, and what is undeclared is denied— and the user
    reads `esa capacidad no está autorizada en este empleado` about something that is authorized.
    Measured on 20/09/2026 with the shortcut already firing: `Directo · consultar_contexto_viajero`
    and, below it, the refusal."""
    llamado = {}

    async def _llamar(conector, tool, argumentos, acting_user=""):
        llamado['si'] = True
        return {"content": []}

    async def _gate_que_denegaria(*_a, **_k):
        llamado['gate'] = True
        return {"error": "Esa capacidad no está autorizada en este empleado."}

    async def _sin_veto(*_a, **_k):
        return None

    monkeypatch.setattr("app.routes.mcp_proxy_routes.llamar_herramienta", _llamar)
    monkeypatch.setattr("app.mcp_actor.gate_error", _gate_que_denegaria)
    monkeypatch.setattr("app.mcp_policy.veto_for_call", _sin_veto)
    r = await atajos.ejecutar(
        {"herramienta": "wai-viajes_consultar_contexto_usuario"}, {}, run_id=1)
    assert r["ok"] is True, "una herramienta de conector no puede pasar por gate_error"
    assert llamado.get('si') and not llamado.get('gate')


@pytest.mark.asyncio
async def test_la_politica_del_conector_SI_se_aplica(monkeypatch):
    """And the one that applies does govern: if the operator set the connector to read-only
    and someone declared a write shortcut there, it stops — just as the agent would."""
    async def _veta(*_a, **_k):
        return "No puedo escribir en ese sistema: está en solo lectura."
    async def _llamar(*_a, **_k):
        raise AssertionError("no debería haberse llamado")
    monkeypatch.setattr("app.mcp_policy.veto_for_call", _veta)
    monkeypatch.setattr("app.routes.mcp_proxy_routes.llamar_herramienta", _llamar)
    r = await atajos.ejecutar({"herramienta": "wai-viajes_borrar_todo"}, {}, run_id=1)
    assert r["ok"] is False and "solo lectura" in r["denegado"]["error"]


def test_solo_viajan_los_argumentos_declarados():
    """What recognizes the request is a model, and models fill gaps. Asked for the data of a
    capability that needs NONE, it invented `peticion` and the tool died with «got an
    unexpected keyword argument 'peticion'» — measured on 20/09/2026 with the shortcut
    already firing. The declaration is what rules."""
    sin_datos = {"herramienta": "wai-viajes_consultar_contexto_usuario", "argumentos": []}
    assert atajos.solo_los_declarados(sin_datos, {"peticion": "cuáles son mis viajes"}) == {}

    con_datos = {"herramienta": "wai-viajes_consultar_viaje", "argumentos": ["trip_id"]}
    # What is declared passes; what is invented stays out, in the same call.
    assert atajos.solo_los_declarados(
        con_datos, {"trip_id": 4, "peticion": "el de Budapest"}) == {"trip_id": 4}
    # And if something declared is missing, it is still missing: filtering does not fill.
    assert atajos.faltan_argumentos(
        con_datos, atajos.solo_los_declarados(con_datos, {"peticion": "x"})) == ["trip_id"]


@pytest.mark.asyncio
async def test_reconocer_usa_el_modelo_del_empleado(monkeypatch):
    """THE MEASURED FAILURE: `reconocer` looked up the model in `AGENT_SMALL_MODEL`/`GENAI_MODEL`,
    two variables the deployer does NOT inject into the employee's pod. It returned an
    empty string, the call was skipped, and by VOICE a shortcut was NEVER recognized — while
    in writing it worked, because the reception resolves its model another way. Two
    paths for the same decision, and one without a model."""
    monkeypatch.delenv("AGENT_SMALL_MODEL", raising=False)
    monkeypatch.delenv("GENAI_MODEL", raising=False)

    async def _del_operador():
        return "gpt-5.6-luna"
    monkeypatch.setattr("app.recepcion._modelo_del_operador", _del_operador)
    assert await atajos._modelo_pequeno() == "gpt-5.6-luna"


@pytest.mark.asyncio
async def test_se_puede_poner_uno_mas_barato(monkeypatch):
    """`AGENT_SMALL_MODEL` is still the way for an operator to put another model here.
    What changes is that its absence no longer leaves the piece without a model."""
    monkeypatch.setenv("AGENT_SMALL_MODEL", "openai/gpt-4.1-mini")
    assert await atajos._modelo_pequeno() == "gpt-4.1-mini"


# ── What is SHOWN when the shortcut answers ─────────────────────────────────

def _payload_vacio():
    class P:
        attachments = None
        request_id = None
        capacidad = None
        argumentos = None
    return P()


async def _resuelve_por(canal, monkeypatch, *, redaccion="Tienes 4 viajes."):
    """Runs `_atajo_resuelve` through the given gate and returns what would have
    closed the turn."""
    from app.routes import agent_routes as ar

    crudo = {"content": [{"type": "text", "text": '{"viajes": [{"id": 4}]}'}]}
    dicho = {}

    async def _activos():
        return True

    async def _ejecutar(*_a, **_k):
        return {"ok": True, "resultado": crudo}

    async def _redactar(*_a, **_k):
        return redaccion

    async def _cerrar(_turn, *, texto, **_k):
        dicho["texto"] = texto

    monkeypatch.setattr(atajos, "activos", _activos)
    monkeypatch.setattr(atajos, "buscar",
                        lambda _c: {"herramienta": "wai-viajes_consultar_contexto_usuario",
                                    "argumentos": []})
    monkeypatch.setattr(atajos, "ejecutar", _ejecutar)
    monkeypatch.setattr(atajos, "redactar", _redactar)
    monkeypatch.setattr(ar, "_cerrar_turno_corto", _cerrar)

    class _Med:
        def por_atajo(self, _c): pass

    ok = await ar._atajo_resuelve(
        None, payload=_payload_vacio(), canal=canal,
        reconocido={"capacidad": "consultar_contexto_viajero", "argumentos": {}},
        nivel_pedido="", lo_puso="nadie", medicion=_Med(), run_id=1,
        user_message="dime mis viajes", hilo_id=None, scope=None,
        chat_email="ana@x.com", started_at=0.0, started_wall=0.0, session_id="s")
    assert ok is True
    return dicho["texto"]


@pytest.mark.asyncio
@pytest.mark.parametrize("canal", ["voz", "chat"])
async def test_el_atajo_nunca_deja_el_crudo_a_la_vista(canal, monkeypatch):
    """THE MEASURED FAILURE (20/09/2026): by voice the shortcut returned the tool's result
    AS IS —`ya redacta el conversador, que es quien habla`—. And yes, it read it out
    fine; but that same text is what gets stored in the thread, so the whole
    `{"content":[{"type":"text",...}]}` showed up on screen.

    Both gates answer the same thing: prose. The normal path never returns raw output
    by voice either —it returns what the agent wrote—, so it was the shortcut that broke
    the contract."""
    texto = await _resuelve_por(canal, monkeypatch)
    assert texto == "Tienes 4 viajes."
    assert '"content"' not in texto and '"viajes"' not in texto


@pytest.mark.asyncio
@pytest.mark.parametrize("canal", ["voz", "chat"])
async def test_sin_redaccion_tampoco_se_suelta_el_json(canal, monkeypatch):
    """And if the wording fails, the raw output does NOT come out EITHER: in the chat it is
    unreadable and by voice it is read aloud, which is worse. It says it could not, and that's it."""
    texto = await _resuelve_por(canal, monkeypatch, redaccion="")
    assert "{" not in texto and "content" not in texto
    assert texto.strip()
