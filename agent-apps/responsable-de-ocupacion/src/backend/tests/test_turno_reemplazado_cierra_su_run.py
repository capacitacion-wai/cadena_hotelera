"""A chat turn replaced by a newer message CLOSES its run (15.3.1).

Measured in `cobsa` on 27/10/2026: the person asked «how is it going?» twice while a long
turn was working. Each new message supersedes the running turn (`chat_runtime.start_turn`
cancels its task) and the runner only re-raised the cancellation: the row stayed
«En ejecución» in the history forever — three of them in an hour — until a pod restart
swept it as «Lost during restart».
"""
from __future__ import annotations

import pathlib

import pytest

_RUTA = (pathlib.Path(__file__).resolve().parent.parent
         / "app" / "routes" / "agent_routes.py")
_SRC = _RUTA.read_text(encoding="utf-8")


def _rama_cancelada() -> str:
    i = _SRC.index('return "complete"\n        except asyncio.CancelledError:')
    return _SRC[i:i + 1200]


def test_el_turno_cancelado_cierra_su_run_como_cancelado():
    r = _rama_cancelada()
    assert "_finish_chat_run" in r and 'status="cancelled"' in r
    assert r.rstrip().splitlines()[-1].strip() == "raise" or "\n            raise" in r, (
        "the cancellation must still propagate: the turn is marked cancelled by chat_runtime")


def test_el_cierre_no_lo_corta_la_misma_cancelacion():
    assert "asyncio.shield(_finish_chat_run(" in _rama_cancelada()


def test_cancelled_se_guarda_como_CANCELLED_y_no_como_fallo():
    assert 'else "CANCELLED" if status == "cancelled" else "FAILED"' in _SRC


# ── The calls of a run that ended without their answer ─────────────────────────────────────
# Same afternoon, run #28: its `apply_patch` showed «Pendiente» forever, and a call that FAILED
# showed «Pendiente» too — the card only knew `ok` and «anything else».

_USOS = (_RUTA.parent.parent / "tool_usage.py").read_text(encoding="utf-8")
_RUNS = (_RUTA.parent / "runs_routes.py").read_text(encoding="utf-8")
_FICHA = _RUTA.parents[3] / "frontend" / "src" / "pages" / "RunDetail.tsx"


def test_una_llamada_abierta_de_un_run_terminado_sale_como_interrumpida():
    assert '"interrupted" if run_terminado and f.status == "running"' in _USOS
    assert 'usos_de(run_id, run_terminado=(run.status != "RUNNING"))' in _RUNS


@pytest.mark.skipif(not _FICHA.exists(), reason="the frontend is not next to this backend")
def test_la_ficha_distingue_fallo_interrumpida_y_en_curso():
    _DETALLE = _FICHA.read_text(encoding="utf-8")
    for clave in ('t("runs.tools.failed")', 't("runs.tools.interrupted")', 't("runs.tools.running")'):
        assert clave in _DETALLE
    assert 'estado === "error"' in _DETALLE
