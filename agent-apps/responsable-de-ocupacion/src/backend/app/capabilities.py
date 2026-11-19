"""v7.65.0 — CAPABILITIES resolver ("What can the employee do?").

RIGOR is the whole point: this module does NOT invent a parallel model of what the
employee can do. For every connector tool it reads the SAME row the runtime reads to
gate a real call — the platform's permissions table for this employee
(``mcp_policy.load_table`` + ``mcp_policy.table_permission``, 14.8.0: nobody in this pod
calculates a permission) — plus ``autonomy_state`` (the posture), the real automations,
channels, budget and limits. So the snapshot IS the reality, not a description of it.

Verdict per capability, one of:
  * ``allow``   — the employee can do it.
  * ``approve`` — it can, but a human must approve (the table says ``ask``).
  * ``deny``    — blocked (the table says ``deny``).
  * ``unavailable`` — nothing to act with (e.g. no channel, connector not
                  authorised, toolkit not enumerable now → HONEST unknown).

The MODE lens (autónomo / chat / canales) reinterprets the SAME verdict, because
the gate itself is mode-agnostic; what differs is how ``approve`` manifests
(parked vs interactive vs pending) and the AUTONOMY POSTURE (in observe/propose
the employee does not act outward on its own even when the permission would allow
it — a posture bias, flagged as such, not a hard gate).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

# NB: DB imports (sqlalchemy / app_database / AppSetting) are done INSIDE the
# functions that need them, so the pure resolution logic (``_by_mode``, ``_worse``,
# ``_count_summary``, ``_connector_catalog_state``,
# ``_unenumerated_verdicts``) imports and unit-tests without a database.

logger = logging.getLogger(__name__)

# Verdicts, ordered by restrictiveness so "worst-case wins" is a max().
V_ALLOW, V_APPROVE, V_DENY, V_NA = "allow", "approve", "deny", "unavailable"
_V_RANK = {V_ALLOW: 0, V_APPROVE: 1, V_DENY: 2, V_NA: 3}
MODES = ("autonomous", "chat", "channels")


def _worse(a: str, b: str) -> str:
    return a if _V_RANK.get(a, 0) >= _V_RANK.get(b, 0) else b


# ── A connector's catalog can be read, or not ─────────────────────────────
# A Composio connector comes in through the single gate and exposes ONLY its meta-tools
# (``<conn>_COMPOSIO_SEARCH_TOOLS`` / ``_MULTI_EXECUTE_TOOL``), so ``tools/list`` returns no
# static catalog to enumerate — BY DESIGN, it is not a failure. Mistaking that for a
# breakdown made a perfectly connected connector say "not connected / its catalog could
# not be read" (the most confusing false negative).
#
# In an OWN connector (not Composio) there is no dynamic gate: if its catalog comes back
# empty, that IS a breakdown. The same excuse as for Composio ones used to be applied to
# them and their failure came out disguised as "resolved on the fly".
def _connector_catalog_state(enumerated: bool, es_composio: bool) -> str:
    """``enumerated`` (we have the real list), ``dynamic`` (Composio: resolved on the
    fly, there is no static list and it is not an error) or ``unavailable`` (own and
    empty: an honest breakdown, usually transient)."""
    if enumerated:
        return "enumerated"
    return "dynamic" if es_composio else "unavailable"


def _unenumerated_verdicts(policy: str, catalog: str) -> tuple[str, str]:
    """(read_verdict, write_verdict) for a connector with NO enumerated tools.

    * ``dynamic`` (router): the connector IS connected and usable; reads are
      allowed and writes follow the connector's write-policy FLOOR — exactly what
      an enumerated read/write would resolve to under that floor, only discovered
      on demand. So it must NOT read as ``unavailable``.
    * ``unavailable`` (raw + empty): HONEST unknown — reads ``unavailable``; writes
      report the floor when the policy forbids/gates, else ``unavailable`` too."""
    from app import mcp_policy
    floor = (V_DENY if policy == mcp_policy.POLICY_READ_ONLY
             else V_APPROVE if policy == mcp_policy.POLICY_APPROVAL else V_ALLOW)
    if catalog == "dynamic":
        return V_ALLOW, floor
    write_v = (V_DENY if policy == mcp_policy.POLICY_READ_ONLY
               else V_APPROVE if policy == mcp_policy.POLICY_APPROVAL else V_NA)
    return V_NA, write_v


# ── Curated business-capability catalog for the app's OWN tools ──────────
# Maps each app-tool to a business capability in plain language (the usability
# layer). ``outward`` = a consequential/outward action (send, commit, external) —
# the axis the autonomy posture governs. Local tools are EXEMPT from the connector
# write-policy ("locally it always can"), so their verdict is purely the
# allow/ask/deny permission cascade.
# v7.77.0 — the list is the BUSINESS capabilities the operator cares about,
# grouped by PURPOSE (understand / act / organize). The internal MECHANISMS that
# used to appear as cards — request_approval ("pedir aprobación"), suppress_signal
# ("silenciar"), local.write ("escribir en tablas propias") — are NOT capabilities
# the operator grants; they are shown as a governance NOTE in the UI, not cards.
_APP_CAPS: list[dict[str, Any]] = [
    {"key": "see_data", "group": "understand", "outward": False,
     "label": {"es": "Ver y analizar tus datos", "en": "See and analyse your data"},
     "desc": {"es": "Consulta y cruza la información del negocio para entender qué pasa.",
              "en": "Reads and cross-checks the business data to understand what's going on."},
     "tools": ["local.read", "list_alerts", "list_recommendations", "list_decisions",
               "list_scheduled_items", "list_communications", "list_scoring",
               "list_approvals", "describe_entities", "find_user", "list_users"]},
    # v7.84.0 — consult the operator's uploaded reference DOCUMENTS (catalogue,
    # prices, policies, requirements, FAQs) for official content, WITH citation.
    # Reading; never outward.
    {"key": "consult_knowledge", "group": "understand", "outward": False,
     "label": {"es": "Consultar material de referencia", "en": "Consult reference material"},
     "desc": {"es": "Busca en los documentos que subes (catálogos, tarifas, políticas, requisitos, FAQs) para responder con contenido oficial y citar la fuente.",
              "en": "Searches the documents you upload (catalogues, prices, policies, requirements, FAQs) to answer with official content and cite the source."},
     "tools": ["search_knowledge", "list_knowledge_docs", "get_knowledge_doc"]},
    {"key": "record_findings", "group": "understand", "outward": False,
     "label": {"es": "Registrar hallazgos y avisos", "en": "Record findings and alerts"},
     "desc": {"es": "Anota internamente lo que detecta —alertas, recomendaciones, decisiones y puntuaciones— que verás en sus pantallas.",
              "en": "Records internally what it spots —alerts, recommendations, decisions and scores— which you'll see on its screens."},
     "tools": ["upsert_alert", "register_recommendation", "log_decision", "upsert_scoring"]},
    # v7.79.0 — communicate + schedule are NOT "outward" risk: notifying YOUR
    # managers and creating INTERNAL tasks/reminders is how it reports and organises
    # itself (essential even in "Observe and propose" — otherwise it could not tell
    # you what it observes). What is truly "outward" (writing to external systems)
    # lives in the CONNECTORS, governed by their policy.
    {"key": "communicate", "group": "act", "outward": False,
     "label": {"es": "Avisar y comunicar a personas", "en": "Notify and message people"},
     "desc": {"es": "Redacta y ENVÍA avisos (email/WhatsApp/Slack) a tus responsables por un canal — así te informa de lo que ve.",
              "en": "Drafts and SENDS notices (email/WhatsApp/Slack) to your people through a channel — this is how it keeps you informed."},
     "tools": ["log_communication"], "needs_channel": True},
    {"key": "schedule_work", "group": "act", "outward": False,
     "label": {"es": "Agendar y delegar trabajo", "en": "Schedule and delegate work"},
     "desc": {"es": "Crea encargos y recordatorios internos, y asigna tareas a tus personas.",
              "en": "Creates internal tasks and reminders, and assigns work to your people."},
     "tools": ["schedule_task", "assign_task_to_user", "complete_scheduled_item"]},
    # v7.82.0 — outward=True: authoring a standing order grants the employee
    # FUTURE autonomous action, so below full autonomy it should need approval
    # ("With your approval"); only "Acts autonomously" lets it do so alone.
    {"key": "manage_automations", "group": "organize", "outward": True,
     "label": {"es": "Crear y ajustar sus automatizaciones", "en": "Create and tune its automations"},
     "desc": {"es": "Define órdenes permanentes «Cuando pase X → haz Y». Se las pides en el chat (o las propone él); las gestionas en «Trabajo autónomo».",
              "en": "Sets up standing orders «When X happens → do Y». You ask for them in chat (or it proposes them); you manage them in «Autonomous work»."},
     "tools": ["get_automation_catalog", "list_automations", "create_automation",
               "update_automation", "set_automation_enabled", "test_automation", "run_automation"]},
    {"key": "manage_memory", "group": "organize", "outward": False,
     "label": {"es": "Recordar a las personas", "en": "Remember people"},
     "desc": {"es": "Guarda hechos y preferencias POR PERSONA para personalizar su trabajo.",
              "en": "Stores facts and preferences PER PERSON to personalise its work."},
     "tools": ["remember_fact", "record_episode", "index_file", "forget_fact",
               "compact_memory", "list_memory", "search_memory"]},
    # v7.140.0 — the second memory axis. ``outward=False``: LEARNING is inward
    # and harmless (it informs its judgment). Turning a learning into a RULE is
    # a separate, governed step: ``propose_silence`` only ever creates a
    # proposal, and the gate ignores it until an admin accepts — so this
    # capability grants knowledge, never permission.
    {"key": "learn_environment", "group": "organize", "outward": False,
     "label": {"es": "Aprender de su entorno", "en": "Learn from its environment"},
     "desc": {"es": "Aprende cómo se comporta lo que vigila —cuánto tarda algo "
                    "normalmente, qué es ruido, cómo se revisa bien— para no "
                    "tratar como incidencia lo que aquí es normal. Lo ves y lo "
                    "gobiernas en «Aprendizaje»; para dejar de avisarte de algo "
                    "necesita tu OK.",
              "en": "Learns how what it watches behaves — how long things "
                    "normally take, what is noise, how a check is best done — so "
                    "it stops treating as an incident what is normal here. You "
                    "see and govern it in «Learning»; silencing anything needs "
                    "your OK."},
     "tools": ["remember_learning", "search_learnings", "forget_learning",
               "compact_learnings", "propose_silence"]},
]

_GROUP_LABELS = {
    "understand": {"es": "Entender", "en": "Understand"},
    "act": {"es": "Avisar y organizar el trabajo", "en": "Notify & organise work"},
    "organize": {"es": "Aprender y automatizarse", "en": "Learn & automate"},
}


async def _get_setting(key: str) -> Optional[str]:
    try:
        from sqlalchemy import select
        from app.app_database import app_session
        from app.app_models.runtime import AppSetting
        async with app_session() as s:
            return (await s.execute(
                select(AppSetting.value).where(AppSetting.key == key)
            )).scalar_one_or_none()
    except Exception:  # pragma: no cover — never break the snapshot on a read
        return None


async def _tools_cache() -> dict[str, list[dict]]:
    """Last-good per-connector tool catalogue (AppSetting ``mcp_tools_cache``),
    the same cache the Conectores UI builds from tools/list."""
    raw = await _get_setting("mcp_tools_cache")
    try:
        data = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    out: dict[str, list[dict]] = {}
    if isinstance(data, dict):
        for k, tools in data.items():
            if isinstance(tools, list):
                out[str(k).strip().lower()] = [t for t in tools if isinstance(t, dict) and t.get("name")]
    return out


def _by_mode(verdict: str, outward: bool, autonomy: str) -> dict[str, dict]:
    """Reinterpret ONE verdict across the three modes. The gate is mode-agnostic;
    modes differ in how ``approve`` manifests and — for autonomous — in the
    posture bias on OUTWARD actions (flagged as posture, not a hard gate)."""
    posture_holds = outward and autonomy in ("propose", "act_with_approval")

    def auton() -> dict:
        if verdict == V_DENY:
            return {"state": "deny", "reason": "permission"}
        if verdict == V_NA:
            return {"state": "unavailable"}
        if verdict == V_APPROVE:
            return {"state": "approval", "reason": "gate"}
        # allow:
        if posture_holds:
            note = "posture_observe" if autonomy == "propose" else "posture_ack"
            return {"state": "approval", "reason": note}
        return {"state": "can"}

    def chat() -> dict:
        if verdict == V_DENY:
            return {"state": "deny"}
        if verdict == V_NA:
            return {"state": "unavailable"}
        if verdict == V_APPROVE:
            return {"state": "ask"}          # interactive question, you're present
        return {"state": "can"}

    def channels() -> dict:
        if verdict == V_DENY:
            return {"state": "deny"}
        if verdict == V_NA:
            return {"state": "unavailable"}
        if verdict == V_APPROVE:
            return {"state": "approval"}      # parked before it goes out
        return {"state": "can"}

    return {"autonomous": auton(), "chat": chat(), "channels": channels()}


async def _app_capabilities(autonomy: str, has_channel: bool) -> list[dict]:
    """The employee's OWN tools. Local is exempt from connector permissions by design
    ("locally it always can"): what governs them is the autonomy posture (``_by_mode``)."""
    out: list[dict] = []
    for cap in _APP_CAPS:
        verdict, reason = V_ALLOW, ""
        # Communication needs a live channel — no channel ⇒ nothing to send with.
        if cap.get("needs_channel") and not has_channel and verdict != V_DENY:
            verdict, reason = V_NA, "no_channel"
        out.append({
            "key": cap["key"], "group": cap["group"], "kind": "app",
            "label": cap["label"], "desc": cap["desc"], "outward": cap["outward"],
            "tools": cap["tools"], "verdict": verdict, "reason": reason,
            "by_mode": _by_mode(verdict, cap["outward"], autonomy),
        })
    return out


async def _connector_groups(autonomy: str) -> list[dict]:
    """One group per connected external connector, with read/write verdicts + a per-tool
    breakdown read from the platform's permissions table — the SAME rows the proxy applies
    (``mcp_policy.table_permission``)."""
    from app import mcp_policy
    from app.mcp_runtime import composio_connectors, custom_connectors
    try:
        # v7.135.0 — include CUSTOM (non-Composio) MCPs (e.g. cluster-mcp): the
        # agent uses them, so they must show as capability groups too.
        de_composio = composio_connectors()
        nombres_composio = {(c.get("name") or "").strip() for c in de_composio}
        connectors = de_composio + custom_connectors()
    except Exception:
        connectors = []
    if not connectors:
        return []
    table = await mcp_policy.load_table()
    autonomia = await mcp_policy._por_autonomia()
    cache = await _tools_cache()
    groups: list[dict] = []
    as_verdict = {mcp_policy.PERM_ALLOW: V_ALLOW, mcp_policy.PERM_ASK: V_APPROVE,
                  mcp_policy.PERM_DENY: V_DENY}
    for c in connectors:
        name = (c.get("name") or "").strip()
        toolkit = (c.get("toolkit") or "").strip().lower()
        label = c.get("display_name") or name
        policy = mcp_policy.connector_policy(table, name, autonomia)
        # The tools: the CATALOG's, as the table lists them. Without a table row for the
        # connector, the cache or the catalogue an own connector registered when published
        # (`tools_catalogo`) — the SAME fallback the Conectores screen uses — judged by the
        # table's row for a tool it does not name.
        entry = mcp_policy.connector_entry(table, name)
        tools = [dict(r) for r in (entry or {}).get("tools") or []] or (
            cache.get(name.lower()) or cache.get(toolkit)
            or [dict(t) for t in (c.get("tools_catalogo") or []) if isinstance(t, dict)]
            or [])
        # SEED the write verdict from the connector headline, not V_ALLOW: tools the table
        # denies are REMOVED from tools/list, so a cache may hold only reads and the loop
        # below would never lower ``write_v`` — a false "can".
        read_v = V_ALLOW
        write_v = (V_DENY if policy == mcp_policy.POLICY_READ_ONLY
                   else V_APPROVE if policy == mcp_policy.POLICY_APPROVAL else V_ALLOW)
        tool_rows: list[dict] = []
        enumerated = bool(tools)
        catalog = _connector_catalog_state(enumerated, name in nombres_composio)
        for t in tools:
            tname = str(t.get("name") or "").strip()
            if not tname:
                continue
            perm, source, is_write = mcp_policy.table_permission(table, name, tname)
            if source == "platform":
                # The platform's own tools (built-in testing: list_test_datasets, seed_test_data, sweep_test_run):
                # denied to every agent and not the employee's to use — they are no capability of it. Counted, the
                # read one made a connector to a WAI application read «cannot read» (08/11/2026, WAI Viajes).
                continue
            if (is_write and source != "read" and perm == mcp_policy.PERM_ALLOW
                    and autonomia == mcp_policy.POLICY_APPROVAL):
                perm = mcp_policy.PERM_ASK
            v = as_verdict[perm]
            if not is_write:
                read_v = _worse(read_v, v)
            tool_rows.append({"name": tname, "write": is_write, "verdict": v,
                              "source": source})
        if any(r["write"] for r in tool_rows):
            # The most this connector can do with a write is its BEST write tool — the same
            # reading as the headline chip (`connector_policy`); the rows say which.
            write_v = min((r["verdict"] for r in tool_rows if r["write"]),
                          key=lambda v: _V_RANK[v])
        if not enumerated:
            # ``dynamic`` (router) → connected + usable, verdicts follow the headline.
            # ``unavailable`` (raw + empty) → HONEST unknown: report only the headline,
            # not fabricated tools. See ``_unenumerated_verdicts``.
            read_v, write_v = _unenumerated_verdicts(policy, catalog)
        groups.append({
            "key": f"connector:{name}", "kind": "connector", "connector": name,
            "toolkit": toolkit, "label": label,
            "write_policy": policy, "enumerated": enumerated,
            "catalog": catalog,
            "read_verdict": read_v, "write_verdict": write_v,
            "tools": sorted(tool_rows, key=lambda r: (r["write"], r["name"]))[:200],
            "capabilities": [
                {"key": f"{name}:read", "kind": "connector", "label": {"es": "Leer / consultar", "en": "Read / query"},
                 "desc": {"es": f"Consultar información en {label}.", "en": f"Read data in {label}."},
                 "outward": False, "verdict": read_v, "reason": "",
                 "by_mode": _by_mode(read_v, False, autonomy)},
                {"key": f"{name}:write", "kind": "connector", "label": {"es": "Crear / modificar / enviar", "en": "Create / modify / send"},
                 "desc": {"es": f"Escribir o ejecutar acciones en {label}.", "en": f"Write or act in {label}."},
                 "outward": True, "verdict": write_v, "reason": "policy" if policy != mcp_policy.POLICY_WRITE else "",
                 "by_mode": _by_mode(write_v, True, autonomy)},
            ],
        })
    return groups


def _count_summary(groups: list[dict]) -> dict:
    counts = {"can": 0, "approval": 0, "read_only": 0, "cannot": 0, "unavailable": 0}
    for g in groups:
        for cap in g.get("capabilities", []):
            v = cap["verdict"]
            if v == V_ALLOW:
                counts["can"] += 1
            elif v == V_APPROVE:
                counts["approval"] += 1
            elif v == V_DENY:
                counts["read_only" if cap.get("outward") else "cannot"] += 1
            else:
                counts["unavailable"] += 1
    return counts


async def build_snapshot() -> dict:
    """The full, rigorous capability snapshot. Never raises — degrades to honest
    unknowns so an admin page always renders."""
    from sqlalchemy import select
    from app.app_database import app_session
    from app.autonomy_state import autonomy_level, LEVELS, _BY_KEY

    level = await autonomy_level()
    lvl = _BY_KEY.get(level, _BY_KEY["propose"])

    # Channels (can it communicate at all?).
    active_kinds: list[str] = []
    try:
        from app.app_models.channels import Channel
        async with app_session() as s:
            for ch in (await s.execute(
                select(Channel).where(Channel.status == "active", Channel.is_active == 1)
            )).scalars().all():
                active_kinds.append(ch.kind)
    except Exception:
        pass
    has_channel = bool(active_kinds)

    app_caps = await _app_capabilities(level, has_channel)
    conn_groups = await _connector_groups(level)

    # App capabilities grouped by their business group.
    app_groups: dict[str, dict] = {}
    for cap in app_caps:
        g = app_groups.setdefault(cap["group"], {
            "key": f"app:{cap['group']}", "kind": "app", "label": _GROUP_LABELS.get(cap["group"], {"es": cap["group"], "en": cap["group"]}),
            "capabilities": [],
        })
        g["capabilities"].append(cap)
    ordered_app = [app_groups[k] for k in ("understand", "act", "organize") if k in app_groups]

    all_groups = ordered_app + conn_groups

    # Automations (real standing orders).
    autos = {"total": 0, "enabled": 0, "act_alone": 0, "requires_approval": 0, "preview": []}
    try:
        from app import automations_service as asvc
        items = await asvc.list_automations()
        autos["total"] = len(items)
        for a in items:
            if a.get("enabled"):
                autos["enabled"] += 1
                if a.get("requires_approval"):
                    autos["requires_approval"] += 1
                else:
                    autos["act_alone"] += 1
        autos["preview"] = [
            {"id": a["id"], "title": a.get("title"), "enabled": bool(a.get("enabled")),
             "requires_approval": bool(a.get("requires_approval")),
             "reaction_type": a.get("reaction_type"),
             "runtime_state": a.get("runtime_state")}
            for a in items[:12]
        ]
    except Exception:
        logger.debug("automations snapshot failed", exc_info=True)

    # Guardrails.
    guardrails: dict[str, Any] = {}
    try:
        # 7.122.0: `over` covers BOTH layers (the employee's monthly limit and the
        # project's exhausted balance); `blocked_reason` says which one fired.
        from app.budget import budget_block_reason
        from app.config import DAILY_BUDGET_USD
        _reason = await budget_block_reason()
        guardrails["budget"] = {"over": _reason is not None,
                                "blocked_reason": _reason,
                                "daily_cap_usd": DAILY_BUDGET_USD}
    except Exception:
        guardrails["budget"] = {"over": False, "blocked_reason": None, "daily_cap_usd": 0}
    try:
        from app.execution_limits import effective_limits
        mt, ma = await effective_limits()
        guardrails["limits"] = {"max_concurrent": mt, "max_autonomous": ma}
    except Exception:
        pass
    try:
        from app.app_models.business import Approval
        async with app_session() as s:
            guardrails["pending_approvals"] = len((await s.execute(
                select(Approval.id).where(Approval.status == "pending")
            )).scalars().all())
    except Exception:
        guardrails["pending_approvals"] = 0
    try:
        from app.app_models.signal_suppressions import SignalSuppression
        async with app_session() as s:
            guardrails["suppressions"] = len((await s.execute(
                select(SignalSuppression.id).where(SignalSuppression.enabled == True)  # noqa: E712
            )).scalars().all())
    except Exception:
        guardrails["suppressions"] = 0

    return {
        "autonomy": {
            "level": level,
            "label": lvl["label"], "description": lvl["description"], "posture": lvl["posture"],
            "levels": [{"key": l["key"], "label": l["label"], "description": l["description"]} for l in LEVELS],
        },
        "summary": _count_summary(all_groups),
        "modes": list(MODES),
        "groups": all_groups,
        "automations": autos,
        "channels": {"active": sorted(set(active_kinds)), "can_communicate": has_channel},
        "guardrails": guardrails,
    }


# ── Agent-assisted narration ("Explain it to me / translate my goal") ───
# Plain-business phrasing of each verdict — the digest must NEVER hand the model
# raw codes (allow/deny/unavailable), or it echoes them back to the operator.
# The digest stays in Spanish on purpose: it is DATA (the person's labels) and also the
# deterministic answer the person reads when the agent call fails.
_VERDICT_ES = {
    V_ALLOW: "sí, por su cuenta",
    V_APPROVE: "sí, pero con tu aprobación",
    V_DENY: "no",
    V_NA: "no se puede confirmar (el conector no está conectado/autorizado o no se ha leído su catálogo de acciones)",
}


def _compact_snapshot(snap: dict) -> str:
    """A compact, DETERMINISTIC text digest of the snapshot to ground the agent —
    the facts come from here, never from the model's imagination. Phrased in plain
    business language so the agent doesn't parrot internal codes."""
    lvl = snap["autonomy"]["label"]
    lvl = lvl.get("es") if isinstance(lvl, dict) else lvl
    lines = [f"Nivel de autonomía actual: {lvl}."]
    s = snap["summary"]
    lines.append(f"Resumen: actúa por su cuenta en {s['can']} capacidades, con tu "
                 f"aprobación en {s['approval']}, solo lectura en {s['read_only']}, "
                 f"no puede en {s['cannot']}, y sin confirmar en {s['unavailable']}.")
    for g in snap["groups"]:
        gl = g["label"]["es"] if isinstance(g["label"], dict) else g["label"]
        caps = "; ".join(
            f"{(c['label']['es'] if isinstance(c['label'], dict) else c['label'])}: "
            f"{_VERDICT_ES.get(c['verdict'], c['verdict'])}"
            for c in g.get("capabilities", [])
        )
        line = f"- {gl}: {caps}"
        # Router-mode connector: connected, but its catalog is resolved on demand
        # (no static list) — say so, so the agent never calls it "no conectado".
        if g.get("catalog") == "dynamic":
            line += (" (conector conectado en modo dinámico: usa su catálogo de "
                     "acciones bajo demanda, sin una lista fija que mostrar)")
        lines.append(line)
    a = snap["automations"]
    lines.append(f"Trabajos autónomos: {a['enabled']} activos ({a['act_alone']} actúan solos, "
                 f"{a['requires_approval']} requieren aprobación).")
    return "\n".join(lines)


async def explain(question: str, snapshot: Optional[dict] = None) -> dict:
    """Ask the employee to explain the (deterministic) snapshot in plain business
    language and translate the operator's intent into concrete settings. The FACTS
    are the snapshot; the agent only narrates. Best-effort: on any failure returns
    the deterministic digest so the UI still answers."""
    snap = snapshot or await build_snapshot()
    digest = _compact_snapshot(snap)
    q = (question or "").strip() or "¿Qué puedes hacer y qué no, y cómo lo cambio?"
    prompt = (
        "You are the AI employee yourself. Here is the EXACT and VERIFIED SNAPSHOT of your "
        "current permissions (do not contradict it or invent anything outside it):\n\n"
        f"{digest}\n\n"
        f"Question from your manager: «{q}»\n\n"
        "Answer in the language the question is written in, clearly and BRIEFLY (2-4 "
        "sentences, no very long lists): "
        "1) what you can or cannot do regarding what they ask, with the reason in "
        "one line; 2) if they want to change it, the concrete setting (raise/lower autonomy, "
        "connector permission, write policy, or enabling/tuning an "
        "automation) and where it is. Do not execute anything; only explain and advise.\n\n"
        "IMPORTANT on style: ALWAYS speak in business language, for a non-technical "
        "person. Do NOT use internal codes or terms such as «allow», «deny», "
        "«approve», «unavailable», «read_only» or table/column names: if a "
        "connector is not connected or authorised, say it like that (\"it is not connected "
        "yet\"), not as «unavailable». If a connector appears as «modo dinámico», it IS "
        "connected and operational (it uses its action catalog on demand): never say "
        "that it is not connected or that you cannot read its actions."
    )
    try:
        from app.agent_proxy import get_agent_proxy
        from app.agent_sessions import resolve_operator_model, resolve_tool_permissions
        proxy = get_agent_proxy()
        session_id = await proxy.create_session(
            role="autonomous", model=await resolve_operator_model(),
            tool_permissions=await resolve_tool_permissions(),
        )
        try:
            # v7.68.0 — this is an INTERACTIVE admin explanation, not a background
            # run: cap it tight (the default is the 900 s autonomous budget, which
            # would hang the request / 504 at the ingress). Degrades to the
            # deterministic digest below if it times out.
            raw = await proxy.send_autonomous(
                session_id, prompt, timeout=90, require_structured=False)
        finally:
            await proxy.abort_session(session_id)
            await proxy.delete_session(session_id)
        text = (raw.get("text") or "").strip()
        if text:
            return {"ok": True, "answer": text, "source": "agent"}
    except Exception:
        logger.debug("capabilities.explain agent call failed", exc_info=True)
    return {"ok": True, "answer": digest, "source": "deterministic"}
