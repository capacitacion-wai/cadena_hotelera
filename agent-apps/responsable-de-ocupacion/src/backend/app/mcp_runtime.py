"""Runtime resolution of external MCP connectors for the autonomous agent.

This module is part of the ``wai-agent-app-builder`` SKILL. It is the single
place that turns the manifest's external Composio connectors
(``agent_app.mcps[]``) into FULLY RESOLVED opencode MCP entries the agent can
actually use, and into the operator-facing connection status.

Why it exists (the bug it fixes): the autonomous agent runs stock opencode,
which cannot talk Composio's hosted MCP directly (``type: remote`` → SSE →
fails) and has no ``composio.mcp.generate`` step at boot. The platform used to
ship a dead ``type: remote`` → ``mcp.composio.dev`` placeholder. Instead, the
APP pod (which talks to Composio through the tenant engine with its own
runtime JWT, see ``mcp_connectors``) resolves each
connector PER CONNECTOR and the agent loads the result at boot via
``GET /mcp/agent-runtime`` (see ``mcp_server.py``), writing a local
``streaming-http-proxy`` bridge per connector into ``opencode.json``.

ENTITY-PER-CONNECTOR: each connector authorises its own Composio account under
``{tenant}:{autonomous_email}#{connector_name}`` (see
``config.composio_identity``), so two connectors — even of the same toolkit —
never collide on a single connected account.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app import mcp_connectors as mc
from app.agent_proxy import AUTONOMOUS_USER_EMAIL
from app.app_database import app_session, app_write
from app.app_models.mcp_connectors import McpConnector
from app.config import (
    AGENT_ID, BACKEND_API_SECRET, WAI_APP_JSON_PATH, WAI_BACKEND_URL,
    composio_identity, segmento_de_identidad,
)
# mcp_policy has no module-level app imports (DB access is lazy), so this is
# cycle-safe. POLICY_WRITE is needed by _bridge_url's fail-closed branch — it
# used to be referenced without import and NameError'd exactly in the degraded
# path it guards (v7.89.1).
from app.mcp_policy import POLICY_READ_ONLY, POLICY_WRITE, effective_policy

logger = logging.getLogger(__name__)

# Local stdio MCP bridge baked into the opencode-server image. opencode spawns
# it as ``type: local`` and it forwards to the Composio Streamable-HTTP URL,
# sidestepping opencode's broken SSE handling of remote Composio endpoints.
COMPOSIO_BRIDGE_BIN = "streaming-http-proxy"
# Disabled bridges still need a syntactically valid command; opencode skips
# them because ``enabled`` is false. Mirrors engine-api config_generator.
_DISABLED_URL = "http://127.0.0.1/"


# How long the catalog card is reused. Short on purpose: if the operator changes a
# connector, the employee finds out in under a minute and not when its image is rebuilt.
_VIDA_FICHAS_S = 60.0
_FICHAS: tuple[float, dict[str, dict]] | None = None
# A connector's entry SIGNED for a specific PERSON (interactive chat). Apart from `_FICHAS`
# —which is the owner's— because the key includes the person; same short life.
_ENTRADA_PERSONA: dict[tuple[str, str], tuple[float, dict]] = {}


def olvidar_fichas() -> None:
    """Drops the cached card so the next read goes to the catalog.

    It is called right after connecting or disconnecting. Without this, the screen came back
    from connecting and kept saying "not connected" for up to a minute —the cache's life—,
    which is exactly the moment someone concludes it did not work and repeats it."""
    global _FICHAS
    _FICHAS = None


def fichas_de_conectores(fresco: bool = False) -> dict[str, dict]:
    """WHAT each connector of this employee is, asked to the catalog, indexed by name.

    The manifest says WHICH connectors the employee has —that is its own and is frozen with
    its version—, but what they ARE (which Composio toolkit they belong to, how they
    authenticate) is known by the platform. Having it copied in the manifest aged by
    construction: it freezes when the image is generated and any later change, or any field
    whoever copied it left out, stays lying. It happened with the Odoo 17/18 connector: the
    manifest carried `auth_type` but not `composio_toolkit`, the toolkit was deduced from the
    card's NAME —"odoo-17-18"— and Composio answered, rightly, that that toolkit does not exist.

    If the platform does not answer, the last thing it said is returned. Being left without
    connectors over a network hiccup would be worse than using a card from a minute ago."""
    global _FICHAS
    ahora = time.monotonic()
    # `fresco` = do not give me the cached one. The SCREENS use it: a person who has just
    # connected looks at the result in the same second, and showing them the state of a
    # minute ago is what makes them repeat it believing it did not work. The cache stays
    # where it does help —the runtime looks it up many times per conversation— and there a
    # minute of delay nobody notices.
    if not fresco and _FICHAS and ahora - _FICHAS[0] < _VIDA_FICHAS_S:
        return _FICHAS[1]
    if not (WAI_BACKEND_URL and BACKEND_API_SECRET and AGENT_ID):
        return _FICHAS[1] if _FICHAS else {}
    url = f"{WAI_BACKEND_URL.rstrip('/')}/backend/agents/{int(AGENT_ID)}/mcp-servers"
    try:
        r = httpx.get(url, headers={"X-API-Secret": BACKEND_API_SECRET}, timeout=8.0)
        r.raise_for_status()
        filas = {
            (f.get("name") or "").strip(): f
            for f in (r.json().get("servers") or [])
            if (f.get("name") or "").strip()
        }
    except Exception as exc:  # noqa: BLE001 — the network is no reason to be left without connectors
        logger.warning("No se pudo leer el catálogo de conectores (%s); se usa lo último "
                       "conocido", exc)
        return _FICHAS[1] if _FICHAS else {}
    _FICHAS = (ahora, filas)
    return filas


def _entrada_por_persona(nombre: str, acting_user: str) -> dict[str, Any] | None:
    """This connector's `opencode_entry` SIGNED BY THAT PERSON, asked to the catalog.

    It only changes something for the connectors that DELEGATE identity (`wai_app`): the
    platform signs the token for the turn's person instead of for the owner, so "put in MY
    profile…" ends up in the profile of whoever asks. For the other own ones (`api_key`) the
    person does not change the entry; asking too much is harmless. Short cache per
    (connector, person). If the platform does not answer, `None` is returned and the caller
    uses the owner's."""
    persona = (acting_user or "").strip().lower()
    if not persona or not (WAI_BACKEND_URL and BACKEND_API_SECRET and AGENT_ID):
        return None
    clave = (nombre, persona)
    ahora = time.monotonic()
    guardada = _ENTRADA_PERSONA.get(clave)
    if guardada and ahora - guardada[0] < _VIDA_FICHAS_S:
        return guardada[1] or None
    url = f"{WAI_BACKEND_URL.rstrip('/')}/backend/agents/{int(AGENT_ID)}/mcp-servers"
    try:
        r = httpx.get(url, headers={"X-API-Secret": BACKEND_API_SECRET},
                      params={"acting_user": persona}, timeout=8.0)
        r.raise_for_status()
        fila = next((f for f in (r.json().get("servers") or [])
                     if (f.get("name") or "").strip() == nombre), None)
        entrada = (fila or {}).get("opencode_entry") or {}
    except Exception as exc:  # noqa: BLE001 — the network is no reason to fall; the owner's is used
        logger.warning("No se pudo firmar la entrada de %s para %s (%s); se usa la del dueño",
                       nombre, persona, exc)
        return None
    _ENTRADA_PERSONA[clave] = (ahora, entrada)
    return entrada or None


def extras_de_conexion(nombre: str) -> dict:
    """What THIS connector's form needs on top of what Composio asks for.

    Its card on the platform declares it; here it is only read. Writing it in the code would
    be writing it twice —this screen and the code agent's panel paint the same form— and two
    rules diverge. Shape:

        {"extra_fields": [{name,label,description,required,secret}],
         "compose": "user_at_host", "user_field": "wai_login", "url_field": "full"}
    """
    extras = (fichas_de_conectores().get(nombre) or {}).get("connection_extras") or {}
    return extras if isinstance(extras, dict) else {}


def componer_credenciales(nombre: str, credenciales: dict) -> dict:
    """Applies that declaration before sending the connection to Composio.

    When the system is reached through a TRANSLATOR of ours, the "instance" Composio registers
    has to be the translator's: the person writes the one of THEIR system —the only thing
    they know— and ours is set by the platform. The fields we ask for do NOT travel to
    Composio: there they would be fields its form does not know and would bring down the
    whole connection."""
    from urllib.parse import quote

    ficha = fichas_de_conectores().get(nombre) or {}
    base = (ficha.get("translator_base") or "").strip().rstrip("/")
    extras = extras_de_conexion(nombre)
    if not base or not extras:
        return credenciales
    campo_url = extras.get("url_field") or "full"
    destino = str((credenciales or {}).get(campo_url) or "").strip()
    if not destino:
        return credenciales
    host = destino.split("://")[-1].split("/")[0].strip().strip(".")
    if not host:
        raise ValueError("La dirección del sistema no se entiende.")
    nuestros = {(c or {}).get("name") for c in (extras.get("extra_fields") or [])}
    salida = {k: v for k, v in (credenciales or {}).items() if k not in nuestros}
    if extras.get("compose") == "user_at_host":
        campo_usuario = extras.get("user_field") or "wai_login"
        usuario = str((credenciales or {}).get(campo_usuario) or "").strip()
        if not usuario:
            etiqueta = next((c.get("label") for c in (extras.get("extra_fields") or [])
                             if (c or {}).get("name") == campo_usuario), campo_usuario)
            raise ValueError(f"Falta «{etiqueta}»: sin ese dato la conexión no autentica.")
        salida[campo_url] = f"{base}/o/{quote(usuario, safe='')}@{host}"
    else:
        salida[campo_url] = f"{base}/o/{host}"
    return salida


def sistemas_de_negocio() -> list[dict[str, str]]:
    """Which SYSTEMS this employee reaches — all of them, not only the ones to be connected.

    It exists because the two lists above answer another question. `composio_connectors` and
    `custom_connectors` feed the Connectors screen, so they exclude on purpose what the
    operator does not have to connect: `app-tools` (its own tools) and WAI's native MCPs like
    `wai-odoo`, whose credentials the platform injects. For that screen it is correct; for
    the CARD a colleague reads it is exactly the opposite — the Odoo Employee came out saying
    it reaches no system, and its only system is Odoo. A colleague decides whether to call
    you by what you REACH, not by what has to be plugged in.

    Only what is not a business system is excluded: `app-tools` (itself) and the
    `empleado-*` (colleagues, which are counted separately and are not a system)."""
    try:
        cfg = json.loads(Path(WAI_APP_JSON_PATH).read_text())
    except Exception:  # noqa: BLE001 — manifiesto opcional / ilegible
        return []
    agent_app = cfg.get("agent_app", cfg) if isinstance(cfg, dict) else {}
    fichas = fichas_de_conectores()
    fuera = {"app-tools"}
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for mcp in agent_app.get("mcps", []) or []:
        if isinstance(mcp, str):
            mcp = {"name": mcp}
        if not isinstance(mcp, dict):
            continue
        name = (mcp.get("name") or "").strip()
        if not name or name in seen or name in fuera or name.startswith("empleado-"):
            continue
        ficha = fichas.get(name) or {}
        display = ((ficha.get("display_name") or mcp.get("display_name") or "").strip()
                   or name.replace("composio-", "").replace("-", " ").replace("_", " ").title())
        seen.add(name)
        out.append({"name": name, "display_name": display})
    return out


def composio_connectors() -> list[dict[str, str]]:
    """Project the manifest's external Composio MCP connectors to
    ``{name, toolkit, display_name}``. ``name`` is the unique connector key
    (``agent_app.mcps[].name``) used for the per-connector identity and the
    opencode mcp key. A connector is "Composio-external" when it declares
    ``auth_type == "composio"`` or uses the ``composio-<toolkit>`` name
    convention. Odoo is excluded — it is served by the WAI-native ``wai-odoo``
    MCP, not by this app-resolved bridge.
    """
    try:
        cfg = json.loads(Path(WAI_APP_JSON_PATH).read_text())
    except Exception:  # noqa: BLE001 — manifest optional / malformed
        return []
    agent_app = cfg.get("agent_app", cfg) if isinstance(cfg, dict) else {}
    fichas = fichas_de_conectores()
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for mcp in agent_app.get("mcps", []) or []:
        if isinstance(mcp, str):
            mcp = {"name": mcp}
        if not isinstance(mcp, dict):
            continue
        name = (mcp.get("name") or "").strip()
        if not name or name in seen:
            continue
        ficha = fichas.get(name) or {}
        auth_type = ((ficha.get("auth_type") or mcp.get("auth_type") or "")
                     .strip().lower())
        toolkit = ((ficha.get("composio_toolkit") or mcp.get("composio_toolkit") or "")
                   .strip().lower())
        # Three ways of being "Composio's", and the third is new: an OWN WAI MCP that inside
        # asks Composio for the user's credentials. In the catalog it appears as `api_key`
        # —its own lock— but for the operator it is exactly the same: their account has to be
        # connected or the connector can do nothing. Without this line, the screen showed it
        # as "connected · no authentication" and there was no way to connect it: a useless
        # connector without saying why.
        is_composio = (auth_type == "composio" or name.startswith("composio-")
                       or bool(toolkit))
        if not is_composio:
            continue
        if not toolkit and name.startswith("composio-"):
            # Historical convention of the catalog, not a deduction: `composio-gmail` IS the
            # `gmail` toolkit. Outside it NOTHING is invented — deducing the toolkit from the
            # name is what broke the Odoo 17/18 connector.
            toolkit = name[len("composio-"):]
        toolkit = toolkit.strip().lower()
        if not toolkit:
            logger.warning("El conector «%s» no dice a qué toolkit pertenece: la plataforma "
                           "no lo tiene en su catálogo o su ficha está incompleta", name)
            continue
        # Odoo is excluded by NAME and not by toolkit: what is not connected here is the native
        # MCP `wai-odoo` (its credentials are injected by the platform). An own connector
        # against Odoo —the 17/18 one, for example— uses the same toolkit and DOES have to be connected.
        if name in ("wai-odoo", "odoo") or name.startswith("composio-odoo"):
            continue
        seen.add(name)
        display = (
            (mcp.get("display_name") or "").strip()
            or toolkit.replace("-", " ").replace("_", " ").title()
        )
        out.append({"name": name, "toolkit": toolkit, "display_name": display,
                    "mcp_server_id": ficha.get("id")})
    return out


def custom_connectors(fresco: bool = False) -> list[dict[str, str]]:
    """Manifest MCP servers that are NEITHER Composio-backed NOR internal
    (``app-tools`` / WAI-native ``wai-odoo``) — e.g. a WAI-custom MCP like
    ``cluster-mcp`` wired to a static URL/token at DEPLOY time. They have no
    per-user auth (nothing for the operator to "connect"), but they ARE what the
    agent uses at runtime, so the UI must SURFACE them (read-only "connected",
    governable write-policy) instead of hiding a connector the agent depends on.
    Same ``{name, toolkit, display_name}`` shape as :func:`composio_connectors`
    (``toolkit == name``: a custom MCP has no Composio toolkit). v7.135.0."""
    try:
        cfg = json.loads(Path(WAI_APP_JSON_PATH).read_text())
    except Exception:  # noqa: BLE001 — manifest optional / malformed
        return []
    agent_app = cfg.get("agent_app", cfg) if isinstance(cfg, dict) else {}
    fichas = fichas_de_conectores(fresco=fresco)
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for mcp in agent_app.get("mcps", []) or []:
        if isinstance(mcp, str):
            mcp = {"name": mcp}
        if not isinstance(mcp, dict):
            continue
        name = (mcp.get("name") or "").strip()
        if not name or name in seen:
            continue
        ficha = fichas.get(name) or {}
        auth_type = ((ficha.get("auth_type") or mcp.get("auth_type") or "")
                     .strip().lower())
        # Same rule as in `composio_connectors`, so as not to list it TWICE: if it declares a
        # toolkit, its connection is Composio's and it is connected there, even if its
        # auth_type is `api_key` (the own connector's lock, which is something else).
        if (auth_type == "composio" or name.startswith("composio-")
                or (ficha.get("composio_toolkit")
                    or mcp.get("composio_toolkit") or "").strip()):
            continue  # Composio → composio_connectors()
        if name in ("app-tools", "wai-odoo"):
            continue  # internal / WAI-native, not an operator-facing connector
        seen.add(name)
        display = (
            (mcp.get("display_name") or "").strip()
            or name.replace("-", " ").replace("_", " ").title()
        )
        # Two different things that do not have to be connected, and better not to call them
        # the same:
        #   · with its own key, set at deploy time (a Postgres, an API with its api key);
        #   · internal to the platform, which authenticates against nothing.
        # Saying "no authentication" of the first is false —it does authenticate, with its
        # key— and leaves whoever reads it believing their connector is open.
        con_clave = auth_type in ("api_key", "bearer")
        # And if that key is set by EACH ONE —`requires_user_auth`, which is what a connector
        # built in WAI declares since the key stopped travelling in the catalog card—, then
        # there is something to connect here. Saying "requires no authentication" of one of
        # those was false and left whoever read it waiting for it to work on its own.
        hay_que_conectarlo = bool(ficha.get("requires_user_auth")) and auth_type == "api_key"
        out.append({"name": name, "toolkit": name, "display_name": display,
                    "credencial": "propia" if con_clave else "ninguna",
                    "necesita_llave": hay_que_conectarlo,
                    "wai_mcp_server_id": ficha.get("id") if hay_que_conectarlo else None,
                    # The platform says it in the card: it is the only one that knows who has
                    # connected what. Without this it had to be assumed, and assuming
                    # "connected" is exactly what made a keyless connector announce itself as ready.
                    "conectado": bool(ficha.get("connected")) if hay_que_conectarlo else True,
                    # The ones the catalog already knows. They serve as a fallback when live
                    # discovery has not seen anything yet —the pod has just started, or the
                    # connector has just been connected—: without them the permissions screen
                    # is left with nothing to govern exactly when one is about to configure it.
                    "tools_catalogo": [h for h in (ficha.get("tools") or [])
                                       if isinstance(h, dict) and h.get("name")]})
    return out


def composio_tools_hint() -> str:
    """Always-present guidance teaching the agent HOW to drive its Composio
    connectors — the agent-app's equivalent of the code agent's tool-router
    hint (config_generator ``_composio_hint`` + the ``wai-composio-integrations``
    skill). WITHOUT it, a router-mode agent sees ONLY the ``COMPOSIO_*``
    meta-tools, doesn't realise it must drive them, describes the integration
    abstractly and then falls back to "I have no access, send me a screenshot".

    Returns "" when the app declares no Composio connectors. Mode-agnostic: it
    points the agent at its OWN tool list (router = meta-tools only; raw =
    direct tools), so it stays correct whichever mode the operator picks."""
    # Connectors built in WAI go FIRST and on their own: their tools are direct and named,
    # without meta-tools or a router in between. Without this part, an employee whose only
    # connector is its own received NO explanation —this function returned "" as there were
    # no Composio connectors— and answered "I have no access" with the tool loaded. It was
    # right according to what it had been told.
    propios = [c for c in custom_connectors() if c.get("conectado")]
    aviso_propios = ""
    if propios:
        detalle = []
        for c in propios:
            hs = ", ".join(f"`{h['name']}`" for h in (c.get("tools_catalogo") or [])[:8])
            detalle.append(f"**{c['display_name']}** (`{c['name']}`)"
                           + (f" — its tools: {hs}" if hs else ""))
        aviso_propios = (
            "\n\nYOUR IN-HOUSE CONNECTORS (deployed in WAI, not Composio's) — "
            + "; ".join(detalle) + ".\n"
            "They are connected and their tools are IN YOUR LIST with their exact name: "
            "call them directly, without meta-tools or a previous search. NEVER say "
            "that you \"have no access\" or that \"the connector is not available\" without "
            "having looked at your tool list and tried the call: if something fails, report "
            "the error it returns, which is useful information — \"it is not available\" "
            "is not.\n"
        )

    # The ones it has that are NOT connected are said too. Knowing it changes the answer:
    # "this connector is not connected, ask whoever administers the employee to set its key"
    # is actionable; "I have no access" leaves whoever asks without knowing what to do.
    sin_conectar = [c for c in custom_connectors() if c.get("necesita_llave")
                    and not c.get("conectado")]
    if sin_conectar:
        aviso_propios += (
            "\n\nUNCONNECTED CONNECTORS — "
            + "; ".join(f"**{c['display_name']}** (`{c['name']}`)" for c in sin_conectar)
            + ".\nThey are assigned to you but they lack their key, so their tools are NOT "
            "in your list. If someone asks you for something that needs one of them, say so "
            "as it is: it has to be connected from the employee's Connectors screen. It is "
            "not that you have no access: a step is missing, and saying which one is the "
            "difference between it getting fixed or not.\n"
        )

    conns = composio_connectors()
    if not conns:
        return aviso_propios
    names = ", ".join(f"**{c['display_name']}** (`{c['name']}`)" for c in conns)
    return aviso_propios + (
        "\n\nEXTERNAL CONNECTORS (Composio) — " + names + ".\n"
        "These integrations (e.g. the CRM) are ALREADY connected; the platform manages "
        "the access. NEVER say that you \"have no access\", nor ask for screenshots/exports "
        "or credentials: operate with their tools. How to use them — LOOK AT YOUR TOOL "
        "LIST:\n"
        "- The tools your capabilities declare usually appear DIRECT, as "
        "`<conector>_<SLUG>` next to the meta-tools (preloaded, with their schema): call "
        "them directly — never `SEARCH_TOOLS`, `GET_TOOL_SCHEMAS` or `MULTI_EXECUTE_TOOL` "
        "for a tool you already see in your list. The meta-tools are for the rest.\n"
        "- If of a connector you only see the meta-tools `<conector>_COMPOSIO_SEARCH_TOOLS` "
        "and `<conector>_COMPOSIO_MULTI_EXECUTE_TOOL` (router mode), there are NO direct "
        "tools. **And the first step depends on whether you ALREADY KNOW the slug:**\n"
        "  · If the tool is declared in your capabilities (with its connector and its "
        "slug), DO NOT SEARCH: go straight to `<conector>_COMPOSIO_MULTI_EXECUTE_TOOL` with "
        "that `tool_slug`. Searching for what you already have costs a call and a whole "
        "turn of deliberation, and it is the measured cause of the long times (19/09/2026: "
        "a \"which trips do I have?\" of 41 s with the slug written in its own definition). "
        "The router session is already open and warm: `MULTI_EXECUTE` does not depend on a "
        "previous `SEARCH`.\n"
        "  · If you really do NOT know which tool is needed: 1) "
        "`<conector>_COMPOSIO_SEARCH_TOOLS` (describe the task as `use_case`; it returns "
        "the plan + the slugs), 2) execute.\n"
        "  · `<conector>_COMPOSIO_GET_TOOL_SCHEMAS` (input schemas) ONLY for a tool whose "
        "arguments you do not have. The tools in your capabilities' ROUTES come with their "
        "arguments written next to them: call those directly, without asking for the schema "
        "(measured on 07/10/2026: 3 of 18 round trips of a turn went on schemas the "
        "platform already had). 3) `<conector>_COMPOSIO_MULTI_EXECUTE_TOOL` (executes; "
        "group only independent calls).\n"
        "- If you see direct tools `<conector>_<TOOL>`, call them directly.\n"
        "For any CRM/email/calendar/chat/drive request, resolve it "
        "with these tools before saying you cannot.\n"
        "IF, when you need it, you see NO tool at all of a connector (not even its "
        "meta-tools `<conector>_COMPOSIO_*`): that connector is STILL connecting or is "
        "not authorized. Tell the user naturally — \"I'm connecting to <conector>, try "
        "again in a few seconds\", in their language — and NEVER invent data (figures, "
        "contacts, opportunities, amounts, records): never state a number or piece of "
        "data you have not obtained from a tool in THIS conversation."
    )


def _account_email(account: Optional[dict[str, Any]]) -> Optional[str]:
    if not isinstance(account, dict):
        return None
    for key in ("account", "email", "user_email", "connected_account_email", "user_id"):
        v = account.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


async def _upsert_binding(values: dict[str, Any]) -> None:
    values = dict(values)
    values["updated_at"] = datetime.now(timezone.utc)
    stmt = sqlite_insert(McpConnector).values(**values).on_conflict_do_update(
        index_elements=[McpConnector.connector_name],
        set_={k: v for k, v in values.items() if k != "connector_name"},
    )
    async with app_write() as s:
        await s.execute(stmt)
        await s.commit()


async def resolve_connector(conn: dict[str, str], autonomous: str,
                            necesita_url: bool = True) -> dict[str, Any]:
    """Resolve a single connector: check the per-connector connected account,
    generate the signed bridge URL when it is needed, and persist the binding.
    Returns a status dict (never raises).

    ``necesita_url=False`` in ROUTER mode, and it is not an optimization: it is what
    prevented half of the catalog from working. Creating the MCP server per toolkit
    (`ensure_server`) is only needed to sign `raw`'s bridge URL; the router mounts its
    session without it. But it was called ALWAYS and FIRST, so a toolkit whose name Composio
    rejects died there — before checking whether it had an account, before reaching the router.

    And it rejects the name often: `wai-{toolkit}` is composed, and Composio slugs carry an
    underscore (`google_maps`, `browser_tool`, `composio_search`), which Composio does not
    admit in an MCP server's name. Measured on 21/08/2026: of twelve toolkits in the
    catalog, the THREE with an underscore were exactly the three that did not work in any
    employee — and they worked in the code agent, which goes through the router and never
    passes through here.

    **Toolkits WITHOUT authentication** (`browser_tool`, `composio_search`,
    `weathermap`): they do not and will not have a connected account — the screen rightly
    does not offer "Connect"—, so requiring one left them out forever. They are marked as
    ready and the auth-service already knows how to declare them to the router
    (`no_auth_apps`); here they only have to be let through."""
    name = conn["name"]
    toolkit = conn["toolkit"]
    identity = ""
    server_id = ""
    url = ""
    status = "error"
    status_reason: Optional[str] = None
    account_email: Optional[str] = None
    connection_id: Optional[str] = None
    try:
        # The SAME entity the Connectors screen authorizes (`segmento_de_identidad`): asking
        # about another is saying "not connected" of an account that is.
        segmento = segmento_de_identidad(conn)
        identity = composio_identity(autonomous, segmento)

        # Does this toolkit authenticate against something? If not, there will never be an
        # account and asking for one condemns it. When in doubt it is assumed it DOES
        # authenticate: a connector that asks to be connected too much is seen and fixed; one
        # taken as ready without being so fails on the user's first call.
        sin_auth = False
        try:
            info = await mc.auth_info(toolkit)
            sin_auth = (str((info or {}).get("connection_mode") or "").strip().lower()
                        == "none")
        except Exception as exc:  # noqa: BLE001 — not knowing cannot bring startup down
            logger.info("auth_info(%s) no disponible: %s", toolkit, exc)

        # Only `raw` needs the MCP server per toolkit, and it is the call Composio rejects
        # when the slug carries an underscore. In router it is skipped.
        if necesita_url:
            server_id = await mc.ensure_server(toolkit)

        connections = await mc.list_connections(autonomous, segmento)
        account = mc.connected_account_for(connections, toolkit)
        if account is None and sin_auth:
            # Ready without an account: it is what the screen already says ("connected · no
            # authentication") and what the runtime did not know how to respect.
            status = "active"
        elif account is None:
            status = "pending"
            status_reason = "no Composio account authorised for this connector yet"
        else:
            acc_status = (account.get("status") or "").upper()
            account_email = _account_email(account)
            connection_id = account.get("id") or None
            if acc_status == "ACTIVE":
                if not necesita_url:
                    # Router: the account is alive and that is enough; the session is mounted
                    # by the caller, with this `connection_id`.
                    status = "active"
                else:
                    url = await mc.generate_url(autonomous, segmento, server_id)
                    if url:
                        status = "active"
                    else:
                        status = "error"
                        status_reason = "generate-url returned empty for an active account"
            else:
                status = "unauthorized"
                status_reason = f"Composio account status={acc_status or 'UNKNOWN'}"
    except mc.McpConnectorError as exc:
        status_reason = str(exc)
        logger.warning("resolve_connector(%s) failed: %s", name, exc)
    except Exception as exc:  # noqa: BLE001 — never break the whole block
        status_reason = str(exc)
        logger.warning("resolve_connector(%s) crashed: %s", name, exc)

    await _upsert_binding({
        "connector_name": name,
        "toolkit": toolkit,
        "composio_user_id": identity or None,
        "composio_server_id": server_id or None,
        "composio_connection_id": connection_id,
        "connected_account_email": account_email,
        "status": status,
        "status_reason": status_reason,
        "last_resolved_at": datetime.now(timezone.utc),
    })

    return {
        "name": name,
        "toolkit": toolkit,
        "display_name": conn.get("display_name") or toolkit.title(),
        "status": status,
        "status_reason": status_reason,
        "connected": status == "active",
        "account_email": account_email,
        "connection_id": connection_id,
        "url": url,
        # Which catalog card it is. The own OAuth application is stored per CONNECTOR, so
        # without this it would have to be guessed by the name — which is exactly how the
        # Odoo 17/18 connector broke.
        "mcp_server_id": conn.get("mcp_server_id"),
    }


async def _router_tool_denies(toolkits: list[str]) -> dict[str, list[str]]:
    """Per-toolkit slugs to HIDE from the router session — the tools the platform's
    permissions table DENIES for this employee, grouped by toolkit via the ``{TOOLKIT}_``
    prefix.

    A UX filter, not the gate: the AUTHORITATIVE allow/ask/deny lives in the app-pod proxy
    (``mcp_policy.decide_call``). ``ask`` tools are DELIBERATELY still exposed — the agent
    may call them and the proxy PARKS them for approval (works on autonomous runs)."""
    from app import mcp_policy
    tabla = await mcp_policy.load_table()
    denied_tools = [str(r.get("name")) for c in (tabla or {}).get("connectors") or []
                    for r in c.get("tools") or []
                    if mcp_policy._perm(r.get("permission")) == mcp_policy.PERM_DENY and r.get("name")]
    out: dict[str, list[str]] = {}
    for tk in toolkits:
        prefix = f"{tk.upper()}_"
        d = sorted({k for k in denied_tools if k.upper().startswith(prefix)})
        if d:
            out[tk] = d
    return out


# Live registry of each connector's REAL upstream MCP URL (signed Composio
# URL / router session). Since v7.26.0 the URL handed to the agent pod is
# the app-pod POLICY PROXY — the signed upstream never leaves this pod. The
# registry is refreshed on every resolve (boot + the agent's ~2 min
# refresher) and read by ``routes/mcp_proxy_routes.upstream_for``.
_UPSTREAM_URLS: dict[str, str] = {}


def upstream_url_for(connector: str) -> str:
    return _UPSTREAM_URLS.get((connector or "").strip().lower(), "")


def _proxy_url(name: str) -> str:
    """Bridge-facing URL: the app pod's policy proxy for this connector.
    ``APP_INTERNAL_BASE_URL`` is injected by the deployer (the app Service
    as seen FROM the agent pod). When absent (older deployer) no bridge URL
    can be built and the connector is disabled — see ``_bridge_url``."""
    import os
    base = (os.environ.get("APP_INTERNAL_BASE_URL") or "").rstrip("/")
    token = os.environ.get("APP_MCP_TOKEN") or ""
    if not base or not token:
        return ""
    return f"{base}/mcp-proxy/{token}/{name}"


def _bridge_url(name: str, upstream: str, *, policy: str = "write") -> str:
    """ALWAYS the policy proxy, never the raw upstream. The proxy is not just
    the write-policy: it also enforces per-tool allow/ask/deny, the global and
    per-connector defaults, and parks ``ask`` calls for operator approval — a
    ``write`` connector bridged direct would bypass ALL of that with a
    credentialed URL (the v7.29.1 fallback assumed "nothing to enforce" for
    write, which stopped being true when tool permissions landed). So when the
    proxy URL can't be built (older deployer without ``APP_INTERNAL_BASE_URL``/
    ``APP_MCP_TOKEN``) EVERY connector is DISABLED, whatever its policy —
    fail closed and tell the operator to upgrade the deployer (v7.89.2;
    surfaced as the ``platform_degraded`` health blocker)."""
    _UPSTREAM_URLS[(name or "").strip().lower()] = upstream or ""
    if not upstream:
        return ""
    proxied = _proxy_url(name)
    if proxied:
        return proxied
    logger.error(
        "APP_INTERNAL_BASE_URL/APP_MCP_TOKEN missing — DISABLING connector %s "
        "(policy=%s) rather than bypassing the policy proxy (per-tool "
        "permissions, ask-approval and write-policy all live there). "
        "Upgrade the deployer.", name, policy,
    )
    return ""


# ── Router-session cache (router mode) ──────────────────────────────────────
# Composio's ``POST /tool_router/session`` is NOT idempotent: every call mints a
# FRESH session (new ``session_id`` + url), leaks the previous one, and — until
# Composio warms it — the new session serves NO tools for a few seconds to a few
# minutes. The manifest's ~2-min refresher re-resolves on every tick, so it used
# to REPOINT the live upstream (``_UPSTREAM_URLS``) to a brand-new COLD session
# under a running agent: a tool call landing right after a refresh failed
# mid-conversation ("falla en mitad"). And at boot the first session opencode
# created could bind against a not-yet-warm session and — because opencode fixes
# a session's tool list at creation — stay PERMANENTLY without that connector's
# tools (only a fresh session recovered, and nothing forced one).
#
# Fix: create ONE session per connector and REUSE it while its binding is
# unchanged. Recreate ONLY when the binding key changes (identity / toolkit /
# per-tool denies / read-only / connected-account) or the proxy reports the
# upstream expired (:func:`invalidate_router_session`). Warm-up readiness is
# handled OUT of band (the boot ``/agent-runtime/ready`` gate), never by
# re-minting the session.
_ROUTER_SESSIONS: dict[str, dict[str, Any]] = {}  # name -> {"key": str, "url": str}


#: Most tools preloaded per connector: each schema travels in the agent's context on every step.
MAX_PRELOAD = 24


def herramientas_declaradas(servidor: str) -> list[str]:
    """The slugs of `servidor` that THIS employee's definition says it uses (14.24.0).

    The definition names them `{servidor}_{SLUG}` (the server has no `_`): in each capability's
    `apoyo`, its `resuelve_con`, the calls of its `ruta`, and in `contexto_de_turno`. Those are
    preloaded in the connector's router session, so the agent calls them DIRECTLY instead of
    `SEARCH_TOOLS` + `GET_TOOL_SCHEMAS` before every use — 30 % of the time of the travel
    assistant's answers (measured 12/10/2026). Only what is declared: nothing is guessed."""
    try:
        from app.conversacion import declaracion  # local: conversacion imports this module
        d = declaracion() or {}
    except Exception:  # noqa: BLE001 — no definition, nothing to preload (the usual session)
        return []
    nombre = (servidor or "").strip().lower()
    out: list[str] = []

    def anotar(x: Any) -> None:
        if isinstance(x, dict):
            x = x.get("herramienta")
        srv, _, slug = str(x or "").strip().partition("_")
        slug = slug.strip().upper()
        if srv.strip().lower() == nombre and slug and not slug.startswith("COMPOSIO_") and slug not in out:
            out.append(slug)

    for c in d.get("hago") or []:
        if not isinstance(c, dict):
            continue
        for a in c.get("apoyo") or []:
            anotar(a)
        anotar(c.get("resuelve_con"))
        for ronda in ((c.get("ruta") or {}).get("rondas") or []) if isinstance(c.get("ruta"), dict) else []:
            for ll in (ronda if isinstance(ronda, list) else (ronda or {}).get("llamadas") or []):
                anotar(ll)
    for x in d.get("contexto_de_turno") or []:
        anotar(x)
    return out[:MAX_PRELOAD]


def _router_session_key(
    identity: str,
    toolkit: str,
    denies: dict[str, list[str]],
    read_only: bool,
    connection_id: Optional[str],
    auth_config_id: str = "",
    preload: Optional[list[str]] = None,
) -> str:
    """Stable fingerprint of everything that changes a router session's binding.
    Reuse is safe iff this key is unchanged.

    `auth_config_id` goes into the fingerprint because changing the application changes the
    gate: without it, a cached session would keep being served open with the OLD
    application and the employee would work with an account that is no longer valid, without
    a visible error."""
    payload = {
        "identity": identity or "",
        "toolkit": (toolkit or "").strip().lower(),
        "denies": {k: sorted(v or []) for k, v in sorted((denies or {}).items())},
        "read_only": bool(read_only),
        "connection_id": connection_id or "",
        "auth_config_id": auth_config_id or "",
        # What is preloaded changes the session too: a newly declared tool must reach the agent.
        "preload": sorted(preload or []),
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def invalidate_router_session(connector: str) -> None:
    """Drop a connector's cached router session so the next resolve RE-CREATES it.
    Called by the app proxy when the upstream rejects with auth/expired — the
    session self-heals lazily on the next resolve instead of being re-minted on a
    timer (which was the churn this cache removes)."""
    name = (connector or "").strip().lower()
    _ROUTER_SESSIONS.pop(name, None)
    _UPSTREAM_URLS.pop(name, None)


def _ficha(nombre: str) -> dict[str, Any] | None:
    """A connector's card by name, case-insensitive."""
    clave = (nombre or "").strip().lower()
    for otro, ficha in fichas_de_conectores().items():
        if (otro or "").strip().lower() == clave:
            return ficha
    return None


def _es_de_composio(ficha: dict[str, Any], nombre: str) -> bool:
    """Same rule as in `custom_connectors`, and for the same reason: if it declares a toolkit
    —or is called `composio-*`— the connection is Composio's and its path resolves it, even
    if its `auth_type` is `api_key` (which is something else: the connector's lock). Having
    it written twice with different nuances is how you end up hijacking someone else's
    connector."""
    return (bool((ficha.get("composio_toolkit") or "").strip())
            or (nombre or "").strip().lower().startswith("composio-"))


def upstream_propio(nombre: str, acting_user: str = "") -> dict[str, Any] | None:
    """With which URL and which header an own-lock connector is talked to (one built in WAI,
    or an external one with its api-key), or `None` if it is not one of those or not
    connected yet.

    The platform says it in its card (`opencode_entry`: url + header with the key), and it
    is asked LIVE —one-minute cache—, not frozen at deploy time. That is the whole difference
    between "I connect the connector and the employee already uses it" and "I connect the
    connector and it has to be redeployed": a connection's key is pasted —or rotated— long
    after the employee exists, and the pod's environment was written on deployment day.

    `acting_user`: the turn's person. If there is one, for the connectors that DELEGATE
    identity (`wai_app`) the entry signed BY THEM is requested instead of the owner's
    (cached) — so "put in MY profile…" ends up in the profile of whoever asks. For an
    `api_key` nothing changes. Without a person (autonomous run), the owner's as usual.

    It is only used by this pod's policy proxy (`routes/mcp_proxy_routes`): the key lives
    HERE and does not travel to the agent's pod, just like Composio's."""
    ficha = _ficha(nombre)
    if not ficha or _es_de_composio(ficha, nombre):
        return None
    entrada = ficha.get("opencode_entry") or {}
    if acting_user:
        propia = _entrada_por_persona(nombre, acting_user)
        if propia and str(propia.get("url") or "").strip():
            entrada = propia
    url = str(entrada.get("url") or "").strip()
    if not url:
        return None  # it is one of ours, but not connected: no URL or key to use
    cabeceras = dict(entrada.get("headers") or {})
    # WHERE the files of whoever asks are. Without this, a connector to a WAI application
    # cannot pick up what the user has just uploaded to the chat: the file lives in THIS
    # employee's workspace, and the connector only knows how to reach the platform and its
    # own application — neither of which has it.
    #
    # The employee tells it and not the platform because the employee knows its address
    # with certainty (`APP_INTERNAL_BASE_URL`, set by the deployer), while the platform
    # would have to recompose a Service name.
    #
    # It is not a credential: the download still requires the token of whoever asks, which
    # the connector forwards. Knowing the URL gives access to nothing.
    base = (os.environ.get("APP_INTERNAL_BASE_URL") or "").rstrip("/")
    if base:
        cabeceras["X-WAI-Workspace-Url"] = f"{base}/api/agent/workspace/download"
    return {"url": url, "headers": cabeceras}


def es_wai_app(nombre: str) -> bool:
    """Does this connector DELEGATE its identity to a WAI application (`wai_app`)?

    Only these sign the token for the turn's PERSON instead of for the owner; for the rest
    (Composio, api_key…) the person changes nothing. The catalog says it (non-secret field
    `wai_app`), NOT the name or the `auth_type` —which any external one can also carry as
    `bearer`—. The policy proxy uses it for two things and only for these connectors:
    declaring `run_id` in its `tools/list` (so the model fills it in) and deriving from the
    run who the person is in a `tools/call`."""
    ficha = _ficha(nombre)
    return bool(ficha and ficha.get("wai_app"))


def espera_llave(nombre: str) -> bool:
    """Is it an own-lock connector that has NOT been given its key yet?

    It serves one single thing: giving an error that can be understood. The deployer
    publishes the connector in the agent's configuration as soon as it is in the catalog,
    connected or not, so the agent will try it anyway; without this, what it sees is a 401
    from the connector that does not explain that the only thing missing is pasting the key
    in Connectors."""
    ficha = _ficha(nombre)
    if ficha is not None:
        return (
            not _es_de_composio(ficha, nombre)
            and (ficha.get("auth_type") or "").strip().lower() == "api_key"
            and bool(ficha.get("requires_user_auth"))
            and not (ficha.get("opencode_entry") or {}).get("url")
        )
    return False


def _bloque_de_conectores_propios() -> dict[str, Any]:
    """The opencode entries of the in-house connectors: ALWAYS the policy proxy.

    The platform says how the connector is talked to (URL and header with the key) and here
    it is decided who does it: this pod. The agent gets the same kind of entry as for any
    other connector —the `/mcp-proxy/…` URL, without a credential— and that way a call of
    its goes through the write veto, the per-tool permissions and the parking of those that
    ask for approval. Giving it the platform's direct entry, with the key inside, skipped
    the three things; and moreover it clashed with the entry —proxy— the deployer already
    writes for it, two definitions of the same server in two different files: opencode kept
    the deployer's, called the proxy, the proxy did not know about this connector and
    answered 401. The agent said "I don't have that tool" and there was not a single error.

    Without `opencode_entry` the connector cannot be plugged in yet (not connected, no key):
    it is not published. Better that the agent does not see the tool than that it sees it
    and fails with a 401 it will not be able to explain."""
    salida: dict[str, Any] = {}
    for nombre in list(fichas_de_conectores()):
        # ONE single rule of "this connector is mine", `upstream_propio`'s, so what is
        # published to the agent and what the proxy knows how to resolve can never disagree.
        if not upstream_propio(nombre):
            continue
        url = _proxy_url(nombre)
        if not url:
            logger.error(
                "APP_INTERNAL_BASE_URL/APP_MCP_TOKEN missing — DISABLING el conector propio "
                "%s en vez de darle su llave al pod del agente (saltaría el veto de "
                "escritura y los permisos por herramienta). Actualiza el desplegador.", nombre,
            )
            continue
        # `oauth: false` is mandatory: without it opencode starts discovering OAuth against a
        # server that does not speak it and ends up listing not a single tool, silently.
        salida[nombre] = {"type": "remote", "url": url, "enabled": True, "oauth": False}
    return salida


async def resolve_runtime_mcp_block() -> dict[str, Any]:
    """Resolve the manifest Composio connectors into the opencode ``mcp`` block
    the agent merges into ``opencode.json`` at boot.

    Mode (operator-set, default ``router``):
    * **router** — one scoped Composio Tool Router gateway PER CONNECTOR
      (``{connector_name}``, meta-tools), each session keyed on that connector's
      OWN per-connector identity so it owns the bound account. (A single
      per-agent session can't cover per-connector identities — Composio rejects
      cross-user ``connected_accounts`` with code 4306, leaving the agent with
      no tools.)
    * **raw** — one ``streaming-http-proxy`` bridge per connector (active = its
      signed URL; inactive = a disabled placeholder the operator can authorise).

    v7.26.0: in BOTH modes the bridge URL handed to the agent pod is the
    app-pod POLICY PROXY (``/mcp-proxy/…``) — vetoes writes per the
    connector's write-policy and keeps every credential in this pod. A
    ``read_only`` connector additionally gets its router session created
    with Composio's own read-only belt.
    """
    autonomous = (AUTONOMOUS_USER_EMAIL or "").strip().lower()
    block: dict[str, Any] = {}

    # Connectors built in WAI go FIRST and on their own: they are not Composio's, so they
    # depend neither on the auth-service being there nor on there being a single connector
    # of it. Putting them after the two `return`s below left them out in the most common
    # case —an employee whose only connector is its own—, which is exactly the one being
    # tested: the agent said "the connector is not available in this session" while having it connected.
    block.update(_bloque_de_conectores_propios())

    if not autonomous or not mc.is_enabled():
        return block

    conns = composio_connectors()
    if not conns:
        return block

    # A bounded tool-router session PER CONNECTOR, each tied to that connector's OWN identity
    # (``composio_identity(email, conector)``) — the same one its account was authorized
    # under. Composio rejects a session that ties an account of another ``user_id`` (HTTP
    # 400, code 4306 ``ToolRouterV2_InvalidConnectedAccount``), so a single session per agent
    # cannot cover per-connector identities.
    # With which OAuth application each gate is opened. The SAME one the account was
    # authorized with: if the space's were used here on an account tied to the employee's,
    # Composio rejects it (4306) and the gate starts off, without saying anything.
    _apps = await mc.apps_propias()

    for conn in conns:
        # Odoo is served by the WAI-native `wai-odoo*` MCP (one entry per Odoo connector, with its write gate): a router
        # bridge for the 17/18 connector was a second path to the same account that skipped the gate (18/10/2026).
        if (conn.get("toolkit") or "").strip().lower() == "odoo":
            continue
        # The router does NOT need the MCP server per toolkit: asking for it here was what
        # killed the toolkits with an underscore before they started.
        res = await resolve_connector(conn, autonomous, necesita_url=False)
        name = res["name"]
        tk = res["toolkit"]
        policy = await effective_policy(name)
        url = ""
        # A session is mounted if the connector is ready. "Ready" is TWO cases, and before
        # only one was considered: with a connected account (the norm), or WITHOUT
        # authentication —`browser_tool`, `composio_search`, `weathermap`—, which have no
        # account and never will. The auth-service already knows how to declare those to the
        # router (`no_auth_apps`); the only thing needed was letting them get here.
        if res.get("connected"):
            denies = await _router_tool_denies([tk])
            read_only = (policy == POLICY_READ_ONLY)
            precarga = herramientas_declaradas(name)
            key = _router_session_key(
                composio_identity(autonomous, segmento_de_identidad(conn)), tk, denies,
                read_only, res.get("connection_id"),
                _apps.get(str(res.get("mcp_server_id") or ""), ""), precarga,
            )
            cached = _ROUTER_SESSIONS.get(name)
            if cached and cached.get("key") == key and cached.get("url"):
                # REUSE the live session — do NOT mint a new one. Re-minting
                # here (once per ~2-min refresh) leaked the old session and
                # handed the running agent a COLD one → "falla en mitad".
                url = cached["url"]
            else:
                try:
                    # An account is only tied if there IS one. A toolkit without
                    # authentication has no `connection_id`, and sending
                    # `{toolkit: [None]}` is worse than sending nothing: the session is
                    # created tied to a non-existent account and the agent is left without
                    # tools, without a visible error.
                    cuentas = ({tk: [res["connection_id"]]}
                               if res.get("connection_id") else None)
                    # The session's `user_id` has to be the owner of the account being
                    # tied; with another, Composio rejects it and the agent is left without
                    # tools.
                    url = await mc.create_tool_router_session(
                        autonomous, segmento_de_identidad(conn), [tk],
                        cuentas, denies,
                        read_only=read_only,
                        auth_config_id=_apps.get(str(res.get("mcp_server_id") or ""), ""),
                        preload=precarga,
                    )
                except Exception as exc:  # noqa: BLE001 — never break the boot
                    logger.warning("tool-router session for %s failed: %s", name, exc)
                if url:
                    _ROUTER_SESSIONS[name] = {"key": key, "url": url}
                else:
                    _ROUTER_SESSIONS.pop(name, None)
        else:
            # No active account → no session; drop any stale cache entry so a
            # re-auth later mints a fresh one.
            _ROUTER_SESSIONS.pop(name, None)
        # One gateway entry per connector; disabled (placeholder) until its
        # account is active. A restricted connector fails CLOSED if the
        # proxy URL can't be built (see _bridge_url).
        bridged = _bridge_url(name, url, policy=policy)
        block[name] = {
            "type": "local",
            "command": [COMPOSIO_BRIDGE_BIN, name, bridged or _DISABLED_URL],
            "enabled": bool(bridged),
        }
    return block


async def list_connector_status() -> list[dict[str, Any]]:
    """Operator-facing status for every connector, reflecting the REAL runtime
    resolution (server + account + generate-url), not just the OAuth state.
    Reads the persisted binding when present, falling back to a live resolve."""
    autonomous = (AUTONOMOUS_USER_EMAIL or "").strip().lower()
    conns = composio_connectors()
    if not conns:
        return []
    apps = await mc.apps_propias()

    async def _app_del_toolkit(tk: str) -> bool:
        try:
            info = await mc.auth_info(tk)
        except Exception:  # noqa: BLE001 — with no answer nothing is offered
            return False
        if str(info.get("connection_mode") or "").lower() not in ("redirect", "custom_oauth"):
            return False
        return not bool(info.get("auth_config_managed"))
    rows_by_name: dict[str, McpConnector] = {}
    async with app_session() as s:
        rows = (await s.execute(select(McpConnector))).scalars().all()
        rows_by_name = {r.connector_name: r for r in rows}
    out: list[dict[str, Any]] = []
    for conn in conns:
        # v7.28.0 — surface the EFFECTIVE write-policy (platform value
        # hardened by the local setting) so Operations shows what the
        # proxy actually enforces for this connector.
        policy = await effective_policy(conn["name"])
        row = rows_by_name.get(conn["name"])
        if row is None:
            # Never resolved yet — do a live resolve so the first page load is
            # accurate (also seeds the binding row).
            res = await resolve_connector(conn, autonomous) if autonomous else {}
            out.append({
                "name": conn["name"],
                "toolkit": conn["toolkit"],
                "display_name": conn["display_name"],
                "status": res.get("status") or "pending",
                "status_reason": res.get("status_reason"),
                "connected": bool(res.get("connected")),
                "account_email": res.get("account_email"),
                "write_policy": policy,
                # Is there an OAuth application to administer, and is it this employee's?
                "app_oauth_propia": await _app_del_toolkit(conn["toolkit"]),
                "app_propia_mia": bool(apps.get(str(conn.get("mcp_server_id") or ""))),
            })
        else:
            out.append({
                "name": conn["name"],
                "toolkit": conn["toolkit"],
                "display_name": conn["display_name"],
                "status": row.status,
                "status_reason": row.status_reason,
                "connected": row.status == "active",
                "account_email": row.connected_account_email,
                "write_policy": policy,
                # Is there an OAuth application to administer, and is it this employee's?
                "app_oauth_propia": await _app_del_toolkit(conn["toolkit"]),
                "app_propia_mia": bool(apps.get(str(conn.get("mcp_server_id") or ""))),
            })
    return out
