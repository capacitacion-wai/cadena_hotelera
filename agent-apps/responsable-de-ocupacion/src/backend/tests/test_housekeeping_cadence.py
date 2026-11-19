"""The housekeeping cadence is configurable, and has a floor (7.162.0).

It was hard-coded as the default argument of `_housekeeping_run_forever`
(86400 s), called with no arguments: the only way to change it was editing `main.py`. An
employee that learns fast could not sweep more often, and a quiet one could not sweep
less.

The 1-hour FLOOR is what really protects here. Housekeeping writes to the SAME
SQLite as the watcher, the executor and the poller, and SQLite in WAL allows ONE writer:
sweeping every few minutes is asking for the «database is locked» that already cost an incident.
Whoever wants to clean up *now* has the on-demand endpoint; lowering the interval would leave a
permanent sweep competing for the lock.
"""
from __future__ import annotations

import importlib
import pathlib

import pytest

_MAIN = pathlib.Path(__file__).resolve().parent.parent / "app" / "main.py"


def _config(monkeypatch, valor=None):
    if valor is None:
        monkeypatch.delenv("AGENT_HOUSEKEEPING_INTERVAL_HOURS", raising=False)
    else:
        monkeypatch.setenv("AGENT_HOUSEKEEPING_INTERVAL_HOURS", valor)
    import app.config as C
    return importlib.reload(C)


def test_por_defecto_una_vez_al_dia(monkeypatch):
    assert _config(monkeypatch).AGENT_HOUSEKEEPING_INTERVAL_HOURS == 24.0


@pytest.mark.parametrize("valor,esperado", [("6", 6.0), ("1", 1.0), ("72", 72.0)])
def test_se_puede_configurar(monkeypatch, valor, esperado):
    assert _config(monkeypatch, valor).AGENT_HOUSEKEEPING_INTERVAL_HOURS == esperado


@pytest.mark.parametrize("valor,esperado", [
    ("0", 1.0),        # turning the sweep off is not an option → floor
    ("0.01", 1.0),     # every 36 s → floor
    ("-5", 24.0),      # negative = nonsense → `_float_env` falls back to the default
])
def test_el_suelo_de_una_hora_no_se_puede_saltar(monkeypatch, valor, esperado):
    """Sweeping every few minutes competes for SQLite's single writer."""
    v = _config(monkeypatch, valor).AGENT_HOUSEKEEPING_INTERVAL_HOURS
    assert v == esperado and v >= 1.0


def test_un_valor_absurdo_no_tumba_el_arranque(monkeypatch):
    assert _config(monkeypatch, "pero-que-dices").AGENT_HOUSEKEEPING_INTERVAL_HOURS == 24.0


def test_main_USA_la_configuracion_y_no_el_defecto_del_codigo():
    """The concrete regression: it was called with no arguments, so the configured
    value never reached the loop."""
    txt = _MAIN.read_text(encoding="utf-8")
    i = txt.index("_housekeeping_run_forever(", txt.index("housekeeping_task"))
    llamada = txt[i:i + 120]
    assert "_hk_h" in llamada and "3600" in llamada, (
        "main.py vuelve a arrancar el housekeeping sin pasarle el intervalo "
        "configurado: la variable de entorno quedaría de adorno."
    )


def test_existe_la_salida_bajo_demanda():
    """The endpoint is the answer to «I want to clean up now», so nobody is
    tempted to lower the interval to one minute."""
    rutas = (pathlib.Path(__file__).resolve().parent.parent
             / "app" / "routes" / "agent_memory_routes.py").read_text(encoding="utf-8")
    assert '@router.post("/compact")' in rutas
