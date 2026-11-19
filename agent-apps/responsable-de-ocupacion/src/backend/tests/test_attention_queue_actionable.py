"""What the UI ASKS to decide must be decidable right there (7.161.0).

The `«Necesita tu criterio»` block of the Learning panel listed contradictions and
learnings seen only once, and ended each notice with
`«Confirma la que sea cierta hoy y descarta la otra»`… without a single button. The operator
read the instruction, looked for the button and it did not exist: the only way to act was to
go to the list below and find the entry by hand.

It is the same failure `test_prompt_tool_contract` pins on the model side —asking for
something without offering the mechanism—, except that here the one obeying is a person.
And it fails just as quietly: nothing breaks, the queue simply never goes down.

The test is static: it reads the component and checks that each kind of queue notice
carries its wired action. It starts no browser and spends no token.
"""
from __future__ import annotations

import pathlib
import re

import pytest

_VISOR = (pathlib.Path(__file__).resolve().parents[2]
          / "frontend" / "src" / "components" / "LearningViewer.tsx")


def _bloque_atencion() -> str:
    """The queue's JSX.

    Since 7.166.0 the section sits inside a collapsed accordion: the header (title and
    counter) lives in `attention` and the notices with their buttons in `attentionBody`. What
    this test watches is the notices, so it anchors on the BODY.
    """
    txt = _VISOR.read_text(encoding="utf-8")
    i = txt.index("const attentionBody = (")
    fin = txt.index("const health", i)
    return txt[i:fin]


def test_la_cola_de_criterio_existe():
    txt = _VISOR.read_text(encoding="utf-8")
    assert "learning.attention.title" in txt


def test_la_seccion_esta_COLAPSADA_por_defecto():
    """It is a work queue, not a headline: open, it pushed the list below the
    fold. The header counter already says whether there is something to decide."""
    txt = _VISOR.read_text(encoding="utf-8")
    i = txt.index("const attention = (")
    cabecera = txt[i:txt.index("const health", i)]
    assert "defaultExpanded={false}" in cabecera
    assert "learning.attention.title" in cabecera, "el título debe seguir visible plegado"


@pytest.mark.parametrize("aviso,accion", [
    # contradiction → resolving is ONE gesture (confirm one, discard the other)
    ("learning.attention.contradiction", "resolveContradiction"),
    # seen only once → confirm or discard
    ("learning.attention.thin", "reviewById"),
])
def test_cada_aviso_lleva_su_accion(aviso, accion):
    bloque = _bloque_atencion()
    assert aviso in bloque, f"desapareció el aviso {aviso}"
    # The action has to come AFTER the notice's title and before the next one.
    desde = bloque.index(aviso)
    assert accion in bloque[desde:], (
        f"El aviso «{aviso}» se pinta pero no ofrece «{accion}»: le pide al operador "
        f"una decisión que no puede tomar ahí. La cola no bajará nunca."
    )


def test_los_botones_van_cableados_a_un_onClick():
    """A decorative <Button> is worse than none at all."""
    bloque = _bloque_atencion()
    botones = re.findall(r"<Button\b[^>]*", bloque, re.S)
    assert botones, "la cola no tiene ni un botón"
    sin_cablear = [b for b in botones if "onClick" not in b]
    assert not sin_cablear, f"{len(sin_cablear)} botón(es) sin onClick en la cola"


def test_resolver_una_contradiccion_toca_LAS_DOS_entradas():
    """Confirming one without discarding the other leaves the contradiction alive: it would
    show up in the queue again on the next sweep, and the operator would think it does not work."""
    txt = _VISOR.read_text(encoding="utf-8")
    i = txt.index("const resolveContradiction")
    cuerpo = txt[i:txt.index("const review = async", i)]
    assert "'validated'" in cuerpo and "'rejected'" in cuerpo, (
        "resolveContradiction no aplica los dos veredictos"
    )
