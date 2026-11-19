"""Invoking a capability by API (14.23.0, `app/invocacion.py` + `POST /api/agent/invocar`).

What is pinned is what an application relies on: wrong arguments are refused BEFORE a turn is
spent, the JSON is found in the answer and validated, ONE retry with the errors (a question is
retried from a clean session), a 422 with the errors when it still does not conform, and the
route reachable through an application's delegation."""
from __future__ import annotations

import time

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app import invocacion as inv
from app.auth import TokenPayload

ENTRADA = {"type": "object", "required": ["entrega_id"],
           "properties": {"entrega_id": {"type": "integer"}}}
SALIDA = {"type": "object", "required": ["fila_cabecera", "columnas", "sin_decidir"],
          "properties": {
              "fila_cabecera": {"type": "integer"},
              "columnas": {"type": "object", "additionalProperties": {
                  "type": "object", "required": ["columna"],
                  "properties": {"columna": {"type": ["integer", "null"]},
                                 "titulo": {"type": "string"}}}},
              "sin_decidir": {"type": "array", "items": {
                  "type": "object", "required": ["campo", "motivo"],
                  "properties": {"campo": {"type": "string"}, "motivo": {"type": "string"}}}}}}
BUENA = ('{"fila_cabecera": 3, "columnas": {"EAN": {"columna": 1, "titulo": "Código EAN"}}, '
         '"sin_decidir": [{"campo": "PVR", "motivo": "Two columns could be the price without VAT"}]}')


# ── The pure part ───────────────────────────────────────────────────────────────────────────────

def test_validar_dice_donde_falla():
    assert inv.validar({"entrega_id": 7}, ENTRADA) == []
    assert inv.validar({"entrega_id": "7"}, ENTRADA) == ["$.entrega_id: expected integer, got string"]
    assert inv.validar({}, ENTRADA) == ["$: missing required property `entrega_id`"]
    assert inv.validar({"entrega_id": True}, ENTRADA)            # a bool is not an integer in JSON
    malo = {"fila_cabecera": 3, "columnas": {"EAN": {"columna": "B"}}, "sin_decidir": [{}]}
    errores = inv.validar(malo, SALIDA)
    assert "$.columnas.EAN.columna: expected integer or null, got string" in errores
    assert "$.sin_decidir[0]: missing required property `campo`" in errores
    assert inv.validar({"x": 1}, {"type": "object", "properties": {}, "additionalProperties": False})
    assert inv.validar("c", {"type": "string", "enum": ["a", "b"]})


@pytest.mark.parametrize("texto", [
    BUENA,
    "I opened the file and mapped it.\n```json\n" + BUENA + "\n```",
    'Reading {"rows": 2} from the tool... Done.\n' + BUENA,
])
def test_extraer_json_encuentra_la_respuesta(texto):
    ok, valor = inv.extraer_json(texto)
    assert ok and valor["fila_cabecera"] == 3


def test_extraer_json_sin_json():
    assert inv.extraer_json("¿Qué campo corresponde a PVR sin iva?") == (
        False, "the answer contains no JSON object or array")
    assert inv.extraer_json("")[0] is False


def test_el_encargo_prohibe_preguntar_y_lleva_el_esquema(monkeypatch):
    import app.conversacion as conv
    monkeypatch.setattr(conv, "declaracion", lambda: {"idioma_principal": "es"})
    texto = inv.encargo({"id": "preparar_mapeo", "que": "Mapeo", "salida": SALIDA}, {"entrega_id": 7})
    assert "NEVER ask" in texto and '"fila_cabecera"' in texto and '"entrega_id": 7' in texto
    assert "in es." in texto


# ── The route ───────────────────────────────────────────────────────────────────────────────────

ANA = TokenPayload(email="ana@cobsa.es", is_superuser=False, project_ids=[7], data_areas=[])


def _peticion() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/api/agent/invocar", "headers": [],
                    "query_string": b"", "state": {}})


@pytest.fixture
def ruta(monkeypatch):
    """The route with the employee's turn scripted: each call to `_turno_de_invocacion` takes
    the next answer of `respuestas` and records the message and whether the session was new."""
    import app.budget as budget
    import app.conversacion as conv
    from app import chat_threads
    from app.routes import agent_routes as R

    defi = {"hago": [
        {"id": "preparar_mapeo", "que": "Preparo el mapeo", "entrada": ENTRADA, "salida": SALIDA},
        {"id": "conversar", "que": "Hablo"}]}
    monkeypatch.setattr(conv, "declaracion", lambda: defi)

    async def _sin_limite(*a, **k):
        return None
    monkeypatch.setattr(budget, "budget_block_reason", _sin_limite)
    monkeypatch.setattr(budget, "user_cap_status", _sin_limite)

    async def _hilo(email, cid):
        return "api-x"
    monkeypatch.setattr(chat_threads, "hilo_de_invocacion", _hilo)

    estado = {"respuestas": [], "vistos": []}

    async def _turno(payload, request, user, scope_key):
        assert request.state.invocacion == "preparar_mapeo"
        estado["vistos"].append((payload.message, payload.reset, payload.thread_id))
        texto, pregunta = estado["respuestas"].pop(0)
        return {"texto": texto, "run_id": 40 + len(estado["vistos"]), "pregunta": pregunta,
                "error": ""}
    monkeypatch.setattr(R, "_turno_de_invocacion", _turno)

    async def llamar(capacidad="preparar_mapeo", argumentos=None):
        return await R.invocar_capacidad(
            R.InvocacionRequest(capacidad=capacidad,
                                argumentos={"entrega_id": 7} if argumentos is None else argumentos),
            _peticion(), ANA)
    return llamar, estado


async def _rechazo(coro) -> dict:
    with pytest.raises(HTTPException) as e:
        await coro
    return {"estado": e.value.status_code, **e.value.detail}


@pytest.mark.asyncio
async def test_a_la_primera(ruta):
    llamar, estado = ruta
    estado["respuestas"] = [(BUENA, "")]
    r = await llamar()
    assert r["resultado"]["sin_decidir"][0]["campo"] == "PVR"
    assert (r["run_id"], r["intentos"]) == (41, 1)
    mensaje, nueva, hilo = estado["vistos"][0]
    assert nueva and hilo == "api-x" and "[API INVOCATION]" in mensaje


@pytest.mark.asyncio
async def test_argumentos_malos_no_gastan_un_turno(ruta):
    llamar, estado = ruta
    r = await _rechazo(llamar(argumentos={"entrega_id": "siete"}))
    assert (r["estado"], r["code"]) == (400, "ENTRADA_NO_VALIDA")
    assert r["errores"] == ["$.entrega_id: expected integer, got string"]
    assert estado["vistos"] == []


@pytest.mark.asyncio
async def test_sin_salida_o_inexistente(ruta):
    llamar, estado = ruta
    r = await _rechazo(llamar(capacidad="conversar", argumentos={}))
    assert (r["estado"], r["code"]) == (400, "CAPACIDAD_SIN_SALIDA")
    r = await _rechazo(llamar(capacidad="otra"))
    assert (r["estado"], r["code"]) == (404, "CAPACIDAD_NO_EXISTE")
    assert estado["vistos"] == []


@pytest.mark.asyncio
async def test_un_reintento_en_la_misma_sesion_con_los_errores(ruta):
    llamar, estado = ruta
    estado["respuestas"] = [('{"fila_cabecera": "3"}', ""), (BUENA, "")]
    r = await llamar()
    assert r["intentos"] == 2 and r["run_id"] == 42
    mensaje, nueva, _ = estado["vistos"][1]
    assert not nueva and "CORRECTION" in mensaje
    assert "$.fila_cabecera: expected integer, got string" in mensaje


@pytest.mark.asyncio
async def test_si_pregunta_se_reintenta_desde_una_sesion_limpia(ruta):
    llamar, estado = ruta
    estado["respuestas"] = [("", '[{"question": "¿Qué campo es PVR sin iva?"}]'), (BUENA, "")]
    r = await llamar()
    assert r["intentos"] == 2
    mensaje, nueva, _ = estado["vistos"][1]
    assert nueva and "You must not ask" in mensaje and "PVR sin iva" in mensaje


@pytest.mark.asyncio
async def test_tras_el_reintento_422_con_los_errores(ruta):
    llamar, estado = ruta
    estado["respuestas"] = [("¿Qué campo corresponde a PVR?", ""), ('{"fila_cabecera": 3}', "")]
    r = await _rechazo(llamar())
    assert (r["estado"], r["code"], r["run_id"]) == (422, "SALIDA_NO_CUMPLE", 42)
    assert "$: missing required property `columnas`" in r["errores"]
    assert len(estado["vistos"]) == 2                     # one retry, never more


@pytest.mark.asyncio
async def test_el_turno_se_lee_del_texto_crudo(monkeypatch):
    """`_turno_de_invocacion` reads the RAW text of ITS turn (not the markdown-normalised events)."""
    from app import chat_runtime
    from app.routes import agent_routes as R
    crudo = '{"precio": "PVR - sin iva"}'                 # «- s» would get a line break when shown

    async def _chat(payload, request, user):
        async def runner(turn):
            await turn.append({"type": "text_delta", "content": "visible"})
            turn.texto = crudo
            await turn.append({"type": "complete", "runId": 9})
            return "complete"
        await chat_runtime.start_turn("interactive:ana@cobsa.es:api-x", "ses", 9, runner)
    monkeypatch.setattr(R, "chat_with_agent", _chat)
    t = await R._turno_de_invocacion(None, _peticion(), ANA, "interactive:ana@cobsa.es:api-x")
    assert (t["texto"], t["run_id"], t["pregunta"], t["error"]) == (crudo, 9, "", "")


@pytest.mark.asyncio
async def test_no_lee_un_turno_viejo(monkeypatch):
    from app import chat_runtime
    from app.routes import agent_routes as R
    viejo = chat_runtime.ChatTurn(scope_key="interactive:b@x:api-y", session_id="s",
                                  created_at=time.time() - 60)
    chat_runtime._TURNS["interactive:b@x:api-y"] = viejo

    async def _chat(payload, request, user):
        return None                                        # answered without starting a turn
    monkeypatch.setattr(R, "chat_with_agent", _chat)
    r = await _rechazo(R._turno_de_invocacion(None, _peticion(), ANA, "interactive:b@x:api-y"))
    assert r["code"] == "INVOCACION_FALLIDA"


def test_el_hilo_de_invocacion_es_uno_por_persona_y_capacidad_y_nace_archivado():
    """Never the person's conversation, never one row per call, never counted in their list —
    and back if the person deletes it."""
    import asyncio
    from app import chat_threads

    async def _todo():
        a = await chat_threads.hilo_de_invocacion("Ana@cobsa.es", "preparar_mapeo")
        assert a == await chat_threads.hilo_de_invocacion("ana@cobsa.es", "preparar_mapeo")
        assert a != await chat_threads.hilo_de_invocacion("ana@cobsa.es", "otra")
        assert a != await chat_threads.hilo_de_invocacion("bea@cobsa.es", "preparar_mapeo")
        fila = await chat_threads.obtener("ana@cobsa.es", a)
        assert fila.archived_at is not None and fila.title == "API · preparar_mapeo"
        assert a not in [h["id"] for h in await chat_threads.listar("ana@cobsa.es")]
        await chat_threads.borrar("ana@cobsa.es", a)
        assert await chat_threads.obtener("ana@cobsa.es", a) is None
        assert await chat_threads.hilo_de_invocacion("ana@cobsa.es", "preparar_mapeo") == a
        assert await chat_threads.obtener("ana@cobsa.es", a) is not None

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_todo())
    finally:
        loop.close()


def test_las_instrucciones_aprobadas_del_uso_viajan_en_el_encargo(monkeypatch):
    # 15.4.0: the platform resolves the use's APPROVED instructions; they narrow the work, never widen it.
    from app import invocacion as inv
    monkeypatch.setattr(inv, "_idioma", lambda: "")
    cap = {"id": "preparar_mapeo", "que": "Map a delivery", "salida": {"type": "object"}}
    texto = inv.encargo(cap, {"entrega_id": 7}, instrucciones="Only columns of the PVR sheet.")
    assert "Instructions approved for this use" in texto and "Only columns of the PVR sheet." in texto
    assert "Instructions approved" not in inv.encargo(cap, {"entrega_id": 7})


# ── The caller's rejection of a previous output (15.5.0) ──────────────────────────────────────────────────────────
def test_the_rejection_of_a_previous_output_is_validated_and_goes_into_the_turn():
    from app import invocacion as inv
    assert inv.validar_rechazo_previo(None) is None
    for malo in ({}, {"motivos": []}, {"motivos": [{"elemento": "e1"}]}, "no",
                 {"motivos": [{"elemento": "e", "motivo": "m"}] * (inv.RECHAZO_MOTIVOS_MAX + 1)}):
        with pytest.raises(ValueError):
            inv.validar_rechazo_previo(malo)
    r = inv.validar_rechazo_previo({"motivos": [{"elemento": "e1", "motivo": "not among the citable sources"}],
                                    "resultado": {"citas": [{"cita_id": "e1"}]}})
    cap = {"id": "propose_prevaluation", "que": "Propose a pre-assessment", "salida": {"type": "object"}}
    texto = inv.encargo(cap, {"solicitud_id": "S-1"}, rechazo_previo=r)
    assert "REJECTED these elements" in texto and "- e1: not among the citable sources" in texto
    assert '"cita_id": "e1"' in texto and "ONLY those elements fixed" in texto
    assert "REJECTED" not in inv.encargo(cap, {"solicitud_id": "S-1"})
