"""A CONTROLLED test chat (15.8.0, `app/prueba_controlada.py`).

The platform's code agent tries THIS employee from its chat with `prueba_controlada` in the SIGNED delegation.
What is pinned is the promise «every write waits for its person»:

* the session ASKS (opencode ruleset) for every write / unclassified / own state-changing tool, denies `task`
  and the router's write slugs (they cannot be asked one by one), and keeps the table's denies;
* the proxy, given the marker, lets a write through ONLY if the session asks for it (the person said yes), never
  parks an approval, refuses a router EXECUTE with a write inside, and refuses everything once the test is closed;
* the claim only reaches the test conversation's routes; the chat talks only in a test conversation, on its own
  session, with the sidecar NOT auto-granting, and relays each ask as a `permission` event;
* the answer route only answers a write pending in that person's test conversation, through the test delegation,
  and allowing a write the employee would park needs the right to approve it;
* the session manager never mixes a test session with a normal one; an agent pod that ignores the ruleset refuses.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app import mcp_policy as P
from app import prueba_controlada as PC
from app import prueba_solo_lectura as RO
from app.auth import TokenPayload


def _row(name, permission, io="write", runtime="kommo", source="inherited_agent"):
    return {"name": name, "key": f"{runtime}_{name}", "io_kind": io, "permission": permission, "source": source}


TABLE = {
    "unlisted_connector": {"permission": "allow"},
    "connectors": [
        {"name": "composio-kommo", "runtime_name": "kommo", "unlisted_tool": {"permission": "allow"},
         "tools": [_row("KOMMO_LIST_LEADS", "allow", "read", source="read"),
                   _row("KOMMO_CREATE_LEAD", "allow")]},
        {"name": "wai-odoo", "runtime_name": "wai-odoo", "unlisted_tool": {"permission": "allow"},
         "tools": [_row("search_read", "allow", "read", "wai-odoo", "read"),
                   _row("write", "allow", "write", "wai-odoo"),
                   _row("unlink", "deny", "write", "wai-odoo", "custom")]},
    ],
}


@pytest.fixture(autouse=True)
def _tabla(monkeypatch):
    P.reset_table_cache()
    RO.HERRAMIENTAS_VISTAS.clear()
    for d in (PC._CONFIG, PC._PREGUNTADAS, PC.PENDIENTES):
        d.clear()

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
    import app.mcp_server as S
    monkeypatch.setattr(S, "TOOLS", {"list_alerts": 1, "request_approval": 1, "remember_fact": 1})
    yield
    P.reset_table_cache()


def _run(c):
    return asyncio.run(c)


# ── The session's rules ─────────────────────────────────────────────────────────────────────────

def test_the_session_asks_every_write_and_denies_what_cannot_be_asked():
    RO.anotar_herramientas("wai-odoo", [{"name": "create"}, {"name": "search_read"}])
    acl, ruleset, asked = _run(PC.reglas({"wai-odoo_unlink": "deny"}))
    preguntadas = {r["permission"] for r in ruleset if r["action"] == "ask"}
    assert preguntadas == asked == {"wai-odoo_write", "wai-odoo_create", "app-tools_request_approval",
                                    "app-tools_remember_fact"}
    assert {"permission": "task", "pattern": "*", "action": "deny"} in ruleset and acl["task"] == "deny"
    # The router's write slug: denied (by key and bare slug, so also inside an EXECUTE), never «asked».
    assert acl["KOMMO_CREATE_LEAD"] == acl["kommo_KOMMO_CREATE_LEAD"] == "deny"
    assert acl["wai-odoo_unlink"] == "deny" and "wai-odoo_unlink" not in asked     # the table's deny stays
    for k in ("kommo_KOMMO_LIST_LEADS", "wai-odoo_search_read", "app-tools_list_alerts"):
        assert k not in asked and k not in acl, k


# ── The proxy's verdict ─────────────────────────────────────────────────────────────────────────

def test_the_proxy_lets_a_write_through_only_if_the_session_asked_for_it():
    asked = {"wai-odoo_write"}
    assert _run(P.decide_controlled_test("wai-odoo", "write", {}, asked)) == ("allow", [], "")
    assert _run(P.decide_controlled_test("wai-odoo", "create", {}, asked)) == (
        "deny", ["create"], P.CONTROLLED_TEST)                               # unclassified, not asked
    assert _run(P.decide_controlled_test("wai-odoo", "unlink", {}, {"wai-odoo_unlink"}))[0] == "deny"
    assert _run(P.decide_controlled_test("wai-odoo", "search_read", {}, set()))[0] == "allow"
    escribe = {"tools": [{"tool_slug": "KOMMO_LIST_LEADS"}, {"tool_slug": "KOMMO_CREATE_LEAD"}]}
    assert _run(P.decide_controlled_test("kommo", "COMPOSIO_MULTI_EXECUTE_TOOL", escribe, asked)) == (
        "deny", ["KOMMO_CREATE_LEAD"], P.CONTROLLED_TEST)
    lee = {"tools": [{"tool_slug": "KOMMO_LIST_LEADS"}]}
    assert _run(P.decide_controlled_test("kommo", "COMPOSIO_MULTI_EXECUTE_TOOL", lee, asked))[0] == "allow"
    assert PC.TEXTO_DEL_BLOQUEO in P.deny_message("kommo", P.CONTROLLED_TEST)


def test_a_write_that_would_park_an_approval_never_parks_in_a_test(monkeypatch):
    async def aprobacion():
        return P.POLICY_APPROVAL
    monkeypatch.setattr(P, "_por_autonomia", aprobacion)
    assert _run(P.decide_call("wai-odoo", "write", {}))[0] == "approve"              # normal: it would park
    assert _run(P.decide_controlled_test("wai-odoo", "write", {}, {"wai-odoo_write"}))[0] == "allow"
    assert _run(P.decide_controlled_test("wai-odoo", "write", {}, set()))[0] == "deny"


def test_the_marker_is_worth_nothing_once_the_test_conversation_is_closed():
    PC.abrir_hilo("ana@cobsa.es", "h1")
    cfg = _run(PC.config_de("ana@cobsa.es", "h1"))
    assert PC.es_marca(cfg["grant"]) and PC.activa(cfg["grant"]) and "wai-odoo_write" in PC.preguntadas(cfg["grant"])
    assert _run(PC.config_de("ana@cobsa.es", "h1"))["grant"] == cfg["grant"]          # one per conversation
    PC.cerrar_hilo("ana@cobsa.es", "h1")
    assert not PC.activa(cfg["grant"]) and not PC.es_hilo_de_prueba("ana@cobsa.es", "h1")
    assert not PC.activa("pc:inventado") and not PC.es_marca("ro:x")


# ── The delegation ──────────────────────────────────────────────────────────────────────────────

def _req(path, method="POST"):
    return Request({"type": "http", "method": method, "path": path, "headers": [], "query_string": b"",
                    "state": {}})


def test_the_claim_only_reaches_the_test_conversation_routes(monkeypatch):
    import app.colleagues as C
    from app import auth
    datos = {"sub": "ana@cobsa.es", "typ": "WAI-DELEG-APP", "project_ids": [1], "data_areas": [],
             "origen": {"tipo": "app", "app": "prueba-del-agente-de-codigo"}, "prueba_controlada": True}
    monkeypatch.setattr(C, "verify_delegation", lambda _t: datos)
    for ruta in ("/api/agent/chat", "/api/agent/threads", "/api/agent/threads/h1/archive",
                 "/api/agent/chat/permisos/per_1", "/api/agent/chat/stream"):
        r = _req(ruta)
        auth._persona_de_una_aplicacion(r, "x")
        assert r.state.prueba_controlada is True and r.state.origen == "prueba:prueba-del-agente-de-codigo"
    for ruta in ("/api/agent/invocar", "/api/agent/threads/h1/history", "/api/approvals/3/approve"):
        with pytest.raises(HTTPException) as e:
            auth._persona_de_una_aplicacion(_req(ruta), "x")
        assert e.value.status_code == 403, ruta
    # An ordinary application delegation never reaches the answer route.
    datos.pop("prueba_controlada")
    with pytest.raises(HTTPException):
        auth._persona_de_una_aplicacion(_req("/api/agent/chat/permisos/per_1"), "x")


# ── The answer route ────────────────────────────────────────────────────────────────────────────

ANA = TokenPayload(email="ana@cobsa.es", is_superuser=False, project_ids=[1], data_areas=[])


class _Proxy:
    def __init__(self):
        self.respuestas = []

    async def answer_permission(self, sid, pid, permitir, motivo):
        self.respuestas.append((sid, pid, permitir, motivo))


def _prueba(monkeypatch, aprueba=True):
    from app.routes import agent_routes as R
    import app.users_directory as U
    proxy = _Proxy()
    monkeypatch.setattr(R, "get_agent_proxy", lambda: proxy)

    async def puede(_e):
        return aprueba
    monkeypatch.setattr(U, "can_approve", puede)
    PC.abrir_hilo("ana@cobsa.es", "h1")
    PC.anotar_pendiente("per_1", sesion="ses_t", email="ana@cobsa.es", hilo="h1", clave="wai-odoo_write")
    req = _req("/api/agent/chat/permisos/per_1")
    req.state.prueba_controlada = True
    return R, proxy, req


def test_the_person_answers_a_pending_write_of_their_test_conversation(monkeypatch):
    R, proxy, req = _prueba(monkeypatch)
    out = _run(R.responder_permiso("per_1", R.RespuestaAlPermiso(permitir=False, motivo="no", thread_id="h1"),
                                   req, ANA))
    assert out == {"ok": True, "permitido": False} and proxy.respuestas == [("ses_t", "per_1", False, "no")]
    assert "per_1" not in PC.PENDIENTES


@pytest.mark.parametrize("caso", ["sin_prueba", "otro_hilo", "otra_persona", "desconocido"])
def test_nobody_else_answers_it(monkeypatch, caso):
    R, proxy, req = _prueba(monkeypatch)
    pid, hilo, quien = "per_1", "h1", ANA
    if caso == "sin_prueba":
        req = _req("/api/agent/chat/permisos/per_1")
    elif caso == "otro_hilo":
        PC.abrir_hilo("ana@cobsa.es", "h2")
        hilo = "h2"
    elif caso == "otra_persona":
        quien = TokenPayload(email="luis@cobsa.es", is_superuser=False, project_ids=[1], data_areas=[])
    else:
        pid = "per_9"
    with pytest.raises(HTTPException) as e:
        _run(R.responder_permiso(pid, R.RespuestaAlPermiso(permitir=True, thread_id=hilo), req, quien))
    assert e.value.status_code == 403 and proxy.respuestas == []


def test_allowing_what_the_employee_would_park_needs_the_right_to_approve(monkeypatch):
    async def aprobacion():
        return P.POLICY_APPROVAL
    monkeypatch.setattr(P, "_por_autonomia", aprobacion)
    R, proxy, req = _prueba(monkeypatch, aprueba=False)
    with pytest.raises(HTTPException) as e:
        _run(R.responder_permiso("per_1", R.RespuestaAlPermiso(permitir=True, thread_id="h1"), req, ANA))
    assert e.value.detail["code"] == "ONLY_AN_APPROVER" and proxy.respuestas == []
    # Rejecting is always theirs.
    _run(R.responder_permiso("per_1", R.RespuestaAlPermiso(permitir=False, thread_id="h1"), req, ANA))
    assert proxy.respuestas[-1][2] is False


def test_a_pending_write_reads_as_the_person_needs_it():
    p = PC.legible({"id": "per_1", "permission": "wai-odoo_write", "callID": "c1"},
                   {"model": "res.partner", "values": {"name": "ACME"}, "api_key": "s3cr3t", "__wai_write_grant": "pc:x"})
    assert (p["herramienta"], p["conector"], p["clave"]) == ("write", "wai-odoo", "wai-odoo_write")
    assert p["argumentos"]["api_key"] == "***" and "__wai_write_grant" not in p["argumentos"]
    assert "ACME" in p["que_cambiaria"] and "Nothing has run yet" in p["que_cambiaria"]


# ── The session manager and the agent pod ───────────────────────────────────────────────────────

def test_a_test_chat_never_runs_on_a_normal_session_nor_the_reverse(monkeypatch):
    from app import agent_sessions as A
    creadas = []

    class _P:
        async def create_session(self, **kw):
            creadas.append(kw)
            return f"ses_{len(creadas)}"

        async def delete_session(self, sid):
            return None
    monkeypatch.setattr(A, "get_agent_proxy", lambda: _P())

    async def tabla():
        return {}
    monkeypatch.setattr(A, "resolve_tool_permissions", tabla)
    mgr = A.SessionManager()
    scope = A.interactive_scope("ana@cobsa.es", "h1")
    cfg = {"grant": "pc:abc", "permisos": {"task": "deny"}, "reglas": [{"permission": "x_y", "pattern": "*",
                                                                          "action": "ask"}]}

    async def go():
        normal = await mgr.resolve(scope)
        prueba = await mgr.resolve(scope, prueba_controlada=cfg)
        otra = await mgr.resolve(scope, prueba_controlada=cfg)
        ro = await mgr.resolve(scope, solo_lectura={"grant": "ro:1", "permisos": {}})
        despues = await mgr.resolve(scope)
        return normal, prueba, otra, ro, despues
    loop = asyncio.new_event_loop()
    try:
        normal, prueba, otra, ro, despues = loop.run_until_complete(go())
    finally:
        loop.close()
    assert prueba.session_id != normal.session_id and otra.session_id == prueba.session_id
    assert len({normal.session_id, prueba.session_id, ro.session_id, despues.session_id}) == 4
    assert creadas[1]["permission"] == cfg["reglas"] and creadas[1]["write_grant"] == "pc:abc"
    assert "permission" not in creadas[0] and "permission" not in creadas[2] and "permission" not in creadas[3]


def test_an_agent_pod_that_ignores_the_ruleset_refuses_the_test(monkeypatch):
    import httpx
    from app.agent_proxy import AgentProxy
    borradas = []

    def responder(req):
        if req.method == "DELETE":
            borradas.append(req.url.path)
            return httpx.Response(204)
        return httpx.Response(200, json={"session_id": "ses_v", "user": {}, "created_at": "x"})
    p = AgentProxy.__new__(AgentProxy)
    p._client = httpx.AsyncClient(base_url="http://sidecar", transport=httpx.MockTransport(responder))
    with pytest.raises(PC.NoDisponible):
        _run(p.create_session(user_email="ana@cobsa.es", role="interactive",
                              permission=[{"permission": "x_y", "pattern": "*", "action": "ask"}]))
    assert borradas == ["/v1/sessions/ses_v"]


# ── The chat turn relays each ask ───────────────────────────────────────────────────────────────

def _harness():
    """The chat-turn harness of `test_tiempo_maximo_del_turno.py` (everything around the stream scripted)."""
    import importlib.util
    import pathlib
    ruta = pathlib.Path(__file__).with_name("test_tiempo_maximo_del_turno.py")
    spec = importlib.util.spec_from_file_location("_harness_del_turno", ruta)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


T = _harness()
chat = T.chat

@pytest.mark.asyncio
async def test_the_test_chat_runs_on_its_session_without_auto_grant_and_relays_each_ask(chat, monkeypatch):
    from app import agent_sessions
    from app.routes import agent_routes as R
    vistos = {}

    class _Proxy(T._Proxy):
        async def stream_interactive(self, session_id, prompt, *, timeout=None, request_id=None, nivel=None,
                                     auto_grant=True):
            vistos["auto_grant"] = auto_grant
            for ev in self.eventos:
                yield ev

    class _Sesiones(T._Sesiones):
        async def resolve(self, scope, force_new=False, **kw):
            vistos["resolve"] = kw
            return T._Handle()

    async def tabla():
        return {}
    monkeypatch.setattr(agent_sessions, "resolve_tool_permissions", tabla)
    monkeypatch.setattr(R, "get_session_manager", lambda: _Sesiones())
    eventos = [{"type": "tool", "tool": "wai-odoo_write", "callID": "c1",
                "state": {"status": "running", "input": {"model": "res.partner", "values": {"name": "ACME"}}}},
               {"type": "permission", "id": "per_7", "permission": "wai-odoo_write", "callID": "c1"},
               {"type": "delta", "delta": "Hecho."}, {"type": "done"}]
    proxy = _Proxy(eventos)
    monkeypatch.setattr(R, "get_agent_proxy", lambda: proxy)
    PC.abrir_hilo("ana@cobsa.es", "h1")
    req = Request({"type": "http", "method": "POST", "path": "/api/agent/chat", "headers": [], "query_string": b"",
                   "state": {"prueba_controlada": True}})
    await R.chat_with_agent(R.ChatRequest(message="Crea ACME", thread_id="h1"), req, {"email": "ana@cobsa.es"})
    t = await R.get_turn(agent_sessions.interactive_scope("ana@cobsa.es", "h1").scope_key)
    await asyncio.wait_for(t.task, timeout=5)
    assert vistos["auto_grant"] is False and "wai-odoo_write" in {
        r["permission"] for r in vistos["resolve"]["prueba_controlada"]["reglas"]}
    (perm,) = [e for e in t.events if e["type"] == "permission"]
    assert perm["id"] == "per_7" and perm["argumentos"]["values"] == {"name": "ACME"}
    assert PC.PENDIENTES["per_7"]["sesion"] == "ses_cora" and PC.PENDIENTES["per_7"]["hilo"] == "h1"


@pytest.mark.asyncio
async def test_a_test_chat_never_talks_in_a_real_conversation(chat, monkeypatch):
    from app.routes import agent_routes as R
    monkeypatch.setattr(R, "get_agent_proxy", lambda: T._Proxy([]))
    req = Request({"type": "http", "method": "POST", "path": "/api/agent/chat", "headers": [], "query_string": b"",
                   "state": {"prueba_controlada": True}})
    with pytest.raises(HTTPException) as e:
        await R.chat_with_agent(R.ChatRequest(message="hola", thread_id="h1"), req, {"email": "ana@cobsa.es"})
    assert e.value.status_code == 403
