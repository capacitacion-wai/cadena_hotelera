"""A CONTROLLED test chat: the code agent tries THIS employee and every write waits for its person (15.8.0).

COPIED VERBATIM by ``wai-agent-app-builder`` into every generated agent-app. INVARIANT (imported by
``auth.py``, ``agent_proxy.py``, ``agent_sessions.py``, ``routes/agent_routes.py``,
``routes/chat_threads_routes.py`` and ``routes/mcp_proxy_routes.py``, all invariant).

WHY. The platform's code agent explores a deployed employee from its chat (`probar_pieza`): it sends the
employee what a person would say, in a TEST conversation, through the platform's signed delegation with
the claim ``prueba_controlada`` (backend ``probar-empleado``). Same rule as every other piece: every write
PAUSES and the person answers in the code agent's chat — never the employee's approvals flow (that runs
later as a separate autonomous run the try cannot see) and never on its own.

HOW, with the layers that already exist (15.7.0 did the same for a read-only invocation):

1. **opencode's own per-session ruleset** — the test session is created with ``ask`` for every tool that
   could change something (connector writes and unclassified tools by opencode key, the tools a connector
   served that the table does not classify, the employee's own tools that change its state) and ``deny``
   for ``task`` (a subagent would run in a session without these rules). opencode asks
   (``permission.asked``) before running one; the sidecar does NOT auto-grant it for this turn
   (``auto_grant=false``) and relays it; the chat turn forwards it as a ``permission`` event with what it
   would change; ``POST /api/agent/chat/permisos/{id}`` answers it (grant, or reject with its reason).
2. **the session's ACL** (``tool_permissions``): the table's denies stay, and every slug-shaped write is
   denied by its bare slug too — that is what reaches the inside of a Composio router EXECUTE, which cannot
   be asked call by call: denied and reported, like the code agent's own tries.
3. **the policy proxy**: the session's grant slot carries a MARKER ``pc:<random>``; ``mcp_proxy_routes``
   never treats it as a write grant and judges with ``mcp_policy.decide_controlled_test``: a write passes
   only if its key is one this session ASKS for (so the person allowed it a moment ago), never parks an
   approval, and the router's EXECUTE is judged on its inner tools.

What is NOT restricted, on purpose (as 15.7.0): opencode's native file/shell tools — they only reach the
agent pod's workspace, which carries no connector credential.
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: The marker a controlled test session's grant slot carries. Never a valid write grant.
MARCA = "pc:"
#: What the agent reads when the proxy refuses a call of a controlled test.
TEXTO_DEL_BLOQUEO = "CONTROLLED TEST"
#: The opencode keys a controlled session ASKS for, per marker token: the proxy lets a write through
#: only when its key is here (the person was asked and said yes). Process memory: after a restart every
#: marker is unknown and the proxy refuses (fail-closed); the session manager mints a new session.
_PREGUNTADAS: dict[str, set[str]] = {}
#: (email, thread) → the test's configuration (marker, ACL map, ruleset). One per test conversation.
_CONFIG: dict[tuple[str, str], dict[str, Any]] = {}
#: The pending asks relayed to the code agent: permission id → {sesion, email, hilo, clave, cuando}.
PENDIENTES: dict[str, dict[str, Any]] = {}
_MAX_PENDIENTES = 500


class NoDisponible(Exception):
    """The agent pod cannot run a controlled test (its sidecar ignores the session ruleset): refuse."""


def _clave_hilo(email: str, hilo: str) -> tuple[str, str]:
    return ((email or "").strip().lower(), str(hilo or ""))


def es_marca(grant: Any) -> bool:
    return str(grant or "").startswith(MARCA)


def activa(grant: Any) -> bool:
    return es_marca(grant) and str(grant)[len(MARCA):] in _PREGUNTADAS


def preguntadas(grant: Any) -> set[str]:
    return _PREGUNTADAS.get(str(grant or "")[len(MARCA):], set()) if es_marca(grant) else set()


def es_hilo_de_prueba(email: str, hilo: str) -> bool:
    return _clave_hilo(email, hilo) in _CONFIG


def abrir_hilo(email: str, hilo: str) -> None:
    """A test conversation was created by a controlled delegation: only there may a test talk."""
    _CONFIG.setdefault(_clave_hilo(email, hilo), {})


def cerrar_hilo(email: str, hilo: str) -> None:
    """The test is over (its conversation archived): its marker is worth nothing from now on."""
    cfg = _CONFIG.pop(_clave_hilo(email, hilo), None) or {}
    _PREGUNTADAS.pop(str(cfg.get("grant") or "")[len(MARCA):], None)
    for pid in [p for p, d in PENDIENTES.items() if d.get("hilo") == hilo and d.get("email") == email]:
        PENDIENTES.pop(pid, None)


# ── The session's rules ────────────────────────────────────────────────────────────────────────

def _slug(nombre: str) -> bool:
    return bool(re.match(r"^[A-Z][A-Z0-9]+_[A-Z0-9_]+$", nombre or ""))


async def reglas(base: Optional[dict[str, str]] = None) -> tuple[dict[str, str], list[dict[str, str]], set[str]]:
    """``(tool_permissions, ruleset, asked keys)`` of a controlled test session. ``base`` = the table's
    denies (they stay). Asked: everything the table does not call a READ allowed outright. Denied: the
    router's write slugs (cannot be asked one by one) and ``task``."""
    from app import mcp_policy as P
    from app.prueba_solo_lectura import HERRAMIENTAS_VISTAS, local_que_lee
    tabla = await P.load_table()
    acl: dict[str, str] = dict(base or {})
    pregunta: set[str] = set()
    conocidos: set[str] = set()

    def _preguntar(clave: str) -> None:
        if acl.get(clave) != P.PERM_DENY:
            pregunta.add(clave)

    for c in (tabla or {}).get("connectors") or []:
        runtime = str(c.get("runtime_name") or c.get("name") or "").strip()
        conocidos |= {P._n(c.get("name")), P._n(runtime)}
        for r in c.get("tools") or []:
            if P._perm(r.get("permission")) == P.PERM_ALLOW and P._n(r.get("io_kind")) == "read":
                continue
            nombre = str(r.get("name") or "")
            if _slug(nombre):
                acl[nombre] = P.PERM_DENY                     # inside a router EXECUTE: denied, reported
                if r.get("key"):
                    acl[str(r["key"])] = P.PERM_DENY
            elif r.get("key"):
                _preguntar(str(r["key"]))
        vistos = HERRAMIENTAS_VISTAS.get(P._n(runtime), set()) | HERRAMIENTAS_VISTAS.get(P._n(c.get("name")), set())
        for t in vistos:
            if P._is_router_meta(t):
                continue                                      # judged by the proxy on the inner tools
            perm, _src, escribe = P.table_permission(tabla, runtime, t)
            if escribe or perm != P.PERM_ALLOW:
                if _slug(t):
                    acl[t] = acl[f"{runtime}_{t}"] = P.PERM_DENY
                else:
                    _preguntar(f"{runtime}_{t}")
    for conector, vistos in HERRAMIENTAS_VISTAS.items():
        if conector in conocidos or conector == "app-tools":
            continue
        for t in vistos:                                      # a connector the table does not name
            if not P._is_router_meta(t):
                _preguntar(f"{conector}_{t}")
    try:
        from app.mcp_server import TOOLS
        for n in TOOLS:
            if not local_que_lee(n):
                _preguntar(f"app-tools_{n}")
    except Exception:  # noqa: BLE001 — never break the turn; the proxy still judges connectors
        logger.warning("prueba controlada: no se pudo leer la lista de herramientas propias", exc_info=True)
    acl["task"] = P.PERM_DENY
    ruleset = [{"permission": k, "pattern": "*", "action": "ask"} for k in sorted(pregunta)]
    ruleset.append({"permission": "task", "pattern": "*", "action": "deny"})
    return acl, ruleset, pregunta


async def config_de(email: str, hilo: str, base: Optional[dict[str, str]] = None) -> dict[str, Any]:
    """The test's configuration for the session manager (``{grant, permisos, reglas}``), minted once per test
    conversation: a session rotated mid-test is minted with the SAME marker."""
    k = _clave_hilo(email, hilo)
    cfg = _CONFIG.get(k)
    if not cfg:
        acl, ruleset, pregunta = await reglas(base)
        token = secrets.token_hex(16)
        _PREGUNTADAS[token] = pregunta
        cfg = {"grant": MARCA + token, "permisos": acl, "reglas": ruleset}
        _CONFIG[k] = cfg
    return cfg


# ── A pending ask ──────────────────────────────────────────────────────────────────────────────

_SECRETO = re.compile(r"(token|secret|password|passwd|api[_-]?key|authorization|credential)", re.I)


def sin_secretos(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: ("***" if _SECRETO.search(str(k)) else sin_secretos(x)) for k, x in v.items()
                if not str(k).startswith("__wai") and k not in ("run_token", "run_id")}
    if isinstance(v, list):
        return [sin_secretos(x) for x in v]
    return v


def legible(evento: dict[str, Any], argumentos: Any) -> dict[str, Any]:
    """The pending write as the person reads it: tool, connector, arguments (no secrets), what it would change."""
    from app.tool_usage import servidor_de
    clave = str(evento.get("permission") or "")
    conector = servidor_de(clave) or ""
    herramienta = clave[len(conector) + 1:] if conector and clave.startswith(conector + "_") else clave
    args = sin_secretos(argumentos if isinstance(argumentos, dict) else {})
    texto = json.dumps(args, ensure_ascii=False, default=str)
    return {"id": str(evento.get("id") or ""), "clave": clave, "herramienta": herramienta, "conector": conector,
            "argumentos": args if len(texto) <= 4000 else {"_recortado": texto[:4000]},
            "que_cambiaria": (f"It would call «{herramienta}»" + (f" of «{conector}»" if conector else "")
                              + (f" with {texto[:300]}" if args else "") + ". It is a write: it changes the real "
                              "system behind it. Nothing has run yet: it waits for the person's answer.")}


def anotar_pendiente(pid: str, *, sesion: str, email: str, hilo: str, clave: str) -> None:
    while len(PENDIENTES) >= _MAX_PENDIENTES:
        PENDIENTES.pop(next(iter(PENDIENTES)))
    PENDIENTES[pid] = {"sesion": sesion, "email": (email or "").strip().lower(), "hilo": hilo, "clave": clave,
                       "cuando": time.time()}


def tomar_pendiente(pid: str, email: str, hilo: str) -> Optional[dict[str, Any]]:
    """The pending ask ``pid`` if it is of THIS person's test conversation ``hilo``; else None (nobody answers
    another session's write)."""
    d = PENDIENTES.get(pid)
    if not d or d["email"] != (email or "").strip().lower() or (hilo and d["hilo"] != hilo):
        return None
    return d
