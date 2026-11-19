# src/backend/app/self_knowledge.py
"""What the employee knows ABOUT ITSELF — its own application, screen by screen.

It is odd to give out an employee with chat that cannot explain the application it lives
in: you ask it "where do I see what you have spent?" and it makes it up, or says it cannot.
This module is the source of that knowledge, and it is built with one obsession: **it
cannot get out of sync when functionality is added.**

A hand-written manual goes stale the day someone adds a screen and does not remember to
touch it. So the knowledge is split into three layers according to how automatic it can be:

1. **DERIVED from the code, impossible to get out of sync** — the API route table with the
   permission gate that protects them (``route_gates``: read from the FastAPI app itself at
   runtime, so it IS the code) and this employee's DOMAIN screens (they come from the
   manifest, like the menu).
2. **DECLARED here and WATCHED** — the skill's fixed screens. This cannot be derived from
   the TSX without a fragile parser, so it is declared… and ``tests/test_self_knowledge.py``
   cross-checks this table against reality: against the routes of ``App.tsx``, against the
   menu entries of ``Layout.tsx`` with their admin mark, and against the i18n keys in ``es``
   and ``en``. You add a screen and do not declare it: RED. You declare one that no longer
   exists: RED. That is what keeps it in sync without depending on anyone remembering.
3. **DERIVED from the tool registry** — what can be done through chat and with which
   permission comes from ``mcp_actor.TOOL_MIN_ROLE``, which is already mandatory.

How the agent consumes it: NOT by dumping this into the prompt (the priming is already
8-12 kB and the sidecar prunes by compaction). Only a compact INDEX goes into the prompt
(``index_block``) so it knows what exists, and the detail is requested on demand with the
``describe_app`` tool. Each answer is filtered by who is asking: a ``member`` is not
offered the consumption, they are told whom to ask.

The texts are for the agent (English). The screen ``title`` and ``keywords`` stay as the
person sees and types them (Spanish UI labels and search words).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

AREA_EMPLOYEE = "employee"
AREA_ADMIN = "admin"


@dataclass(frozen=True)
class Screen:
    """A screen of the employee's application, as a person sees it."""

    route: str
    title: str
    purpose: str
    area: str = AREA_EMPLOYEE
    admin_only: bool = False
    # i18n key of its menu entry. ``None`` = it is not in the menu (it is reached through a
    # link, like the detail records). The test checks it against Layout.
    nav_key: Optional[str] = None
    # Detail view of another screen (so as not to repeat the explanation).
    detail_of: Optional[str] = None
    # What a person can DO there. No endpoint jargon.
    actions: tuple[str, ...] = ()
    # Tools that do the same through chat (parity). Empty = UI only.
    tools: tuple[str, ...] = ()
    # What can ONLY be done there, and why. It is as important as the rest: it stops the
    # employee from promising something it has no hands to do.
    ui_only: tuple[str, ...] = ()
    # Synonyms a person would use to ask about this screen.
    keywords: tuple[str, ...] = ()


# ── Layer 2: the skill's fixed screens (declared + watched) ──────────
#
# When adding a screen: add it here (EXACT route of App.tsx, and if it goes in the menu,
# its i18n key exactly as Layout.tsx puts it). Otherwise, the test catches it.
SCREENS: tuple[Screen, ...] = (
    Screen(
        route="/login",
        title="Entrar",
        purpose="The door: you get in with the project's WAI user.",
        keywords=("login", "entrar", "acceso", "contraseña", "sesion"),
        ui_only=("Signing in and changing the password: the employee does not issue "
                 "credentials, the identity is given by the WAI platform.",),
    ),
    Screen(
        route="/",
        title="Bienvenida",
        purpose="Who the employee is, what it does for you and what you can ask it.",
        nav_key="nav.welcome",
        keywords=("bienvenida", "inicio", "presentacion", "quien eres", "portada"),
        actions=("Read its presentation and examples of what you can ask it.",),
        tools=("describe_app",),
    ),
    Screen(
        route="/dashboard",
        title="Panel",
        purpose="The business picture at a glance: indicators, trends and what "
                "needs attention.",
        nav_key="nav.dashboard",
        keywords=("panel", "dashboard", "kpi", "indicadores", "grafico", "resumen"),
        actions=("See the business indicators and jump to what is open.",),
        tools=("get_activity", "get_health"),
    ),
    Screen(
        route="/agent",
        title="Chat",
        purpose="Talk to the employee: ask it for work, ask it about its data "
                "or about how it is used.",
        nav_key="nav.agent",
        keywords=("chat", "hablar", "preguntar", "conversacion", "asistente"),
        actions=("Write to it, attach files and see what it is doing.",
                 "Ask it to remember something about you or to learn something about the environment."),
        tools=("describe_app", "remember_fact", "remember_learning"),
    ),
    Screen(
        route="/manual",
        title="Manual",
        purpose="The guide: how a WAI employee works and what is specific to this one.",
        nav_key="nav.manual",
        keywords=("manual", "ayuda", "guia", "documentacion", "como funciona"),
        actions=("Open the general manual and this employee's.",),
        tools=("describe_app",),
    ),
    Screen(
        route="/trabajo-autonomo",
        title="Trabajo autónomo",
        purpose="The standing orders: \"when\" something happens, \"only if\" a condition "
                "is met, \"then\" the employee acts.",
        nav_key="nav.autonomousWork",
        keywords=("automatizacion", "automatizaciones", "trabajo autonomo", "orden",
                  "cuando", "solo si", "cooldown", "anti-repeticion", "embudo"),
        actions=("See the orders and whether they are active (anyone).",
                 "Create, edit, enable, pause, test or run one (administrator).",
                 "See its activity and governance: how many times it checked, how many "
                 "it fired, what it generated and what it remembers so as not to repeat "
                 "itself (administrator).",
                 "Forget what it remembers about an item so it alerts again "
                 "(administrator)."),
        tools=("list_automations", "create_automation", "update_automation",
               "set_automation_enabled", "test_automation", "run_automation",
               "get_automation_history", "reset_automation_memory"),
    ),
    Screen(
        route="/calendar",
        title="Calendario",
        purpose="The shared agenda: what the employee has to do on a date.",
        nav_key="nav.calendar",
        keywords=("calendario", "agenda", "fecha", "recordatorio", "seguimiento"),
        actions=("See, create, edit or delete agenda entries.",
                 "Mark one as done."),
        tools=("list_scheduled_items", "schedule_task", "complete_scheduled_item"),
    ),
    Screen(
        route="/encargos",
        title="Encargos",
        purpose="The work you explicitly delegate to it and its result.",
        nav_key="nav.encargos",
        keywords=("encargo", "encargos", "delegar", "tarea", "pedido"),
        actions=("See the tasks, their status and what it delivered.",
                 "Delegate a new task to it."),
        tools=("list_scheduled_items", "assign_task_to_user"),
    ),
    Screen(
        route="/encargos/:id",
        title="Detalle de un encargo",
        purpose="Everything about a task: what you asked, what it did and what it delivered.",
        detail_of="/encargos",
        keywords=("encargo", "detalle", "entregable"),
    ),
    Screen(
        route="/approvals",
        title="Aprobaciones",
        purpose="The actions the employee PARKS waiting for a yes or a no.",
        nav_key="nav.approvals",
        keywords=("aprobacion", "aprobaciones", "aprobar", "rechazar", "permiso",
                  "autorizar", "pendiente"),
        actions=("See what is waiting for a decision (anyone).",
                 "Approve or reject (administrator or whoever they designate).",
                 "Delete the record of an approval (administrator)."),
        tools=("list_approvals", "approve_action", "reject_action", "list_approvers"),
    ),
    Screen(
        route="/approvals/:id",
        title="Detalle de una aprobación",
        purpose="What exactly asks for permission, with what risk and who decided it.",
        detail_of="/approvals",
        keywords=("aprobacion", "detalle", "riesgo"),
    ),
    Screen(
        route="/actividad",
        title="Actividad",
        purpose="What the employee has done, told in business language.",
        nav_key="nav.activity",
        keywords=("actividad", "que has hecho", "historial", "feed", "ultimo"),
        actions=("See the trail of what it did and why.",),
        tools=("get_activity",),
    ),
    Screen(
        route="/domain/:slug",
        title="Pantallas de datos y de resultados",
        purpose="Each type of data it queries and each thing it generates has its "
                "screen; the exact names depend on this employee.",
        keywords=("entidad", "listado", "alertas", "incidencias", "recomendaciones",
                  "decisiones", "datos", "tabla", "origen", "filtro"),
        actions=("Filter, sort and open a record.",
                 "See which autonomous work created each row and filter by it.",
                 "Close or decide what is open."),
        tools=("describe_entities", "local.read", "list_alerts",
               "list_recommendations", "list_decisions", "resolve_alert"),
    ),
    Screen(
        route="/domain/:slug/:id",
        title="Ficha de un registro",
        purpose="A record with its history, its comments and its lifecycle.",
        detail_of="/domain/:slug",
        keywords=("ficha", "detalle", "comentario", "registro"),
    ),
    Screen(
        route="/identity",
        title="Identidad",
        purpose="Who the employee is: its name, its tone, its business time zone.",
        area=AREA_ADMIN,
        admin_only=True,
        nav_key="nav.identity",
        keywords=("identidad", "persona", "nombre", "tono", "zona horaria", "tema"),
        actions=("Edit its persona (name, presentation, greeting).",
                 "Set the business time zone and the default theme."),
        tools=("get_settings", "set_setting"),
    ),
    Screen(
        route="/users",
        title="Usuarios",
        purpose="Who can use the employee, who administers it and who can "
                "decide its actions.",
        area=AREA_ADMIN,
        admin_only=True,
        nav_key="nav.users",
        keywords=("usuario", "usuarios", "personas", "alta", "rol", "admin",
                  "aprobador", "contacto", "directorio"),
        actions=("Register project people IN BULK.",
                 "Register an external contact (channels only, cannot sign in).",
                 "Change the role, mark who can approve and set a credit cap "
                 "per person.",
                 "Deactivate or delete someone."),
        tools=("list_users", "find_user", "create_user", "list_approvers"),
        ui_only=("The BULK registration from the project: reading the project's user "
                 "list from WAI needs the administrator's session, and the employee "
                 "does not have it. It is done in Usuarios → «Añadir del "
                 "proyecto».",),
    ),
    Screen(
        route="/operations",
        title="Configuración",
        purpose="The engine room: status, knowledge, connectors, sessions, "
                "files, database and the two memories.",
        area=AREA_ADMIN,
        admin_only=True,
        nav_key="nav.operations",
        keywords=("configuracion", "operaciones", "conectores", "mcp", "sesiones",
                  "workspace", "ficheros", "base de datos", "memoria", "aprendizaje",
                  "conocimiento", "modelo", "idioma", "skills"),
        actions=("See its health and its figures.",
                 "Manage the business knowledge and the document library.",
                 "Connect external connectors and set their permissions.",
                 "See and delete sessions, workspace files and database usage.",
                 "Review each person's memory and what the employee has "
                 "learned about the environment, and accept or reject what it proposes.",
                 "Change its model, its language and the skills it carries."),
        tools=("get_settings", "set_setting", "search_knowledge", "set_knowledge",
               "list_knowledge_docs", "search_learnings", "remember_learning",
               "forget_learning", "list_memory", "list_suppressions",
               "unsuppress_signal"),
        ui_only=("Connecting a connector through OAuth and uploading files: they are "
                 "wizards with a browser and secrets, not something typed in a chat.",),
    ),
    Screen(
        route="/companeros",
        title="Compañeros",
        purpose="Which other employees it works with and who works with it, and with "
                "which permissions each query travels.",
        area=AREA_ADMIN,
        admin_only=True,
        nav_key="nav.colleagues",
        keywords=("companeros", "compañeros", "colegas", "colaboracion", "red",
                  "quien me habla", "con quien hablo", "delegar", "consultar"),
        actions=("See whom it queries and who queries it.",
                 "Accept, deny or pause a colleague's request.",
                 "Follow the trail of each query: what was asked, what was "
                 "answered and why it was cut off if it was."),
        ui_only=("Accepting or denying a colleague: it is a governance decision "
                 "and it is taken here, not through chat.",
                 "Through chat the colleague tools carry each one's name "
                 "(`consultar_a_...`), so they appear and disappear with the "
                 "accepted collaborations.",),
    ),

    Screen(
        route="/capacidades",
        title="Comportamiento",
        purpose="How much initiative it has: its autonomy level, its caps and what "
                "it can write to the outside.",
        area=AREA_ADMIN,
        admin_only=True,
        nav_key="nav.capabilities",
        keywords=("comportamiento", "capacidades", "autonomia", "iniciativa",
                  "concurrencia", "tiempo maximo", "politica de escritura", "triggers",
                  "permisos"),
        actions=("Raise or lower its autonomy and start or stop autonomous work.",
                 "Adjust concurrency, the maximum working time of a chat reply and of an "
                 "autonomous task, open tasks and self-scheduling.",
                 "Set the write policy of each connector.",
                 "Ask it to explain in its own words what it can do today."),
        tools=("get_settings", "set_setting"),
    ),
    Screen(
        route="/channels",
        title="Canales",
        purpose="Through where people from outside talk to it: WhatsApp, Telegram, email, Slack.",
        area=AREA_ADMIN,
        admin_only=True,
        nav_key="nav.channels",
        keywords=("canal", "canales", "whatsapp", "telegram", "correo", "email",
                  "slack", "mensaje"),
        actions=("See the channels, their status and their messages.",
                 "Create, enable, disable, test or delete a channel."),
        tools=("list_channels",),
        ui_only=("Creating a channel and reauthorizing it: it is a wizard with OAuth and "
                 "credentials, it is done on the screen.",),
    ),
    Screen(
        route="/channels/:id",
        title="Detalle de un canal",
        purpose="A channel: its configuration, its messages and its authorization.",
        area=AREA_ADMIN,
        admin_only=True,
        detail_of="/channels",
        keywords=("canal", "detalle", "mensajes"),
    ),
    Screen(
        route="/runs",
        title="Ejecuciones",
        purpose="Every time the employee has worked: what it did, how long it took and "
                "how much it cost.",
        area=AREA_EMPLOYEE,
        admin_only=True,
        nav_key="nav.runs",
        keywords=("ejecucion", "ejecuciones", "run", "runs", "coste por run",
                  "tokens", "auditoria", "cancelar"),
        actions=("See the runs, filter them and cancel one in progress.",),
        tools=("list_runs", "get_run"),
    ),
    Screen(
        route="/runs/:id",
        title="Detalle de una ejecución",
        purpose="The X-ray of a run: steps, tools, cost and the prompt sent "
                "(readable formatted or as text, and maximizable).",
        area=AREA_EMPLOYEE,
        admin_only=True,
        detail_of="/runs",
        keywords=("run", "detalle", "herramientas", "pasos", "prompt"),
        actions=("Read the prompt formatted or as raw text and maximize it.",),
        tools=("get_run",),
    ),
    Screen(
        route="/events",
        title="Eventos",
        purpose="The queue of what reaches it and wakes up its work.",
        area=AREA_EMPLOYEE,
        admin_only=True,
        nav_key="nav.events",
        keywords=("evento", "eventos", "cola", "reprocesar", "pendiente"),
        actions=("See the queue and reprocess or cancel an event.",),
        tools=("list_events",),
    ),
    Screen(
        route="/events/:id",
        title="Detalle de un evento",
        purpose="An event: its content, its status and its trail.",
        area=AREA_EMPLOYEE,
        admin_only=True,
        detail_of="/events",
        keywords=("evento", "detalle"),
    ),
    Screen(
        route="/consumo",
        title="Consumo",
        purpose="How much the employee spends: credits of the month, cost and where it goes.",
        area=AREA_ADMIN,
        admin_only=True,
        nav_key="nav.consumo",
        keywords=("consumo", "gasto", "coste", "credito", "creditos", "presupuesto",
                  "factura", "dinero", "tokens", "cuanto llevo gastado"),
        actions=("See the month's budget and what has been consumed.",
                 "See the spend by date range, by person and by concept."),
        tools=("get_consumption",),
    ),
)

_BY_ROUTE = {s.route: s for s in SCREENS}


def screen(route: str) -> Optional[Screen]:
    return _BY_ROUTE.get(route)


def menu_routes() -> dict[str, str]:
    """``{route: i18n key}`` of what IS in the menu. The test uses it to cross-check it
    with ``Layout.tsx``."""
    return {s.route: s.nav_key for s in SCREENS if s.nav_key}


# ── Layer 1: what is derived (THIS employee's domain and real gates) ─


def domain_screens() -> list[dict[str, Any]]:
    """THIS employee's data/results screens, from the manifest.

    They are derived the same way as the menu (``Layout.tsx``) so that what the employee
    tells and what the person sees are the same thing: one employee calls «Incidencias»
    what another calls «Avisos», and telling them "go to /domain/alerts" when its route is
    ``/domain/incidencias`` is sending them to a screen that does not exist.
    """
    try:
        from app.config import APP_MANIFEST
        from app.routes.manifest_routes import _is_runtime_entity
    except Exception:  # noqa: BLE001
        return []
    out: list[dict[str, Any]] = []
    entities = (APP_MANIFEST.get("agent_app", {}) or {}).get("domain_entities", []) or []
    for e in entities:
        slug = str(e.get("slug") or "").strip()
        table = str(e.get("table") or slug).strip().lower()
        if not slug:
            continue
        try:
            if _is_runtime_entity(e):
                continue
        except Exception:  # noqa: BLE001 — if it cannot be filtered, better to show
            pass
        local = (e.get("source") or "project") == "local"
        out.append({
            "route": f"/domain/{slug}",
            "label": str(e.get("label") or e.get("title") or slug),
            "kind": "genera" if local else "consulta",
            "table": table,
        })
    return out


_GATE_LEVEL = {
    "require_admin": "administrador",
    "require_approver": "administrador o aprobador designado",
    "require_project_access": "cualquier usuario del proyecto",
    "get_current_user": "cualquier usuario autenticado",
}


def route_gates(app: Any) -> list[dict[str, str]]:
    """The real table of API routes with the gate that protects them.

    Derived from the FastAPI app at runtime: this is not documentation, it IS the code, so
    it cannot lie or go stale. It serves to answer "and who can do this?" without anyone
    maintaining a list.
    """
    rows: list[dict[str, str]] = []
    try:
        for r in getattr(app, "routes", []) or []:
            path = getattr(r, "path", "") or ""
            methods = sorted(getattr(r, "methods", set()) or set())
            methods = [m for m in methods if m not in ("HEAD", "OPTIONS")]
            if not path.startswith(("/api", "/mcp")) or not methods:
                continue
            rows.append({
                "path": path,
                "methods": ",".join(methods),
                "who": _gate_of(r),
            })
    except Exception:  # noqa: BLE001 — never break because of introspection
        logger.debug("route_gates introspection failed", exc_info=True)
    rows.sort(key=lambda x: x["path"])
    return rows


def _gate_of(route: Any) -> str:
    """The name of a route's most restrictive auth dependency."""
    names: set[str] = set()

    def walk(dep: Any, depth: int = 0) -> None:
        if dep is None or depth > 6:
            return
        call = getattr(dep, "call", None)
        name = getattr(call, "__name__", "")
        if name:
            names.add(name)
        for sub in getattr(dep, "dependencies", []) or []:
            walk(sub, depth + 1)

    try:
        walk(getattr(route, "dependant", None))
    except Exception:  # noqa: BLE001
        return "desconocido"
    for key in ("require_admin", "require_approver", "require_project_access",
                "get_current_user"):
        if key in names:
            return _GATE_LEVEL[key]
    if "_check_token" in names:
        return "solo el propio empleado (token de servicio)"
    return "sin autenticación"


# ── What is given to the model ───────────────────────────────────────


def index_block() -> str:
    """COMPACT index of the application for the priming (~1 kB).

    Deliberately short: it is not the manual, it is the map. The detail is requested with
    ``describe_app``, which also filters by who is asking. Putting the 25 screens with their
    actions here would duplicate the manual in every session and still lose it at the first
    compaction.
    """
    lines = ["## Your own application (know it: the person will ask you about it)"]
    for s in SCREENS:
        if s.detail_of or s.route == "/login":
            continue
        mark = " [admin]" if s.admin_only else ""
        lines.append(f"- `{s.route}` · {s.title}{mark}: {s.purpose}")
    dom = domain_screens()
    if dom:
        lines.append("- Data/results screens of this employee: "
                     + ", ".join(f"{d['label']} (`{d['route']}`)" for d in dom[:12]))
    lines.append(
        "For the detail of a screen (what is done there, who can and with which "
        "tool you do it) use `describe_app`. Do NOT make it up: if you are not "
        "sure, look it up before answering. Screen titles are the names the person "
        "sees (in Spanish when their UI is in Spanish)."
    )
    return "\n".join(lines)


def _fold(text: str) -> str:
    """Lowercase without accents: the person writes «configuración» or «configuracion»."""
    import unicodedata
    return "".join(
        c for c in unicodedata.normalize("NFD", (text or "").lower())
        if unicodedata.category(c) != "Mn"
    )


def describe(topic: str, actor: Any = None) -> dict[str, Any]:
    """What there is in the application about ``topic``, told for whoever is asking.

    ``actor`` is the call's ``mcp_actor.Actor``: with it we mark what that person can open
    and what not, and that is why the employee can say "an administrator sees that" BEFORE
    trying it — instead of hitting a rejection and leaving the person without knowing whom
    to ask.
    """
    from app.mcp_actor import TOOL_MIN_ROLE

    needle = _fold(topic).strip()
    is_admin = bool(getattr(actor, "is_admin", False))
    may_approve = bool(getattr(actor, "may_approve", False))

    found: list[dict[str, Any]] = []
    for s in SCREENS:
        if not _matches(s, needle):
            continue
        can_open = (not s.admin_only) or is_admin
        tools = [t for t in s.tools if _tool_allowed(TOOL_MIN_ROLE.get(t), is_admin, may_approve)]
        found.append({
            "pantalla": s.title,
            "ruta": s.route,
            "para_que": s.purpose,
            "area": "Administración" if s.area == AREA_ADMIN else "Empleado",
            "en_el_menu": bool(s.nav_key),
            "solo_administradores": s.admin_only,
            "quien_pregunta_puede_abrirla": can_open,
            "se_puede_hacer_ahi": list(s.actions),
            "lo_puedo_hacer_yo_por_chat": tools,
            "solo_en_la_pantalla": list(s.ui_only),
            "detalle_de": s.detail_of,
        })

    # This employee's domain screens: their name is its own, so they are searched by the
    # real label, not by the table name.
    dominio = [d for d in domain_screens()
               if not needle or needle in _fold(f"{d['label']} {d['route']} {d['table']}")]

    out: dict[str, Any] = {
        "ok": True,
        "quien_pregunta": (getattr(actor, "describe", lambda: "unknown")()
                           if actor is not None else "unknown"),
        "pantallas": found,
        "pantallas_de_datos_y_resultados": dominio,
    }
    if not found and not dominio:
        out["nada_encontrado"] = (
            "No screen matches that. Do not make it up: tell the person what DOES "
            "exist (the index is in your prompt) and ask them what they are looking for."
        )
    if not is_admin:
        out["aviso"] = (
            "The person asking is NOT an administrator of this employee: do not offer "
            "them the screens or the actions marked as administrators only; "
            "tell them whom to ask (use list_approvers)."
        )
    return out


def _matches(s: Screen, needle: str) -> bool:
    """Tolerant search: without accents and over EVERYTHING about the screen (route, title,
    purpose, actions, synonyms and tools), because the person asks «lo que me gasto»
    ("what I spend"), not "/consumo"."""
    if not needle:
        return True
    return needle in _fold(" ".join((
        s.route, s.title, s.purpose, " ".join(s.actions), " ".join(s.keywords),
        " ".join(s.tools),
    )))


def _tool_allowed(level: Optional[str], is_admin: bool, may_approve: bool) -> bool:
    if level in (None, "agent", "human"):
        return True
    if level == "admin":
        return is_admin
    if level == "approver":
        return may_approve
    return False
