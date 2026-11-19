"""No tool may be born OPEN by oversight.

The agent reaches the tools with a SERVICE token: that channel carries nobody's
identity. While the tools only materialized business data it did not matter;
since 7.146.0 there are tools that see the money, the technical audit, the
settings and the approvals, and there "not knowing who is asking" is a leak: a
`member` would ask for the spend over chat and get it.

The defense is not remembering to check the role inside each handler (that is how
you end up at exactly the tool that forgot): it is that the permission is declared
in ONE place, the dispatch enforces it, and this test breaks the build when the
declaration and the registry do not match. It is static on purpose: the failure mode
(a new tool with no gate) is not caught by any smoke test, because the tool works
perfectly, just for someone who should not have it.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "app"


def _tools_and_roles():
    from app.mcp_actor import TOOL_MIN_ROLE
    from app.mcp_server import TOOLS
    return TOOLS, TOOL_MIN_ROLE


def test_toda_herramienta_declara_su_rol_minimo() -> None:
    tools, roles = _tools_and_roles()
    sin_declarar = sorted(set(tools) - set(roles))
    assert not sin_declarar, (
        "Estas herramientas están registradas pero NO declaran rol mínimo en "
        "mcp_actor.TOOL_MIN_ROLE, así que el dispatch las deniega (fail-closed) y "
        "no funcionarán. Declara quién puede usarlas:\n  " + "\n  ".join(sin_declarar)
    )


def test_no_se_declara_permiso_de_una_herramienta_que_ya_no_existe() -> None:
    tools, roles = _tools_and_roles()
    fantasmas = sorted(set(roles) - set(tools))
    assert not fantasmas, (
        "Estas herramientas tienen permiso declarado pero ya no están registradas: "
        "la tabla miente sobre lo que el empleado puede hacer.\n  "
        + "\n  ".join(fantasmas)
    )


def test_los_niveles_son_los_del_vocabulario() -> None:
    from app.mcp_actor import LEVELS
    _, roles = _tools_and_roles()
    raros = sorted({v for v in roles.values() if v not in LEVELS})
    assert not raros, f"niveles inventados: {raros} (válidos: {list(LEVELS)})"


def test_el_dinero_y_el_gobierno_exigen_administrador() -> None:
    """The ones that CANNOT be relaxed without someone noticing.

    They are named one by one: if tomorrow someone lowers `get_consumption` to `agent`
    "so the employee can report it", this test stops it. It is the list of what the
    user explicitly asked to keep closed.
    """
    _, roles = _tools_and_roles()
    for tool in ("get_consumption", "list_runs", "get_run", "list_events",
                 "get_settings", "set_setting", "unsuppress_signal",
                 "list_channels", "get_automation_history",
                 "reset_automation_memory", "create_automation",
                 "update_automation", "set_automation_enabled", "run_automation"):
        assert roles.get(tool) == "admin", (
            f"{tool} debe exigir administrador: ve o cambia el gobierno del empleado"
        )
    for tool in ("approve_action", "reject_action"):
        assert roles.get(tool) == "approver", (
            f"{tool} decide una acción real: administrador o aprobador designado"
        )


def test_el_dispatch_aplica_la_puerta_antes_de_llamar_al_handler() -> None:
    """The gate lives in the DISPATCH, not in the handlers.

    If it were inside each handler, the new tool that forgets to check it would be
    left open and nobody would notice. It is verified on the source code because it
    is a structural property: the order of two lines.
    """
    src = (APP / "mcp_server.py").read_text(encoding="utf-8")
    for surface in ('@router.post("/invoke"', 'if method == "tools/call"'):
        # Wide window: in JSON-RPC resolving the run takes quite a lot before
        # reaching the handler, and cutting too short would give a false red.
        chunk = src.split(surface, 1)[1][:12000]
        assert "gate_error" in chunk, (
            f"la superficie {surface} llama a herramientas sin pasar por gate_error"
        )
        gate = chunk.index("gate_error")
        handler = chunk.index("await handler(call)")
        assert gate < handler, (
            f"en {surface} el handler se ejecuta ANTES de comprobar el permiso"
        )


def test_la_identidad_no_se_la_pregunta_al_modelo() -> None:
    """The actor is DERIVED from the run on the server; an argument would be forgeable."""
    src = (APP / "mcp_actor.py").read_text(encoding="utf-8")
    assert "request_meta_json" in src and "memory_user" in src, (
        "el actor debe salir de la fila del run (request_meta_json.memory_user)"
    )
    resolver = src.split("async def resolve_actor", 1)[1].split("\n\n\n", 1)[0]
    assert "arguments" not in resolver, (
        "resolve_actor no puede leer los argumentos de la llamada: el modelo los "
        "controla, y entonces cualquiera se declararía administrador"
    )


def test_un_run_no_de_chat_nunca_es_una_persona() -> None:
    """An autonomous or channel run does not prove anyone is present."""
    from app.mcp_actor import Actor
    assert Actor(email="jefe@acme.com", surface="autonomous", role="admin").is_admin is False
    assert Actor(email="jefe@acme.com", surface=None, role="admin").is_human is False
    assert Actor(email=None, surface="chat", role="admin").is_human is False
    assert Actor(email="jefe@acme.com", surface="chat", role="admin").is_admin is True


def test_un_member_no_puede_aprobar_ni_ver_el_dinero() -> None:
    from app.mcp_actor import Actor
    member = Actor(email="ana@acme.com", surface="chat", role="member")
    assert member.is_admin is False
    assert member.may_approve is False
    designado = Actor(email="ana@acme.com", surface="chat", role="member",
                      can_approve=True)
    assert designado.may_approve is True
    assert designado.is_admin is False, (
        "poder aprobar no puede convertir a alguien en administrador: era justo el "
        "acoplamiento que 7.145.0 rompió"
    )


@pytest.mark.asyncio
async def test_lo_no_declarado_se_deniega() -> None:
    """Fail-closed: an undeclared tool does not pass, even if it is our own bug."""
    from app.mcp_actor import Actor, gate_error
    admin = Actor(email="jefe@acme.com", surface="chat", role="admin")
    denied = await gate_error("herramienta_que_nadie_declaro", admin)
    assert denied is not None and denied.get("denied") is True


@pytest.mark.asyncio
async def test_el_rechazo_explica_a_quien_pedirlo() -> None:
    """A bare "you do not have permission" leaves the person at a dead end."""
    from app.mcp_actor import Actor, gate_error
    member = Actor(email="ana@acme.com", surface="chat", role="member")
    denied = await gate_error("get_consumption", member)
    assert denied is not None
    assert "administrator" in denied["error"].lower()
    assert "who_can" in denied, "el rechazo debe decir a quién pedírselo"


@pytest.mark.asyncio
async def test_el_trabajo_del_empleado_sigue_funcionando_solo() -> None:
    """Regression: business tools CANNOT require a person to be present.

    If an autonomous run could no longer register an alert, the employee would go
    silent at night: the unacceptable price of tightening the gate.
    """
    from app.mcp_actor import Actor, gate_error
    nadie = Actor(surface="autonomous")
    for tool in ("upsert_alert", "register_recommendation", "log_decision",
                 "remember_learning", "search_learnings", "list_alerts",
                 "schedule_task", "request_approval", "suppress_signal"):
        assert await gate_error(tool, nadie) is None, (
            f"{tool} es parte de su trabajo: debe funcionar sin nadie delante"
        )


def test_las_puertas_de_la_UI_siguen_cerradas() -> None:
    """The holes 7.146.0 closed in the endpoint itself (not only in the chat).

    Chat/UI parity would be false comfort if the UI let through the front door what
    the chat blocks at the back: the agent reuses these very same handlers.
    """
    src = (APP / "routes" / "manifest_routes.py").read_text(encoding="utf-8")
    gov = src.split("_GOVERNANCE_KEYS = (", 1)[1].split(")", 1)[0]
    for key in ("agent_model", "agent_language", "business_timezone",
                "self_schedule", "max_open_tasks", "max_concurrent_runs",
                "max_autonomous_concurrent", "autonomy_level", "triggers_active"):
        assert key in gov, f"{key} cambia el comportamiento del empleado: es de admin"

    for signature, why in (
        ("async def set_extra_skills", "engancharle skills cambia lo que sabe hacer"),
        ("async def reset_persona", "borrar su identidad es lo mismo que editarla"),
        ("async def fire_watcher", "disparar un watcher gasta créditos reales"),
    ):
        head = src.split(signature, 1)[1][:300]
        assert "require_admin" in head, f"{signature}: {why}"


def test_ninguna_herramienta_de_paridad_reimplementa_la_regla_de_la_UI() -> None:
    """The new tools REUSE the screen's handler.

    That is what keeps the chat and the UI from drifting apart: two implementations
    of the same rule diverge at the first fix, and the one left stale is always the
    one nobody looks at.
    """
    src = (APP / "mcp_operations.py").read_text(encoding="utf-8")
    for handler in ("activity_routes import get_activity",
                    "approvals_routes import approve",
                    "approvals_routes import reject",
                    "manifest_routes import patch_settings",
                    "channels_routes import list_channels",
                    "suppressions_routes import delete_rule"):
        assert handler in src, f"falta la reutilización de {handler}"
    # And it cannot check permissions on its own: the gate already did that, and a
    # second check here would be the one that drifts out of sync.
    assert not re.search(r"def \w+\(call[^)]*\):(?:[^\n]*\n){0,20}?\s+if .*is_admin",
                         src), "el permiso se decide en la puerta, no en el handler"
