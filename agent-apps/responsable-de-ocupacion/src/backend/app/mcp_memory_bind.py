"""Memory-tool bind helpers and artifact coercion for MCP.

Extracted from ``mcp_server.py`` (v6.41.0) so the MCP router stays
focused on tool handlers and JSON-RPC wiring.
"""

from __future__ import annotations

import json
from typing import Any

MEMORY_KINDS = ("fact", "preference", "procedure")

MEMORY_TOOLS = frozenset({
    "remember_fact", "record_episode", "index_file", "forget_fact",
    "compact_memory", "list_memory", "search_memory",
})

# v7.140.0 — the SELF-memory tools (what the employee learned about its
# ENVIRONMENT). Deliberately NOT in ``MEMORY_TOOLS``: the dispatcher binds those
# to the run's human owner, and an autonomous run has none — which is precisely
# why the watcher is memoryless today. These need ``run_id`` (for provenance)
# but no owner, because their identity is the ``subject``, not a person.
SELF_MEMORY_TOOLS = frozenset({
    "remember_learning", "search_learnings", "forget_learning",
    "compact_learnings", "propose_silence",
})

AGENT_MEMORY_KINDS = ("observation", "baseline", "lesson", "procedure", "entity")

_MEMORY_NO_OWNER = {
    "ok": False,
    "skipped": True,
    "reason": (
        "no valid per-user memory owner: `user_email` must be a real human "
        "email address. Autonomous/watcher runs and messages with an unknown "
        "sender do NOT persist per-user memory."
    ),
}

_MEMORY_ACCESS_DENIED = {
    "ok": False,
    "error": (
        "memory access denied: user_email does not match this run's "
        "memory owner"
    ),
}

_MEMORY_NO_RUN_OWNER = {
    "ok": False,
    "error": (
        "memory owner not bound to this run yet — ensure this run targets "
        "the user and pass the exact run_id from the prompt on every "
        "memory tool call"
    ),
}


async def _bound_memory_user(call: Any, args: dict) -> str | None:
    """Resolve ``user_email`` against the run's persisted memory owner."""
    from app.memory_utils import authorized_memory_user, valid_memory_user

    requested = args.get("user_email")
    human = valid_memory_user(
        str(requested) if requested is not None else None,
        allow_autonomous_identity=True,
    )
    if not human:
        return None
    return await authorized_memory_user(call.run_id, human)


async def _memory_bind_result(
    call: Any, args: dict, owner: str | None,
) -> dict | None:
    """Return an error payload when ``owner`` is missing; else None."""
    if owner:
        return None
    from app.memory_utils import memory_owner_for_run, valid_memory_user

    if not valid_memory_user(
        str(args.get("user_email") or ""),
        allow_autonomous_identity=True,
    ):
        return _MEMORY_NO_OWNER
    if await memory_owner_for_run(call.run_id) is None:
        return _MEMORY_NO_RUN_OWNER
    return _MEMORY_ACCESS_DENIED


def _parse_artifacts(args: dict, owner: str | None = None) -> str | None:
    """Coerce an optional ``artifacts`` argument (list of
    ``{filename, workspace_path}`` or a JSON string) into compact JSON for
    storage. Returns None when absent/empty so we don't clobber other
    provenance. Keeps memory entries SELF-CONTAINED: a fact/preference/
    episode about a file carries the file's workspace path with it.

    SECURITY: ``owner`` is the person who owns the memory (the run's). An artifact is only
    valid if it lives in THEIR folder (``sessions/users/<token(owner)>/``): the path is set by
    the model, and until now only ``..`` was rejected, so a fact could point at another
    person's folder and end up linked in this person's memory. Without ``owner`` (old
    call) the previous behaviour is kept."""
    raw = args.get("artifacts")
    if raw in (None, "", [], {}):
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return None
    if not isinstance(raw, list):
        return None
    from app.memory_utils import reject_workspace_path

    carpeta = ""
    if owner:
        from app.agent_sessions import interactive_scope
        carpeta = (interactive_scope(owner).workspace_subdir or "").strip().strip("/")

    cleaned: list[dict] = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        wp = str(it.get("workspace_path") or it.get("path") or "").strip()
        if not wp or reject_workspace_path(wp):
            continue
        if carpeta:
            norm = "/".join(x for x in wp.lstrip("/").split("/") if x not in ("", "."))
            if not (norm == carpeta or norm.startswith(carpeta + "/")):
                continue  # outside the owner's folder: not linked
        cleaned.append({
            "filename": str(it.get("filename") or wp.rsplit("/", 1)[-1]),
            "workspace_path": wp,
        })
    if not cleaned:
        return None
    try:
        return json.dumps(cleaned, default=str)[:1500]
    except Exception:
        return None


def _artifacts_from(blob: str | None) -> list[dict]:
    """Decode the artifact-ref JSON stored in provenance/source_ref."""
    if not blob:
        return []
    try:
        data = json.loads(blob)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [
        {"filename": d.get("filename"), "workspace_path": d.get("workspace_path")}
        for d in data
        if isinstance(d, dict) and d.get("workspace_path")
    ]
