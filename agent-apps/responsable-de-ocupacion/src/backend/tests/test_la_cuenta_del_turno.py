"""Where a turn's time went (19/09/2026).

The only measure there was is `duration_sec`, rounded to seconds. With that you cannot
decide what to optimize: a 41 s turn can be the model thinking or six slow tools, and
the answer completely changes what needs fixing.

What is checked here is the breakdown, which is exactly where it is easy to lie without
noticing: tools can run AT THE SAME TIME and adding them up gave more tool time than
turn time.
"""
from __future__ import annotations

import time

from app.medicion import MedicionDelTurno, _union_ms


def test_lo_solapado_se_cuenta_una_vez():
    # Two 100 ms calls that fully overlap are 100 ms of waiting, not 200.
    t = 1000.0
    assert _union_ms([(t, t + 0.1), (t, t + 0.1)]) == 100
    # And two back to back do add up.
    assert _union_ms([(t, t + 0.1), (t + 0.2, t + 0.3)]) == 200


def test_el_reparto_no_puede_pasarse_del_total():
    m = MedicionDelTurno(run_id=1)
    m.herramienta_empieza("a", "buscar")
    m.herramienta_empieza("b", "mapa")
    time.sleep(0.02)
    m.herramienta_termina("a", "buscar")
    m.herramienta_termina("b", "mapa")
    d = m.resumen()
    assert d["ms_herramientas"] <= d["ms_total"]
    assert d["ms_modelo"] == d["ms_total"] - d["ms_herramientas"]
    assert d["llamadas"] == 2


def test_la_primera_palabra_se_marca_una_vez():
    m = MedicionDelTurno()
    m.texto(5)
    primera = m.ms_primer_texto
    time.sleep(0.01)
    m.texto(5)
    assert m.ms_primer_texto == primera
    assert m.resumen()["caracteres_respuesta"] == 10


def test_la_misma_parte_reemitida_no_cuenta_tres_veces():
    """opencode re-emits the SAME invocation as it progresses (pending → running → completed).
    Counting each event, a turn with 6 calls declared 18 and opened 18 intervals it never
    closed: "tool time" became "from the first call to the end of the turn". Measured in
    demo on 19/09/2026, before the sidecar sent `callID`.
    """
    m = MedicionDelTurno()
    for _ in range(3):
        m.herramienta_empieza("c1", "buscar")       # pending, running, running…
    time.sleep(0.01)
    for _ in range(3):
        m.herramienta_termina("c1", "buscar")       # completed, re-emitted twice
    d = m.resumen()
    assert d["llamadas"] == 1, d["herramientas"]
    ficha = d["herramientas"][0]
    assert ficha["n"] == 1 and not ficha.get("sin_cerrar")
    # In a real turn the breakdown leaves room for the model; here the tool takes up
    # almost all of the synthetic "turn", so what is required is that it does not EXCEED it.
    assert d["ms_herramientas"] <= d["ms_total"]


def test_sin_llave_de_invocacion_sigue_cuadrando():
    """With an old sidecar (no `callID`) it falls back to the tool name. Two simultaneous
    calls to the same tool merge into one (the code says so) but the breakdown still means
    what it says: what must not happen is an interval that never closes."""
    m = MedicionDelTurno()
    m.herramienta_empieza("", "mapa")
    time.sleep(0.01)
    m.herramienta_termina("", "mapa")
    d = m.resumen()
    assert d["llamadas"] == 1
    assert not d["herramientas"][0].get("sin_cerrar")


def test_una_herramienta_que_no_devuelve_sale_en_la_cuenta():
    """It is exactly the one to look at: if it were discarded, the turn would look like all model."""
    m = MedicionDelTurno()
    m.herramienta_empieza("x", "pms_consultar")
    time.sleep(0.01)
    d = m.resumen()
    ficha = next(h for h in d["herramientas"] if h["nombre"] == "pms_consultar")
    assert ficha.get("sin_cerrar") is True
    assert d["ms_herramientas"] > 0


def test_los_tokens_de_razonamiento_y_cache_se_suman():
    m = MedicionDelTurno()
    m.paso_cerrado(tokens={"input": 10, "output": 2, "reasoning": 7,
                           "cache": {"read": 100, "write": 3}}, coste=0.01,
                   modelo="openai/gpt-5.6-luna")
    m.paso_cerrado(tokens={"input": 5, "output": 1, "reasoning": 0,
                           "cache": {"read": 50, "write": 0}}, coste=0.02)
    d = m.resumen()
    assert d["tokens"] == {"entrada": 15, "salida": 3, "razonamiento": 7,
                           "cache_lectura": 150, "cache_escritura": 3}
    assert d["pasos"] == 2 and round(d["coste"], 4) == 0.03
    assert d["modelo"] == "openai/gpt-5.6-luna"
