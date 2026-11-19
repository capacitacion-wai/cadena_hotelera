"""The employee's OWN layer of connector permissions, kept on the platform (14.8.0).

The model (contract: ``docs/permisos-de-conectores.md``): the employee inherits its agent's
permissions or personalises them with the SAME shape —a default authority plus, per
connector, a mode (``inherit`` · ``write`` · ``write_with_approval`` · ``read_only`` ·
``custom`` with per-tool rules)— and can NEVER open more than the agent. That is checked by
the platform, not here:

    PUT {WAI_BACKEND_URL}/backend/agents/{AGENT_ID}/runtime-write-policy
        {"authority": ..., "connectors": {name: {"mode", "tools"?, "reasons"?}}}
    → 422 ``EMPLEADO_ABRE_MAS`` / ``PERMISO_SIN_CAMBIO`` / … with what to fix.

The platform keeps it next to the agent and returns the result in the table this pod applies
(``mcp_policy.load_table``). The WAI-native ``wai-odoo`` gate — hosted by the engine, it never
passes through this pod's proxy — reads the same row (its ``resolved``), so ONE save governs
both doors (the INC-AGO-61/64/99 class: a local setting that never reached wai-odoo).

A copy of what the employee chose is kept in ``AppSetting mcp_employee_policy`` so the pod can
re-assert it on boot and the Conectores screen can show it.

**Old data, converted once.** Until 14.x the operator's choice lived in two local settings
(``mcp_write_policy`` = per-connector authorities, ``mcp_tool_permissions`` = an
allow/ask/deny cascade) and was pushed in the OLD shape. On the first boot of 14.8.0 they are
converted to the new shape (old per-connector policies → fixed modes; per-tool restrictions →
``custom`` rules) against the platform's table, so the employee keeps exactly the
restrictions it had — never fewer. The old settings are left untouched (image rollback) and
never read again once the new one exists.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from app import mcp_policy
from app.config import AGENT_ID, BACKEND_API_SECRET, WAI_BACKEND_URL

logger = logging.getLogger("wai.runtime_policy_sync")

EMPLOYEE_POLICY_KEY = "mcp_employee_policy"
_LEGACY_HARDENING_KEY = "mcp_write_policy"
_LEGACY_PERMISSIONS_KEY = "mcp_tool_permissions"

AUTHORITIES = ("write", "write_with_approval", "read_only")
MODES = ("inherit",) + AUTHORITIES + ("custom",)
_AUTH_RANK = {"write": 0, "write_with_approval": 1, "read_only": 2}
_AS_PERMISSION = {"write": "allow", "write_with_approval": "ask", "read_only": "deny"}
_CODE = re.compile(r"^([A-Z_]+):")


def write_fingerprint(model: str, method: str, payload: Optional[dict]) -> str:
    """sha256 hex of the canonical {model, method, payload}. MUST match the
    engine gate's fingerprint (mcp_server/tools/odoo.py::_write_fingerprint) so a
    minted grant binds to the EXACT approved operation. Keep both identical."""
    canon = json.dumps(
        {"model": model, "method": method, "payload": payload or {}},
        sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _configured() -> bool:
    return bool(WAI_BACKEND_URL and BACKEND_API_SECRET and AGENT_ID)


def _url(path: str) -> str:
    return f"{WAI_BACKEND_URL.rstrip('/')}/backend/agents/{int(AGENT_ID)}/{path}"


async def push_write_grant(connector: str, fingerprint: str, ttl_s: int = 600) -> Optional[str]:
    """Mint a SINGLE-USE, args-bound write grant in the platform so the engine's
    wai-odoo gate lets ONE approved write through (INC-AGO-100). Returns the
    ``grant_id`` on success, else None. Never raises: a failed mint just means
    the agent's re-execution re-parks (safe, not a data risk)."""
    if not _configured():
        logger.debug("write-grant mint skipped (backend bridge not configured)")
        return None
    if not connector or not fingerprint:
        return None
    body = {"connector": str(connector).strip().lower(), "fingerprint": fingerprint, "ttl_s": ttl_s}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(_url("write-grant"),
                                     headers={"X-API-Secret": BACKEND_API_SECRET}, json=body)
        if resp.status_code // 100 == 2:
            gid = (resp.json() or {}).get("grant_id")
            logger.info("write-grant minted (agent_id=%s, connector=%s, grant=%s)",
                        AGENT_ID, connector, str(gid)[:8])
            return gid
        logger.warning("write-grant mint HTTP %s: %s", resp.status_code, resp.text[:200])
    except Exception as e:  # noqa: BLE001
        logger.warning("write-grant mint failed: %s", e)
    return None


# ── The shape ────────────────────────────────────────────────────────

def _n(v: Any) -> str:
    return str(v or "").strip().lower()


def empty_policy() -> dict:
    return {"authority": "inherit", "connectors": {}}


def normalize_policy(raw: Any) -> dict:
    """The NEW shape, clean: what is not a restriction is dropped (``inherit`` entries,
    empty rules). It does not validate against the agent — that is the platform's job."""
    pol = raw if isinstance(raw, dict) else {}
    autoridad = _n(pol.get("authority")) or "inherit"
    out = {"authority": autoridad if autoridad in ("inherit",) + AUTHORITIES else "inherit",
           "connectors": {}}
    for nombre, entrada in (pol.get("connectors") or {}).items():
        if not str(nombre).strip() or not isinstance(entrada, dict):
            continue
        modo = _n(entrada.get("mode")) or "inherit"
        if modo == "inherit":
            continue
        item: dict[str, Any] = {"mode": modo}
        if modo == "custom":
            tools = {str(t): _n(p) for t, p in (entrada.get("tools") or {}).items() if str(t).strip()}
            reasons = {str(t): str(r) for t, r in (entrada.get("reasons") or {}).items()
                       if str(t) in tools and str(r or "").strip()}
            item["tools"] = tools
            if reasons:
                item["reasons"] = reasons
        out["connectors"][str(nombre)] = item
    return out


def convert_legacy(table: dict, hardening: Optional[dict], cascade: Optional[dict]) -> dict:
    """The old local settings → the NEW shape, with exactly the restrictions they imposed.

    * ``hardening`` = ``{"default": p, "connectors": {name: p}}`` (old ``mcp_write_policy``):
      the connector's value, else the default, on its WRITE tools.
    * ``cascade`` = ``{"default", "connectors", "tools"}`` (old ``mcp_tool_permissions``, a
      flat ``{tool: state}`` map is its per-tool layer): tool, else connector, else default,
      on EVERY tool.

    Per connector: if a fixed mode reproduces it, a fixed mode; otherwise ``custom`` with the
    tools that differ from what the employee would inherit. Nothing more permissive than the
    agent is produced (each target is at least the agent's permission), and nothing equal to
    what is inherited (that would be ``PERMISO_SIN_CAMBIO``). Pure."""
    h = hardening if isinstance(hardening, dict) else {}
    casc = cascade if isinstance(cascade, dict) else {}
    if casc and not ({"default", "connectors", "tools"} & set(casc)):
        casc = {"tools": casc}

    def _auth(v: Any, defecto: Optional[str]) -> Optional[str]:
        p = _n(v)
        return p if p in AUTHORITIES else defecto

    def _perm(v: Any, defecto: str) -> str:
        p = _n(v)
        return p if p in ("allow", "ask", "deny") else defecto

    agente = _auth(table.get("agent_authority"), "read_only")
    h_def = _auth(h.get("default"), "write")
    autoridad = h_def if _AUTH_RANK[h_def] > _AUTH_RANK[agente] else "inherit"
    techo_empleado = _AS_PERMISSION.get(autoridad)
    h_con = {_n(k): _auth(v, None) for k, v in (h.get("connectors") or {}).items()}
    c_def = _perm(casc.get("default"), "allow")
    c_con = {_n(k): _perm(v, "allow") for k, v in (casc.get("connectors") or {}).items()}
    c_tools = {_n(k): _perm(v, "allow") for k, v in (casc.get("tools") or {}).items()}

    conectores: dict[str, Any] = {}
    for c in table.get("connectors") or []:
        nombres = [_n(c.get("name")), _n(c.get("runtime_name"))]
        h_c = next((h_con[x] for x in nombres if h_con.get(x)), None)
        h_eff = h_c or h_def
        c_c = next((c_con[x] for x in nombres if x in c_con), c_def)
        filas = c.get("tools") or []
        objetivo: dict[str, str] = {}
        for r in filas:
            viejo = c_tools.get(_n(r.get("name")), c_tools.get(_n(r.get("key")), c_c))
            if _n(r.get("io_kind")) != "read":
                viejo = mcp_policy.most_restrictive(viejo, _AS_PERMISSION[h_eff])
            objetivo[r["name"]] = mcp_policy.most_restrictive(
                mcp_policy._perm(r.get("agent_permission")), viejo)

        def heredado(r: dict, modo: Optional[str]) -> str:
            base = mcp_policy._perm(r.get("agent_permission"))
            if _n(r.get("io_kind")) != "read":
                techo = _AS_PERMISSION[modo] if modo else techo_empleado
                if techo:
                    base = mcp_policy.most_restrictive(base, techo)
            return base

        techo_c = _auth(c.get("authority"), agente)
        modo = h_c if h_c and _AUTH_RANK[h_c] >= _AUTH_RANK[techo_c] else None
        if all(objetivo[r["name"]] == heredado(r, modo) for r in filas):
            if modo and modo != (autoridad if autoridad != "inherit" else techo_c):
                conectores[str(c.get("name"))] = {"mode": modo}
            continue
        reglas = {r["name"]: objetivo[r["name"]] for r in filas
                  if objetivo[r["name"]] != heredado(r, None)}
        if reglas:
            conectores[str(c.get("name"))] = {"mode": "custom", "tools": reglas}
    return {"authority": autoridad, "connectors": conectores}


# ── Local copy ───────────────────────────────────────────────────────

async def _read_setting(key: str) -> Optional[Any]:
    from sqlalchemy import select
    from app.app_database import app_session
    from app.app_models.runtime import AppSetting
    async with app_session() as s:
        v = (await s.execute(select(AppSetting.value).where(AppSetting.key == key))).scalar_one_or_none()
    if not v:
        return None
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return None


async def load_employee_policy() -> Optional[dict]:
    """What this employee chose (new shape), or None if it never saved one in 14.8.0."""
    raw = await _read_setting(EMPLOYEE_POLICY_KEY)
    return normalize_policy(raw) if isinstance(raw, dict) else None


async def load_legacy() -> Optional[tuple[dict, dict]]:
    """The two old local settings, or None when the employee never touched either."""
    h = await _read_setting(_LEGACY_HARDENING_KEY)
    c = await _read_setting(_LEGACY_PERMISSIONS_KEY)
    if not isinstance(h, dict) and not isinstance(c, dict):
        return None
    return (h if isinstance(h, dict) else {}), (c if isinstance(c, dict) else {})


async def save_employee_policy(policy: dict, who: str) -> None:
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    from app.app_database import app_write
    from app.app_models.runtime import AppSetting
    values = {"key": EMPLOYEE_POLICY_KEY, "value": json.dumps(policy, sort_keys=True),
              "updated_by": who or None, "updated_at": datetime.now(timezone.utc)}
    stmt = sqlite_insert(AppSetting).values(**values).on_conflict_do_update(
        index_elements=[AppSetting.key],
        set_={k: v for k, v in values.items() if k != "key"},
    )
    async with app_write() as s:
        await s.execute(stmt)
        await s.commit()


async def current_policy(table: Optional[dict]) -> tuple[dict, bool]:
    """``(policy, converted)``: the saved choice, or —before the first save in 14.8.0— the
    old settings converted (``converted=True``, not saved yet), or the empty one."""
    saved = await load_employee_policy()
    if saved is not None:
        return saved, False
    legacy = await load_legacy()
    if legacy is not None and table is not None:
        return convert_legacy(table, *legacy), True
    return empty_policy(), False


# ── The platform ─────────────────────────────────────────────────────

def rejection(status: int, body: Any) -> dict:
    """``{"code", "message"}`` of a platform rejection (``detail`` = ``"CODE: message"``)."""
    detail = body.get("detail") if isinstance(body, dict) else body
    texto = str(detail or "")
    m = _CODE.match(texto)
    return {"code": m.group(1) if m else ("HTTP_%s" % status), "message": texto}


async def put_employee_policy(policy: dict) -> tuple[int, Any]:
    """PUT the new shape. ``(status, body)``; ``status`` 0 when the platform is unreachable."""
    if not _configured():
        return 0, {"detail": "backend bridge not configured"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.put(_url("runtime-write-policy"),
                                    headers={"X-API-Secret": BACKEND_API_SECRET}, json=policy)
        try:
            body = resp.json()
        except ValueError:
            body = {"detail": resp.text[:300]}
        return resp.status_code, body
    except Exception as e:  # noqa: BLE001
        logger.warning("employee policy PUT failed: %s", e)
        return 0, {"detail": str(e)}


async def dry_run(policy: dict, version_id: Optional[int]) -> tuple[int, Any]:
    """The platform's ensayo with this draft as the employee layer: ``{valid, errors, table}``
    (always 200 when reachable). The screen never calculates on its own."""
    if not _configured():
        return 0, {"detail": "backend bridge not configured"}
    body: dict[str, Any] = {"employee": policy}
    if version_id:
        body["version_id"] = int(version_id)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(_url("permissions/dry-run"),
                                     headers={"X-API-Secret": BACKEND_API_SECRET}, json=body)
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, {"detail": resp.text[:300]}
    except Exception as e:  # noqa: BLE001
        logger.warning("permissions dry-run failed: %s", e)
        return 0, {"detail": str(e)}


async def push_local_write_policy() -> bool:
    """On boot: hand the platform this employee's layer in the NEW shape.

    First boot on 14.8.0 with old local settings → convert them once (against the platform's
    table) and save the result as the new setting. Afterwards → re-assert the saved one (the
    platform row may have been lost or written by an older image). A rejection is logged and
    nothing is widened: the platform keeps whatever row it had, and the table is always
    ``most_restrictive(agent, employee)``. Returns True when the platform holds our policy
    (or there is nothing to hold). Never raises."""
    if not _configured():
        logger.debug("employee policy sync skipped (WAI_BACKEND_URL/BACKEND_API_SECRET/AGENT_ID missing)")
        return False
    try:
        policy = await load_employee_policy()
        migrating = False
        if policy is None:
            legacy = await load_legacy()
            if legacy is None:
                await mcp_policy.load_table(force=True)
                return True
            table = await mcp_policy.load_table(force=True)
            if table is None:
                logger.error("cannot convert the old connector permissions: no table from the platform yet")
                return False
            policy = convert_legacy(table, *legacy)
            migrating = True
        status, body = await put_employee_policy(policy)
        if status // 100 == 2:
            if migrating:
                await save_employee_policy(policy, "skill 14.8.0")
                logger.info("old connector permissions converted to the new shape: %s", policy)
            mcp_policy.invalidate_table()
            await mcp_policy.load_table(force=True)
            return True
        logger.error("the platform rejected this employee's permissions (HTTP %s): %s",
                     status, rejection(status, body))
    except Exception:  # noqa: BLE001
        logger.warning("employee policy sync failed", exc_info=True)
    return False
