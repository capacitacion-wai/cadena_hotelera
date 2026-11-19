"""A READ-ONLY invocation: the employee does the work of a capability and changes NOTHING (15.7.0).

COPIED VERBATIM by ``wai-agent-app-builder`` into every generated agent-app. INVARIANT (imported by
``routes/agent_routes.py``, ``routes/mcp_proxy_routes.py`` and ``agent_sessions.py``, all invariant).

WHY. In the live view of an application the person presses «Probar con el empleado real» on a call
the app made: the platform calls THIS employee with those arguments so the person sees a real answer
instead of a simulated one. Real data, real cost — and no real effects: a preview must never create a
lead, send an email or park an approval in someone's queue. The platform asks for it in the SIGNED
delegation (`solo_lectura: true`, `auth._persona_de_una_aplicacion`): the application that is being
built cannot forge it nor remove it.

HOW, in two layers that already existed (nothing parallel is invented):

1. **The session's ACL** — the opencode session of the read-only run is created with a permission map
   (`tool_permissions`, the same field `agent_sessions.resolve_tool_permissions` fills with the
   table's denies; the engine's test pods do the same with `permisos_para_pruebas`). The sidecar's
   plugin enforces it on EVERY tool call of that session whatever the model does. It denies every
   connector tool the platform's table does not call a READ allowed outright (writes, unclassified
   tools, `ask`), the tools the proxy saw a connector serve that the table does not classify, and the
   employee's own tools that change its state (alerts, approvals, memory, automations, users…).
2. **The policy proxy** — the router's EXECUTE meta-tools carry the real tool in their arguments, and
   only the proxy unwraps them reliably. The read-only run's session carries a MARKER in its run-scoped
   grant slot (`write_grant`, which the plugin injects into connector calls as `__wai_write_grant`); the
   proxy recognises it, never treats it as a write grant, and judges the inner tools with
   `mcp_policy.decide_read_only_test`: a write (or an unclassified tool) is refused with a message
   that tells the agent to describe what it would have done instead.

What is NOT restricted, on purpose: opencode's native file/shell tools. They only reach the agent
pod's own workspace, which carries no connector credential: every effect outside the employee goes
through a connector or through the employee's own tools, and those are what this closes.

The refused calls are collected from the turn's own events (`es_bloqueo` / `bloqueada`) and returned
to the caller as `escrituras_bloqueadas`, so the person sees what the employee WOULD have changed.
"""
from __future__ import annotations

import json
import logging
import secrets
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: The marker that a session's grant slot carries in a read-only run. Never a valid write grant.
MARCA = "ro:"
#: How long a read-only test stays open: the longest a turn can last (`execution_limits`, 60 min) plus
#: its retry margin. After that the marker is worth nothing and the proxy refuses (fail-closed).
_TTL_S = 65 * 60.0
#: What the agent reads when a call is refused in a read-only run (proxy). The plugin's own text is
#: «Permission denied: …»; both are recognised by `es_bloqueo`.
TEXTO_DEL_BLOQUEO = "READ-ONLY TEST"

_ACTIVAS: dict[str, float] = {}


def abrir() -> str:
    """Opens a read-only test and returns the grant-slot value of its session (`ro:<random>`)."""
    ahora = time.monotonic()
    for t, hasta in list(_ACTIVAS.items()):
        if hasta <= ahora:
            _ACTIVAS.pop(t, None)
    token = secrets.token_hex(16)
    _ACTIVAS[token] = ahora + _TTL_S
    return MARCA + token


def cerrar(grant: str) -> None:
    """Closes it: from now on its session's connector calls are refused (the test is over)."""
    _ACTIVAS.pop(str(grant or "")[len(MARCA):], None)


def es_marca(grant: Any) -> bool:
    return str(grant or "").startswith(MARCA)


def activa(grant: Any) -> bool:
    if not es_marca(grant):
        return False
    hasta = _ACTIVAS.get(str(grant)[len(MARCA):])
    return hasta is not None and hasta > time.monotonic()


# ── What each connector serves (seen by the proxy) ─────────────────────────────────────────────

#: connector (lower) → the tool names its upstream served in a `tools/list`. Filled by the proxy
#: (every relayed `tools/list` and the boot readiness probe), so a tool the catalogue does not name
#: can still be denied by its opencode key in a read-only session.
HERRAMIENTAS_VISTAS: dict[str, set[str]] = {}


def anotar_herramientas(conector: str, tools: Any) -> None:
    nombres = {str(t.get("name")) for t in (tools or []) if isinstance(t, dict) and t.get("name")}
    if nombres:
        HERRAMIENTAS_VISTAS.setdefault((conector or "").strip().lower(), set()).update(nombres)


# ── The session's permission map ───────────────────────────────────────────────────────────────

_LOCAL = "app-tools"
_LEEN = ("list_", "search_", "get_", "describe_", "find_")


def local_que_lee(nombre: str) -> bool:
    """Whether one of the employee's OWN tools only reads. A closed rule, not a guess: the read verbs
    of the toolbox (`list_*`, `search_*`, `get_*`, `describe_*`, `find_*`, `local.read`) plus the
    operations `mcp_operations.READ_ONLY` declares. Everything else changes something."""
    n = (nombre or "").strip()
    if n == "local.read" or n.startswith(_LEEN):
        return True
    try:
        from app.mcp_operations import READ_ONLY
        return n in READ_ONLY
    except Exception:  # noqa: BLE001 — without the list, the prefix rule alone (it only narrows)
        return False


def _slug(nombre: str) -> bool:
    import re
    return bool(re.match(r"^[A-Z][A-Z0-9]+_[A-Z0-9_]+$", nombre or ""))


async def permisos(base: Optional[dict[str, str]] = None) -> dict[str, str]:
    """The `tool_permissions` of a read-only session: `base` (the table's denies) plus a `deny` for
    everything that could change something. Only `deny`: a read stays as the table says."""
    from app import mcp_policy as P
    tabla = await P.load_table()
    fuera: dict[str, str] = dict(base or {})
    conocidos: set[str] = set()
    for c in (tabla or {}).get("connectors") or []:
        runtime = str(c.get("runtime_name") or c.get("name") or "").strip()
        conocidos |= {P._n(c.get("name")), P._n(runtime)}
        for r in c.get("tools") or []:
            if P._perm(r.get("permission")) == P.PERM_ALLOW and P._n(r.get("io_kind")) == "read":
                continue
            if r.get("key"):
                fuera[str(r["key"])] = P.PERM_DENY
            # The bare slug too, so the plugin also refuses it inside a router EXECUTE. Only when it IS
            # slug-shaped: a bare `write` would also deny opencode's own native tool.
            if _slug(str(r.get("name") or "")):
                fuera[str(r["name"])] = P.PERM_DENY
        vistos = HERRAMIENTAS_VISTAS.get(P._n(runtime), set()) | HERRAMIENTAS_VISTAS.get(P._n(c.get("name")), set())
        for t in vistos:
            if P._is_router_meta(t):
                continue        # judged by the proxy on the inner tool (or allowed: pure search)
            perm, _src, escribe = P.table_permission(tabla, runtime, t)
            if escribe or perm != P.PERM_ALLOW:
                fuera[f"{runtime}_{t}"] = P.PERM_DENY
    for conector, vistos in HERRAMIENTAS_VISTAS.items():
        if conector in conocidos or conector == _LOCAL:
            continue
        for t in vistos:                 # a connector the table does not name: nothing is a known read
            if not P._is_router_meta(t):
                fuera[f"{conector}_{t}"] = P.PERM_DENY
    try:
        from app.mcp_server import TOOLS
        for n in TOOLS:
            if not local_que_lee(n):
                fuera[f"{_LOCAL}_{n}"] = P.PERM_DENY
    except Exception:  # noqa: BLE001 — never break the turn; the proxy still judges connectors
        logger.warning("solo lectura: no se pudo leer la lista de herramientas propias", exc_info=True)
    return fuera


# ── What was refused (from the turn's events) ──────────────────────────────────────────────────

def es_bloqueo(resultado: Any) -> bool:
    """Whether a tool's result is a refusal of this read-only run (proxy or session ACL)."""
    texto = resultado if isinstance(resultado, str) else json.dumps(resultado, ensure_ascii=False, default=str)
    return TEXTO_DEL_BLOQUEO in (texto or "") or "Permission denied" in (texto or "")


def bloqueada(nombre: str, argumentos: Any) -> dict[str, Any]:
    """One refused call as the caller reads it: the tool, its connector, the real tools a router EXECUTE
    carried and its arguments (trimmed: enough to recognise the call, never an archive)."""
    from app import mcp_policy as P
    from app.tool_usage import servidor_de
    fuera: dict[str, Any] = {"herramienta": nombre, "conector": servidor_de(nombre)}
    dentro = P.unwrap_execute_slugs(nombre, argumentos)
    if dentro:
        fuera["herramientas"] = dentro
    if isinstance(argumentos, dict):
        limpio = {k: v for k, v in argumentos.items() if not str(k).startswith("__wai")
                  and k not in ("run_token", "run_id")}
        if len(json.dumps(limpio, ensure_ascii=False, default=str)) <= 2000:
            fuera["argumentos"] = limpio
    return fuera
