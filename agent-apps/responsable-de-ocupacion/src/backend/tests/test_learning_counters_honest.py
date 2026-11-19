"""The learnings counter counts the same thing the employee uses (7.162.0).

What happened, measured on the Vigía: the prune ran at 22:05:52 and retired 39 entries
(`system:no-reforzable`). The block injected into the employee stopped using them
instantly —it filters `review_state != 'rejected'`—, but the panel header kept counting
them as live, so the number stayed stuck at 107.

The operator looked at the panel and said «I don't see it cleaning». And they were right
**about what they saw**: the cleanup had happened and the only number that counted it
ignored it. An invisible prune is indistinguishable from a broken prune.

Two invariants, which are what make that number mean something:

  1. «Live» = the same here as in the injected block. If the two definitions drift
     apart, the panel contradicts the employee again.
  2. What was pruned is PUBLISHED. It is the proof that the lifecycle works.
"""
from __future__ import annotations

import pathlib
import re

_UTILS = (pathlib.Path(__file__).resolve().parent.parent
          / "app" / "agent_memory_utils.py")
_PROMPT = (pathlib.Path(__file__).resolve().parent.parent
           / "app" / "agent_memory_prompt.py")
_VISOR = (pathlib.Path(__file__).resolve().parents[2]
          / "frontend" / "src" / "components" / "LearningViewer.tsx")


def _cuerpo_stats() -> str:
    txt = _UTILS.read_text(encoding="utf-8")
    i = txt.index("async def stats()")
    return txt[i:txt.index("async def redundancy_report", i)]


def test_vivas_excluye_las_retiradas():
    """The exact regression: 39 retired entries were still counted as live."""
    cuerpo = _cuerpo_stats()
    i = cuerpo.index("live = ")
    definicion = cuerpo[i:i + 220]
    assert 'review_state != "rejected"' in definicion, (
        "«live» no excluye las retiradas: el panel contará como vivo lo que el "
        "empleado ya no usa, y la poda se volverá invisible otra vez."
    )


def test_la_misma_definicion_que_el_bloque_inyectado():
    """If the block filters out the discarded ones and the counter does not, the panel lies."""
    assert 'review_state != "rejected"' in _PROMPT.read_text(encoding="utf-8")
    assert 'review_state != "rejected"' in _cuerpo_stats()


def test_lo_depurado_se_publica():
    cuerpo = _cuerpo_stats()
    assert '"retired"' in cuerpo and '"merged"' in cuerpo, (
        "sin publicar lo retirado/fusionado, un ciclo de vida que funciona y uno "
        "roto se ven igual desde el panel"
    )


def test_caducadas_se_miden_sobre_la_base_no_sobre_las_vivas():
    """The prune marks expired entries as «rejected»; measuring them over «live» would
    always give 0 and we would lose the figure without anyone noticing."""
    cuerpo = _cuerpo_stats()
    i = cuerpo.index("expired = ")
    assert "base.where" in cuerpo[i:i + 200]


def test_el_panel_ENSENA_lo_depurado():
    visor = _VISOR.read_text(encoding="utf-8")
    assert "learning.healthTidied" in visor
    assert re.search(r"stats\.retired", visor), "el visor no lee `retired`"
