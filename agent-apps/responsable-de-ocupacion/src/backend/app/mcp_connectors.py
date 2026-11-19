"""External MCP connector authentication (Composio) for the agent-app.

The agent uses external MCP servers (Odoo, Gmail, …) that are backed by
Composio. Their per-user credentials live in Composio, brokered by the WAI
auth-service. The app pod never holds the Composio API key — and since 14.0.0
it no longer holds the auth-service's shared HMAC secret either: every call
goes to the tenant's ENGINE (``/opencode/empleado/composio/{operacion}``)
authenticated with THIS employee's runtime JWT, and the engine signs towards
the auth-service. (Composio is MCP tooling only — it is NOT an email channel
adapter; email channels are IMAP/SMTP, see ``app/channels/base.py::get_adapter``.)

Why the hop through the engine (14.0.0): the HMAC secret was ONE for the whole platform,
and with it the auth-service accepted any identity of any tenant. An employee had it in
its environment and that pod runs code written by an agent. The runtime JWT, instead,
only says "I am THIS employee", and the engine rejects (403) any identity that is not its
own. One single path: there is no direct route to the auth-service to go back to if the
engine fails.

Crucially, the connection MUST be created under the SAME Composio identity
the runtime resolves — ``config.composio_identity`` builds it for every
caller, and the engine only accepts ``{tenant}:{email}`` or
``{tenant}:{email}+a{AGENT_ID}[#segmento]`` of THIS employee.

Two connection modes, decided by the toolkit's Composio ``auth_scheme``:

* credential schemes (API_KEY / BEARER_TOKEN / BASIC) — e.g. Odoo: the
  operator fills a small form and we POST it to ``connect-credentials``.
* redirect schemes (OAUTH2 / OAUTH1) — we hand back a
  ``/composio/{toolkit}/start`` URL the SPA opens in a popup (that one is a
  BROWSER redirect to the public auth-service; it carries no secret).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

from app import llave_de_wai
from app.config import (
    AUTH_SERVICE_URL,
    WAI_AGENT_RUNTIME_JWT,
    WAI_ENGINE_URL,
    composio_identity,
)

logger = logging.getLogger(__name__)

# Composio auth schemes that take user-supplied credentials (a form) vs
# the browser redirect (OAuth). Mirrors auth-service composio_sdk.py.
CREDENTIAL_AUTH_SCHEMES = frozenset(
    {"API_KEY", "BEARER_TOKEN", "BASIC", "NO_AUTH"}
)
REDIRECT_AUTH_SCHEMES = frozenset({"OAUTH1", "OAUTH2", "DCR_OAUTH"})


class McpConnectorError(RuntimeError):
    """Raised when the engine / auth-service is unreachable or rejects a call."""


def is_enabled() -> bool:
    """True when the pod can talk to the engine AS this employee: the tenant's engine URL
    and its runtime JWT, both injected by the deployer."""
    return bool(WAI_ENGINE_URL and WAI_AGENT_RUNTIME_JWT)


# What each "no" of the engine means, in the operator's words (UI text, Spanish). The
# engine returns `{"detail", "code"}`; the technical `detail` is appended for whoever debugs.
_MOTIVOS = {
    401: ("la plataforma no reconoce la credencial de este empleado (su JWT de runtime "
          "falta, caducó o no es de un empleado). Vuelve a desplegarlo para que la reciba"),
    400: "a la petición le falta la identidad de este empleado",
    502: "el servicio de autenticación de conectores no responde; reinténtalo en un momento",
}


def _mensaje_de_error(operacion: str, status: int, cuerpo: Any, texto: str) -> str:
    detalle = ""
    codigo = ""
    if isinstance(cuerpo, dict):
        detalle = str(cuerpo.get("detail") or cuerpo.get("error") or "")
        codigo = str(cuerpo.get("code") or "")
    detalle = (detalle or texto or "")[:300]
    if status == 403 and codigo == "CUENTA_AJENA":
        motivo = "esa cuenta conectada no es de este conector del empleado"
    elif status == 403:
        # IDENTIDAD_AJENA: the identity does not match the token's. With the stock code it only
        # happens if the connector's name carries something outside [a-z0-9._-] (spaces,
        # accents, more than 64 characters) — the engine does not accept it as a segment.
        motivo = ("la plataforma rechaza la identidad: solo puede usar la de este empleado "
                  "(revisa que el nombre del conector sea [a-z0-9._-], hasta 64 caracteres)")
    else:
        motivo = _MOTIVOS.get(status, f"la plataforma respondió {status}")
    return f"Composio «{operacion}»: {motivo}. [{codigo or status}] {detalle}".rstrip()


async def _post(path: str, payload: dict[str, Any], *, timeout: float = 12.0) -> Any:
    """POST of a Composio operation through the tenant's engine.

    ``path`` is the usual one in the auth-service (``/composio/connections``…): the
    operation is what goes after ``/composio/`` and the body travels THE SAME as before, so
    the callers do not change. No signature nor ``X-WAI-Tenant``: the tenant is set by the
    engine (its own, never the one the pod says) and the credential is the employee's
    runtime JWT."""
    if not is_enabled():
        raise McpConnectorError(
            "Los conectores de Composio no están disponibles en este empleado: faltan "
            "WAI_ENGINE_URL o WAI_AGENT_RUNTIME_JWT (los inyecta el desplegador)."
        )
    operacion = path.removeprefix("/composio/").strip("/")
    url = f"{WAI_ENGINE_URL.rstrip('/')}/opencode/empleado/composio/{operacion}"
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                url,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    # The newest valid key (it renews itself: `llave_de_wai`), not the deploy-time one.
                    "Authorization": f"Bearer {await llave_de_wai.llave(WAI_AGENT_RUNTIME_JWT, WAI_ENGINE_URL)}",
                },
            )
    except httpx.HTTPError as exc:
        raise McpConnectorError(
            f"Composio «{operacion}»: el motor de la plataforma no responde ({exc})."
        ) from exc
    try:
        datos = r.json()
    except Exception:  # noqa: BLE001
        datos = None
    if r.status_code >= 400:
        raise McpConnectorError(_mensaje_de_error(operacion, r.status_code, datos, r.text))
    if datos is None:
        raise McpConnectorError(f"Composio «{operacion}»: la respuesta no es JSON.")
    return datos


# ── high-level operations (all keyed by the autonomous identity) ──────


async def list_connections(user_email: str, connector: str = "") -> list[dict[str, Any]]:
    """Return the connected accounts for the ``(user, connector)`` entity.

    With entity-per-connector each connector authorises under its OWN Composio
    identity (``{tenant}:{email}#{connector}``), so this lists only that
    connector's account(s) — never another connector's.
    """
    identity = composio_identity(user_email, connector)
    if not identity:
        return []
    data = await _post("/composio/connections", {"user_email": identity})
    items = (data or {}).get("connections") or []
    return [c for c in items if isinstance(c, dict)]


async def ensure_server(
    toolkit: str, *, server_name: str = "", allowed_tools: Optional[list] = None
) -> str:
    """Ensure a Composio MCP *server* exists for ``toolkit`` and return its id.

    Idempotent on the auth-service / Composio side (the server is keyed by the
    toolkit's managed auth_config), so a stable ``server_name`` of
    ``wai-{toolkit}`` is reused across connectors and reboots — the MCP server
    is shared per toolkit; per-connector isolation happens later in
    :func:`generate_url` via the per-connector identity.
    """
    slug = (toolkit or "").strip().lower()
    if not slug:
        raise McpConnectorError("toolkit is required")
    # The name can NOT carry the slug as is: Composio rejects underscores with a 400
    # "Error in payload.name", and Composio slugs use them (`google_maps`, `browser_tool`,
    # `composio_search`). Measured on 21/08/2026: of twelve toolkits of the catalog, the
    # three with an underscore were exactly the three that did not work in any employee.
    # The failure was SILENT —the connector stayed as a disabled placeholder, the screen
    # kept saying "connected"— and it was only seen by asking the pod which tools it
    # really had.
    #
    # In router mode this function is no longer called (the router needs no server); it
    # remains for `raw`, which does need it to sign the bridge URL.
    nombre = server_name or f"wai-{slug.replace('_', '-')}"
    payload: dict[str, Any] = {"name": nombre, "toolkit": slug}
    if allowed_tools:
        payload["allowed_tools"] = list(allowed_tools)
    data = await _post("/composio/mcp/create-server", payload, timeout=25.0)
    server_id = (data or {}).get("server_id") or ""
    if not server_id:
        raise McpConnectorError("composio create-server returned empty server_id")
    return server_id


async def generate_url(user_email: str, connector: str, server_id: str) -> str:
    """Per-connector signed MCP URL (the auth payload is embedded in the URL).

    MUST be called for each connector at agent start: the URL is scoped to the
    connector's identity (``{tenant}:{email}#{connector}``) and to ``server_id``
    so it resolves THAT connector's connected account. Returns ``""`` when the
    connector has no usable account yet.
    """
    identity = composio_identity(user_email, connector)
    if not identity or not (server_id or "").strip():
        return ""
    data = await _post(
        "/composio/mcp/generate-url",
        {"user_email": identity, "server_id": server_id},
    )
    return (data or {}).get("url") or ""


async def create_tool_router_session(
    user_email: str,
    connector: str,
    toolkits: list[str],
    connected_accounts: Optional[dict[str, list[str]]] = None,
    tool_denies: Optional[dict[str, list[str]]] = None,
    read_only: bool = False,
    auth_config_id: str = "",
    preload: Optional[list[str]] = None,
) -> str:
    """Composio **tool-router** session URL for ONE connector's meta-tool
    gateway (router mode). Returns "" on failure.

    ``preload`` (14.24.0): the slugs this employee declares it uses of this connector. The SAME
    router session exposes them DIRECT, next to the meta-tools, so the agent does not go through
    ``SEARCH_TOOLS`` + ``GET_TOOL_SCHEMAS`` every time (measured live: one step instead of three).
    The auth-service drops a denied one and retries without what Composio rejects; an old
    auth-service ignores the field and the session is the usual one.

    The session MUST run under the connector's OWN per-connector identity
    (``composio_identity(email, connector)``) — the SAME identity the account
    was authorised under by ``connect``/``generate_url``. Composio's tool-router
    requires every bound ``connected_account`` to belong to the same ``user_id``
    as the session; a session under the per-AGENT identity (``{tenant}:{email}
    +a{AGENT_ID}``) binding an account stored under ``…#{connector}`` is rejected
    with HTTP 400 ``ToolRouterV2_InvalidConnectedAccount`` (code 4306) and the
    gateway boots disabled — i.e. the agent ends up with NO tools. So we key the
    session on the connector, giving one session per connector (the agent merges
    them as separate gateways). ``tool_denies={toolkit:[slug]}`` maps the
    operator's per-tool denies into the session's hard execution allowlist.
    """
    identity = composio_identity(user_email, connector)
    if not identity or not toolkits:
        return ""
    payload: dict[str, Any] = {
        "user_id": identity,
        "toolkits": list(toolkits),
        "tool_denies": tool_denies or {},
        # Extra server-side belt for read-only connectors (v7.26.0): Composio
        # drops destructive-tagged tools from the session itself. The app-pod
        # proxy remains the guarantee; this narrows the session upstream.
        "read_only": bool(read_only),
    }
    # WHICH OAuth application the gate is opened with. It has to be the SAME one the account
    # was authorized with: Composio rejects a session that ties an account of another
    # configuration (400, code 4306) and the gate starts OFF — the employee is left without
    # those tools without anything saying so.
    if auth_config_id:
        payload["auth_config_ids"] = {tk: auth_config_id for tk in toolkits}
    if connected_accounts:
        payload["connected_accounts"] = {
            tk: list(ids) for tk, ids in connected_accounts.items() if ids
        }
    if preload:
        payload["preload"] = list(preload)
    data = await _post("/composio/tool-router/session", payload, timeout=25.0)
    return (data or {}).get("url") or ""


_APPS_PROPIAS: dict[str, Any] = {"cuando": 0.0, "valor": {}}
_APPS_TTL_S = 120.0


async def apps_propias(fresco: bool = False) -> dict[str, str]:
    """`{connector_id: application_id}` of THIS employee, according to the platform.

    The OAuth application a service is asked with can be the space's or an own one. The
    cascade is resolved by the platform, which is who stores it; here it is only read.
    Cached for two minutes because the runtime refresh asks for it on every round.

    With no answer an empty dict is returned: using the space's is the usual behaviour and
    does not break anyone who does not have their own.
    """
    import time

    from app.config import AGENT_ID, BACKEND_API_SECRET, WAI_BACKEND_URL

    if not fresco and (time.monotonic() - _APPS_PROPIAS["cuando"]) < _APPS_TTL_S:
        return _APPS_PROPIAS["valor"]
    if not (WAI_BACKEND_URL and BACKEND_API_SECRET and AGENT_ID):
        return {}
    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get(
                f"{WAI_BACKEND_URL.rstrip('/')}/backend/mcp-servers/own-auth-configs",
                params={"owner_kind": "agent_app", "owner_ref": str(int(AGENT_ID))},
                headers={"X-API-Secret": BACKEND_API_SECRET},
            )
            r.raise_for_status()
            valor = {str(k): str(v) for k, v in ((r.json() or {}).get("apps") or {}).items()}
    except Exception as exc:  # noqa: BLE001 — with no answer, the space's
        logger.warning("no se pudieron leer las aplicaciones propias: %s", exc)
        return _APPS_PROPIAS["valor"]
    _APPS_PROPIAS["cuando"] = time.monotonic()
    _APPS_PROPIAS["valor"] = valor
    return valor


async def conectar_con_forma(
    mcp_server_id: Any, forma: str, credentials: dict[str, Any], identity: str,
) -> dict[str, Any]:
    """Connects the connector with a WAY that is not the main one (the Google Maps API key
    instead of Composio's app, whose quota all its customers share).

    The PLATFORM does it and not the auth-service directly because the connection stays with
    a configuration of THIS employee, and whoever records it —and then gives it to its gate,
    `apps_propias`— is the platform. `identity` is this connector's: the platform cannot
    compute it and checks that it belongs to this employee.
    """
    from app.config import AGENT_ID, BACKEND_API_SECRET, WAI_BACKEND_URL

    if not (WAI_BACKEND_URL and BACKEND_API_SECRET and AGENT_ID and mcp_server_id):
        raise McpConnectorError("la plataforma no está configurada en este empleado")
    try:
        async with httpx.AsyncClient(timeout=45.0) as c:
            r = await c.post(
                f"{WAI_BACKEND_URL.rstrip('/')}/backend/mcp-servers/"
                f"{int(mcp_server_id)}/composio-connect",
                json={"forma": (forma or "").strip().upper(), "credentials": credentials,
                      "agent_app_id": int(AGENT_ID), "identity": identity},
                headers={"X-API-Secret": BACKEND_API_SECRET},
            )
    except httpx.HTTPError as exc:
        raise McpConnectorError(f"la plataforma no contesta: {exc}") from exc
    if r.status_code >= 400:
        try:
            motivo = (r.json() or {}).get("detail") or r.text
        except ValueError:
            motivo = r.text
        raise McpConnectorError(str(motivo)[:300])
    await apps_propias(fresco=True)
    return r.json() or {}


async def quitar_config_propia(mcp_server_id: Any) -> None:
    """Stops using THIS employee's OWN configuration on that connector and goes back to the
    space's. It deletes nothing in Composio."""
    from app.config import AGENT_ID, BACKEND_API_SECRET, WAI_BACKEND_URL

    if not (WAI_BACKEND_URL and BACKEND_API_SECRET and AGENT_ID and mcp_server_id):
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.delete(
                f"{WAI_BACKEND_URL.rstrip('/')}/backend/mcp-servers/"
                f"{int(mcp_server_id)}/own-auth-config",
                params={"agent_app_id": int(AGENT_ID)},
                headers={"X-API-Secret": BACKEND_API_SECRET},
            )
            r.raise_for_status()
    except httpx.HTTPError as exc:
        raise McpConnectorError(f"no se pudo volver a la del espacio: {exc}") from exc
    await apps_propias(fresco=True)


async def forma_de_config(auth_config_id: str, identity: str = "") -> str:
    """What way a configuration is (`API_KEY`, `OAUTH2`…); "" if unknown."""
    acid = (auth_config_id or "").strip()
    if not acid:
        return ""
    try:
        d = await _post("/composio/auth-config/scheme",
                        {"auth_config_id": acid, "user_email": identity})
    except McpConnectorError:
        return ""
    return str((d or {}).get("auth_scheme") or "").upper()


def es_forma_oauth(esquema: str) -> bool:
    return (esquema or "").strip().upper() in REDIRECT_AUTH_SCHEMES


async def auth_info(toolkit: str) -> dict[str, Any]:
    """Auth metadata for a toolkit (generic, drives the UI):
    ``{auth_scheme, connection_mode, managed_oauth_available,
       auth_config_fields[], connection_fields[]}``.

    ``connection_mode`` ∈ ``redirect`` (managed OAuth) | ``custom_oauth``
    (OAuth needing the operator's own app) | ``credentials`` (API key/basic) |
    ``none``. ``auth_config_fields`` are operator-once (client_id/secret);
    ``connection_fields`` are per-user (api key, instance URL/subdomain)."""
    slug = (toolkit or "").strip().lower()
    if not slug:
        raise McpConnectorError("toolkit is required")
    return await _post("/composio/toolkits/auth-info", {"toolkit": slug})


async def register_auth_config(
    toolkit: str, credentials: dict[str, Any], replace: bool = False,
) -> str:
    """Register (operator-once) the toolkit's OAuth app / auth_config with the
    supplied credentials (client_id/client_secret for ``custom_oauth`` toolkits
    like Kommo/Salesforce). Idempotent. Returns the auth_config_id.

    ``replace=True`` deletes any auth_config already registered for the toolkit
    and recreates it — lets the operator correct credentials entered wrong (the
    normal path reuses the stale config). Account/tenant-wide."""
    slug = (toolkit or "").strip().lower()
    if not slug:
        raise McpConnectorError("toolkit is required")
    data = await _post(
        "/composio/auth-config",
        {"toolkit": slug, "credentials": credentials or {}, "replace": bool(replace)},
        timeout=25.0,
    )
    acid = (data or {}).get("auth_config_id") or ""
    if not acid:
        raise McpConnectorError("auth-config registration returned no id")
    return acid


async def connect_credentials(
    user_email: str, toolkit: str, credentials: dict[str, Any], connector: str = ""
) -> dict[str, Any]:
    """Create a connected account from user-supplied credentials (API key,
    etc.) under the connector's per-connector identity."""
    identity = composio_identity(user_email, connector)
    if not identity:
        raise McpConnectorError("autonomous user identity is not configured")
    slug = (toolkit or "").strip().lower()
    if not slug:
        raise McpConnectorError("toolkit is required")
    if not isinstance(credentials, dict) or not credentials:
        raise McpConnectorError("credentials are required")
    return await _post(
        "/composio/connect-credentials",
        {"user_email": identity, "toolkit": slug, "credentials": credentials},
    )


async def disconnect(
    connected_account_id: str, user_email: str = "", connector: str = "",
) -> dict[str, Any]:
    """Revokes ONE connected account. It carries the identity of the connector it belongs to:
    the auth-service revokes by bare id, so the engine checks that account is among those of
    this identity (403 CUENTA_AJENA if not) — without an identity there is nobody to ask and
    the engine answers 400."""
    cid = (connected_account_id or "").strip()
    if not cid:
        raise McpConnectorError("connected_account_id is required")
    identity = composio_identity(user_email, connector)
    if not identity:
        raise McpConnectorError(
            "para desconectar una cuenta hace falta la identidad del empleado a la que pertenece"
        )
    return await _post(
        "/composio/disconnect", {"connected_account_id": cid, "user_email": identity},
    )


async def disconnect_toolkit(user_email: str, toolkit: str, connector: str = "") -> dict[str, Any]:
    """Revoke EVERY connected account for the ``(user, connector)`` entity's
    toolkit.

    Universal disconnect: unlike :func:`disconnect` (single account), this
    clears all of the toolkit's accounts under the connector identity so a
    stale/duplicate connection can never survive a credential rotation and keep
    being selected at runtime. Returns ``{"revoked": [ids...]}``.
    """
    identity = composio_identity(user_email, connector)
    if not identity:
        raise McpConnectorError("autonomous user identity is not configured")
    slug = (toolkit or "").strip().lower()
    if not slug:
        raise McpConnectorError("toolkit is required")
    return await _post(
        "/composio/disconnect-toolkit",
        {"user_email": identity, "toolkit": slug},
    )


def oauth_start_url(
    toolkit: str,
    user_email: str,
    *,
    connector: str = "",
    return_origin: str = "",
    server_name: str = "",
    force: bool = False,
    connection_fields: Optional[dict[str, Any]] = None,
    auth_config_id: str = "",
) -> str:
    """Browser entry point for OAuth toolkits. The SPA opens this in a popup;
    the auth-service runs the Composio consent and closes it. The consent is
    bound to the connector's per-connector identity. ``connection_fields``
    carries per-user OAuth-init values (e.g. a Salesforce/Zendesk subdomain)."""
    identity = composio_identity(user_email, connector)
    if not identity:
        raise McpConnectorError("autonomous user identity is not configured")
    if not AUTH_SERVICE_URL:
        raise McpConnectorError("AUTH_SERVICE_URL (public) is not configured")
    slug = (toolkit or "").strip().lower()
    params = {
        "user_email": identity,
        "return_origin": return_origin,
        "server_name": server_name or f"mcp:{slug}",
    }
    cf = {k: v for k, v in (connection_fields or {}).items() if v not in (None, "")}
    if cf:
        params["connection_fields"] = json.dumps(cf, separators=(",", ":"))
    # THIS employee's application, if it has its own. Empty = the space's.
    if auth_config_id:
        params["auth_config_id"] = auth_config_id
    if force:
        params["force"] = "1"
        params["prompt"] = "select_account"
    qs = urlencode({k: v for k, v in params.items() if v})
    return f"{AUTH_SERVICE_URL}/composio/{slug}/start?{qs}"


def connected_account_for(
    connections: list[dict[str, Any]], toolkit: str
) -> Optional[dict[str, Any]]:
    """Pick the best connected account for a toolkit (ACTIVE first, newest)."""
    slug = (toolkit or "").strip().lower()
    matches = [c for c in connections if (c.get("toolkit") or "").lower() == slug]
    if not matches:
        return None

    def _key(c: dict[str, Any]):
        active = 1 if (c.get("status") or "").upper() == "ACTIVE" else 0
        ts = c.get("updated_at") or c.get("created_at") or ""
        return (active, ts)

    matches.sort(key=_key, reverse=True)
    return matches[0]
