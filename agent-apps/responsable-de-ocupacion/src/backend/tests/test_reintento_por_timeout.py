"""A turn that runs out the clock WITHOUT having emitted anything is retried once.

Measured in `demo` on 31/07/2026: a model call took **7 minutes** to emit its
first token, with the ones before and after at 8 seconds. The latency tail
exists —one in every 170 calls— and sometimes crosses the turn's ceiling.
Losing the whole turn to that is avoidable.

The condition is **strict on purpose**: only what never got to start is retried.
If text or a tool already came out, repeating would duplicate work —and
writes— and the operator would see the answer twice. A retry that duplicates
a write in a customer's ERP is worse than a lost turn.

15.6.0 — the turn's CEILING (`stream timeout`, now `chat_turn_max_minutes`, default 30 min)
is no longer part of this: it is the operator's decision, so it is never retried (a retry is
a second full ceiling); the run is aborted and the person told (`_cerrar_por_tope`,
tests/test_tiempo_maximo_del_turno.py). The retry stays for the other clocks (the sidecar's
10-s «SSE connection timeout»).
"""
from __future__ import annotations

import ast
import pathlib

_RUTA = (pathlib.Path(__file__).resolve().parent.parent
         / "app" / "routes" / "agent_routes.py")
_SRC = _RUTA.read_text(encoding="utf-8")


def _rama_error() -> str:
    """The chunk that decides what to do when the sidecar sends an `error`."""
    i = _SRC.index('elif etype == "error":')
    # The window is measured in characters and the branch GROWS with everything recorded in it
    # (12.22.0: the turn's bill). If it falls short, the test fails saying a guard is missing
    # that IS there — a red that blames the product for something in the test.
    return _SRC[i:i + 2900]


def test_reintenta_cuando_agota_el_reloj():
    r = _rama_error()
    assert 'attempt == 1' in r, "solo una vez, nunca en bucle"
    assert '"timeout" in err.lower()' in r, "solo por reloj, no por cualquier error"


def test_NO_reintenta_si_el_turno_ya_habia_emitido_algo():
    """The condition that avoids duplicating work and writes."""
    r = _rama_error()
    # 15.12.0: a heartbeat (`latido`) is not output, so the guard skips it.
    assert 'not any(e.get("type") != "latido" for e in turn.events)' in r, (
        "falta la guarda: sin ella un turno que ya escribió en el ERP se "
        "repetiría entero y el operador vería la respuesta dos veces")


def test_aborta_la_sesion_antes_de_reintentar():
    """Without aborting, the previous generation stays alive in OpenCode consuming
    budget while the second one runs."""
    r = _rama_error()
    assert "abort_session" in r


def test_el_abort_no_puede_impedir_el_reintento():
    """If the abort fails, the retry must go ahead: the goal is to recover
    the turn, not leave it lost because of a cleanup failure."""
    r = _rama_error()
    i = r.index("abort_session")
    assert "except Exception" in r[i - 200:i + 260]


def test_cualquier_otro_error_sigue_terminando_el_turno():
    """The retry is the exception, not the rule: a real error has to reach
    the operator, not be retried silently."""
    r = _rama_error()
    assert 'return "error"' in r
    assert "_finish_chat_run" in r


def test_el_techo_del_turno_se_mira_antes_que_el_reintento():
    """The ceiling is matched BEFORE the retry: `"timeout" in err` would also catch it."""
    r = _rama_error()
    assert r.index("is_turn_time_limit(err)") < r.index("attempt == 1")
    assert "_cerrar_por_tope" in r
