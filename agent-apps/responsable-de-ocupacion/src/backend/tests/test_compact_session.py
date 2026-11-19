"""v7.95.1 — ``compact_session`` tri-state contract.

OpenCode's session store lives in the agent CONTAINER (not the PVC), so an
agent-pod redeploy wipes it while the app's registry row survives. Compacting
that stale id 404s — which is NOT a failure: there is nothing left to compact
and the next message self-heals into a fresh session. ``compact_session``
must therefore distinguish ``"gone"`` (404 → the route resets the stale
counter and answers 200 ``{compacted: false, stale: true}``) from ``"ok"``
(200) and ``"error"`` (anything else → 502 to the operator).
"""
from __future__ import annotations

import httpx
import pytest

pytestmark = pytest.mark.asyncio


class _StubResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.text = ""

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self.status_code}", request=None, response=None  # type: ignore[arg-type]
            )


class _StubClient:
    """Minimal httpx.AsyncClient stand-in: fixed status or raised error."""

    def __init__(self, status_code: int = 200, exc: Exception | None = None) -> None:
        self._status = status_code
        self._exc = exc

    def __call__(self, *args, **kwargs):  # AsyncClient(timeout=…, auth=…) → instance
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, *args, **kwargs):
        if self._exc:
            raise self._exc
        return _StubResponse(self._status)


def _proxy(monkeypatch, client: _StubClient):
    import app.agent_proxy as ap
    # _BASIC_AUTH is sealed at import from AGENT_SHARED_TOKEN (absent in the
    # test env) and __init__ refuses to build an unauthenticated proxy —
    # inject a stand-in credential the same way the stub replaces the client.
    monkeypatch.setattr(ap, "_BASIC_AUTH", httpx.BasicAuth("opencode", "test-token"))
    monkeypatch.setattr(ap.httpx, "AsyncClient", client)
    return ap.AgentProxy(base_url="http://agent:4097")


async def test_compact_ok(monkeypatch) -> None:
    proxy = _proxy(monkeypatch, _StubClient(200))
    assert await proxy.compact_session("ses_x", "anthropic/claude-sonnet-5") == "ok"


async def test_compact_gone_on_404(monkeypatch) -> None:
    proxy = _proxy(monkeypatch, _StubClient(404))
    assert await proxy.compact_session("ses_x", "anthropic/claude-sonnet-5") == "gone"


async def test_compact_error_on_5xx(monkeypatch) -> None:
    proxy = _proxy(monkeypatch, _StubClient(500))
    assert await proxy.compact_session("ses_x", "anthropic/claude-sonnet-5") == "error"


async def test_compact_error_on_network_failure(monkeypatch) -> None:
    proxy = _proxy(monkeypatch, _StubClient(exc=httpx.ConnectError("boom")))
    assert await proxy.compact_session("ses_x", "anthropic/claude-sonnet-5") == "error"


async def test_compact_error_on_flat_model(monkeypatch) -> None:
    # A model without the provider/ prefix can't satisfy SummarizePayload —
    # refuse locally instead of letting OpenCode 400.
    proxy = _proxy(monkeypatch, _StubClient(200))
    assert await proxy.compact_session("ses_x", "claude-sonnet-5") == "error"


# ── v12.4.0 — compaction leaves a MARK in the thread ────────────────────────────
# Compacting summarised the thread for the model and left the screen just as long: the
# conversation became heavy to look at right when you had just lightened it inside, and
# where the employee had lost the detail was visible nowhere. Now it is marked
# IN THE THREAD —not in the client— so the fold survives a reload and shows up the same
# on mobile and on the web. These cases pin that part, which is the one that can break
# without anyone noticing: the summary itself is done by OpenCode and already covered above.

async def test_marcar_compactado_deja_la_marca_y_se_lee():
    from app import chat_threads as T
    ana = "ana@ejemplo.com"
    hilo = await T.asegurar_por_defecto(ana)

    assert await T.compactado_en(ana, hilo) is None, "nace sin marca: nunca se compactó"
    marca = await T.marcar_compactado(ana, hilo)
    assert marca, "marcar tiene que devolver CUÁNDO, que es lo que el cliente usa para plegar"
    assert marca.endswith("+00:00"), "en UTC y con la zona puesta, o el cliente la lee como local"
    assert await T.compactado_en(ana, hilo) == marca
    # And it shows up in the list, which is where the client reads it when opening the conversation.
    fila = next(h for h in await T.listar(ana) if h["id"] == hilo)
    assert fila["compactado_en"] == marca


async def test_el_alias_default_marca_la_conversacion_de_quien_llama():
    """An old client sends an empty `thread_id` or 'default'. Without resolving it, the mark would be
    written to a thread that does not exist and the fold would never appear."""
    from app import chat_threads as T
    ana = "ana@ejemplo.com"
    suyo = await T.asegurar_por_defecto(ana)
    assert await T.marcar_compactado(ana, "") is not None
    assert await T.compactado_en(ana, suyo) is not None


async def test_compactar_lo_mio_no_marca_lo_de_otra_persona():
    from app import chat_threads as T
    ana, bea = "ana@ejemplo.com", "bea@ejemplo.com"
    de_ana = await T.asegurar_por_defecto(ana)
    de_bea = await T.asegurar_por_defecto(bea)

    await T.marcar_compactado(ana, de_ana)
    assert await T.compactado_en(bea, de_bea) is None, "la de Bea sigue sin compactar"
    # And Ana cannot mark Bea's even when naming it by its exact id.
    assert await T.marcar_compactado(ana, de_bea) is None
    assert await T.compactado_en(bea, de_bea) is None
