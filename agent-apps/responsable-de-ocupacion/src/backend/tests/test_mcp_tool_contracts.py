"""Each MCP tool's contract matches what the code does (2026-08-01).

The 51 tools the employee sees are 51 hand-maintained docstrings, and their
docstring **is** their interface: it is literally the only thing the model reads
before calling. When text and code drift apart, the agent does not fail loudly —
it **improvises**.

It happened and cost half a demo: `request_approval` only said *"action_type (a
short label, e.g. send_email)"*. It did not say there are two values the platform
executes on its own, nor that with any other value the agent is the one who
executes, nor that nobody reads `action_payload`. The employee parked "load 75
products", the operator approved it, **nothing was executed** and the task was left
unfinished, silently. The agent was not wrong: it read the contract we gave it.

This file turns "we review it" into "it cannot break". It walks the real tool
registry and demands three things of each one:

1. **Every argument it reads, it documents.** An invisible parameter does not exist
   for the model — and if it is a filter of a `list_*`, the agent asks for
   EVERYTHING and the context fills up with the dump.
2. **Every enum lists its values.** "priority (default medium)" without saying which
   ones exist forces guessing.
3. **Every tool that WRITES says what it returns.** Without that, what was just done
   cannot be verified, nor the next step chained.

When a new tool does not comply, this test fails and says exactly what is missing.
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

_FUENTE = (pathlib.Path(__file__).resolve().parent.parent
           / "app" / "mcp_server.py")
_SRC = _FUENTE.read_text(encoding="utf-8")
_ARBOL = ast.parse(_SRC)
_FUNCS = {n.name: n for n in _ARBOL.body if isinstance(n, ast.AsyncFunctionDef)}


def _registro() -> dict[str, str]:
    """`{exposed name: function}` read from the real TOOLS dict, not from a parallel
    list that would go stale."""
    m = re.search(r"^TOOLS: dict\[.*?\] = \{(.*?)^\}", _SRC, re.S | re.M)
    assert m, "no encuentro el registro TOOLS en mcp_server.py"
    return dict(re.findall(r'"([a-z_]+)":\s*([a-z_]+),', m.group(1)))


REGISTRO = _registro()


def _lee(fn: ast.AsyncFunctionDef) -> tuple[set[str], set[str], dict[str, tuple]]:
    """Arguments the function really reads: required, optional and enums."""
    req: set[str] = set()
    opt: set[str] = set()
    enums: dict[str, tuple] = {}
    for nd in ast.walk(fn):
        if isinstance(nd, ast.Call):
            f = nd.func
            nm = getattr(f, "id", None) or getattr(f, "attr", None)
            if nm == "_require" and len(nd.args) >= 2 and isinstance(nd.args[1], ast.Constant):
                req.add(nd.args[1].value)
            elif nm == "_enum" and len(nd.args) >= 2 and isinstance(nd.args[1], ast.Constant):
                clave = nd.args[1].value
                opt.add(clave)
                try:
                    enums[clave] = tuple(ast.literal_eval(nd.args[2]))
                except Exception:
                    pass
            elif (nm == "get" and isinstance(f, ast.Attribute)
                  and getattr(f.value, "id", "") == "args"
                  and nd.args and isinstance(nd.args[0], ast.Constant)):
                opt.add(nd.args[0].value)
        elif (isinstance(nd, ast.Subscript)
              and getattr(getattr(nd, "value", None), "id", "") == "args"
              and isinstance(getattr(nd, "slice", None), ast.Constant)):
            opt.add(nd.slice.value)
    return req, opt - req, enums


def _escribe(fn: ast.AsyncFunctionDef) -> bool:
    cuerpo = ast.unparse(fn)
    return any(k in cuerpo for k in ("_persist(", "session.add", "await _do", ".commit("))


def _documenta(doc: str, arg: str) -> bool:
    return f"``{arg}``" in doc or f"`{arg}`" in doc


# Plumbing arguments the model NEVER passes: the platform injects them or they are
# internal leftovers. Documenting them would be noise for whoever reads the contract.
_INTERNOS = {"run_id", "idempotency_key", "acting_user", "extra", "_source"}

_CASOS = sorted(REGISTRO.items())
_IDS = [n for n, _ in _CASOS]


@pytest.mark.parametrize("nombre,fname", _CASOS, ids=_IDS)
def test_documenta_todos_los_argumentos_que_lee(nombre, fname):
    """A parameter the code reads and the contract keeps quiet about DOES NOT EXIST for
    the model.

    It hurts especially in the `list_*` tools: `list_alerts` accepted `dedupe_key`,
    `impact` and `urgency` without mentioning them, so the agent could not filter,
    asked for everything and the context filled up with the dump."""
    fn = _FUNCS.get(fname)
    if fn is None:
        pytest.skip(f"{nombre} no se define en este módulo")
    doc = ast.get_docstring(fn) or ""
    req, opt, _ = _lee(fn)
    faltan = sorted(a for a in (req | opt) - _INTERNOS if not _documenta(doc, a))
    assert not faltan, (
        f"`{nombre}` lee estos argumentos y no los documenta: {', '.join(faltan)}. "
        "El docstring ES la interfaz: lo que no está, el modelo no lo puede usar.")


@pytest.mark.parametrize("nombre,fname", _CASOS, ids=_IDS)
def test_los_enums_listan_sus_valores(nombre, fname):
    """"priority (default medium)" without saying which values exist forces guessing,
    and the agent guesses wrong silently."""
    fn = _FUNCS.get(fname)
    if fn is None:
        pytest.skip(f"{nombre} no se define en este módulo")
    doc = ast.get_docstring(fn) or ""
    _, _, enums = _lee(fn)
    mudos = {k: v for k, v in enums.items()
             if not all(str(x) in doc for x in v)}
    assert not mudos, (
        f"`{nombre}` valida estos campos contra una lista cerrada pero no publica "
        f"los valores: " + "; ".join(f"{k} → {v}" for k, v in mudos.items()))


@pytest.mark.parametrize("nombre,fname", _CASOS, ids=_IDS)
def test_lo_que_escribe_dice_que_devuelve(nombre, fname):
    """Without knowing what comes back, the agent cannot verify what it just did nor
    chain the next step — and ends up claiming results it never checked."""
    fn = _FUNCS.get(fname)
    if fn is None:
        pytest.skip(f"{nombre} no se define en este módulo")
    if not _escribe(fn):
        pytest.skip(f"{nombre} no escribe")
    doc = ast.get_docstring(fn) or ""
    dice = any(s in doc for s in ("→", "Returns", "returns", "Devuelve", "devuelve"))
    assert dice, (
        f"`{nombre}` escribe en la base y su contrato no dice qué devuelve. "
        "Añade una línea «Returns: …» o «→ …».")


def test_el_contrato_de_request_approval_dice_QUIEN_ejecuta():
    """The case that gave rise to this file. The concrete regression: if it goes
    missing again, an employee will again park an action nobody will execute."""
    doc = ast.get_docstring(_FUNCS["request_approval"]) or ""
    assert "tool_call" in doc and "automation_reaction" in doc, (
        "el contrato no nombra los dos action_type que la plataforma ejecuta sola")
    for exigido, porque in (
        ("YOU execute", "no dice que con cualquier otra etiqueta ejecuta el agente"),
        ("does not read", "no dice que la plataforma NO interpreta action_payload"),
        ("log_decision", "no dice qué hacer si no sabes llevar a cabo la acción"),
    ):
        assert exigido in doc, porque


def test_el_registro_no_expone_funciones_inexistentes():
    """A stray entry in TOOLS would be a tool the model sees and that blows up when
    called."""
    huerfanas = [n for n, f in REGISTRO.items() if f not in _FUNCS]
    assert not huerfanas, f"registradas pero sin implementación: {huerfanas}"
