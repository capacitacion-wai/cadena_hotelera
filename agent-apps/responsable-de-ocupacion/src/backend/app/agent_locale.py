"""Operator-configured agent language for prompts and MCP schemas.

Centralises ``app_settings.agent_language`` (editable from Operations)
so autonomous prompts and MCP tool descriptions stay consistent.
"""

from __future__ import annotations

import copy
from typing import Any

from sqlalchemy import select

from app.app_database import app_session

AGENT_LANGUAGES = frozenset({"es", "en"})

_RUN_ID_DESC = {
    "es": (
        "Id de la ejecución (AgentRun) en curso. El prompt del "
        "sistema te lo indica como run_id=N. Pásalo SIEMPRE que "
        "lo conozcas para que la fila quede auditada."
    ),
    "en": (
        "Current AgentRun id from the system prompt (run_id=N). "
        "Always pass it when known so the row is audited."
    ),
}

_MEMORY_SCHEMA_EN: dict[str, dict[str, str]] = {
    "remember_fact": {
        "user_email": "person's email address (memory key)",
        "kind": "fact (default) | preference | procedure",
        "tags": "comma-separated tags",
        "pinned": "protected from automatic replacement and always first",
        "supersedes": "id of the previous fact this one corrects",
        "artifacts": (
            "workspace files this fact references, for self-containment: "
            '[{"filename":..., "workspace_path":"sessions/.../exports/x.pdf"}]'
        ),
    },
    "record_episode": {
        "summary": "one sentence",
        "artifacts": "workspace files referenced by this episode",
    },
    "index_file": {
        "workspace_path": "relative to /workspace",
        "source": "email | chat | agent (default agent)",
    },
    "list_memory": {
        "limit": "default 30, max 50",
    },
    "search_memory": {
        "limit": "default 20, max 50",
    },
    "forget_fact": {
        "fact_id": "exact id to delete",
        "content": "delete facts whose content matches exactly",
    },
}


def product_language() -> str:
    """The PRODUCT's primary language declared in the agent's Definition
    (``presentation.idioma_principal``, a BCP-47 tag) as its primary subtag
    (``pt-BR`` → ``pt``), or ``""`` when it is not declared. It is what the
    employee produces outward in (posts, emails, documents) unless a task says
    otherwise; the platform's definition reaches the pod through
    ``conversacion.declaracion()``."""
    try:
        from app.conversacion import declaracion
        code = str((declaracion() or {}).get("idioma_principal") or "").strip()
    except Exception:  # pragma: no cover — an unreadable definition never blocks a run
        return ""
    return code.split("-")[0].lower()


async def resolve_agent_language() -> str:
    """Output language for autonomous runs. The operator's
    ``app_settings.agent_language`` (Operations) wins; without it, the
    product's primary language of the Definition when it is one of
    ``AGENT_LANGUAGES``; else ``es``. Channel replies ignore this — they
    adapt to the sender's language."""
    try:
        from app.app_models.runtime import AppSetting
        async with app_session() as s:
            val = (await s.execute(
                select(AppSetting.value).where(AppSetting.key == "agent_language")
            )).scalar_one_or_none()
        if val in AGENT_LANGUAGES:
            return val
    except Exception:  # pragma: no cover — never block a run on this
        pass
    base = product_language()
    return base if base in AGENT_LANGUAGES else "es"


def language_block(lang: str) -> str:
    """Trailer that pins the language of every HUMAN-FACING text the
    agent produces in a non-channel run: run summary, alert /
    recommendation / decision rows, and the proactive ``notify``
    subject/body. Without this the model drifts into whatever language
    the prompt template happens to use."""
    if lang == "en":
        return (
            "\n\nOUTPUT LANGUAGE: write ALL human-facing text in "
            "ENGLISH — the summary, every title/description/rationale/"
            "reason you persist via app-tools, and the subject/body of "
            "any proactive `notify` block. Keep table/column names and "
            "tool arguments unchanged."
        )
    return (
        "\n\nIDIOMA DE SALIDA: redacta TODO el texto destinado a "
        "humanos en ESPAÑOL — el summary, cada título/descripción/"
        "justificación/motivo que persistas vía app-tools, y el asunto/"
        "cuerpo de cualquier bloque `notify` proactivo. No traduzcas "
        "nombres de tablas/columnas ni argumentos de tools."
    )


def common_mcp_props(lang: str) -> dict[str, Any]:
    """Shared MCP JSON-schema properties merged into every tool schema."""
    code = lang if lang in AGENT_LANGUAGES else "es"
    return {
        "run_id": {
            "type": "integer",
            "description": _RUN_ID_DESC[code],
        },
    }


def localize_tool_schema(
    tool_name: str,
    schema: dict[str, Any],
    lang: str,
) -> dict[str, Any]:
    """Return a copy of ``schema`` with operator-language descriptions."""
    if lang not in AGENT_LANGUAGES or lang == "es":
        return schema
    out = copy.deepcopy(schema)
    props = out.setdefault("properties", {})
    props["run_id"] = common_mcp_props(lang)["run_id"]
    overrides = _MEMORY_SCHEMA_EN.get(tool_name, {})
    for key, desc in overrides.items():
        if key in props and isinstance(props[key], dict):
            props[key]["description"] = desc
    return out


# ── THE MEMORY'S LANGUAGE (14.17.0) ─────────────────────────────────────────────────────────
# Everything the employee stores —facts, preferences, episodes, learnings, file descriptions—
# goes in ITS working language (`resolve_agent_language`), whatever language the person used.
# Before, each writer decided on its own (the agent copied whoever spoke, platform templates
# were Spanish) and memory ended up half and half. Memory SEARCH matches words, not meaning,
# so the query has to be in that language too: that is half of the note.
_NOMBRES = {"es": "SPANISH", "en": "ENGLISH"}


def memory_language_note(lang: str) -> str:
    nombre = _NOMBRES.get(lang, _NOMBRES["es"])
    return (
        f"MEMORY LANGUAGE: everything you store in memory (facts, preferences, episodes, what "
        f"you learn, file descriptions) is written in {nombre}, even when the person speaks "
        f"another language: translate it, and keep names, figures and literal quotes as they "
        f"are. When you SEARCH memory, write the query in {nombre} too: the search matches "
        f"words, not meaning, so a query in another language finds nothing."
    )


#: Labels of the texts the PLATFORM itself stores in memory (no model involved). What the
#: person said (their request, the reason of a rejection) is quoted as is.
_ROTULOS = {
    "es": {
        "peticion": "Petición: {x}", "interaccion": "Interacción", "genero": "Generó: {x}",
        "genero_ficheros": "Generó ficheros", "a_raiz": "A raíz de la petición: «{x}»",
        "generado_por": "Generado por {x}", "adjuntado_chat": "Adjuntado por el usuario en el chat",
        "rechazo": "No realizar la acción «{titulo}» ({tipo}) sin aprobación. Motivo del operador: {motivo}",
    },
    "en": {
        "peticion": "Request: {x}", "interaccion": "Interaction", "genero": "Produced: {x}",
        "genero_ficheros": "Produced files", "a_raiz": "Following the request: «{x}»",
        "generado_por": "Generated by {x}", "adjuntado_chat": "Attached by the user in the chat",
        "rechazo": "Do not perform the action «{titulo}» ({tipo}) without approval. Operator's reason: {motivo}",
    },
}


def rotulo(lang: str, clave: str, **valores: Any) -> str:
    return _ROTULOS.get(lang, _ROTULOS["es"])[clave].format(**valores)
