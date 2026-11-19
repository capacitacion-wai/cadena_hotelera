"""The maximum time of a turn is the operator's, live (15.6.0, `app/execution_limits.py`).

Measured on 06/11/2026 in cobsa («Cora»): the chat cut at exactly 600.0 s three times in one
afternoon while the agent was legitimately working (80 ERP lookups), the person read «stream
timeout», and the run went on unseen in the same session — so the next message («why did you
error?») queued behind it and died at 600.0 s too.

What is pinned: the resolution order (stored choice → manifest → deploy env → default) and its
clamping; the PATCH that takes effect on the next message; the chat passing ITS ceiling to the
sidecar; and, on reaching it, the run ABORTED before the person gets a coded error
(`TURN_TIME_LIMIT`) instead of «stream timeout»."""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app import execution_limits as el
from app.app_database import app_write
from app.app_models.runtime import AppSetting


@pytest.fixture(autouse=True)
def _sin_cache():
    el.invalidate_cache()
    yield
    el.invalidate_cache()


async def _guardar(key: str, value: str) -> None:
    async with app_write() as s:
        s.add(AppSetting(key=key, value=value))
        await s.commit()


# ── Resolution order ────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sin_nada_30_min_de_chat_y_los_15_de_siempre_en_autonomo(monkeypatch):
    monkeypatch.setattr(el, "_exec_cfg", lambda: {})
    assert await el.effective_time_limits() == (30, 15)
    assert await el.chat_turn_seconds() == 1800.0
    assert await el.autonomous_run_seconds() == 900.0


@pytest.mark.asyncio
async def test_el_env_del_despliegue_sigue_mandando_en_el_autonomo(monkeypatch):
    """A deployment that set AGENT_AUTONOMOUS_TIMEOUT_SEC behaves the same as before."""
    import app.agent_proxy as ap
    monkeypatch.setattr(el, "_exec_cfg", lambda: {})
    monkeypatch.setattr(ap, "AUTONOMOUS_TIMEOUT_SEC", 1800.0)
    assert await el.effective_time_limits() == (30, 30)


@pytest.mark.asyncio
async def test_el_manifiesto_gana_al_env_y_lo_guardado_gana_al_manifiesto(monkeypatch):
    import app.agent_proxy as ap
    monkeypatch.setattr(ap, "AUTONOMOUS_TIMEOUT_SEC", 1800.0)
    monkeypatch.setattr(el, "_exec_cfg", lambda: {"chat_turn_max_minutes": 20,
                                                  "autonomous_run_max_minutes": 10})
    assert await el.effective_time_limits() == (20, 10)
    el.invalidate_cache()
    await _guardar(el.CHAT_TURN_KEY, "45")
    await _guardar(el.AUTONOMOUS_RUN_KEY, "25")
    assert await el.effective_time_limits() == (45, 25)


@pytest.mark.asyncio
async def test_todo_se_acota_al_rango(monkeypatch):
    import app.agent_proxy as ap
    monkeypatch.setattr(ap, "AUTONOMOUS_TIMEOUT_SEC", 60.0)       # 1 min ⇒ the floor
    monkeypatch.setattr(el, "_exec_cfg", lambda: {"chat_turn_max_minutes": 500})
    assert await el.effective_time_limits() == (60, 5)
    el.invalidate_cache()
    await _guardar(el.CHAT_TURN_KEY, "2")
    await _guardar(el.AUTONOMOUS_RUN_KEY, "basura")                 # unreadable ⇒ the layer below
    assert await el.effective_time_limits() == (5, 5)


# ── The settings endpoint ───────────────────────────────────────────────────────────────────────

class _Admin:
    email = "admin@wai.com"
    is_superuser = True


@pytest.fixture
def admin(monkeypatch):
    import app.users_directory as ud

    async def _is_admin(email: str) -> bool:
        return email == _Admin.email
    monkeypatch.setattr(ud, "is_admin", _is_admin, raising=False)
    monkeypatch.setattr(el, "_exec_cfg", lambda: {})
    return _Admin()


@pytest.mark.asyncio
async def test_el_patch_se_nota_en_el_siguiente_mensaje(admin, monkeypatch):
    from app.routes import manifest_routes as mr
    assert await el.chat_turn_seconds() == 1800.0                 # warms the cache
    escrituras: dict[str, str] = {}

    async def _upsert(key, value, user):                          # noqa: ANN001 — test double
        escrituras[key] = value
        await _guardar(key, value)
    monkeypatch.setattr(mr, "_upsert_setting", _upsert)

    async def _ajustes(user):                                     # the GET is not under test here
        return {}
    monkeypatch.setattr(mr, "get_settings", _ajustes)
    await mr.patch_settings(body={"chat_turn_max_minutes": 90,
                                  "autonomous_run_max_minutes": 20}, user=admin)
    assert escrituras == {el.CHAT_TURN_KEY: "60", el.AUTONOMOUS_RUN_KEY: "20"}
    # No TTL wait: the PATCH dropped the cache, so the very next turn reads it.
    assert await el.chat_turn_seconds() == 3600.0
    assert await el.autonomous_run_seconds() == 1200.0

    with pytest.raises(HTTPException) as e:
        await mr.patch_settings(body={"chat_turn_max_minutes": "media hora"}, user=admin)
    assert e.value.status_code == 400


@pytest.mark.asyncio
async def test_el_get_ensena_el_efectivo_el_defecto_y_el_rango(admin):
    from app.routes import manifest_routes as mr
    await _guardar(el.CHAT_TURN_KEY, "40")
    out = await mr.get_settings(admin)
    assert out["chat_turn_max_minutes"] == 40
    assert out["chat_turn_max_minutes_default"] == 30
    assert out["autonomous_run_max_minutes"] == 15
    assert out["time_limit_range"] == {"min": 5, "max": 60}


# ── The autonomous path reads it per run ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_autonomous_sin_timeout_usa_el_techo_vivo(monkeypatch):
    import app.agent_proxy as ap
    monkeypatch.setattr(el, "_exec_cfg", lambda: {})
    await _guardar(el.AUTONOMOUS_RUN_KEY, "40")
    vistos = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"text": "ok"}

    class _Cliente:
        async def post(self, url, json, timeout):                  # noqa: A002 — httpx's name
            vistos["timeout_s"] = json["timeout_s"]
            vistos["read"] = timeout.read
            return _Resp()

    proxy = ap.AgentProxy.__new__(ap.AgentProxy)
    proxy._client = _Cliente()
    await proxy.send_autonomous("ses_1", "do it")
    assert vistos == {"timeout_s": 2400.0, "read": 2430.0}
    # An explicit, deliberately shorter budget (a colleague) is still honoured.
    await proxy.send_autonomous("ses_1", "do it", timeout=180.0, require_structured=False)
    assert vistos["timeout_s"] == 180.0


def test_el_tope_es_el_texto_exacto_del_sidecar():
    from app.agent_proxy import is_turn_time_limit
    from app.autonomous.executor import _is_heavy_failure
    from app.agent_proxy import RecoverableAgentError
    assert is_turn_time_limit("stream timeout")
    assert not is_turn_time_limit("SSE connection timeout")       # the sidecar's 10-s attach
    assert not is_turn_time_limit("stream transport: ReadTimeout")
    # The executor's heavy-failure classification keeps matching it.
    assert _is_heavy_failure(RecoverableAgentError("stream timeout"))


# ── The chat turn ───────────────────────────────────────────────────────────────────────────────

class _Proxy:
    """The sidecar, scripted: records the ceiling each turn was given and every abort."""

    def __init__(self, eventos):
        self.eventos, self.timeouts, self.abortadas = eventos, [], []

    async def stream_interactive(self, session_id, prompt, *, timeout=None, request_id=None,
                                 nivel=None):
        self.timeouts.append(timeout)
        for ev in self.eventos:
            yield ev

    async def abort_session(self, session_id):
        self.abortadas.append(session_id)
        return True


class _Handle:
    session_id = "ses_cora"
    needs_prime = False


class _Sesiones:
    async def resolve(self, scope, force_new=False):
        return _Handle()

    async def touch(self, *a, **k):
        return None

    async def delete(self, *a, **k):
        return None


@pytest.fixture
def chat(monkeypatch):
    """`POST /chat` with everything around the stream scripted, so what runs is the turn."""
    import app.budget as budget
    import app.memory_utils as mu
    from app import chat_threads, razonamiento
    from app import contexto_de_turno
    from app.routes import agent_routes as R

    async def _nada(*a, **k):
        return None
    monkeypatch.setattr(budget, "budget_block_reason", _nada)
    monkeypatch.setattr(budget, "user_cap_status", _nada)
    monkeypatch.setattr(mu, "persist_run_memory_user", _nada)

    async def _hilo(email, tid):
        return "h1"

    async def _obtener(email, hid):
        return {"id": hid}

    async def _ninguna(*a, **k):
        return []
    monkeypatch.setattr(chat_threads, "resolver_hilo", _hilo)
    monkeypatch.setattr(chat_threads, "obtener", _obtener)
    monkeypatch.setattr(chat_threads, "sesiones_a_desalojar", _ninguna)
    monkeypatch.setattr(chat_threads, "tocar", _nada)

    async def _nivel(*a, **k):
        return "", "nadie"

    async def _largo(*a, **k):
        return ""
    monkeypatch.setattr(razonamiento, "resolver", _nivel)
    monkeypatch.setattr(razonamiento, "longitud_para", _largo)
    monkeypatch.setattr(razonamiento, "variante", lambda *a, **k: "")

    async def _delego(**k):
        return {"accion": "delego"}

    async def _sin_atajo(*a, **k):
        return False

    async def _sin_contexto(run_id):
        return "", {}

    async def _run(*a, **k):
        return 7, "tok"
    finales: list[dict] = []

    async def _fin(run_id, **k):
        finales.append(k)
    monkeypatch.setattr(R, "_recepcion_decide", _delego)
    monkeypatch.setattr(R, "_atajo_resuelve", _sin_atajo)
    monkeypatch.setattr(contexto_de_turno, "bloque", _sin_contexto)
    monkeypatch.setattr(R, "_create_chat_run", _run)
    monkeypatch.setattr(R, "_finish_chat_run", _fin)
    monkeypatch.setattr(R, "get_session_manager", lambda: _Sesiones())
    monkeypatch.setattr(el, "_exec_cfg", lambda: {})

    async def turno(eventos) -> tuple[_Proxy, list[dict], list[dict]]:
        proxy = _Proxy(eventos)
        monkeypatch.setattr(R, "get_agent_proxy", lambda: proxy)
        user = {"email": "ana@cobsa.es"}
        peticion = Request({"type": "http", "method": "POST", "path": "/api/agent/chat",
                            "headers": [], "query_string": b"", "state": {}})
        await R.chat_with_agent(R.ChatRequest(message="¿Qué pedidos faltan?"), peticion, user)
        from app.agent_sessions import interactive_scope
        t = await R.get_turn(interactive_scope("ana@cobsa.es", "h1").scope_key)
        await asyncio.wait_for(t.task, timeout=5)
        return proxy, list(t.events), finales
    return turno


@pytest.mark.asyncio
async def test_el_chat_pasa_su_techo_configurado_al_sidecar(chat):
    await _guardar(el.CHAT_TURN_KEY, "45")
    proxy, eventos, finales = await chat([{"type": "delta", "delta": "Hecho."},
                                          {"type": "done"}])
    assert proxy.timeouts == [2700.0]
    assert any(e["type"] == "complete" for e in eventos)
    assert proxy.abortadas == []


@pytest.mark.asyncio
async def test_al_agotar_el_techo_detiene_la_ejecucion_y_lo_dice_con_un_codigo(chat):
    await _guardar(el.CHAT_TURN_KEY, "30")
    proxy, eventos, finales = await chat([
        {"type": "tool", "tool": "erp_consulta", "callID": "c1",
         "state": {"status": "running", "input": {}}},
        {"type": "error", "error": "stream timeout"},
        {"type": "done"},
    ])
    # The run is STOPPED (it would go on unseen and the next message would queue behind it)…
    assert proxy.abortadas == ["ses_cora"]
    # …never retried (a second full ceiling)…
    assert proxy.timeouts == [1800.0]
    # …and the screen gets a code and the minutes, not «stream timeout».
    error = [e for e in eventos if e["type"] == "error"]
    assert len(error) == 1
    assert error[0]["code"] == "TURN_TIME_LIMIT" and error[0]["minutes"] == 30
    assert "stream timeout" not in error[0]["message"]
    assert finales[-1]["status"] == "failed"
    assert finales[-1]["error"].startswith("TURN_TIME_LIMIT")
