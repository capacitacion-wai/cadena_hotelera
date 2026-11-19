"""A READ-ONLY invocation (15.7.0, `app/prueba_solo_lectura.py`).

The live view of an application tries THIS employee for real with `solo_lectura` in the platform's
SIGNED delegation. What is pinned is the promise «real answer, no real effect»:

* the proxy, given the marker in a session's grant slot, lets only READS through — a write, an
  unclassified tool, an EXECUTE whose inner tool cannot be read, and anything that would PARK an
  approval are refused with a message that tells the agent to describe instead; the marker is never
  a write grant and is worth nothing once the test is closed;
* the session's permission map denies every connector write / unclassified tool (by opencode key and
  bare slug), the tools a connector served that the table does not classify, and the employee's own
  tools that change its state — and leaves reads, router meta-tools and native tools alone;
* the delegation's claim only reaches `invocar`, and the run is a test (`prueba:<app>`);
* the route runs in its own thread and session, returns `solo_lectura` + `escrituras_bloqueadas`
  collected from the turn's events, and closes the test when it ends;
* the session manager never runs a read-only turn on a session that can write, nor the reverse.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app import mcp_policy as P
from app import prueba_solo_lectura as RO
from app.auth import TokenPayload


def _row(name, permission, io="write", runtime="kommo", source="inherited_agent"):
    return {"name": name, "key": f"{runtime}_{name}", "io_kind": io, "permission": permission,
            "source": source}


TABLE = {
    "unlisted_connector": {"permission": "allow"},
    "connectors": [
        {"name": "composio-kommo", "runtime_name": "kommo", "unlisted_tool": {"permission": "allow"},
         "tools": [_row("KOMMO_LIST_LEADS", "allow", "read", source="read"),
                   _row("KOMMO_CREATE_LEAD", "allow"),
                   _row("KOMMO_GET_PIPELINE", "ask", "read")]},
        {"name": "wai-odoo", "runtime_name": "wai-odoo", "unlisted_tool": {"permission": "allow"},
         "tools": [_row("search_read", "allow", "read", "wai-odoo", "read"),
                   _row("write", "allow", "write", "wai-odoo")]},
    ],
}


@pytest.fixture(autouse=True)
def _tabla(monkeypatch):
    P.reset_table_cache()
    RO.HERRAMIENTAS_VISTAS.clear()

    async def fetch():
        return TABLE

    async def nada(*_a):
        return None

    async def autonomia():
        return P.POLICY_WRITE
    monkeypatch.setattr(P, "_fetch_table", fetch)
    monkeypatch.setattr(P, "_persist", nada)
    monkeypatch.setattr(P, "_load_persisted", nada)
    monkeypatch.setattr(P, "_por_autonomia", autonomia)
    yield
    P.reset_table_cache()
    RO.HERRAMIENTAS_VISTAS.clear()


def ro(*a):
    return asyncio.run(P.decide_read_only_test(*a))


# ── The proxy's verdict ─────────────────────────────────────────────────────────────────────────

def test_only_reads_pass():
    assert ro("kommo", "KOMMO_LIST_LEADS", {})[0] == "allow"
    assert ro("kommo", "KOMMO_CREATE_LEAD", {}) == ("deny", ["KOMMO_CREATE_LEAD"], P.READ_ONLY_TEST)
    # Unclassified counts as a write, although the table would let it through normally.
    assert ro("kommo", "KOMMO_SOMETHING_NEW", {})[0] == "deny"
    assert asyncio.run(P.decide_call("kommo", "KOMMO_SOMETHING_NEW", {}))[0] == "allow"


def test_a_read_that_would_park_an_approval_is_refused_not_parked():
    assert asyncio.run(P.decide_call("kommo", "KOMMO_GET_PIPELINE", {}))[0] == "approve"
    assert ro("kommo", "KOMMO_GET_PIPELINE", {})[0:3:2] == ("deny", P.READ_ONLY_TEST)


def test_router_execute_is_judged_on_the_inner_tool():
    lee = {"tools": [{"tool_slug": "KOMMO_LIST_LEADS", "arguments": {}}], "current_step": "FETCHING_LEADS_PAGE_1"}
    escribe = {"tools": [{"tool_slug": "KOMMO_LIST_LEADS"}, {"tool_slug": "KOMMO_CREATE_LEAD"}]}
    assert ro("kommo", "COMPOSIO_MULTI_EXECUTE_TOOL", lee)[0] == "allow"
    assert ro("kommo", "COMPOSIO_MULTI_EXECUTE_TOOL", escribe) == ("deny", ["KOMMO_CREATE_LEAD"], P.READ_ONLY_TEST)
    assert ro("kommo", "COMPOSIO_MULTI_EXECUTE_TOOL", {"x": 1})[0] == "deny"          # unverifiable
    assert ro("kommo", "COMPOSIO_SEARCH_TOOLS", {})[0] == "allow"                     # pure meta


def test_the_message_tells_the_agent_to_describe_instead():
    m = P.deny_message("kommo", P.READ_ONLY_TEST)
    assert RO.TEXTO_DEL_BLOQUEO in m and "describe what you would have done" in m
    assert RO.es_bloqueo(m) and RO.es_bloqueo('Permission denied: tool "x" is denied')
    assert not RO.es_bloqueo({"ok": True})


def test_the_marker_lives_while_the_test_is_open():
    g = RO.abrir()
    assert RO.es_marca(g) and RO.activa(g)
    RO.cerrar(g)
    assert RO.es_marca(g) and not RO.activa(g)
    assert not RO.activa("ro:inventado") and not RO.es_marca("grant-de-verdad")


# ── The session's permission map ────────────────────────────────────────────────────────────────

def test_the_session_map_denies_every_possible_change(monkeypatch):
    import app.mcp_server as S
    monkeypatch.setattr(S, "TOOLS", {"list_alerts": 1, "search_memory": 1, "upsert_alert": 1,
                                     "remember_fact": 1, "request_approval": 1, "local.read": 1,
                                     "local.write": 1, "get_consumption": 1, "set_setting": 1})
    RO.anotar_herramientas("wai-odoo", [{"name": "search_read"}, {"name": "unlink"}])
    RO.anotar_herramientas("kommo", [{"name": "COMPOSIO_MULTI_EXECUTE_TOOL"}, {"name": "COMPOSIO_SEARCH_TOOLS"},
                                     {"name": "COMPOSIO_REMOTE_WORKBENCH"}])
    RO.anotar_herramientas("propio", [{"name": "consultar"}])
    m = asyncio.run(RO.permisos({"gmail_GMAIL_SEND": "deny"}))
    assert m["gmail_GMAIL_SEND"] == "deny"                                   # the table's denies stay
    for k in ("kommo_KOMMO_CREATE_LEAD", "KOMMO_CREATE_LEAD", "kommo_KOMMO_GET_PIPELINE",
              "wai-odoo_write", "wai-odoo_unlink", "kommo_COMPOSIO_REMOTE_WORKBENCH", "propio_consultar",
              "app-tools_upsert_alert", "app-tools_remember_fact", "app-tools_request_approval",
              "app-tools_local.write", "app-tools_set_setting"):
        assert m.get(k) == "deny", k
    for k in ("kommo_KOMMO_LIST_LEADS", "KOMMO_LIST_LEADS", "wai-odoo_search_read",
              "kommo_COMPOSIO_MULTI_EXECUTE_TOOL", "kommo_COMPOSIO_SEARCH_TOOLS",
              "app-tools_list_alerts", "app-tools_search_memory", "app-tools_local.read",
              "app-tools_get_consumption"):
        assert k not in m, k
    # A bare non-slug name is never denied: `write` would also be opencode's own native tool.
    assert "write" not in m


def test_a_refused_call_is_reported_with_its_inner_tools():
    b = RO.bloqueada("kommo_COMPOSIO_MULTI_EXECUTE_TOOL",
                     {"tools": [{"tool_slug": "KOMMO_CREATE_LEAD"}], "__wai_write_grant": "ro:x", "run_token": "t"})
    assert b["herramientas"] == ["KOMMO_CREATE_LEAD"] and b["conector"] == "kommo"
    assert "__wai_write_grant" not in b["argumentos"] and "run_token" not in b["argumentos"]


# ── The delegation ──────────────────────────────────────────────────────────────────────────────

def _req(path):
    return Request({"type": "http", "method": "POST", "path": path, "headers": [], "query_string": b"",
                    "state": {}})


def test_the_signed_claim_only_reaches_invocar_and_marks_the_run_as_a_test(monkeypatch):
    import app.colleagues as C
    from app import auth
    datos = {"sub": "ana@cobsa.es", "typ": "WAI-DELEG-APP", "project_ids": [1], "data_areas": [],
             "origen": {"tipo": "app", "app": "portal"}, "solo_lectura": True}
    monkeypatch.setattr(C, "verify_delegation", lambda _t: datos)
    r = _req("/api/agent/invocar")
    auth._persona_de_una_aplicacion(r, "x")
    assert r.state.solo_lectura is True and r.state.origen == "prueba:portal"
    with pytest.raises(HTTPException) as e:
        auth._persona_de_una_aplicacion(_req("/api/agent/chat"), "x")
    assert e.value.status_code == 403
    datos.pop("solo_lectura")
    r2 = _req("/api/agent/invocar")
    auth._persona_de_una_aplicacion(r2, "x")
    assert not getattr(r2.state, "solo_lectura", False) and r2.state.origen == "app:portal"


# ── The route ───────────────────────────────────────────────────────────────────────────────────

ANA = TokenPayload(email="ana@cobsa.es", is_superuser=False, project_ids=[1], data_areas=[])


@pytest.mark.asyncio
async def test_the_route_runs_a_test_in_its_own_thread_and_reports_what_was_refused(monkeypatch):
    import app.budget as budget
    import app.conversacion as conv
    from app import agent_sessions, chat_threads
    from app.routes import agent_routes as R
    monkeypatch.setattr(conv, "declaracion", lambda: {"hago": [
        {"id": "mapear", "que": "Mapeo", "salida": {"type": "object"}}]})

    async def nada(*a, **k):
        return None
    monkeypatch.setattr(budget, "budget_block_reason", nada)
    monkeypatch.setattr(budget, "user_cap_status", nada)
    monkeypatch.setattr(agent_sessions, "resolve_tool_permissions", nada)
    hilos = []

    async def _hilo(email, cid, prueba=False):
        hilos.append(prueba)
        return "prueba-x" if prueba else "api-x"
    monkeypatch.setattr(chat_threads, "hilo_de_invocacion", _hilo)
    visto = {}

    async def _turno(payload, request, user, scope_key):
        visto["sesion"] = getattr(request.state, "solo_lectura_sesion", None)
        visto["mensaje"] = payload.message
        if visto["sesion"] is not None:
            assert RO.activa(visto["sesion"]["grant"])
        return {"texto": "{}", "run_id": 5, "pregunta": "", "error": "",
                "bloqueadas": [{"herramienta": "kommo_KOMMO_CREATE_LEAD"}]}
    monkeypatch.setattr(R, "_turno_de_invocacion", _turno)
    monkeypatch.setattr(R, "_anotar_invocacion", lambda *a: asyncio.sleep(0, result=0.02))

    req = _req("/api/agent/invocar")
    req.state.solo_lectura = True
    out = await R.invocar_capacidad(R.InvocacionRequest(capacidad="mapear"), req, ANA)
    assert out["solo_lectura"] is True and out["escrituras_bloqueadas"] == [{"herramienta": "kommo_KOMMO_CREATE_LEAD"}]
    assert hilos == [True] and "READ-ONLY TEST" in visto["mensaje"]
    assert "kommo_KOMMO_CREATE_LEAD" in visto["sesion"]["permisos"]
    grant = visto["sesion"]["grant"]
    assert not RO.activa(grant)                                         # closed when the test ends

    # Without the claim: today's invocation, untouched.
    out = await R.invocar_capacidad(R.InvocacionRequest(capacidad="mapear"), _req("/api/agent/invocar"), ANA)
    assert "solo_lectura" not in out and hilos[-1] is False and visto["sesion"] is None


@pytest.mark.asyncio
async def test_the_turn_collects_the_refused_calls_from_its_events(monkeypatch):
    from app import chat_runtime
    from app.routes import agent_routes as R

    async def _chat(payload, request, user):
        async def runner(turn):
            await turn.append({"type": "tool_call", "name": "kommo_KOMMO_CREATE_LEAD", "call_id": "c1",
                               "args": {"name": "Lead"}})
            await turn.append({"type": "tool_result", "name": "kommo_KOMMO_CREATE_LEAD", "call_id": "c1",
                               "result": 'Permission denied: tool "kommo_KOMMO_CREATE_LEAD" is denied'})
            await turn.append({"type": "tool_call", "name": "kommo_KOMMO_LIST_LEADS", "call_id": "c2", "args": {}})
            await turn.append({"type": "tool_result", "name": "kommo_KOMMO_LIST_LEADS", "call_id": "c2",
                               "result": "[]"})
            turn.texto = "{}"
            await turn.append({"type": "complete", "runId": 3})
            return "complete"
        await chat_runtime.start_turn("interactive:ana@cobsa.es:prueba-x", "ses", 3, runner)
    monkeypatch.setattr(R, "chat_with_agent", _chat)
    req = _req("/api/agent/invocar")
    req.state.solo_lectura_sesion = {"grant": "ro:x", "permisos": {}}
    t = await R._turno_de_invocacion(None, req, ANA, "interactive:ana@cobsa.es:prueba-x")
    assert t["bloqueadas"] == [{"herramienta": "kommo_KOMMO_CREATE_LEAD", "conector": "kommo",
                                "argumentos": {"name": "Lead"}}]


# ── The session manager ─────────────────────────────────────────────────────────────────────────

def test_a_read_only_turn_never_runs_on_a_session_that_can_write(monkeypatch):
    from app import agent_sessions as A
    creadas = []

    class _Proxy:
        async def create_session(self, **kw):
            creadas.append(kw)
            return f"ses_{len(creadas)}"

        async def delete_session(self, sid):
            return None
    monkeypatch.setattr(A, "get_agent_proxy", lambda: _Proxy())

    async def sin_permisos():
        return {"x_y": "deny"}
    monkeypatch.setattr(A, "resolve_tool_permissions", sin_permisos)
    mgr = A.SessionManager()
    scope = A.interactive_scope("ana@cobsa.es", "prueba-y")
    ro_cfg = {"grant": "ro:abc", "permisos": {"kommo_KOMMO_CREATE_LEAD": "deny"}}

    async def go():
        normal = await mgr.resolve(scope)
        prueba = await mgr.resolve(scope, solo_lectura=ro_cfg)            # same scope, fresh: must rotate
        reintento = await mgr.resolve(scope, solo_lectura=ro_cfg)         # the retry reuses ITS session
        despues = await mgr.resolve(scope)                                # a normal turn never reuses it
        return normal, prueba, reintento, despues
    loop = asyncio.new_event_loop()
    try:
        normal, prueba, reintento, despues = loop.run_until_complete(go())
    finally:
        loop.close()
    assert prueba.session_id != normal.session_id and reintento.session_id == prueba.session_id
    assert despues.session_id not in (prueba.session_id, normal.session_id)
    assert creadas[1]["tool_permissions"] == ro_cfg["permisos"] and creadas[1]["write_grant"] == "ro:abc"
    assert creadas[0]["write_grant"] is None and creadas[2]["write_grant"] is None
    assert len(creadas) == 3
