"""What the PROMPTS order the agent to do has to exist in the tools (7.158.0).

This class of failure has bitten us TWICE, both times silently — which is what makes it
dangerous: the model obeys, the call is ignored or fails, the run ends "fine" and nobody
notices until someone looks at the data months later.

  · 7.153.0 — the maintenance prompt ordered `remember_learning(..., supersedes=[ids])`
    and the tool did NOT read `supersedes`. Measured consequence: 14 paid LLM executions and
    `superseded_by = 0` in the database. Consolidation was a no-op by construction.
  · 7.158.0 — the same prompt ordered `forget_learning(id, reason=...)` and the tool required
    `learning_id` and ignored `reason`: every attempt to retire an obsolete learning
    failed, and the reason was lost.

The test cross-checks both halves statically: for every call with parameters that appears
in a prompt, it checks that the tool exists and that the parameter is read. It needs to run
nothing and spends no token.
"""
from __future__ import annotations

import pathlib
import re

import pytest

_APP = pathlib.Path(__file__).resolve().parent.parent / "app"

# Files that TALK to the model. If you add another, add it here.
_PROMPT_FILES = (
    "autonomous/executor.py", "chat_prompt.py", "agent_memory_prompt.py",
    "memory_prompt.py", "memory_contract.py", "prompts_defaults.py",
)

# Parameters that do NOT travel in `arguments`: the server sets them.
_NOT_ARGUMENTS = {"run_id"}


def _tools() -> dict[str, str]:
    """Tool name → body of its handler."""
    src = (_APP / "mcp_server.py").read_text(encoding="utf-8")
    return {
        m.group(1): m.group(2)
        for m in re.finditer(
            r"\nasync def ([a-z_]+)\(call: ToolCall\):(.*?)(?=\nasync def |\nTOOLS =|\Z)",
            src, re.S)
    }


def _registered() -> set[str]:
    src = (_APP / "mcp_server.py").read_text(encoding="utf-8")
    return set(re.findall(r'^\s*"([a-z_]+)":\s+[a-z_]+,', src, re.M))


def _prompt_calls():
    """(file, tool, parameter) for every `tool(param=…)` that appears in a prompt."""
    registradas = _registered()
    for rel in _PROMPT_FILES:
        p = _APP / rel
        if not p.exists():
            continue
        txt = p.read_text(encoding="utf-8")
        for m in re.finditer(r"`?([a-z_]{4,})\(([^)`\"']{0,200})\)`?", txt):
            tool, args = m.group(1), m.group(2)
            if tool not in registradas:
                continue
            for pm in re.finditer(r"(\w+)\s*=", args):
                param = pm.group(1)
                if param not in _NOT_ARGUMENTS:
                    yield rel, tool, param


def test_todo_parametro_que_el_prompt_ordena_lo_lee_su_tool():
    tools = _tools()
    huerfanos = sorted({
        (rel, tool, param) for rel, tool, param in _prompt_calls()
        if param not in tools.get(tool, "")
    })
    assert not huerfanos, (
        "Un prompt le ordena al agente pasar parámetros que la tool NO lee. El modelo "
        "obedecerá, la llamada se ignorará o fallará, y la run terminará en verde sin "
        "hacer su trabajo — que es exactamente cómo la consolidación estuvo 14 rondas "
        "sin fusionar nada:\n  " +
        "\n  ".join(f"{r}: {t}(… {p}=…)" for r, t, p in huerfanos)
    )


@pytest.mark.parametrize("tool,param", [
    ("remember_learning", "supersedes"),   # 7.153.0
    ("forget_learning", "reason"),         # 7.158.0
    ("forget_learning", "learning_id"),
])
def test_los_dos_que_ya_fallaron_siguen_leyendose(tool, param):
    """Explicit regression: they must not silently break again."""
    assert param in _tools().get(tool, ""), f"{tool} dejó de leer {param}"


def test_forget_learning_acepta_el_alias_que_usa_el_prompt():
    """The prompt says `forget_learning(id, ...)` and the tool requires `learning_id`. The alias
    is accepted instead of rewriting the prompt: `id` is what comes naturally to the model, and
    breaking it again would only be a matter of time."""
    cuerpo = _tools().get("forget_learning", "")
    assert '"learning_id" not in args' in cuerpo and 'args.get("id")' in cuerpo
