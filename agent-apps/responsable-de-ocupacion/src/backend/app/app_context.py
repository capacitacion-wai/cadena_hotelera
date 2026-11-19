"""Manifest-derived "who am I / what data I can access" context.

The agent's identity and project tables live in ``AGENTS.md``, but the
app's OWN internal business entities (``domain_entities`` with
``source="local"`` — alerts, communications, decisions, …) were never
surfaced to the agent. So when an operator asked "what data do you have
access to?" the agent listed the project tables but omitted everything it
itself produces and the app displays.

This module renders, straight from ``wai-app.json``:

* :func:`build_data_catalogue` — a compact markdown block that enumerates
  BOTH the read-only project data AND the internal app entities, with the
  exact MCP tool to use for each. It enumerates EVERY entity declared
  under ``domain_entities`` — the canonical ones (alerts, recommendations,
  …) AND any CUSTOM entity this particular agent defines — so entities
  that vary per agent are always covered. Custom tables with no named tool
  are mapped to the generic ``local.read`` / ``local.write`` escape
  hatches.
* :func:`build_app_context_skill` — a fuller ``SKILL.md`` the agent can
  load on demand for the complete picture (identity + objective + data).

Both are pure functions of the manifest, so they stay correct as the
manifest evolves (no hand-maintained duplication). The catalogue is also
exposed LIVE to the agent through the ``app-tools`` MCP tool
``describe_entities`` (``mcp_server.describe_entities``), which calls
:func:`build_data_catalogue` so the agent can always enumerate the exact
set of entities THIS app defines — including the custom ones.
"""

from __future__ import annotations

from typing import Any

# Canonical local business tables → (write tool, read tool). Mirrors the
# MCP surface in ``mcp_server.py`` — keep in lock-step when adding tools.
# Every canonical entity has BOTH a write and a read tool. CUSTOM tables
# the generator adds (not in this map) fall back to the generic
# ``local.read`` / ``local.write`` escape hatches.
_LOCAL_TOOLS: dict[str, tuple[str, str | None]] = {
    "alerts": ("upsert_alert", "list_alerts"),
    "recommendations": ("register_recommendation", "list_recommendations"),
    "communications": ("log_communication", "list_communications"),
    "scoring": ("upsert_scoring", "list_scoring"),
    "decisions": ("log_decision", "list_decisions"),
    "scheduled_items": ("schedule_task", "list_scheduled_items"),
    "approvals": ("request_approval", "list_approvals"),
}


def _label(entity: dict[str, Any], lang: str = "es") -> str:
    labels = entity.get("labels") or {}
    return labels.get(lang) or entity.get("label") or entity.get("slug") or "?"


def _agent_app(manifest: dict[str, Any]) -> dict[str, Any]:
    return manifest.get("agent_app", manifest) or {}


def _entities(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return _agent_app(manifest).get("domain_entities", []) or []


def build_data_catalogue(manifest: dict[str, Any], lang: str = "es") -> str:
    """Always-loaded markdown: the full list of data the agent can touch."""
    ents = _entities(manifest)
    project = [e for e in ents if (e.get("source") or "project") == "project"]
    local = [e for e in ents if e.get("source") == "local"]

    out: list[str] = ["## Data you have access to", ""]
    out.append(
        "When you are asked which data you handle, ALWAYS list both "
        "blocks (not only the project's tables)."
    )
    out.append("")
    out.append("### Project data (read-only, via MCP `wai-data`)")
    for e in project:
        tbl = e.get("table")
        suffix = f" (`{tbl}`)" if tbl else ""
        out.append(
            f"- **{_label(e, lang)}**{suffix} — query it with `wai-data` "
            f"(`execute_sql` / `get_sql_result`, read-only)."
        )
    out.append(
        "- The project's instructions and rules, documents/knowledge and PDFs "
        "(semantic search), also via `wai-data`."
    )
    out.append("")
    out.append(
        "### Internal entities of the application (you generate them and the app shows them)"
    )
    if local:
        for e in local:
            tbl = e.get("table") or ""
            # Canonical tables map to a named read/write tool; CUSTOM ones
            # (specific to THIS agent) fall back to the generic escape
            # hatches so the agent is told EXACTLY how to touch them too.
            write_tool, read_tool = _LOCAL_TOOLS.get(
                tbl, ("local.write", "local.read"),
            )
            how = f" — read with `{read_tool}`, write with `{write_tool}`."
            out.append(f"- **{_label(e, lang)}** (`{tbl}`){how}")
    else:
        out.append("- (this app declares no internal entities)")
    out.append("")
    out.append("### Time management and authority")
    out.append(
        "- **Calendar and tasks** (`scheduled_items`) — CHECK the agenda "
        "with `list_scheduled_items` (it lists tasks, delegated tasks, follow-ups and "
        "calendar entries; use it to answer \"what is on the "
        "calendar?\" or \"what do I have pending?\"). Schedule future work "
        "or follow-ups for yourself with `schedule_task` (when they are due, the system "
        "launches a run for you and you close it with `complete_scheduled_item`), or put "
        "tasks/reminders on a person with `assign_task_to_user`: when it is "
        "due, the system SENDS THEM A MESSAGE on their preferred channel "
        "(WhatsApp/email) with what they must do and marks it as done — that is how you "
        "PLAN COMMUNICATIONS to users for later (for an immediate message "
        "use `log_communication`). "
        "ALWAYS interpret dates/times in the business time zone "
        "the prompt tells you."
    )
    out.append(
        "- **Approvals** (`approvals`) — if an action is consequential, "
        "irreversible or outside your authority, leave it waiting for the "
        "operator's sign-off with `request_approval` instead of executing it."
    )
    out.append("")
    out.append(
        "You have no free access to the Internet nor to data outside the permissions "
        "of the project or of your own workspace (`/workspace`)."
    )
    return "\n".join(out)


def build_app_context_skill(manifest: dict[str, Any], lang: str = "es") -> str:
    """On-demand ``SKILL.md`` with the full identity + data picture."""
    aa = _agent_app(manifest)
    pres = manifest.get("presentation", {}) or {}
    name = (
        manifest.get("agent", {}).get("display_name")
        or aa.get("agent_display_name")
        or aa.get("display_name")
        or manifest.get("display_name")
        or "this AI employee"
    )
    objective = (
        (aa.get("notes", {}) or {}).get("discovery", {}).get("objective")
        or pres.get("objective")
        or ""
    )

    out: list[str] = [
        "---",
        "name: app-context",
        "description: >-",
        "  Who you are and which data you have access to (identity, goal and "
        "full data catalog: project + internal entities of the "
        "app). Load this skill when you are asked what you are, what you do or "
        "which data/tables you access.",
        "---",
        "",
        f"# Context: {name}",
        "",
        "## Who you are",
        "",
    ]
    if objective:
        out.append(f"You are **{name}**. Your goal: {objective}")
    else:
        out.append(f"You are **{name}**, an autonomous AI employee of this application.")
    out.append("")
    out.append(
        "You work with the project's data (read-only) and materialize "
        "your findings as internal entities of the app (alerts, "
        "communications, decisions, …) through the MCP `app-tools`."
    )
    out.append("")
    out.append(build_data_catalogue(manifest, lang))
    out.append("")
    out.append("## How to answer \"which data do you have access to?\"")
    out.append("")
    out.append(
        "Answer DIRECTLY (without repeating the question), in the person's language. Use "
        "EXACTLY this two-section format, each header in bold and on its own "
        "line, with a `- ` list below (do NOT put the header as one more item "
        "of the list, do NOT mix both blocks in a single list):"
    )
    out.append("")
    out.append("```markdown")
    out.append("**Project data (read-only):**")
    out.append("- `table` — description")
    out.append("- …")
    out.append("")
    out.append("**Internal entities of the app (I create and manage them):**")
    out.append("- `entity` — description")
    out.append("- …")
    out.append("```")
    out.append("")
    out.append(
        "The closing sentence (e.g. \"I have no free access to the Internet…\" or "
        "\"If you like, I can detail each one\") goes in a SEPARATE PARAGRAPH, never "
        "attached to the last item of the list."
    )
    return "\n".join(out)
