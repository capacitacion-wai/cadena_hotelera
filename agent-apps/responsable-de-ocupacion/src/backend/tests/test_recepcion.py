"""The conversation, also when it is written (19/09/2026).

What this piece can break is invisible: it answers something about the business itself and
leaves a false fact someone believes, or it returns an odd decision and the turn goes down a
path nobody asked for. That is why what is pinned here is that **anything that does not fit
ends in `delego`** — the usual path, where being wrong costs nothing.
"""
from __future__ import annotations

import pytest

from app import recepcion as r


def test_lo_que_no_encaja_acaba_delegando():
    """A model that returns something odd cannot end up answering on its own."""
    for entrada in ({}, {"accion": "inventada"}, {"accion": ""}, None,
                    {"accion": "contesto"},            # no text, no answer
                    {"accion": "contesto", "texto": "   "},
                    {"accion": "atajo"},               # no capability, no shortcut
                    {"accion": "atajo", "capacidad": ""}):
        assert r.normalizar(entrada or {})["accion"] == "delego", entrada


def test_una_decision_buena_se_respeta():
    d = r.normalizar({"accion": "pregunto", "texto": "¿Qué fechas?"})
    assert d == {"accion": "pregunto", "texto": "¿Qué fechas?"}
    d = r.normalizar({"accion": "atajo", "capacidad": "consultar_viajes",
                      "argumentos": {"persona": "ana@x.com"}})
    assert d["capacidad"] == "consultar_viajes" and d["argumentos"]["persona"] == "ana@x.com"


def test_el_nivel_sugerido_solo_vale_si_es_uno_de_los_tres():
    assert r.normalizar({"accion": "delego", "razonamiento": "profundo"})["razonamiento"] == "profundo"
    # Not `directo`: only a declaration on a specific capability can say that.
    assert r.normalizar({"accion": "delego", "razonamiento": "directo"})["razonamiento"] == ""
    assert r.normalizar({"accion": "delego", "razonamiento": "turbo"})["razonamiento"] == ""


def test_una_respuesta_kilometrica_no_se_recorta_SE_DELEGA():
    """THE HOLE (until 12.36.0): the cap's comment said "if it needs more than this,
    it was not its to answer" and the code TRUNCATED and showed the piece. An invented
    1,200-character route was not rejected: it was served half-way, looking like a good answer.

    The cap does not judge the content —nobody here knows how to—, it judges the SHAPE:
    long where short was expected."""
    for accion in ("contesto", "pregunto"):
        assert r.normalizar({"accion": accion, "texto": "x" * 5000}) == {"accion": "delego"}


def test_lo_corto_sigue_pasando():
    """And a courtesy or a unit conversion fit easily: the cap cannot turn the
    reception into a forwarder that never answers anything."""
    d = r.normalizar({"accion": "contesto", "texto": "Dos y dos son cuatro."})
    assert d == {"accion": "contesto", "texto": "Dos y dos son cuatro."}


def test_la_costura_de_la_memoria_cuenta_lo_que_el_agente_no_vio():
    """The opencode session IS the conversation's memory. If three messages are
    resolved at reception and do not go through it, the agent will have a gap and may
    contradict something already discussed — days later and looking like the model's fault."""
    historial = [
        {"de": "persona", "texto": "hola"},
        {"de": "agente", "texto": "tienes dos viajes", "atendido_por": "agente"},
        {"de": "persona", "texto": "gracias"},
        # From outside this answer is identical to the one above: both are the
        # employee's text. Only `atendido_por` tells apart what the agent did NOT see.
        {"de": "agente", "texto": "a ti", "atendido_por": "recepcion"},
        {"de": "persona", "texto": "apúntame Lisboa"},
    ]
    texto = r.pendiente_de_contar(historial)
    assert "gracias" in texto and "apúntame Lisboa" in texto
    # Not what came BEFORE its last turn: it already has that.
    assert "hola" not in texto and "tienes dos viajes" not in texto


def test_sin_nada_que_contar_no_se_ensucia_el_encargo():
    assert r.pendiente_de_contar(
        [{"de": "agente", "texto": "ya está", "atendido_por": "agente"}]) == ""
    assert r.pendiente_de_contar([]) == ""


@pytest.mark.asyncio
async def test_sin_desplegarla_no_existe(monkeypatch):
    """It is in BETA: if the deployment does not bring it, it does not run — even if the
    Operations switch is on. The former rules over the latter and not the other way round: a
    switch governing something that is not deployed is a control that does nothing."""
    monkeypatch.setattr("app.config.RECEPTION_ENABLED", False, raising=False)
    assert await r.activa() is False


@pytest.mark.asyncio
async def test_desplegada_viene_encendida(monkeypatch):
    """And if the employee DOES have it, it starts running: the Operations switch
    is there to turn it off if something goes wrong, not something you have to discover."""
    monkeypatch.setattr("app.config.RECEPTION_ENABLED", True, raising=False)
    assert await r.activa() is True


@pytest.mark.asyncio
async def test_sin_clave_no_se_decide_nada_y_se_delega(monkeypatch):
    """Any failure —no key, no network, broken JSON— falls back to the usual path. A
    convenience cannot prevent talking."""
    monkeypatch.setattr("app.config.TRANSCRIPTION_API_KEY", "", raising=False)
    assert await r.decidir("hola") == {}


# ── The same rule at both doors ─────────────────────────────────────────────

def test_la_prueba_de_que_algo_no_es_tuyo_es_la_misma_en_voz_y_por_escrito():
    """Written in two places it diverges by the third change and one of the two doors starts
    answering from memory. So the test is ONE text, and both embed it."""
    from app.conversacion import QUE_PUEDES_CONTESTAR_TU
    from app.voz_texto import TEXTO_VOZ

    assert "{QUE_PUEDES_CONTESTAR}" in TEXTO_VOZ, (
        "la voz tiene que incrustar la prueba compartida, no una copia suya")
    assert QUE_PUEDES_CONTESTAR_TU in r.CONTRATO.replace(
        "{QUE_PUEDES_CONTESTAR}", QUE_PUEDES_CONTESTAR_TU)


def test_lo_que_el_empleado_hace_no_lo_contesta_la_conversacion():
    """THE MEASURED FAILURE (20/09/2026): "plan me a route for this afternoon around Madrid Río"
    —with `recomendar_y_navegar` and `organizar_experiencias` declared— was resolved entirely
    from memory, without A SINGLE call to the employee.

    The existing test checked whether the answer "would be different for another person", and a
    walk through a public park passes that test: it is the same for everyone. The first and
    decisive one was missing: is it on the list of what the employee does? If it is, it is not yours.
    """
    from app.conversacion import QUE_PUEDES_CONTESTAR_TU as prueba

    # The first question is about the LIST, not about how variable the answer is.
    cabeza = prueba[:prueba.find("THE SECOND")]
    assert "one of the things it does" in cabeza
    for verbo in ("route", "plan", "Recommending", "comparing"):
        assert verbo in cabeza, f"la primera prueba no nombra «{verbo}»"

    # And a missing piece of data does not open the door to filling in the rest: that was the
    # second leg of the same failure ("you don't give me the location, but here is a general route").
    assert "DOES NOT AUTHORISE YOU TO INVENT THE REST" in prueba
    # No line breaks: the text is justified and the excuses end up split.
    seguido = " ".join(prueba.split())
    for coartada in ("in the meantime", "in general", "broadly speaking"):
        assert coartada in seguido, coartada


def test_por_voz_hay_orden_de_parar_y_acuse_de_recibo():
    """Also measured: an "OK." made it repeat the whole list of trips, and a
    "Finish." made it write a new plan. Neither one is a request."""
    from app.voz_texto import TEXTO_VOZ

    assert "WHEN NOTHING IS BEING ASKED OF YOU" in TEXTO_VOZ
    for palabra in ("«Ok»", "«gracias»", "«para»", "«cancela»"):
        assert palabra in TEXTO_VOZ, palabra
    assert "keep talking under another name" in TEXTO_VOZ  # closing by "summarising" does not count


# ── It must not be TIRESOME (12.52.0) ────────────────────────────────────────────────────
# Measured on 22/09/2026 asking for a hotel in Madrid: seven messages, six questions, two of
# them identical word for word, one asking for the YEAR and another asking for the adults that
# had already been given in the first message. Three causes, and none was the model:

def test_la_ventana_cuenta_INTERCAMBIOS_no_entradas():
    """The list has one entry PER MESSAGE, so cutting it by the plain number of
    exchanges showed HALF: six entries are three messages, and the first one
    —where "two adults and two children" was— dropped out exactly when it was needed."""
    historial = []
    for i in range(1, 13):
        historial.append({"de": "persona", "texto": f"mensaje {i}"})
        historial.append({"de": "agente", "texto": f"respuesta {i}"})

    p = r._prompt([], [], [], historial, "y ahora qué", quien="", zona="Europe/Madrid")
    charla = p[1]["content"]
    # Eight exchanges are SIXTEEN entries: messages 5 to 12.
    assert "mensaje 5" in charla, "la ventana enseña la mitad de lo que dice enseñar"
    assert "mensaje 12" in charla
    assert "mensaje 4" not in charla, "la ventana no se para donde debería"


def test_sabe_qué_día_es():
    """Without this it cannot resolve "tomorrow" or know what year it lives in, and asks — which is
    exactly what it did. No model knows the date unless told."""
    from datetime import datetime
    p = r._prompt([], [], [], [], "quiero un hotel para mañana", quien="", zona="Europe/Madrid")
    sistema = p[0]["content"]
    assert "TODAY is" in sistema
    assert str(datetime.now().year) in sistema, "sin el año, vuelve a preguntarlo"
    assert "do not ask for a date that can already be deduced" in sistema


def test_lo_opcional_no_se_pregunta():
    """Asking for "budget or preferences" is what turns a question into an interrogation."""
    p = r._prompt([], [], [], [], "un hotel", quien="", zona="UTC")
    assert "What is OPTIONAL is not asked" in p[0]["content"]


def test_una_zona_mal_escrita_no_deja_sin_fecha():
    assert "TODAY is" in r._hoy("Marte/Olympus")


# ── What is NOT asked (12.56.0) ──────────────────────────────────────────────────────────
# Measured on 24/09/2026 in the travel assistant, by voice: "what trips do I have?" answered
# "I need the active session…" without querying anything, because the capability declared "I need
# to be given: the active session" and the conversation read it as data to ASK for. And "a
# restaurant near my hotel" asked which hotel it was, which the employee has in its bookings.

def test_la_sesion_y_lo_que_el_tiene_no_se_preguntan():
    from app.conversacion import LO_QUE_NO_SE_PIDE as regla

    seguido = " ".join(regla.split())
    assert "the session is active by definition" in seguido
    assert "THEIR DATA" in seguido and "«Cerca de mi hotel»" in seguido
    # And when in doubt, ask the employee, not the person: it is the same brake as `contesto`.
    assert "If you doubt whether the data is in their data, do NOT ask" in seguido


def test_la_regla_va_en_las_dos_puertas_y_con_la_misma_redaccion():
    from app.conversacion import LO_QUE_NO_SE_PIDE
    from app.voz_texto import TEXTO_VOZ

    assert "{LO_QUE_NO_SE_PIDE}" in TEXTO_VOZ, "la voz tiene que incrustar la regla, no copiarla"
    p = r._prompt([], [], [], [], "un restaurante cerca de mi hotel", quien="", zona="UTC")
    assert LO_QUE_NO_SE_PIDE in p[0]["content"]


def test_pregunto_y_atajo_no_leen_necesito_como_dato_a_pedir():
    seguido = " ".join(r.CONTRATO.split())
    assert "that ONLY the person can give" in seguido
    assert "says what IT uses, not what you have to ask for" in seguido
    assert "do not count as missing data" in seguido


def test_si_el_movil_puede_dar_la_ubicacion_la_recepcion_no_pregunta_donde_esta():
    """AVJ-33 (19/10/2026): «find me a pharmacy near where I am» got «which city?» from here and never reached the
    agent, which is who asks the device for the location."""
    con = r._prompt([], [], [], [], "¿Me buscas una farmacia cerca de donde estoy?", puede_dar_ubicacion=True)
    sin = r._prompt([], [], [], [], "¿Me buscas una farmacia cerca de donde estoy?")
    assert r.PUEDE_DAR_UBICACION in con[0]["content"] and r.PUEDE_DAR_UBICACION not in sin[0]["content"]
    assert r.depende_de_donde_esta("¿Me buscas una farmacia cerca de donde estoy?")
    assert r.depende_de_donde_esta("Find a pharmacy NEAR ME")
    assert not r.depende_de_donde_esta("Apúntame Lisboa en el viaje")


def test_la_regla_de_la_ubicacion_no_depende_del_modelo():
    """The chat route turns a `pregunto` about where they are into `delego` when the device can send it."""
    from pathlib import Path
    fuente = (Path(__file__).resolve().parents[1] / "app" / "routes" / "agent_routes.py").read_text(encoding="utf-8")
    assert "depende_de_donde_esta(mensaje)" in fuente and '"ubicacion" in capacidades' in fuente
