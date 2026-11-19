"""The agent's instructions are not repeated in several places.

The working discipline was written **three times** —backend, deployer template and
the trailer of every autonomous run—, in two languages and across three services.
Improving one left the other two saying the old thing, and nothing warned.

Now there is an explicit split and this file holds it in place:

* **`AGENTS.md`** carries the four generic rules. It is loaded in ALL sessions,
  so the agent already has them in front of it. Its single source is the
  ``AGENTS-MD`` block of ``skill-agent-core.md`` (see `test_agent_core_rules_sync.py`
  in the platform backend).
* **The `wai-agent-core` skill** carries the detail, on demand.
* **The run trailer** (`plan_verify_block`) shares NOTHING with `AGENTS.md`.
  The agent already has it loaded in the session —also in autonomous runs, which
  go through the same sidecar—, so repeating it, even just by naming it, only
  spends context. ONLY the guardrails that exist nowhere else go here: the abort
  on repetition, the `bash` watchdog, what to do with a stuck sub-step, and the
  budget.
* **The delivery block** (`delivery_block`) is a pointer to
  `wai-autonomous-delivery` plus the three rules that admit no nuance, and it is only
  injected if the employee has channels.

If someone pastes the long text into the trailer again, this goes red.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

_EJECUTOR = (pathlib.Path(__file__).resolve().parent.parent
             / "app" / "autonomous" / "executor.py")
_SRC = _EJECUTOR.read_text(encoding="utf-8")
_ARBOL = ast.parse(_SRC)


def _texto(nombre: str) -> str:
    """The literal the function returns to the prompt."""
    for n in ast.walk(_ARBOL):
        if isinstance(n, ast.FunctionDef) and n.name == nombre:
            for nd in ast.walk(n):
                if isinstance(nd, ast.Return) and nd.value is not None:
                    try:
                        v = ast.literal_eval(nd.value)
                    except Exception:
                        continue
                    if isinstance(v, str) and len(v) > 100:
                        return v
    raise AssertionError(f"no encuentro el texto de {nombre}")


def test_el_trailer_NO_reescribe_las_reglas_genericas():
    """The regression: explaining again in the trailer what is already in AGENTS.md.
    Detected by the long phrases that ONLY appeared in the copy."""
    t = _texto("plan_verify_block")
    for copiada in (
        "en 1-2 líneas decide el enfoque",
        "¿puedo definir qué es 'hecho'?",
        "son zip+XML",
        "no un clon byte a byte",
        # The same phrases in English (the trailer is in English since 14.2.0).
        "State the approach in 1-2 lines",
        "Can I define \"done\"",
        "byte-for-byte",
    ):
        assert copiada not in t, (
            f"el trailer vuelve a explicar «{copiada}»: eso vive en AGENTS.md, "
            "cuya fuente es el bloque AGENTS-MD de skill-agent-core.md")


def test_el_trailer_NO_COMPARTE_NADA_con_AGENTS_md():
    """The agent already has `AGENTS.md` loaded in the session — also in autonomous
    runs, which go through the same sidecar. Repeating its rules, even just by
    naming them, only spends context and opens the door to the two versions
    drifting apart. The trailer carries ONLY what is nowhere else."""
    t = _texto("plan_verify_block")
    for compartido in ("planifica", "factibilidad", "esfuerzo proporcional",
                       "tipo de dato", "AGENTS.md", "wai-agent-core",
                       "feasibility", "proportional effort", "data type"):
        assert compartido.lower() not in t.lower(), (
            f"el trailer vuelve a mencionar «{compartido}»: eso ya lo tiene el "
            "agente cargado, aquí solo van los guardarraíles del run")


@pytest.mark.parametrize("guardarrail", [
    "AGENT_MAX_REPEAT_STEPS",   # the deterministic abort on repetition
    "bash",                     # the watchdog: do not poll in a loop
    "log_decision",             # what to do when a sub-step gets stuck
    "budget",                   # narrow the scope instead of insisting
])
def test_lo_que_SOLO_existe_en_el_run_autonomo_se_conserva(guardarrail):
    """When removing the generic copy it was easy to take the specific parts with it,
    which are neither in AGENTS.md nor in the skill."""
    assert guardarrail in _texto("plan_verify_block")


def test_las_entregas_apuntan_a_su_skill_en_vez_de_copiarla():
    """`wai-autonomous-delivery` is 8 KB: the trailer only carries what admits no
    nuance and refers to the rest."""
    t = _texto("delivery_block")
    assert "wai-autonomous-delivery" in t
    assert len(t) < 900, (
        "el bloque de entregas está creciendo hacia una copia de la skill "
        f"({len(t)} car): deja aquí lo imprescindible y remite a ella")


def test_las_entregas_solo_se_inyectan_si_hay_canales():
    """A monitoring agent without channels has nowhere to deliver a file;
    loading those rules onto it biased it towards the mail-analyst archetype."""
    fuente = ast.get_source_segment(_SRC, next(
        n for n in ast.walk(_ARBOL)
        if isinstance(n, ast.FunctionDef) and n.name == "delivery_block")) or ""
    assert "CHANNELS_ENABLED" in fuente and 'return ""' in fuente
