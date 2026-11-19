"""Connector permissions in the employee's pod: the PLATFORM's table, applied (14.8.0).

Since 14.8.0 this module does NOT calculate a permission. The platform has ONE calculation
(``backend/app/services/permisos_conectores.py``, contract in
``docs/permisos-de-conectores.md``) and this pod reads its result for THIS employee:

    GET {WAI_BACKEND_URL}/backend/agents/{AGENT_ID}/permissions/employee

— the agent's table with the employee's layer on top (the employee can only keep or tighten
what the agent allows; the platform rejects anything else with ``EMPLEADO_ABRE_MAS``). Every
tool of every connector comes with its final ``permission`` (``allow`` | ``ask`` | ``deny``),
whether it reads or writes (``io_kind``, from the CATALOG) and where the value comes from
(``source``). The pod only looks the row up.

Why it stopped calculating: until 14.x the pod rebuilt the permission from three pieces (the
deploy-time ``MCP_WRITE_POLICY``, the R/W map ``MCP_TOOL_IO_KINDS`` + a name heuristic, and the
operator's two local settings). The agent's sheet, the code agent and this pod each had their
own version of the rules, and they ended up saying different things — the screen said
"read-only" while the pod let the write through, on 07/09 and again on 14/09/2026.

What the pod still adds, and why it is not a second calculation:

* **Autonomy** (``propose`` ⇒ a write the table ``allow``s is PARKED for approval). It is the
  employee's own posture dial, local state of this pod, and it can only RESTRICT.
* **``ask`` = the approvals queue**: the call is parked and replayed verbatim once approved
  (works at 3 a.m. and leaves a trace). ``deny`` blocks.

The guarantee when the platform cannot be reached: the LAST known table keeps applying (in
memory, and persisted in ``AppSetting mcp_permissions_table`` so a restart does not lose it).
Never a wider one. With no table at all (first boot with the platform down) every connector
call is denied — fail-closed — with a message that says so.

The agent pod carries ZERO connector credentials: every external MCP call travels through
``routes/mcp_proxy_routes.py``, which consults this module before forwarding. Local tools
are exempt by design ("locally it always can").
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Connector-level summaries (headline chips, prompt block, colleague card). They are a
# READING of the table, never an input to a decision.
POLICY_WRITE = "write"
POLICY_APPROVAL = "write_with_approval"
POLICY_READ_ONLY = "read_only"

PERM_ALLOW = "allow"
PERM_ASK = "ask"
PERM_DENY = "deny"
_PERMS = (PERM_ALLOW, PERM_ASK, PERM_DENY)
_RANK = {PERM_ALLOW: 0, PERM_ASK: 1, PERM_DENY: 2}
_AS_POLICY = {PERM_ALLOW: POLICY_WRITE, PERM_ASK: POLICY_APPROVAL, PERM_DENY: POLICY_READ_ONLY}


def _n(value: Any) -> str:
    return str(value or "").strip().lower()


def _perm(value: Any) -> str:
    """A permission from the table; anything unexpected is ``deny`` (fail-closed)."""
    v = _n(value)
    return v if v in _PERMS else PERM_DENY


def most_restrictive(a: str, b: str) -> str:
    return a if _RANK.get(a, 2) >= _RANK.get(b, 2) else b


# ── The table ────────────────────────────────────────────────────────

_TABLE: Optional[dict] = None
# Monotonic moment of the last fetch ATTEMPT. A failed attempt also counts, with a shorter
# wait: hammering a platform that is down on every tool call helps nobody.
_TABLE_AT: float = 0.0
_TABLE_TTL_S = 60.0
_TABLE_RETRY_S = 15.0
_TABLE_LOCK: Optional[asyncio.Lock] = None
_TABLE_SETTING = "mcp_permissions_table"
_UNLOADED_LOGGED = False
# Did the LAST attempt reach the platform? The Conectores screen says so when it did not
# (it is then showing the last known table).
_LAST_FETCH_OK = False


def reset_table_cache() -> None:
    """Test hook — forgets the table entirely."""
    global _TABLE, _TABLE_AT, _TABLE_LOCK, _UNLOADED_LOGGED, _LAST_FETCH_OK
    _TABLE, _TABLE_AT, _TABLE_LOCK, _UNLOADED_LOGGED, _LAST_FETCH_OK = None, 0.0, None, False, False


def invalidate_table() -> None:
    """The next read asks the platform again (the table is KEPT until a new one arrives).

    Called after the employee saves its own layer: the result has to show on the next call,
    not in a minute."""
    global _TABLE_AT
    _TABLE_AT = 0.0


def table_is_fresh() -> bool:
    """Whether the last attempt to read the table reached the platform."""
    return _LAST_FETCH_OK


def _valid_table(data: Any) -> bool:
    return isinstance(data, dict) and isinstance(data.get("connectors"), list)


async def _fetch_table() -> Optional[dict]:
    """The table that the platform says this employee applies, or None if it cannot be read."""
    from app.config import AGENT_ID, BACKEND_API_SECRET, WAI_BACKEND_URL
    if not (WAI_BACKEND_URL and BACKEND_API_SECRET and AGENT_ID):
        logger.error("permissions table: WAI_BACKEND_URL/BACKEND_API_SECRET/AGENT_ID missing")
        return None
    import httpx
    url = f"{WAI_BACKEND_URL.rstrip('/')}/backend/agents/{int(AGENT_ID)}/permissions/employee"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, headers={"X-API-Secret": BACKEND_API_SECRET})
    if resp.status_code // 100 != 2:
        logger.warning("permissions table HTTP %s: %s", resp.status_code, resp.text[:200])
        return None
    data = resp.json()
    if not _valid_table(data):
        logger.warning("permissions table with an unexpected shape — ignored")
        return None
    return data


async def _load_persisted() -> Optional[dict]:
    try:
        from sqlalchemy import select
        from app.app_database import app_session
        from app.app_models.runtime import AppSetting
        async with app_session() as s:
            v = (await s.execute(
                select(AppSetting.value).where(AppSetting.key == _TABLE_SETTING)
            )).scalar_one_or_none()
        data = json.loads(v) if v else None
        return data if _valid_table(data) else None
    except Exception:  # noqa: BLE001 — no persisted table is the same as none
        logger.debug("persisted permissions table unreadable", exc_info=True)
        return None


async def _persist(table: dict) -> None:
    try:
        from datetime import datetime, timezone
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        from app.app_database import app_write
        from app.app_models.runtime import AppSetting
        values = {"key": _TABLE_SETTING, "value": json.dumps(table, sort_keys=True),
                  "updated_by": "platform", "updated_at": datetime.now(timezone.utc)}
        stmt = sqlite_insert(AppSetting).values(**values).on_conflict_do_update(
            index_elements=[AppSetting.key],
            set_={k: v for k, v in values.items() if k != "key"},
        )
        async with app_write() as s:
            await s.execute(stmt)
            await s.commit()
    except Exception:  # noqa: BLE001 — the in-memory copy still applies
        logger.warning("could not persist the permissions table", exc_info=True)


async def load_table(*, force: bool = False) -> Optional[dict]:
    """The table this employee applies. Cached ``_TABLE_TTL_S``; ``force`` asks now.

    Platform unreachable → the last known table (memory, then the persisted copy). Never a
    wider one, never a recalculation. None only when there has never been one."""
    global _TABLE, _TABLE_AT, _TABLE_LOCK, _UNLOADED_LOGGED, _LAST_FETCH_OK
    if not force and _TABLE is not None and (time.monotonic() - _TABLE_AT) < _TABLE_TTL_S:
        return _TABLE
    if _TABLE_LOCK is None:
        _TABLE_LOCK = asyncio.Lock()
    async with _TABLE_LOCK:
        ahora = time.monotonic()
        if not force and _TABLE is not None and (ahora - _TABLE_AT) < _TABLE_TTL_S:
            return _TABLE
        try:
            fresh = await _fetch_table()
        except Exception as exc:  # noqa: BLE001 — network: keep what we have
            logger.warning("permissions table unreachable: %s", exc)
            fresh = None
        if fresh is not None:
            if fresh != _TABLE:
                logger.info("permissions table loaded: version %s, %d connector(s)",
                            fresh.get("version_id"), len(fresh.get("connectors") or []))
                await _persist(fresh)
            _TABLE, _TABLE_AT, _UNLOADED_LOGGED, _LAST_FETCH_OK = fresh, ahora, False, True
            return _TABLE
        _LAST_FETCH_OK = False
        if _TABLE is None:
            _TABLE = await _load_persisted()
        # Retry sooner than the TTL, but not on every call.
        _TABLE_AT = ahora - _TABLE_TTL_S + _TABLE_RETRY_S
        if _TABLE is None and not _UNLOADED_LOGGED:
            logger.error("NO permissions table (platform unreachable, none stored): "
                         "every connector call is DENIED until it loads")
            _UNLOADED_LOGGED = True
        return _TABLE


def connector_entry(table: Optional[dict], connector: str) -> Optional[dict]:
    """The table's entry for a connector, by its catalog OR runtime name (``wai-odoo``)."""
    n = _n(connector)
    for c in (table or {}).get("connectors") or []:
        if n and n in {_n(c.get("name")), _n(c.get("runtime_name"))}:
            return c
    return None


def tool_row(entry: Optional[dict], tool: str) -> Optional[dict]:
    """The row of one tool, by its name or its opencode key (``{runtime}_{tool}``)."""
    t = _n(tool)
    for r in (entry or {}).get("tools") or []:
        if t and t in {_n(r.get("name")), _n(r.get("key"))}:
            return r
    return None


def table_permission(table: Optional[dict], connector: str, tool: str) -> tuple[str, str, bool]:
    """``(permission, source, is_write)`` of one tool, read from the table. Pure.

    ``source`` is the table's (``read`` | ``inherited_agent`` | ``connector_mode`` | ``custom`` |
    ``employee``) or, when the row does not exist, ``unlisted_tool`` / ``unlisted_connector``
    / ``unloaded``. A tool the catalog does not name counts as an unclassified WRITE, and its
    value comes in the table too (``unlisted_tool``): nothing is guessed here."""
    if table is None:
        return PERM_DENY, "unloaded", True
    entry = connector_entry(table, connector)
    if entry is None:
        return _perm((table.get("unlisted_connector") or {}).get("permission")), "unlisted_connector", True
    row = tool_row(entry, tool)
    if row is None:
        return _perm((entry.get("unlisted_tool") or {}).get("permission")), "unlisted_tool", True
    return _perm(row.get("permission")), str(row.get("source") or ""), _n(row.get("io_kind")) != "read"


def connector_policy(table: Optional[dict], connector: str, autonomy: str = POLICY_WRITE) -> str:
    """Headline of a connector: the MOST it can do with a write (``write`` |
    ``write_with_approval`` | ``read_only``). A reading of the table for chips and prompts —
    a call is never decided with this, it is decided tool by tool."""
    if table is None:
        return POLICY_READ_ONLY
    entry = connector_entry(table, connector)
    if entry is None:
        perms = [_perm((table.get("unlisted_connector") or {}).get("permission"))]
    else:
        perms = [_perm(r.get("permission")) for r in entry.get("tools") or []
                 if _n(r.get("io_kind")) != "read"]
        perms = perms or [_perm((entry.get("unlisted_tool") or {}).get("permission"))]
    policy = _AS_POLICY[min(perms, key=lambda p: _RANK[p])]
    if autonomy == POLICY_APPROVAL and policy == POLICY_WRITE:
        return POLICY_APPROVAL
    return policy


def denied_tool_keys(table: Optional[dict]) -> list[str]:
    """The opencode keys (``{runtime}_{tool}``) the table denies — defense in depth for
    the agent pod's ACL. The key, never the bare name: a bare ``write`` would also match
    opencode's own built-in tool."""
    out: list[str] = []
    for c in (table or {}).get("connectors") or []:
        for r in c.get("tools") or []:
            if _perm(r.get("permission")) == PERM_DENY and r.get("key"):
                out.append(str(r["key"]))
    return out


def denied_tool_names(table: Optional[dict], connector: str) -> list[str]:
    """The bare tool names one connector denies (the Composio router hides them)."""
    entry = connector_entry(table, connector)
    return [str(r.get("name")) for r in (entry or {}).get("tools") or []
            if _perm(r.get("permission")) == PERM_DENY and r.get("name")]


async def _por_autonomia() -> str:
    """`propose` ⇒ at most, write WITH APPROVAL. Never direct write.

    Until v10 they were two independent settings, and that independence produced the worst
    possible combination: the card said "observes and proposes" while its connector could
    modify customer data WITHOUT anyone approving it. Measured in demo on 31/08/2026 — the
    Kommo employee was exactly like that. Whoever reads "proposes" has the right for it to
    mean something.

    What is closed is DIRECT writing, not all writing: a write the table already parks has
    a human deciding before each change, which is literally what "proposes" means.

    Autonomy can only RESTRICT, never open. On a failure reading it the LEAST restrictive
    value is returned, so nothing changes: a database hiccup cannot silence an employee that
    does have permission to write. The table still applies the same.
    """
    try:
        from app.autonomy_state import autonomy_level
        return POLICY_APPROVAL if (await autonomy_level()) == "propose" else POLICY_WRITE
    except Exception:  # pragma: no cover — the policy never breaks because of this
        logger.warning("no se pudo leer el nivel de autonomía; no se endurece", exc_info=True)
        return POLICY_WRITE


async def effective_policy(connector: str) -> str:
    """Headline of one connector with autonomy applied (see :func:`connector_policy`)."""
    return connector_policy(await load_table(), connector, await _por_autonomia())


# ── Veto & filtering ─────────────────────────────────────────────────

# Composio Tool-Router META-tools. Two classes, handled DIFFERENTLY:
#  * PURE meta (search/schemas) NEVER execute anything → always allowed, even
#    under read_only (the agent needs them to discover tools).
#  * EXECUTE meta run the REAL tool named in their ARGUMENTS. We must NOT
#    exempt these by name (that reopens the write hole) — instead we UNWRAP
#    the inner slug and veto on THAT. So a read via EXECUTE_TOOL passes and a
#    write via EXECUTE_TOOL is vetoed, keeping a read_only router connector
#    usable for reads without letting writes through.
_PURE_META_MARKERS = ("COMPOSIO_SEARCH_TOOLS", "COMPOSIO_GET_TOOL_SCHEMAS")
_EXECUTE_META_MARKERS = ("COMPOSIO_EXECUTE_TOOL", "COMPOSIO_MULTI_EXECUTE_TOOL")
# Keys under which an EXECUTE meta-tool carries the REAL slug. Split by
# confidence so it generalises across EVERY toolkit without misreading router
# metadata:
#  * STRICT — unambiguous slug carriers (Composio's canonical ``tool_slug`` and
#    close synonyms). We trust the value whatever it is; a non-slug value just
#    falls to the table's ``unlisted_tool`` row → denied under a restriction.
#  * LOOSE — generic words that MIGHT be a slug or MIGHT be metadata. We take the
#    value only when it is slug-shaped, so ``name:"pine"`` / ``action:"read"``
#    aren't mistaken for tools.
_SLUG_KEYS_STRICT = ("tool_slug", "slug", "tool_name", "function")
_SLUG_KEYS_LOOSE = ("tool", "action", "name")
_SLUG_SHAPE = None  # compiled lazily below


def _is_pure_meta(tool_name: str) -> bool:
    n = (tool_name or "").upper()
    return any(m in n for m in _PURE_META_MARKERS)


def _is_execute_meta(tool_name: str) -> bool:
    n = (tool_name or "").upper()
    return any(m in n for m in _EXECUTE_META_MARKERS)


def _is_router_meta(tool_name: str) -> bool:
    """Any router meta-tool (pure OR execute) — kept visible in tools/list."""
    return _is_pure_meta(tool_name) or _is_execute_meta(tool_name)


def unwrap_execute_slugs(tool_name: str, arguments: Any) -> list[str]:
    """Real tool slug(s) an EXECUTE meta-tool will run, harvested from its
    arguments. A slug is taken ONLY from an explicit tool-slug KEY
    (``tool_slug``/``slug``/``tool_name``/… — the real Composio
    ``EXECUTE``/``MULTI_EXECUTE`` shape ``{tools:[{tool_slug,arguments}]}``) whose
    VALUE also looks like a slug (``TOOLKIT_VERB_…``). We scan nested
    dicts/lists so ``tools[].tool_slug`` is reached, but we DO NOT harvest
    arbitrary slug-shaped strings from non-slug keys — the router adds session
    metadata (``current_step:"FETCHING_LEADS_PAGE_1"``, ``thought``…) that would
    otherwise be misread as a tool and misclassify a pure READ batch as a write.
    Fail-closed is preserved: a slug the agent hides under a non-standard key is
    NOT extracted → decide_call's no-slug branch denies it under a restriction."""
    global _SLUG_SHAPE
    if _SLUG_SHAPE is None:
        _SLUG_SHAPE = re.compile(r"^[A-Z][A-Z0-9]+_[A-Z0-9_]+$")
    if not _is_execute_meta(tool_name) or not isinstance(arguments, (dict, list)):
        return []
    out: list[str] = []

    def scan(obj, depth: int):
        if depth > 6:
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, str):
                    s = v.strip()
                    # STRICT key → trust any value (generalises to any toolkit);
                    # LOOSE key → only when slug-shaped (skip router metadata).
                    if s and (k in _SLUG_KEYS_STRICT or (k in _SLUG_KEYS_LOOSE and _SLUG_SHAPE.match(s))):
                        out.append(s)
                elif isinstance(v, (dict, list)):
                    scan(v, depth + 1)
        elif isinstance(obj, list):
            for it in obj:
                if isinstance(it, (dict, list)):
                    scan(it, depth + 1)

    scan(arguments, 0)
    return out


def call_args_hash(tool_name: str, arguments: Any) -> str:
    """Stable hash binding an approval to the EXACT call (tool + args)."""
    payload = json.dumps(
        {"tool": (tool_name or "").strip(), "args": arguments or {}},
        sort_keys=True, ensure_ascii=False, default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


# Router SESSION metadata that varies between two attempts of the SAME write
# (so it must be stripped from the write signature, or the loop-guard never
# matches a re-attempt). These are the fields Composio's Tool-Router adds.
# v7.88.0 — ``id``/``jsonrpc`` REMOVED. They are JSON-RPC ENVELOPE fields (they
# live at the request root next to ``method``/``params``, NOT inside
# ``params.arguments``, which is what ``write_signature`` receives). Stripping a
# key named ``id`` at ANY depth of the business arguments made
# ``KOMMO_UPDATE_LEAD(id=1,…)`` and ``KOMMO_UPDATE_LEAD(id=2,…)`` hash IDENTICALLY,
# so the loop-guard would return lead 1's cached result for lead 2 — silently
# dropping a distinct-entity write. Only the router's real in-args noise is
# volatile.
_VOLATILE_SIG_KEYS = frozenset({
    "session_id", "current_step", "current_step_metric", "thought", "reasoning",
})


def write_signature(connector: str, tool_name: str, arguments: Any) -> str:
    """A METADATA-STABLE fingerprint of the WRITE a call performs, used by the
    loop-guard to recognise a re-attempt of an ALREADY-EXECUTED approved write.
    Strips the router's volatile session metadata so the SAME logical write
    (same connector + tool + inner slug/args) yields the SAME signature across
    attempts, while a genuinely different write (different args) does not."""
    def strip(o: Any) -> Any:
        if isinstance(o, dict):
            return {k: strip(v) for k, v in o.items() if k not in _VOLATILE_SIG_KEYS}
        if isinstance(o, list):
            return [strip(x) for x in o]
        return o
    payload = json.dumps(
        {"c": (connector or "").strip().lower(), "t": (tool_name or "").strip(),
         "a": strip(arguments or {})},
        sort_keys=True, ensure_ascii=False, default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


#: The reason of a refusal in a READ-ONLY TEST run (15.7.0, `prueba_solo_lectura.py`).
READ_ONLY_TEST = "read_only_test"
#: 15.8.0 — a CONTROLLED test chat (`prueba_controlada`): a write runs only after its person said yes.
CONTROLLED_TEST = "controlled_test"


def deny_message(connector: str, reason: str = "read_only") -> str:
    if reason == CONTROLLED_TEST:
        return (
            f"CONTROLLED TEST: this chat is a test of the employee and every change waits for the person. This "
            f"call to '{connector}' could not be asked first (a write through Composio's router, or a tool this "
            "test does not ask for), so it was NOT executed. Do not retry it nor look for another way: carry on "
            "with what you can do and say what you would have done (which tool, with what data)."
        )
    if reason == READ_ONLY_TEST:
        return (
            f"READ-ONLY TEST: this run is a read-only test of the employee (someone is previewing an "
            f"application that calls you). This call to '{connector}' would change data, or it cannot "
            "be verified that it does not, so it was NOT executed. Do not retry it and do not look for "
            "another way to make the change: carry on with what you can read, and in your answer "
            "describe what you would have done instead (which tool, with what data)."
        )
    if reason == "permission":
        return (
            f"BLOCKED: the operator has DENIED the use of this tool on "
            f"connector '{connector}'. Do not retry it; use an allowed tool "
            "or ask the operator to enable it."
        )
    if reason == "unverified":
        return (
            f"BLOCKED: it cannot be verified which tool this call would execute "
            f"on '{connector}' under a restricted policy, so it is denied as a "
            "precaution (fail-closed)."
        )
    if reason == "unloaded":
        return (
            f"BLOCKED: this employee's connector permissions could not be loaded from the "
            f"platform yet, so every call to '{connector}' is denied as a precaution. "
            "Tell the operator; do not retry it in a loop."
        )
    return (
        f"WRITE POLICY: connector '{connector}' is in READ-ONLY mode "
        "— this tool modifies data and is not allowed. Use the read tools, or tell "
        "the operator to widen the connector's policy if it is really needed."
    )


_IDIOMAS = {"es": "SPANISH", "en": "ENGLISH"}


def approval_message(connector: str, slugs: list[str], request_ref: str,
                     reason: str = "approval", idioma: str = "es") -> str:
    """Agent-facing text for a parked write (v7.29.4). The platform already
    STAGED the exact request server-side; the agent only records a human-
    readable approval that references it by ``request_ref`` — on approval the
    platform REPLAYS the original request verbatim (no schema guessing).

    ONE language for every approval (14.15.0): the employee's working language
    (``agent_locale.resolve_agent_language``, the one autonomous runs write in). An approval
    is read by whoever DECIDES, not by whoever asked, so the language of the chat it came
    from is not the right one; and a title template in Spanish produced half-Spanish
    approvals in English employees."""
    what = "use this tool" if reason == "ask" else "write"
    lengua = _IDIOMAS.get(idioma, _IDIOMAS["es"])
    return (
        f"Connector '{connector}' requires the operator's APPROVAL to "
        f"{what}. I have ALREADY parked this action; do NOT retry it. Record "
        "the approval with request_approval using EXACTLY this payload (fill in the two "
        "texts) and carry on with the rest of your task:\n"
        + json.dumps({
            "title": f"<one line, in {lengua}: what will happen, concretely — e.g. the reply that "
                     "will be posted and where, or the record that will be created>",
            "action_type": "tool_call",
            "summary": f"<in {lengua}: WHAT exactly will be sent or changed and WHY you want it, "
                       "so the approver can decide without opening anything else>",
            "action_payload": {
                "connector": connector,
                "tools": slugs,
                "request_ref": request_ref,
            },
        }, ensure_ascii=False)
        + f"\nWrite the title and the summary in {lengua}, whatever language this conversation "
          "is in: the people who approve read them. Once approved, the platform will execute "
          "the EXACT request you made (the approver also sees its arguments)."
    )


def _verdict(table: Optional[dict], connector: str, tool: str, autonomy: str,
             write_granted: bool) -> tuple[str, str]:
    """``(verdict, reason)`` of one real tool: ``allow`` | ``deny`` | ``approve``."""
    perm, source, is_write = table_permission(table, connector, tool)
    if perm == PERM_DENY:
        if source == "unloaded":
            return "deny", "unloaded"
        if source in ("custom",) or not is_write:
            return "deny", "permission"
        return "deny", "read_only"
    if perm == PERM_ASK:
        # An explicit per-tool rule is the operator's choice on THAT tool: an approved
        # reaction does not lift it. An authority (agent, connector mode, employee default)
        # is the write gate that the approved reaction already went through.
        explicit = source == "custom" or (
            source == "employee"
            and _n((connector_entry(table, connector) or {}).get("employee_mode")) == "custom")
        reason = "ask" if explicit else "approval"
        if reason == "approval" and write_granted:
            return "allow", ""
        return "approve", reason
    if is_write and source != "read" and autonomy == POLICY_APPROVAL:
        if write_granted:
            return "allow", ""
        return "approve", "approval"
    return "allow", ""


async def decide_call(connector: str, tool_name: str, arguments: Any,
                      *, write_granted: bool = False) -> tuple[str, list[str], str]:
    """The verdict of a tools/call: ``(verdict, slugs, reason)`` where verdict is
    ``allow`` | ``deny`` | ``approve`` and reason (``permission`` | ``read_only`` | ``ask``
    | ``approval`` | ``unverified`` | ``unloaded`` | "") drives the agent-facing message.
    The RELAY turns ``deny``/``approve`` into the right response (approve also stages the
    raw request for replay). Side effect: veto logging.

    ``write_granted`` (v7.35.0 COALESCENCE): this call belongs to a run whose
    automation REACTION the operator ALREADY approved. It flips a write-gate approval
    into ``allow`` so we don't ask a SECOND time for the same logical action. It never
    lifts a ``deny`` nor an explicit per-tool ``ask``."""
    if _is_pure_meta(tool_name):
        return "allow", [], ""
    table = await load_table()
    autonomy = await _por_autonomia()

    if _is_execute_meta(tool_name):
        slugs = unwrap_execute_slugs(tool_name, arguments)
        if not slugs:
            # Unverifiable EXECUTE: it may run anything the catalog does not name, so it is
            # worth exactly what such a tool is worth. Restricted → fail closed.
            v, _r = _verdict(table, connector, "", autonomy, write_granted)
            if v != "allow":
                _log_veto(connector, tool_name, "unverified")
                return "deny", [], ("unloaded" if table is None else "unverified")
            return "allow", [], ""
        judged = [(s, *_verdict(table, connector, s, autonomy, write_granted)) for s in slugs]
        denied = [(s, r) for s, v, r in judged if v == "deny"]
        if denied:
            _log_veto(connector, ",".join(s for s, _ in denied), denied[0][1])
            return "deny", [s for s, _ in denied], denied[0][1]
        approve = [(s, r) for s, v, r in judged if v == "approve"]
        if approve:
            _log_veto(connector, ",".join(s for s, _ in approve), approve[0][1])
            return "approve", [s for s, _ in approve], approve[0][1]
        return "allow", [], ""

    v, reason = _verdict(table, connector, tool_name, autonomy, write_granted)
    if v == "allow":
        return "allow", [], ""
    _log_veto(connector, tool_name, reason)
    return v, [tool_name], reason


async def decide_read_only_test(connector: str, tool_name: str,
                                arguments: Any) -> tuple[str, list[str], str]:
    """The verdict of a tools/call in a READ-ONLY TEST run (15.7.0): only a READ passes, and then
    with its usual verdict. A tool the table classifies as a write, one it does not name (an
    unclassified tool counts as a write), an EXECUTE whose inner tool cannot be read, and anything
    that would PARK an approval (a parked request is an effect too: it lands in someone's queue)
    are refused with ``READ_ONLY_TEST``. Never opens what the table closes."""
    if _is_pure_meta(tool_name):
        return "allow", [], ""
    table = await load_table()
    if _is_execute_meta(tool_name):
        slugs = unwrap_execute_slugs(tool_name, arguments)
        escriben = [s for s in slugs if table_permission(table, connector, s)[2]] if slugs else []
        if not slugs or escriben:
            _log_veto(connector, ",".join(escriben) or tool_name, READ_ONLY_TEST)
            return "deny", escriben, READ_ONLY_TEST
    elif table_permission(table, connector, tool_name)[2]:
        _log_veto(connector, tool_name, READ_ONLY_TEST)
        return "deny", [tool_name], READ_ONLY_TEST
    v, slugs, reason = await decide_call(connector, tool_name, arguments)
    if v == "approve":
        return "deny", slugs, READ_ONLY_TEST
    return v, slugs, reason


async def decide_controlled_test(connector: str, tool_name: str, arguments: Any,
                                 asked: set[str]) -> tuple[str, list[str], str]:
    """The verdict of a tools/call in a CONTROLLED test chat (15.8.0): what the table denies stays denied; a
    write (or unclassified tool, or one that would park an approval) passes only if its opencode key is one
    the session ASKS for — opencode already asked the person and they said yes — and never parks; an
    EXECUTE of the router with a write inside cannot be asked one by one, so it is refused."""
    if _is_pure_meta(tool_name):
        return "allow", [], ""
    table = await load_table()
    if _is_execute_meta(tool_name):
        slugs = unwrap_execute_slugs(tool_name, arguments)
        escriben = [s for s in slugs if table_permission(table, connector, s)[2]] if slugs else []
        if not slugs or escriben:
            _log_veto(connector, ",".join(escriben) or tool_name, CONTROLLED_TEST)
            return "deny", escriben, CONTROLLED_TEST
    v, slugs, reason = await decide_call(connector, tool_name, arguments)
    if v == "deny":
        return v, slugs, reason
    escribe = (not _is_execute_meta(tool_name)) and table_permission(table, connector, tool_name)[2]
    if escribe or v == "approve":
        if f"{connector}_{tool_name}" in (asked or set()):
            return "allow", [], ""
        _log_veto(connector, tool_name, CONTROLLED_TEST)
        return "deny", [tool_name], CONTROLLED_TEST
    return "allow", [], ""


async def veto_for_call(connector: str, tool_name: str, arguments: Any,
                        *, approved_bypass: bool = False) -> Optional[str]:
    """None = proceed; else the agent-facing veto text. For direct callers that do not
    stage + replay (shortcuts); the relay uses :func:`decide_call`."""
    if approved_bypass:
        return None
    decision, slugs, reason = await decide_call(connector, tool_name, arguments)
    if decision == "allow":
        return None
    if decision == "deny":
        return deny_message(connector, reason)
    from app.agent_locale import resolve_agent_language
    return approval_message(connector, slugs, "<pending>", reason, await resolve_agent_language())


def filter_tools_list(connector: str, tools: list[dict], table: Optional[dict]) -> list[dict]:
    """tools/list filtering: a tool the table DENIES does not exist for the agent (it would
    only waste turns trying it). ``ask`` tools stay visible — the agent must know what it can
    request approval for; the veto at call time parks them. Router meta-tools stay: the
    inner slug is judged at call time."""
    return [
        t for t in tools
        if _is_router_meta(str(t.get("name") or ""))
        or table_permission(table, connector, str(t.get("name") or ""))[0] != PERM_DENY
    ]


async def write_policy_block() -> str:
    """Prompt addendum teaching the agent the write rules — only emitted when at least one
    connector is not plain ``write`` (never tempt the agent with a mechanism that can't
    trigger). Read from the table, connector by connector."""
    table = await load_table()
    ro: list[str] = []
    ap: list[str] = []
    for c in (table or {}).get("connectors") or []:
        nombre = str(c.get("runtime_name") or c.get("name") or "")
        p = connector_policy(table, nombre)
        if p == POLICY_READ_ONLY:
            ro.append(nombre)
        elif p == POLICY_APPROVAL:
            ap.append(nombre)
    if table is None:
        ro.append("(every connector: permissions not loaded yet)")
    if not ro and not ap:
        return ""
    lines = ["\n\n[WRITE POLICY ON CONNECTORS]"]
    if ro:
        lines.append(
            "Read-only (do not try to modify anything there): " + ", ".join(sorted(ro)) + "."
        )
    if ap:
        lines.append(
            "Write WITH APPROVAL: " + ", ".join(sorted(ap)) + ". These connectors DO allow "
            "writing, but each write needs the operator's sign-off. NEVER refuse or say "
            "that you \"can't\" or that \"it is forbidden\": when your task requires a "
            "write there, INVOKE the write tool NORMALLY (like any other). The system "
            "will intercept it and return EXACT instructions on how to record the "
            "approval; record it with request_approval (action_type='tool_call') "
            "copying the action_payload EXACTLY as the rejection tells you "
            "(it includes the connector and the operation with its arguments, or the "
            "request_ref if it gives it to you). NEVER invent the payload or call "
            "request_approval without having tried the write first. Then carry on with "
            "your task; the platform will execute the action once approved and will let "
            "you know. Do not retry it yourself."
        )
    return "\n".join(lines)


def _log_veto(connector: str, tool_name: str, reason: str) -> None:
    """Structured trace of every veto (pod logs — greppable forensics:
    WHAT the agent attempted and WHY it was stopped)."""
    logger.warning("connector veto: connector=%s tool=%s reason=%s", connector, tool_name, reason)
