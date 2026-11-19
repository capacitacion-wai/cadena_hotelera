"""External MCP connector authentication endpoints.

Surfaces the agent's external (Composio-backed) MCP servers — Odoo, Gmail,
… — in the Operations → Conectores tab so an operator can authenticate the
AUTONOMOUS user's account without leaving the app. The connection is keyed
by the same ``{tenant}:{autonomous_email}`` identity the engine uses for
``/mcp/odoo``, so what the operator connects here is exactly what the agent
uses at runtime.

Routes (all under ``/api/agent``):
  GET   /mcp-connectors                         list connectors + status
  POST  /mcp-connectors/{toolkit}/connect       credentials OR oauth url
  POST  /mcp-connectors/{toolkit}/disconnect    revoke the account
  POST  /mcp-connectors/{toolkit}/activate      apply a just-completed connection NOW
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app import agent_proxy
from app import mcp_connectors as mc
from app import mcp_runtime
from app.agent_proxy import AUTONOMOUS_USER_EMAIL, get_agent_proxy
from app.app_database import app_session, app_write
from app.app_models.mcp_connectors import McpConnector
from app.app_models.runtime import AppSetting
from app.auth import require_project_access, require_admin, TokenPayload
from app.config import WAI_APP_JSON_PATH

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent", tags=["agent"])

# App-wide Composio execution mode (Operations → Conectores toggle).

# Static fallback tool catalogue per toolkit, used ONLY when the live
# probe to the sidecar returns nothing (e.g. the OpenCode native server
# is briefly unavailable). Keeps the editor usable for the WAI-native
# connectors whose tool names are stable and known. Names MUST match the
# tool names the runtime enforces (the ``wai-impersonation`` plugin sees).
_STATIC_TOOLS: dict[str, list[dict[str, str]]] = {
    "odoo": [
        {"name": "wai-odoo_search_read", "description": "Read records (search + read in one call)."},
        {"name": "wai-odoo_read", "description": "Read-only ORM methods (read, fields_get, search_count, aggregations)."},
        {"name": "wai-odoo_write", "description": "Mutating ORM methods (create, write, unlink, action_*/button_*)."},
    ],
}


def _manifest_mcps() -> list[dict[str, Any]]:
    try:
        cfg = json.loads(Path(WAI_APP_JSON_PATH).read_text())
    except Exception:  # noqa: BLE001 — manifest optional / malformed
        return []
    agent_app = cfg.get("agent_app", cfg) if isinstance(cfg, dict) else {}
    out: list[dict[str, Any]] = []
    for mcp in agent_app.get("mcps", []) or []:
        if isinstance(mcp, dict):
            out.append(mcp)
        elif isinstance(mcp, str):
            out.append({"name": mcp})
    return out


def _composio_connectors() -> list[dict]:
    """Project the manifest's external (Composio) MCP servers to
    ``{name, toolkit, display_name}``. A server is "external" when it
    declares ``auth_type == "composio"`` or its name uses the
    ``composio-<toolkit>`` convention.

    Dedup is by ``name`` (the unique connector key), NOT by ``toolkit`` —
    entity-per-connector means two connectors of the SAME toolkit (e.g. a 2nd
    "Kommo") are distinct accounts and BOTH must be listed/authenticable. This
    MUST mirror ``mcp_runtime.composio_connectors()`` (which also dedups by
    name): if this side dropped the 2nd same-toolkit connector, the runtime
    would still resolve it as a permanently-disabled bridge that connect/status
    here would 404 on — i.e. an unfixable "not connected". Odoo is kept here
    (unlike the runtime, which serves it via WAI-native ``wai-odoo``) so the
    operator can still authorise the Odoo account from the Conectores tab; it is
    authorised under the AGENT identity (``#odoo`` slug, see
    ``_identity_connector``) so the engine's ``wai-odoo`` resolves that same
    account instead of the owner's shared one.
    """
    from app.mcp_runtime import fichas_de_conectores

    fichas = fichas_de_conectores()
    seen: set[str] = set()
    connectors: list[dict] = []
    for mcp in _manifest_mcps():
        name = (mcp.get("name") or "").strip()
        if not name or name in seen:
            continue
        ficha = fichas.get(name) or {}
        auth_type = ((ficha.get("auth_type") or mcp.get("auth_type") or "")
                     .strip().lower())
        toolkit = ((ficha.get("composio_toolkit") or mcp.get("composio_toolkit") or "")
                   .strip().lower())
        is_composio = auth_type == "composio" or name.startswith("composio-")
        # A connector built in WAI does NOT go here: it is listed by `custom_connectors()`,
        # which already existed and already told "with its own key" apart. Adding it here too
        # painted it TWICE —one of them blowing up over an empty `toolkit` the card requires—
        # and it is the usual mistake: touching one piece without looking at the one next to it.
        if not is_composio:
            continue
        if not toolkit and name.startswith("composio-"):
            toolkit = name[len("composio-"):]   # catalog convention, not a deduction
        toolkit = toolkit.strip().lower()
        if not toolkit:
            logger.warning("El conector «%s» no dice a qué toolkit pertenece; no se ofrece "
                           "conectarlo hasta que su ficha en la plataforma lo diga", name)
            continue
        seen.add(name)
        display = (
            (mcp.get("display_name") or "").strip()
            or toolkit.replace("-", " ").replace("_", " ").title()
        )
        # The id of its card in the catalog: with it the platform is asked for THIS employee's
        # OWN configuration (its OAuth app, its key) and it is connected another way. It was
        # missing here —the runtime did have it—, and the "with API key" and "use its app" buttons
        # answered "not in the catalog" about a connector that is (24/09/2026, 12.59.0).
        connectors.append({"name": name, "toolkit": toolkit, "display_name": display,
                           "mcp_server_id": ficha.get("id")})
    return connectors


def _ui_mode(connection_mode: str) -> str:
    """Collapse the toolkit ``connection_mode`` into what the connect endpoint
    does: ``oauth`` (redirect or custom_oauth → popup) | ``credentials`` (form)
    | ``none``."""
    cm = (connection_mode or "").strip().lower()
    if cm in ("redirect", "custom_oauth"):
        return "oauth"
    if cm == "none":
        return "none"
    return "credentials"


async def _mark_auth_config_registered(conn: dict, identity_connector: str) -> None:
    """Records that the registration was done FROM THIS employee.

    It is a TRACE, not a source of truth: who decides whether the OAuth app is registered
    is Composio (via `auth_info`). This mark only serves to know, looking at the app, that
    the operator came through here. Do not use it to decide anything — doing so was the bug
    that made a new employee ask for credentials of an app that already existed.
    """
    values = {
        "connector_name": conn["name"],
        "toolkit": conn["toolkit"],
        "composio_user_id": identity_connector or None,
        "auth_config_registered_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    stmt = sqlite_insert(McpConnector).values(**values).on_conflict_do_update(
        index_elements=[McpConnector.connector_name],
        set_={k: v for k, v in values.items() if k != "connector_name"},
    )
    async with app_write() as s:
        await s.execute(stmt)
        await s.commit()


@router.get("/mcp-connectors")
async def list_mcp_connectors(
    user: TokenPayload = Depends(require_project_access),
):
    """List the agent's external MCP connectors with their auth status for
    the autonomous user. Each entry tells the SPA how to authenticate it
    (a credential form or an OAuth popup)."""
    connectors = _composio_connectors()
    enabled = mc.is_enabled()
    autonomous = (AUTONOMOUS_USER_EMAIL or "").strip().lower()

    payload: dict[str, Any] = {
        "available": enabled and bool(autonomous),
        "autonomous_user_email": autonomous or None,
        # Composio connectors ALWAYS come in through their single gate (the meta-tools
        # router). The switch that allowed a per-connector bridge was retired: it was the
        # legacy path and keeping it alive meant maintaining two paths, one of which almost
        # nobody exercised — and the branch nobody exercises fails the day it is needed.
        "connectors": [],
    }
    payload["error"] = None

    # The Composio loop only makes sense if there are connectors of it. Here there was a
    # `return` that took down EVERYTHING below —including the connectors built in WAI,
    # which are not Composio's and therefore do not count for that list—: an employee with
    # only an own connector saw the tab empty.

    # Status is checked PER CONNECTOR under its own identity (entity-per-
    # connector), so a connector reported "connected" here is exactly the
    # account the runtime resolves — no more false positives from a shared
    # identity. ``account`` is the operator-visible external account.
    from app.mcp_policy import effective_policy
    # THIS employee's OWN configurations (its OAuth app or its API key), in one go: the
    # screen says in which way each connector is connected.
    _apps = await mc.apps_propias() if connectors else {}
    for conn in connectors:
        toolkit = conn["toolkit"]
        identity_connector = _identity_connector(conn)
        entry: dict[str, Any] = {
            "name": conn["name"],
            "toolkit": toolkit,
            "display_name": conn["display_name"],
            # v7.28.0 — EFFECTIVE write-policy (platform value hardened by the
            # local setting): what the app-pod policy proxy actually enforces.
            "write_policy": await effective_policy(conn["name"]),
            "auth_scheme": None,
            "connection_mode": None,
            "managed_oauth_available": None,
            "mode": "credentials",
            # Operator-once fields (OAuth app client_id/secret) and per-user
            # fields (api key, instance/subdomain), kept apart so the UI shows
            # the right form to the right actor. ``fields`` kept for back-compat
            # (= connection_fields).
            "auth_config_fields": [],
            "connection_fields": [],
            "fields": [],
            "redirect_uri": "",
            "auth_config_registered": False,
            "connected": False,
            "status": None,
            "connected_account_id": None,
            "account": None,
        }
        if payload["available"]:
            try:
                connections = await mc.list_connections(autonomous, identity_connector)
                account = mc.connected_account_for(connections, toolkit)
                if account is not None:
                    status = (account.get("status") or "").upper()
                    entry["connected"] = status == "ACTIVE"
                    entry["status"] = status or None
                    entry["connected_account_id"] = account.get("id") or None
                    entry["account"] = mcp_runtime._account_email(account)
            except mc.McpConnectorError as exc:
                logger.warning("list_connections(%s) failed: %s", conn["name"], exc)
                entry["auth_error"] = str(exc)
            try:
                info = await mc.auth_info(toolkit)
                cm = info.get("connection_mode") or "redirect"
                entry["auth_scheme"] = info.get("auth_scheme")
                entry["connection_mode"] = cm
                entry["managed_oauth_available"] = info.get("managed_oauth_available")
                entry["mode"] = _ui_mode(cm)
                entry["auth_config_fields"] = info.get("auth_config_fields") or []
                # What Composio asks for PLUS what this connector declares it needs. Composio's
                # form was designed for ITS way of calling; when the system is reached through
                # a translator of ours data is missing (Odoo 17/18 over XML-RPC lacks the user)
                # and whoever knows it is the card, not this screen.
                from app.mcp_runtime import extras_de_conexion
                extras = extras_de_conexion(conn["name"])
                campos = list(info.get("connection_fields") or [])
                ya = {(c or {}).get("name") for c in campos}
                campos += [c for c in (extras.get("extra_fields") or [])
                           if isinstance(c, dict) and c.get("name") not in ya]
                entry["connection_fields"] = campos
                entry["fields"] = campos
                # Redirect/callback URI to register in the provider's OAuth app
                # (custom_oauth) — shown read-only so the operator never types it.
                entry["redirect_uri"] = info.get("redirect_uri") or ""
                # custom_oauth needs the operator's OAuth app registered before
                # anyone can connect; tell the UI whether that's done.
                #
                # The truth is held by COMPOSIO, not by our SQLite. Before, only the local
                # table (`auth_config_registered_at`) was looked at, which is only filled if
                # the registration was done FROM THIS employee — so an employee just deployed
                # in a tenant where the connector had been registered for months believed it
                # was not and asked for the client_id/client_secret of an OAuth app that
                # already existed. It happened to "Nora" with Gmail on 2026-08-07: the operator
                # expected the authorize button and found a developer form. The registration
                # belongs to the TENANT, not to the employee.
                #
                # auth-service reports it since 0.40; the local table stays as a fallback so as
                # not to depend on a specific deployment of the service.
                entry["auth_config_registered"] = (
                    cm != "custom_oauth" or bool(info.get("auth_config_registered"))
                )
                # The OTHER ways of connecting that go with credentials. Google Maps through
                # Composio's app shares quota with all its customers and runs out without
                # anyone here having done anything; with an API key of the customer's Google
                # Cloud project, the quota is theirs. The screen offers it when connecting.
                entry["formas_alternativas"] = [
                    {"auth_scheme": f.get("auth_scheme"),
                     "connection_fields": f.get("connection_fields") or []}
                    for f in (info.get("formas") or [])
                    if f.get("connection_mode") == "credentials"
                    and f.get("auth_scheme") != info.get("auth_scheme")
                ]
                # In which way it is connected TODAY, if it is not the main one.
                propia = _apps.get(str(conn.get("mcp_server_id") or ""), "")
                entry["forma_propia"] = (await mc.forma_de_config(
                    propia, mc.composio_identity(autonomous, identity_connector))
                    if propia else "")
            except mc.McpConnectorError as exc:
                logger.warning("auth_info(%s) failed: %s", toolkit, exc)
                entry["auth_error"] = str(exc)
        payload["connectors"].append(entry)

    # v7.135.0 — CUSTOM (non-Composio) MCPs the agent uses (e.g. cluster-mcp):
    # no per-user auth (wired to a static URL/token at deploy), so surface them
    # as "connected · no authentication" with a governable write-policy, instead
    # of hiding a connector the agent actually depends on.
    from app.mcp_runtime import custom_connectors as _custom_connectors_rt
    # Fresh: whoever looks at this screen has just connected.
    for conn in _custom_connectors_rt(fresco=True):
        payload["connectors"].append({
            "name": conn["name"],
            "toolkit": conn["toolkit"],
            "display_name": conn["display_name"],
            "write_policy": await effective_policy(conn["name"]),
            "auth_scheme": None,
            # Three different situations, and confusing them is what made a keyless
            # connector announce itself as ready:
            #   · "authenticates against nothing" — an internal MCP;
            #   · "has its own key, set at deploy time" — nothing to do here;
            #   · "needs SOMEONE to set a key for it" — a connector built in WAI, since the
            #     key stopped travelling in the catalog card. That one IS connected, and until
            #     it is done it does not answer.
            "connection_mode": "credentials" if conn.get("necesita_llave") else "none",
            "credential_kind": conn.get("credencial", "ninguna"),
            "managed_oauth_available": None,
            "mode": "credentials",
            "auth_config_fields": [],
            "connection_fields": ([{
                "name": "access_key",
                "displayName": "Llave del conector",
                "type": "password",
                # Optional: empty, the platform issues a key for THIS employee, just like
                # «Conectar» on the platform. Requiring it was asking for a step nobody knew:
                # going to the connector's card, issuing it and pasting it back (Merkalia, 25/09).
                "required": False,
                "description": ("Déjala vacía y la plataforma genera una llave para este "
                                "empleado. Si te han dado una, pégala aquí."),
            }] if conn.get("necesita_llave") else []),
            "fields": [],
            "redirect_uri": "",
            "auth_config_registered": True,
            # The platform says it, which is the only one that knows who has connected what.
            "connected": bool(conn.get("conectado", True)),
            "status": "ACTIVE" if conn.get("conectado", True) else "PENDING",
            "connected_account_id": None,
            "account": None,
            "custom": True,
        })
    return payload


def _identity_connector(conn: dict) -> str:
    """Per-connector Composio identity segment (the ``#…`` part of the entity). The rule lives
    in ``config.segmento_de_identidad``, the SAME one the runtime uses when resolving."""
    from app.config import segmento_de_identidad
    return segmento_de_identidad(conn)


async def _avisar_al_agente(connector: str) -> bool:
    """One `POST /v1/mcp/recheck` to the agent's pod; True if it took it."""
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(8.0, connect=4.0),
            auth=httpx.BasicAuth("opencode", agent_proxy.AGENT_SHARED_TOKEN),
        ) as client:
            r = await client.post(agent_proxy.SIMPLE_BASE_URL + "/v1/mcp/recheck")
            return r.status_code < 300
    except Exception as exc:  # noqa: BLE001 — the pod may be restarting
        logger.info("activate(%s): agent recheck nudge unreachable (%s)", connector, type(exc).__name__)
        return False


async def _insistir_al_agente(connector: str, cada_s: float = 5.0, durante_s: float = 120.0) -> None:
    """Retry the nudge until the agent's pod answers (it was restarting) or time runs out."""
    import asyncio as _asyncio
    esperado = 0.0
    while esperado < durante_s:
        await _asyncio.sleep(cada_s)
        esperado += cada_s
        if await _avisar_al_agente(connector):
            logger.info("activate(%s): agent recheck nudge delivered after %ss", connector, int(esperado))
            return
    logger.warning("activate(%s): agent recheck nudge not delivered in %ss — the periodic refresher applies it", connector, int(durante_s))


def _require_known_connector(connector: str) -> dict:
    """Resolve a connector by its unique manifest name.

    It looks at BOTH sources —Composio's and the ones built in WAI—, because connecting
    exists for both even if through different paths. Looking only at the first, connecting
    an own connector gave a 404 "unknown" about something the screen had just listed."""
    slug = (connector or "").strip()
    for c in _composio_connectors():
        if c["name"] == slug:
            return c
    from app.mcp_runtime import custom_connectors  # noqa: PLC0415
    for c in custom_connectors():
        if c["name"] == slug and c.get("necesita_llave"):
            return c
    raise HTTPException(404, f"Unknown MCP connector: {connector}")


async def _conectar_conector_propio(conn: dict, body: dict) -> dict:
    """Stores an own connector's key in the name of THIS employee.

    The platform checks it by calling the connector before storing it, so a wrong key is
    discovered here and not when the employee tries to use it in the early hours."""
    llave = ""
    creds = body.get("credentials") or {}
    for campo in ("access_key", "api_key", "apiKey", "key"):
        llave = str(creds.get(campo) or body.get(campo) or "").strip()
        if llave:
            break
    import httpx  # noqa: PLC0415
    from app.config import AGENT_ID, BACKEND_API_SECRET, WAI_BACKEND_URL  # noqa: PLC0415
    if not (WAI_BACKEND_URL and BACKEND_API_SECRET and AGENT_ID):
        raise HTTPException(503, "Este empleado no sabe hablar con la plataforma "
                                 "(falta WAI_BACKEND_URL, su secreto o su AGENT_ID).")
    url = (f"{WAI_BACKEND_URL.rstrip('/')}/backend/mcp-servers/"
           f"{conn['wai_mcp_server_id']}/connect")
    # Without a key, the platform ISSUES one and waits for the connector to recognize it:
    # storing its hash restarts the connector's pod, and that takes up to a minute and a half.
    async with httpx.AsyncClient(timeout=45.0 if llave else 150.0) as client:
        # The owner of the connection is the AGENT, which is the identity this employee is
        # known by on the platform —the same one it asks for its cards with—, not the person
        # pasting the key. It is what makes it keep working when that person is no longer there.
        r = await client.post(url, json={"access_key": llave, "agent_app_id": int(AGENT_ID)},
                              headers={"X-API-Secret": BACKEND_API_SECRET})
    if r.status_code >= 400:
        try:
            detalle = (r.json() or {}).get("detail") or r.text[:200]
        except Exception:  # noqa: BLE001
            detalle = r.text[:200]
        raise HTTPException(400, detalle or "No se pudo conectar.")
    # The cached card still says "not connected": if it is not forgotten, the screen comes
    # back from connecting and stays the same for up to a minute — exactly the while in which
    # one concludes it did not work and repeats it.
    from app.mcp_runtime import olvidar_fichas  # noqa: PLC0415
    olvidar_fichas()
    return {"status": "connected", "connector": conn["name"]}


@router.post("/mcp-connectors/{connector}/connect")
async def connect_mcp_connector(
    connector: str,
    body: dict[str, Any] = Body(default={}),
    user: TokenPayload = Depends(require_admin),
):
    """Connect / change the account of a connector (`_connect`), recorded in the connection history
    (`connection_events`, 14.27.0): who, what, the non-secret account data and how it ended."""
    from app import connection_events as ce
    body = body or {}
    accion = "change_account" if body.get("change_account") else "connect"
    try:
        r = await _connect(connector, body, user)
    except HTTPException as exc:
        await ce.record(connector, accion, getattr(user, "email", ""), ce.account_of(body.get("credentials")), "error",
                        str(exc.detail))
        raise
    if isinstance(r, dict) and r.get("authorize_url"):
        # OAuth: the person goes to the provider; the connection is recorded as «activate» when it comes back.
        await ce.record(connector, "authorize", getattr(user, "email", ""), None, "started")
    else:
        conectada = bool((r or {}).get("connected", True)) if isinstance(r, dict) else True
        await ce.record(connector, accion, getattr(user, "email", ""),
                        ce.account_of(body.get("credentials"),
                                      connected_account_id=(r or {}).get("connected_account_id")
                                      if isinstance(r, dict) else None),
                        "ok" if conectada else str((r or {}).get("status") or "not_connected"))
    return r


async def _connect(
    connector: str,
    body: dict[str, Any],
    user: TokenPayload,
):
    """Authenticate (or change the account of) ONE external MCP connector,
    keyed by its unique manifest name. The connection is created under the
    connector's own Composio identity (entity-per-connector), so two
    connectors — even of the same toolkit — bind to independent accounts.

    * credential toolkits (Odoo, …): send ``{"credentials": {...}}`` and the
      account is created synchronously.
    * OAuth toolkits: returns ``{"authorize_url": "..."}`` — the SPA opens
      it in a popup and re-polls the list afterwards.
    * change account: send ``{"change_account": true}`` to revoke the current
      account first and force provider account selection.
    """
    conn = _require_known_connector(connector)
    # Connector built in WAI: its key is stored by THE PLATFORM, in the name of this employee
    # —not of the person pasting it—, so it keeps working the day that person is no longer
    # there. Composio does not intervene: it does not even know this connector exists.
    if conn.get("wai_mcp_server_id"):
        return await _conectar_conector_propio(conn, body or {})
    toolkit = conn["toolkit"]
    identity_connector = _identity_connector(conn)
    if not mc.is_enabled():
        raise HTTPException(503, "auth-service not configured")
    autonomous = (AUTONOMOUS_USER_EMAIL or "").strip().lower()
    if not autonomous:
        raise HTTPException(503, "autonomous user identity is not configured")

    if body.get("change_account"):
        # Revoke the current account so the provider cannot silently reuse the
        # logged-in session; the OAuth URL below is built with force.
        try:
            await mc.disconnect_toolkit(autonomous, toolkit, identity_connector)
        except mc.McpConnectorError as exc:
            logger.warning("change_account disconnect(%s) failed: %s", connector, exc)

    try:
        info = await mc.auth_info(toolkit)
    except mc.McpConnectorError as exc:
        raise HTTPException(502, str(exc))
    cm = info.get("connection_mode") or "redirect"
    mode = _ui_mode(cm)

    # A WAY that is not the main one (the Google Maps API key): the connection stays with a
    # configuration of THIS employee, which the platform records and which later opens its
    # gate. It goes before anything else because that toolkit's main one is OAuth, and
    # carrying on below would open the provider's window.
    forma = str(body.get("forma") or "").strip().upper()
    if forma and forma != str(info.get("auth_scheme") or "").upper():
        credentials = body.get("credentials")
        if not isinstance(credentials, dict) or not credentials:
            raise HTTPException(422, "credentials are required for this form")
        if not conn.get("mcp_server_id"):
            raise HTTPException(409, "Este conector no está en el catálogo de la plataforma.")
        try:
            result = await mc.conectar_con_forma(
                conn["mcp_server_id"], forma, credentials,
                mc.composio_identity(autonomous, identity_connector),
            )
        except mc.McpConnectorError as exc:
            raise HTTPException(502, str(exc))
        return {
            "mode": "credentials",
            "forma": result.get("forma") or forma,
            "connected": (result.get("status") or "").upper() == "ACTIVE",
            "status": result.get("status"),
            "connected_account_id": result.get("connected_account_id"),
            # Composio does not check it when connecting: the first call will say if it is valid.
            "credencial_comprobada": False,
        }

    if mode == "oauth":
        # If it now uses a CREDENTIAL of its own (a key), the provider's window cannot be
        # opened with it: choosing the account means stopping using it first.
        # Only if the way is KNOWN: a "" (auth-service without the query, or no answer) is
        # not "it is a key", and removing an own OAuth app because of that would break a
        # connector that works.
        _propia = (await mc.apps_propias()).get(str(conn.get("mcp_server_id") or ""), "")
        _esquema = (await mc.forma_de_config(
            _propia, mc.composio_identity(autonomous, identity_connector))
            if _propia else "")
        if _esquema and not mc.es_forma_oauth(_esquema):
            try:
                await mc.quitar_config_propia(conn.get("mcp_server_id"))
            except mc.McpConnectorError as exc:
                raise HTTPException(502, str(exc))
        # custom_oauth (no Composio-managed app): the operator's OAuth app must
        # be registered first, or the Composio flow 400s on a missing client_id.
        # Same fact, same source as the listing: whatever Composio says.
        if cm == "custom_oauth" and not info.get("auth_config_registered"):
            raise HTTPException(
                409,
                f"Connector '{connector}' needs its OAuth app credentials "
                f"registered first (POST "
                f"/api/agent/mcp-connectors/{connector}/auth-config).",
            )
        cf = body.get("connection_fields")
        connection_fields = cf if isinstance(cf, dict) else None
        try:
            # With THIS employee's application if it has its own; if not, the space's. It has
            # to be the SAME one that later opens its gate, or Composio rejects the account
            # (4306) and the employee is left without those tools without saying anything.
            _apps = await mc.apps_propias()
            url = mc.oauth_start_url(
                toolkit,
                autonomous,
                connector=identity_connector,
                return_origin=(body.get("return_origin") or "").strip(),
                server_name=conn["display_name"],
                force=bool(body.get("force") or body.get("change_account")),
                connection_fields=connection_fields,
                auth_config_id=_apps.get(str(conn.get("mcp_server_id") or ""), ""),
            )
        except mc.McpConnectorError as exc:
            raise HTTPException(502, str(exc))
        return {"mode": "oauth", "authorize_url": url}

    if mode == "none":
        return {"mode": "none", "connected": True, "status": "ACTIVE"}

    credentials = body.get("credentials")
    if not isinstance(credentials, dict) or not credentials:
        raise HTTPException(422, "credentials are required for this connector")
    # Same rule that paints the form, applied when sending it: if the system is reached
    # through a translator, the "instance" Composio registers is the translator's and the
    # customer's goes inside. It lives in `mcp_runtime` so there are not two versions of this.
    from app.mcp_runtime import componer_credenciales
    try:
        credentials = componer_credenciales(connector, credentials)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    try:
        result = await mc.connect_credentials(
            autonomous, toolkit, credentials, identity_connector,
        )
    except mc.McpConnectorError as exc:
        raise HTTPException(502, str(exc))
    return {
        "mode": "credentials",
        "connected": (result.get("status") or "").upper() == "ACTIVE",
        "status": result.get("status"),
        "connected_account_id": result.get("connected_account_id"),
    }


@router.post("/mcp-connectors/{connector}/auth-config")
async def register_connector_auth_config(
    connector: str,
    body: dict[str, Any] = Body(default={}),
    user: TokenPayload = Depends(require_admin),
):
    """Register (operator-once) the OAuth app credentials for a ``custom_oauth``
    connector — the toolkit's own ``client_id``/``client_secret`` (Composio has
    no managed app for it, e.g. Kommo/Salesforce). After this the OAuth popup
    works for any account. Idempotent.

    Send ``{"replace": true}`` to overwrite credentials registered earlier with
    wrong values — the backend deletes the stale auth_config and recreates it.
    Account/tenant-wide (the OAuth app is operator-once)."""
    conn = _require_known_connector(connector)
    if conn.get("wai_mcp_server_id"):
        raise HTTPException(400, "Un conector construido en WAI no usa la app OAuth de nadie: se conecta pegando una llave suya.")
    toolkit = conn["toolkit"]
    identity_connector = _identity_connector(conn)
    if not mc.is_enabled():
        raise HTTPException(503, "auth-service not configured")
    credentials = body.get("credentials")
    if not isinstance(credentials, dict) or not credentials:
        raise HTTPException(422, "credentials (client_id/client_secret) are required")
    replace = bool(body.get("replace"))
    try:
        auth_config_id = await mc.register_auth_config(toolkit, credentials, replace)
    except mc.McpConnectorError as exc:
        raise HTTPException(502, str(exc))
    await _mark_auth_config_registered(conn, identity_connector)
    return {"registered": True, "auth_config_id": auth_config_id}


async def _desconectar_conector_propio(conn: dict) -> dict:
    """Removes THIS employee's connection with an own connector. The key is not revoked: it
    belongs to the connector and may be in use by others; what is removed is this one using it."""
    import httpx  # noqa: PLC0415
    from app.config import AGENT_ID, BACKEND_API_SECRET, WAI_BACKEND_URL  # noqa: PLC0415
    url = (f"{WAI_BACKEND_URL.rstrip('/')}/backend/mcp-servers/"
           f"{conn['wai_mcp_server_id']}/disconnect")
    async with httpx.AsyncClient(timeout=30.0) as client:
        await client.post(url, json={"agent_app_id": int(AGENT_ID)},
                          headers={"X-API-Secret": BACKEND_API_SECRET})
    from app.mcp_runtime import olvidar_fichas  # noqa: PLC0415
    olvidar_fichas()
    return {"status": "disconnected", "connector": conn["name"]}


@router.put("/mcp-connectors/{connector}/own-auth-config")
async def registrar_app_propia(
    connector: str,
    body: dict[str, Any] = Body(...),
    user: TokenPayload = Depends(require_admin),
):
    """Registers THIS employee's OAuth application for a connector.

    The application identifies the program asking for permission, not the person, so the
    norm is one for the whole space. This lets this employee use its own without touching
    anyone else's: useful when the organization requires its own application or wants its
    own usage limits.

    Body: ``{"credentials": {"client_id": "...", "client_secret": "..."}}``.

    It does not replace the space's — that would take down everyone else's connections. And
    if the connector was already connected, it has to be disconnected and reconnected: the
    previous account stayed tied to the previous application and its gate will reject it.
    """
    from app.config import AGENT_ID, BACKEND_API_SECRET, WAI_BACKEND_URL

    conn = _require_known_connector(connector)
    sid = conn.get("mcp_server_id")
    if not sid:
        raise HTTPException(400, f"El conector «{connector}» no está en el catálogo.")
    credentials = body.get("credentials")
    if not isinstance(credentials, dict) or not credentials.get("client_id"):
        raise HTTPException(400, "Hacen falta las credenciales de la aplicación OAuth.")
    if not (WAI_BACKEND_URL and BACKEND_API_SECRET and AGENT_ID):
        raise HTTPException(503, "La plataforma no está accesible desde aquí.")

    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.put(
            f"{WAI_BACKEND_URL.rstrip('/')}/backend/mcp-servers/{int(sid)}/own-auth-config",
            json={"credentials": credentials, "agent_app_id": int(AGENT_ID)},
            headers={"X-API-Secret": BACKEND_API_SECRET},
        )
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text[:300])
    await mc.apps_propias(fresco=True)   # so the next start already uses the new one
    logger.info("aplicación OAuth propia registrada para %s (por %s)",
                connector, getattr(user, "email", "?"))
    return r.json()


@router.delete("/mcp-connectors/{connector}/own-auth-config")
async def quitar_app_propia(
    connector: str,
    user: TokenPayload = Depends(require_admin),
):
    """Goes back to the space's application.

    It deletes nothing in Composio: the configuration and the account connected through it
    are still there, they just stop being used. Deleting it from here would silently drop a
    connection that may still be needed.
    """
    from app.config import AGENT_ID, BACKEND_API_SECRET, WAI_BACKEND_URL

    conn = _require_known_connector(connector)
    sid = conn.get("mcp_server_id")
    if not sid:
        raise HTTPException(400, f"El conector «{connector}» no está en el catálogo.")
    if not (WAI_BACKEND_URL and BACKEND_API_SECRET and AGENT_ID):
        raise HTTPException(503, "La plataforma no está accesible desde aquí.")

    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.delete(
            f"{WAI_BACKEND_URL.rstrip('/')}/backend/mcp-servers/{int(sid)}/own-auth-config",
            params={"agent_app_id": int(AGENT_ID)},
            headers={"X-API-Secret": BACKEND_API_SECRET},
        )
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text[:300])
    await mc.apps_propias(fresco=True)
    logger.info("aplicación OAuth propia retirada de %s (por %s)",
                connector, getattr(user, "email", "?"))
    return r.json()


@router.post("/mcp-connectors/{connector}/disconnect")
async def disconnect_mcp_connector(
    connector: str,
    user: TokenPayload = Depends(require_admin),
):
    """Disconnect a connector (`_disconnect`), recorded in the connection history with who did it."""
    from app import connection_events as ce
    try:
        r = await _disconnect(connector, user)
    except HTTPException as exc:
        await ce.record(connector, "disconnect", getattr(user, "email", ""), None, "error", str(exc.detail))
        raise
    revocadas = (r or {}).get("revoked_accounts") if isinstance(r, dict) else None
    await ce.record(connector, "disconnect", getattr(user, "email", ""), None, "ok",
                    f"revoked accounts: {revocadas}" if revocadas is not None else None)
    return r


async def _disconnect(
    connector: str,
    user: TokenPayload,
):
    """Revoke ALL connected accounts for ONE connector (keyed by manifest
    name), under that connector's own identity.

    Revokes every account for the toolkit under the connector identity (not
    just the "best" one) so stale duplicates left over from a credential
    rotation can't survive and keep being selected at runtime."""
    conn = _require_known_connector(connector)
    if conn.get("wai_mcp_server_id"):
        return await _desconectar_conector_propio(conn)
    toolkit = conn["toolkit"]
    identity_connector = _identity_connector(conn)
    if not mc.is_enabled():
        raise HTTPException(503, "auth-service not configured")
    autonomous = (AUTONOMOUS_USER_EMAIL or "").strip().lower()
    try:
        result = await mc.disconnect_toolkit(autonomous, toolkit, identity_connector)
    except mc.McpConnectorError as exc:
        raise HTTPException(502, str(exc))
    revoked = result.get("revoked") or []
    return {"deleted": len(revoked) > 0, "revoked_accounts": len(revoked)}


@router.post("/mcp-connectors/{connector}/activate")
async def activate_mcp_connector(
    connector: str,
    user: TokenPayload = Depends(require_admin),
):
    """Apply a just-completed connection (`_activate`), recorded in the connection history: for OAuth this is when
    the account is actually connected."""
    from app import connection_events as ce
    try:
        r = await _activate(connector, user)
    except HTTPException as exc:
        await ce.record(connector, "activate", getattr(user, "email", ""), None, "error", str(exc.detail))
        raise
    await ce.record(connector, "activate", getattr(user, "email", ""), None,
                    str((r or {}).get("status") or "ok") if isinstance(r, dict) else "ok")
    return r


@router.get("/mcp-connectors/history")
async def connection_history(
    connector: str | None = None,
    user: TokenPayload = Depends(require_admin),
):
    """Who connected, changed the account, activated or disconnected each connector, and when (kept by the
    platform: survives restarts and log rotation)."""
    from app import connection_events as ce
    return await ce.history(connector)


async def _activate(
    connector: str,
    user: TokenPayload,
):
    """Applies a just-completed connection NOW, without waiting for any poll.

    Before this, a completed OAuth was at the mercy of two timers: the tool-router session
    was lazily minted on the next tick of the agent's refresher (~2 min) and the opencode
    restart required the change to be stable two ticks in a row (~2-4 min in total, with chat
    sessions left mute by binding to a cold session in between). The SPA calls here as soon
    as its polling sees ``connected`` and this endpoint collapses that wait:

    1. it re-resolves the runtime's MCP block — in router mode that MINTS the connector's
       tool-router session right now (Composio's warm-up starts now, not on the next tick),
       and it is idempotent if the live session is still valid (cache per binding);
    2. it notifies the agent's pod (``POST /v1/mcp/recheck``, best-effort): its refresher
       re-resolves at once and restarts opencode with the boot's warm-up gate. A runtime
       older than that route answers 404 and the periodic refresher stays as the safety net
       (nudged=False).
    """
    conn = _require_known_connector(connector)
    if conn.get("wai_mcp_server_id"):
        return {"status": "connected", "connector": conn["name"]}  # nothing to activate: the connection is stored when pasting the key
    if not mc.is_enabled():
        raise HTTPException(503, "auth-service not configured")
    autonomous = (AUTONOMOUS_USER_EMAIL or "").strip().lower()
    connected = False
    try:
        connections = await mc.list_connections(autonomous, _identity_connector(conn))
        account = mc.connected_account_for(connections, conn["toolkit"])
        connected = bool(account and (account.get("status") or "").upper() == "ACTIVE")
    except mc.McpConnectorError as exc:
        raise HTTPException(502, str(exc))

    activated = False
    if connected:
        try:
            await mcp_runtime.resolve_runtime_mcp_block()
            activated = True
        except Exception:  # noqa: BLE001 — activation never breaks the connection flow
            logger.warning("activate(%s): resolve_runtime_mcp_block failed", connector, exc_info=True)

    nudged = False
    if activated and agent_proxy.SIMPLE_BASE_URL and agent_proxy.AGENT_SHARED_TOKEN:
        nudged = await _avisar_al_agente(connector)
        if not nudged:
            # The agent's pod is usually RESTARTING right then (it restarts to apply the
            # previous connector): the nudge finds nobody and the new connector waited up to
            # 4 minutes for the periodic refresher (04/10/2026, Facebook connected at 11:49,
            # usable at 11:53). Keep nudging in the background until the pod answers.
            import asyncio as _asyncio
            _asyncio.create_task(_insistir_al_agente(connector))

    return {"connector": conn["name"], "connected": connected,
            "activated": activated, "nudged": nudged}


async def _save_setting(key: str, value: str, user: TokenPayload) -> None:
    """Upsert one AppSetting (records operator + timestamp)."""
    values = {
        "key": key, "value": value,
        "updated_by": getattr(user, "email", None),
        "updated_at": datetime.now(timezone.utc),
    }
    stmt = sqlite_insert(AppSetting).values(**values).on_conflict_do_update(
        index_elements=[AppSetting.key],
        set_={k: v for k, v in values.items() if k != "key"},
    )
    async with app_write() as s:
        await s.execute(stmt)
        await s.commit()


# Last-good tool catalogue per connector, so the editor stays usable across a
# transient sidecar outage (the #1 cause of "no tools discovered"). Discovery
# refreshes it whenever a live probe succeeds.
_TOOLS_CACHE_KEY = "mcp_tools_cache"


async def _load_tools_cache() -> dict[str, list[dict]]:
    async with app_session() as s:
        v = (await s.execute(
            select(AppSetting.value).where(AppSetting.key == _TOOLS_CACHE_KEY)
        )).scalar_one_or_none()
    try:
        data = json.loads(v) if v else {}
    except (TypeError, ValueError):
        return {}
    out: dict[str, list[dict]] = {}
    if isinstance(data, dict):
        for k, tools in data.items():
            if isinstance(tools, list):
                out[str(k)] = [t for t in tools if isinstance(t, dict) and t.get("name")]
    return out


async def _save_tools_cache(cache: dict[str, list[dict]], user: TokenPayload) -> None:
    await _save_setting(_TOOLS_CACHE_KEY, json.dumps(cache, sort_keys=True), user)






def _server_matches_connector(server_name: str, connector: dict) -> bool:
    """Heuristic match between a runtime MCP server name (as OpenCode
    reports it) and a manifest connector. Tolerant of the naming variants
    in use (``composio-odoo``, ``wai-odoo``, ``odoo``)."""
    sn = (server_name or "").strip().lower()
    name = (connector.get("name") or "").strip().lower()
    toolkit = (connector.get("toolkit") or "").strip().lower()
    if not sn:
        return False
    if sn == name:
        return True
    if toolkit and (sn == toolkit or sn.endswith(toolkit) or toolkit in sn):
        return True
    return False


@router.get("/mcp-connectors/tools")
async def list_mcp_connector_tools(
    user: TokenPayload = Depends(require_project_access),
):
    """Every connector's tools (name + description), for the pickers of Automations.
    Tools are discovered from the running OpenCode server; a per-connector CACHE (last
    good), a static per-toolkit fallback and the catalogue an own connector registered keep
    the list usable when the live probe returns nothing.

    Permissions are NOT here: they are the platform's table (``GET
    /mcp-connectors/permissions``), so nobody in this pod reads a permission off a list that
    could disagree with the one the proxy applies."""
    from app.mcp_runtime import custom_connectors as _custom_connectors_rt
    # v7.135.0 — include CUSTOM (non-Composio) MCPs so their tools are
    # discoverable in the checks/watch picker (the live probe returns every MCP
    # server the agent has, incl. custom ones — they matched by name below).
    connectors = _composio_connectors() + _custom_connectors_rt(fresco=True)
    cache = await _load_tools_cache()

    discovered: list[dict[str, Any]] = []
    try:
        discovered = await get_agent_proxy().list_mcp_tools()
    except Exception as exc:  # noqa: BLE001 — discovery is best-effort
        logger.warning("live MCP tool discovery failed: %s", exc)

    fresh_cache = dict(cache)
    out_connectors: list[dict[str, Any]] = []
    for conn in connectors:
        name = conn["name"]
        live_tools: list[dict[str, Any]] = []
        for server in discovered:
            if isinstance(server, dict) and _server_matches_connector(server.get("name", ""), conn):
                for tool in server.get("tools") or []:
                    if isinstance(tool, dict) and tool.get("name"):
                        live_tools.append({"name": tool["name"], "description": tool.get("description") or ""})
        if live_tools:
            source, tools = "live", live_tools
            fresh_cache[name] = live_tools
        elif cache.get(name):
            source, tools = "cache", [dict(t) for t in cache[name]]
        elif _STATIC_TOOLS.get(conn["toolkit"]):
            source, tools = "static", [dict(t) for t in _STATIC_TOOLS[conn["toolkit"]]]
        elif conn.get("tools_catalogo"):
            # The ones the catalog knows of that connector. Before this, a just-connected own
            # connector showed ZERO tools until live discovery reached it.
            source, tools = "catalogo", [dict(t) for t in conn["tools_catalogo"]]
        else:
            source, tools = "none", []
        out_connectors.append({
            "name": name,
            "toolkit": conn["toolkit"],
            "display_name": conn["display_name"],
            "source": source,
            "tools": tools,
        })

    if fresh_cache != cache:
        try:
            await _save_tools_cache(fresh_cache, user)
        except Exception:  # noqa: BLE001 — cache is best-effort
            logger.debug("tools cache save failed", exc_info=True)

    return {"connectors": out_connectors}


# ── Connector permissions: the employee's layer (14.8.0) ─────────────
# Contract: docs/permisos-de-conectores.md. The employee inherits the agent's permissions or
# personalises them with the SAME shape (default authority + per-connector mode inherit /
# write / write_with_approval / read_only / custom with per-tool rules) and can never open
# more than the agent. NOTHING is calculated here: the screen shows the platform's table,
# drafts go through the platform's ensayo (dry-run), and saving goes through the platform's
# validation (422 with a code the screen translates).


def _respuesta_de_rechazo(status: int, body: Any) -> HTTPException:
    from app.runtime_policy_sync import rejection
    if status == 0:
        return HTTPException(503, {"code": "PLATAFORMA_NO_DISPONIBLE",
                                   "message": str((body or {}).get("detail") or "")})
    r = rejection(status, body)
    return HTTPException(422 if status == 422 else 502, r)


@router.get("/mcp-connectors/permissions")
async def get_connector_permissions(
    user: TokenPayload = Depends(require_project_access),
):
    """The table this employee applies (asked to the platform NOW) + its own layer.

    ``loaded`` false = the platform could not be reached: ``table`` is then the last known
    one (or None) and the screen says so. ``converted`` = the layer shown comes from the
    pre-14.8.0 local settings and has not been saved in the new shape yet."""
    from app import mcp_policy
    from app.runtime_policy_sync import current_policy
    tabla = await mcp_policy.load_table(force=True)
    policy, converted = await current_policy(tabla)
    return {"table": tabla, "policy": policy, "converted": converted,
            "loaded": mcp_policy.table_is_fresh()}


@router.post("/mcp-connectors/permissions/dry-run")
async def dry_run_connector_permissions(
    body: dict[str, Any] = Body(...),
    user: TokenPayload = Depends(require_admin),
):
    """The platform's ensayo with a DRAFT of the employee's layer: ``{valid, errors,
    table}``. The screen paints this table; it never computes a permission itself."""
    from app import mcp_policy
    from app.runtime_policy_sync import dry_run, normalize_policy
    tabla = await mcp_policy.load_table()
    status, data = await dry_run(normalize_policy(body.get("policy")),
                                 (tabla or {}).get("version_id"))
    if status // 100 != 2:
        raise _respuesta_de_rechazo(status, data)
    return data


@router.put("/mcp-connectors/permissions")
async def put_connector_permissions(
    body: dict[str, Any] = Body(...),
    user: TokenPayload = Depends(require_admin),
):
    """Save the employee's layer. The platform validates it against the agent (422
    ``EMPLEADO_ABRE_MAS`` & co. → ``{"code", "message"}``); only an accepted layer is kept
    locally. Returns the new table and the saved layer."""
    from app import mcp_policy
    from app.runtime_policy_sync import normalize_policy, put_employee_policy, save_employee_policy
    policy = normalize_policy(body.get("policy"))
    status, data = await put_employee_policy(policy)
    if status // 100 != 2:
        raise _respuesta_de_rechazo(status, data)
    await save_employee_policy(policy, getattr(user, "email", "") or "")
    logger.info("employee connector permissions saved (by %s): %s",
                getattr(user, "email", "?"), policy)
    # 7.182.0 — the employee could LEARN the previous policy ("this connector is read-only")
    # and keep refusing to act weeks after you open it, without even trying. When turning
    # the dial, what was learned about configuration expires.
    from app.agent_memory_utils import expire_config_state_memories
    await expire_config_state_memories("cambio de permisos de conectores")
    mcp_policy.invalidate_table()
    tabla = await mcp_policy.load_table(force=True)
    return {"table": tabla, "policy": policy}
