"""Runtime configuration for an agent-app.

This module is imported at FastAPI startup. It MUST NOT raise if
`wai-app.json` cannot be found in the expected location — instead it
searches a small set of well-known paths so the pod boots cleanly in
local dev, in CI and inside the Kaniko-built container image.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def _candidate_paths() -> List[Path]:
    """Ordered list of places where ``wai-app.json`` may live."""
    here = Path(__file__).resolve()
    candidates: List[Path] = []

    # 1. Explicit override (useful for tests and for mounting as ConfigMap).
    override = os.environ.get("WAI_APP_JSON_PATH")
    if override:
        candidates.append(Path(override))

    # 2. Walk up from this file; covers the dev layout
    #    (project-root/src/backend/app/config.py) and the in-container
    #    layout (/app/src/backend/app/config.py or /app/app/config.py).
    for parent in here.parents:
        candidates.append(parent / "wai-app.json")

    # 3. Last-resort absolute fallback for the canonical container layout.
    candidates.append(Path("/app/wai-app.json"))

    # De-duplicate while preserving order.
    seen: set[str] = set()
    unique: List[Path] = []
    for c in candidates:
        s = str(c)
        if s not in seen:
            seen.add(s)
            unique.append(c)
    return unique


def _load_app_json() -> Dict[str, Any]:
    for candidate in _candidate_paths():
        try:
            if candidate.is_file():
                logger.info("Loading wai-app.json from %s", candidate)
                return json.loads(candidate.read_text(encoding="utf-8"))
        except OSError:  # pragma: no cover - unreadable path
            continue

    raise FileNotFoundError(
        "wai-app.json not found. Looked at: "
        + ", ".join(str(c) for c in _candidate_paths())
        + ". Make sure the Dockerfile copies it into /app "
          "(`COPY wai-app.json ./`) or set WAI_APP_JSON_PATH."
    )


APP_META: Dict[str, Any] = _load_app_json()
AGENT_APP: Dict[str, Any] = APP_META.get("agent_app") or {}
# Aliased name used by some routers for the full manifest dict.
APP_MANIFEST: Dict[str, Any] = APP_META

# Path to wai-app.json (re-resolved on demand by manifest_routes which
# reload to pick up live edits during dev). Falls back to /app/wai-app.json
# inside the container.
def _resolved_wai_app_path() -> Path:
    for c in _candidate_paths():
        if c.is_file():
            return c
    return Path("/app/wai-app.json")

WAI_APP_JSON_PATH: Path = _resolved_wai_app_path()

# ── Local SQLite (writable) ──────────────────────────────────────────
APP_DB_PATH = os.environ.get("APP_DB_PATH", "/app-data/app.sqlite")
APP_DATABASE_URL = os.environ.get(
    "APP_DATABASE_URL", f"sqlite+aiosqlite:///{APP_DB_PATH}"
)

# ── Project PostgreSQL (read-only) ───────────────────────────────────
PROJECT_DB_RO = {
    "host": os.environ.get("DB_HOST", ""),
    "port": int(os.environ.get("DB_PORT", "5432") or "5432"),
    "user": os.environ.get("DB_USER", ""),
    "password": os.environ.get("DB_PASSWORD", ""),
    "database": os.environ.get("DB_NAME", ""),
    "schema": os.environ.get("DB_SCHEMA", ""),
}
# Module-level shortcut for the schema (some routers import it directly).
DB_SCHEMA: str = PROJECT_DB_RO["schema"]


def _build_project_database_url() -> str:
    """Compose an asyncpg URL from the PROJECT_DB_RO dict.

    Returns an empty string in environments where the project DB is
    not wired (e.g. local frontend-only dev) so imports never fail —
    routers that hit the engine will surface the misconfiguration at
    request time.
    """
    cfg = PROJECT_DB_RO
    if not cfg["host"] or not cfg["database"]:
        return ""
    return (
        f"postgresql+asyncpg://{cfg['user']}:{cfg['password']}"
        f"@{cfg['host']}:{cfg['port']}/{cfg['database']}"
    )


DATABASE_URL: str = os.environ.get("DATABASE_URL") or _build_project_database_url()
PROJECT_ID = int(os.environ.get("PROJECT_ID", "0") or "0")

# ── Presentation / locale / timezone ─────────────────────────────────
# The whole app stores time in UTC and renders it in the BUSINESS
# timezone so cron schedules ("every day at 8am"), calendar items,
# due-dates and the chat clock all agree with the operator's wall
# clock — not the pod's UTC. Resolution order (first non-empty wins):
#   1. ``APP_TIMEZONE`` env (deployer / operator override)
#   2. ``agent_app.locale.timezone`` in wai-app.json (canonical: this is
#      where the generator/SPA put locale — see manifest-schema.md)
#   3. ``presentation.locale.timezone`` in wai-app.json (legacy path)
#   4. top-level ``locale.timezone`` in wai-app.json (legacy path)
#   5. ``"UTC"`` (safe default)
# Value must be an IANA name (e.g. "Europe/Madrid", "America/Bogota").
# NOTE: a missing timezone silently shifts EVERY cron schedule, calendar
# item and encargo due-date by the pod↔business offset (the wall-clock
# "11:40" the operator picked is stored as "11:40 UTC"). Always set
# ``agent_app.locale.timezone`` in the manifest — the verifier enforces it.
PRESENTATION: Dict[str, Any] = APP_META.get("presentation") or {}
LOCALE_CFG: Dict[str, Any] = PRESENTATION.get("locale") or {}


def _resolve_timezone() -> str:
    tz = (os.environ.get("APP_TIMEZONE") or "").strip()
    if not tz:
        tz = str((AGENT_APP.get("locale") or {}).get("timezone") or "").strip()
    if not tz:
        tz = str(LOCALE_CFG.get("timezone") or "").strip()
    if not tz:
        tz = str((APP_META.get("locale") or {}).get("timezone") or "").strip()
    if not tz:
        tz = "UTC"
    # Validate against the system tz database; fall back to UTC so a
    # typo in the manifest can never crash the boot.
    try:
        from zoneinfo import ZoneInfo  # noqa: WPS433 (local import)
        ZoneInfo(tz)
    except Exception:  # pragma: no cover — bad tz name
        logger.warning("Invalid APP_TIMEZONE %r — falling back to UTC", tz)
        tz = "UTC"
    return tz


APP_TIMEZONE: str = _resolve_timezone()

# ── Auth (JWT issued by WAI platform) ────────────────────────────────
# The token of whoever signs in is checked by ASKING the platform (introspection), not with
# the tenant's signing key (14.0.0). That key served to validate and also to MINT the token
# of any person in the tenant, and in this pod runs code written by an agent. The deployer
# always injects the URL (`{WAI_BACKEND_URL}/backend/auth/introspeccion`); without it
# startup refuses (main.py) instead of giving 401 to everyone.
WAI_INTROSPECCION_URL: str = (os.environ.get("WAI_INTROSPECCION_URL") or "").strip()

# Public HTTPS base URL of THIS agent app (its ingress host), injected by
# the deployer as ``https://{app}-{tenant}.{base_domain}``. Used to build
# short-lived signed media links that WhatsApp/Twilio fetches for outbound
# attachments (see ``app.signed_media``). Empty ⇒ outbound workspace files
# degrade to being named in the reply body.
APP_PUBLIC_BASE_URL: str = os.environ.get("APP_PUBLIC_BASE_URL", "")

# URL of the employee's GLOBAL MANUAL (generic guide of how ANY WAI employee works). It lives
# ONCE in the marketplace's employee-portal and is LINKED from every employee (the operator
# deploys it; updating it once updates it for everyone). The deployer injects it as
# platform-config; empty ⇒ the Manual page only shows this employee's specific manual.
EMPLOYEE_MANUAL_URL: str = os.environ.get("EMPLOYEE_MANUAL_URL", "")

# Public base of the employee's SPECIFIC manual (the ZIP served by the marketplace). With it
# + the employee's marketplace slug (agent_app.marketplace_slug) the link to its own manual
# is built: {base}/{slug}/manual/index.html. Platform value the deployer injects (e.g.
# https://employees.usewai.com/api/v1/public/agent-employees). Empty or an employee without
# a slug ⇒ the Manual page only shows the global link.
EMPLOYEE_MANUAL_BASE_URL: str = os.environ.get("EMPLOYEE_MANUAL_BASE_URL", "")

# Slug of THIS employee's marketplace listing, injected by the deployer as env (from
# agent_apps.marketplace_listing_slug: the platform stamps it when PUBLISHING the employee to
# the marketplace and propagates it on every deploy). It is the FALLBACK of
# wai-app.json.agent_app.marketplace_slug (which the install writes): it covers the
# publisher's ORIGINAL employees (origin code_agent) and those installed before that change,
# without rebuilding the image. Empty ⇒ only the manifest rules.
MARKETPLACE_SLUG: str = os.environ.get("MARKETPLACE_SLUG", "")

# ── Voice-note transcription (inbound channel audio → text) ───────────
# The deployer injects TRANSCRIPTION_API_KEY into the app pod (the SAME
# tenant OpenAI secret the agent pod uses) so the channels poller can
# transcribe WhatsApp voice notes via an OpenAI-compatible
# ``/audio/transcriptions`` endpoint. Empty key ⇒ transcription disabled
# (voice notes fall back to "received but not transcribed").
TRANSCRIPTION_API_KEY: str = os.environ.get("TRANSCRIPTION_API_KEY", "")
TRANSCRIPTION_BASE_URL: str = os.environ.get(
    "TRANSCRIPTION_BASE_URL", "https://api.openai.com/v1",
)
TRANSCRIPTION_MODEL: str = os.environ.get("TRANSCRIPTION_MODEL", "whisper-1")

# ── What THIS employee HAS (platform V101) ───────────────────────────
# Two capabilities decided AT DEPLOY time and not in Operations, because deciding that
# something does not exist belongs to whoever deploys. Both in BETA ⇒ off unless the
# deployment says otherwise, and off means they DO NOT EXIST: the talk button is not even
# painted (neither on web nor on mobile) and the written gateway is not even consulted.
#
# The default is "0" on purpose: an employee deployed with an older deployer —which does not
# send these variables— is left without them, which is right for a beta. The opposite would
# be switching them on for someone who did not ask for them.
def _si(nombre: str) -> bool:
    return (os.environ.get(nombre, "0") or "0").strip().lower() in ("1", "true", "yes", "on")


VOICE_ENABLED: bool = _si("AGENT_VOICE_ENABLED")
RECEPTION_ENABLED: bool = _si("AGENT_RECEPTION_ENABLED")

# ── Platform hooks ───────────────────────────────────────────────────
WAI_BACKEND_URL = os.environ.get("WAI_BACKEND_URL", "")
# Service secret (``X-API-Secret``) for the platform WAI backend, injected by
# the deployer. Used READ-ONLY to proxy the FUNCTIONAL (global) skill catalog so
# the operator can attach extra skills to the autonomous agent in caliente
# (Operations → Skills). Empty in local dev = the skills catalog is unavailable.
BACKEND_API_SECRET = os.environ.get("BACKEND_API_SECRET", "")
# In-cluster URL of the SISTER agent pod. Injected by the deployer
# (see wai-app-deployer/app/services/k8s_agent_app_deployer.py). Empty
# in local dev so unit tests don't hit the network.
AGENT_OPENCODE_URL = os.environ.get("AGENT_OPENCODE_URL", "")
AGENT_SHARED_TOKEN = os.environ.get("AGENT_SHARED_TOKEN", "")
AGENT_ID = int(
    os.environ.get("AGENT_ID")
    or AGENT_APP.get("agent_id")
    or 0
)
AGENT_VERSION_ID = int(
    os.environ.get("AGENT_VERSION_ID")
    or AGENT_APP.get("agent_version_id")
    or 0
)
APP_MCP_TOKEN = os.environ.get("APP_MCP_TOKEN", "")


# ── Tenant identity (for Composio connector namespacing) ─────────────
# External MCP servers backed by Composio (Odoo, Gmail, …) key their
# per-user connections by ``{tenant}:{email}`` — the EXACT identity the
# engine (genai-chain-sql ``oauth_proxy.composio_identity``) uses when
# the autonomous agent's ``/mcp/odoo`` call impersonates the autonomous
# user. We MUST build the same string or the connection the operator
# creates here won't be the one the agent finds at runtime (it would 401).
#
# The deployer doesn't inject a tenant env today, so we resolve it
# robustly: explicit env first, then parse it out of the in-cluster
# service URLs the deployer DOES inject (``{tenant}-genai-api`` /
# ``{tenant}-wai-backend``), then fall back to "default".
def _resolve_tenant_name() -> str:
    for key in ("GENAI_API_TENANT", "OPENCODE_TENANT_NAME", "TENANT_NAME"):
        val = (os.environ.get(key) or "").strip().lower()
        if val:
            return val
    from urllib.parse import urlparse
    for url, marker in (
        (GENAI_API_REST_URL, "-genai-api"),
        (WAI_BACKEND_URL, "-wai-backend"),
    ):
        host = (urlparse(url).hostname or "") if url else ""
        if host and marker in host:
            return host.split(marker, 1)[0].strip().lower() or "default"
    return "default"


def segmento_de_identidad(conn: dict) -> str:
    """The ``#…`` piece of a connector's identity: ONE rule for whoever connects (the
    Connectors routes) and for whoever resolves (``mcp_runtime.resolve_connector``).

    Odoo is resolved by the ENGINE —WAI's tools that replace Composio's— with
    ``oauth_proxy.composio_agent_identity(email, agent_id, "odoo")``, so its account lives
    under ``#odoo`` for ANY connector of that toolkit, including the Odoo 17/18 one. Having
    the rule in two places made connecting authorize ``#odoo`` and the status ask about
    ``#odoo-17-18``: the employee worked and the screen said "not connected". The other
    connectors use their own name so two accounts never collide."""
    if (conn.get("toolkit") or "").strip().lower() == "odoo":
        # Each Odoo connector has its own SLOT (18/10/2026): the usual one (no translator, no suffix) is `odoo`; another
        # —the 17/18 one through the translator, or one with a suffix— its own. The SAME rule as the engine
        # (`oauth_proxy.odoo_slot`), which the pod's `X-WAI-Odoo-Identity` header carries; before, both shared `#odoo`
        # and connecting one overwrote the other.
        from app.mcp_runtime import fichas_de_conectores
        nombre = (conn.get("name") or "").strip()
        ficha = fichas_de_conectores().get(nombre) or {}
        sufijo = str(ficha.get("composio_identity_suffix") or "").strip().lower()
        if sufijo:
            return sufijo
        if str(ficha.get("translator_base") or "").strip():
            return nombre.lower() or "odoo"
        return "odoo"
    return (conn.get("name") or "").strip()


def composio_identity(user_email: str, connector: str = "") -> str:
    """Composio ``user_id`` for a WAI user, namespaced per tenant.

    Base form is ``{tenant}:{email}`` (used by the OAuth email *channel*
    adapter, which must stay byte-compatible with
    ``genai-chain-sql/enpoints/opencode_api/oauth_proxy.composio_identity``).

    When ``connector`` is given (the unique ``agent_app.mcps[].name`` of an
    external MCP connector) the identity becomes
    ``{tenant}:{email}+a{AGENT_ID}#{connector}``. This is the
    ENTITY-PER-CONNECTOR model: each MCP connector authorises its OWN Composio
    connected account, so the operator can point e.g. the Gmail connector and
    the Calendar connector at DIFFERENT Google accounts — and even two
    connectors of the same toolkit at two different accounts — without them
    colliding on a single per-toolkit connected account.

    PER-AGENT scoping (``+a{AGENT_ID}``): the autonomous identity is namespaced
    by the AGENT, not just the autonomous email. Two DIFFERENT autonomous
    agents in the same tenant that happen to share an ``AGENT_AUTONOMOUS_USER_
    EMAIL`` (e.g. both ``admin@wai.com``) are independent — connecting Gmail in
    one does NOT connect it in the other. We key on ``AGENT_ID`` (NOT the
    version id) so DIFFERENT VERSIONS of the SAME agent REUSE the same
    connected accounts (no reconnect on every redeploy). When ``AGENT_ID`` is
    unknown (local dev) the scope is omitted, preserving the legacy identity.

    NOTE: introducing ``+a{AGENT_ID}`` re-keys identities, so connectors
    authorised before this change must be reconnected once (they were stored
    under the old un-scoped identity).

    The full identity MUST be applied identically wherever the same connector
    is referenced (OAuth ``start``, ``connections`` list, ``generate-url`` and
    ``disconnect``) or the operator would authorise one entity and the runtime
    would resolve another. Every caller goes through THIS function, so they
    stay consistent automatically.
    """
    email = (user_email or "").strip().lower()
    if not email:
        return ""
    base = f"{TENANT_NAME}:{email}"
    slug = (connector or "").strip().lower()
    if not slug:
        # BARE BASE — no per-connector scope. This MUST stay byte-compatible
        # with the engine's ``oauth_proxy.composio_identity`` (which has NO
        # ``+a`` scope): the OAuth email *channel* adapter keys off it. The
        # ``+a`` scope lives ONLY on the per-connector form below.
        #
        # Odoo note (contract updated in engine + backend, 2026-07-11): the
        # engine-resolved ``wai-odoo`` tools now PREFER the agent-app identity
        # ``{tenant}:{email}+a{AGENT_ID}#odoo`` (derived from the ``agent_id``
        # claim in the agent-runtime JWT — ``oauth_proxy.composio_agent_
        # identity``) and FALL BACK to this bare base when that entity has no
        # connected account yet. Connect Odoo under the agent identity to give
        # the employee its own account instead of sharing the owner's.
        return base
    # PER-CONNECTOR (router-path) identity — resolved end-to-end by THIS app
    # (mcp_connectors authorise + mcp_runtime resolve build the SAME string),
    # so it never touches the engine. Scope to THIS agent (stable across
    # versions) so two autonomous agents sharing an autonomous email don't
    # collide on one connected account.
    if AGENT_ID:
        base = f"{base}+a{AGENT_ID}"
    return f"{base}#{slug}"


# ── Tenant cost / credit accounting ──────────────────────────────────
# Register every agent execution (chat, channel, trigger, schedule) in
# the tenant's billing ledger, exactly like the WAI code agent does
# (genai-chain-sql/opencode_agent/background_runner._post_agent_cost).
# The deployer injects ``GENAI_API_REST_URL`` (in-cluster genai-api,
# e.g. ``http://{tenant}-genai-api.{ns}.svc.cluster.local:8000``); when
# empty (local dev) cost reporting is a silent no-op.
def _resolve_genai_api_rest_url() -> str:
    """In-cluster URL of the genai-api for the cost mirror.

    FALLBACK (7.122.1) — the why: for months the deployer wrote ``GENAI_API_REST_URL`` in
    the app's ConfigMap but its Deployment did NOT map it as env (there is no envFrom), so
    ``os.environ`` arrived empty, ``cost_reporter`` fell into no-op and NO employee mirrored
    its spend to the central ledger ``public.agent_cost_log`` (root cause verified in demo).
    The deployer's template has been fixed, but pods deployed by a deployer WITHOUT that fix
    would stay silent after every rebuild of the employee: this fallback rescues them by
    deriving the URL from ``WAI_BACKEND_URL`` (which DOES always arrive) with the SAME
    in-cluster naming convention ``_resolve_tenant_name`` already exploits for tenant
    detection — the deployer builds both hosts as ``{tenant}-wai-backend`` /
    ``{tenant}-genai-api`` — replacing the marker and fixing the genai-api's port 8000.
    Without WAI_BACKEND_URL or without the marker (local dev, non-standard URL) the usual
    silent no-op is kept: better not to bill than to bill against an invented host.
    """
    explicit = (os.environ.get("GENAI_API_REST_URL") or "").strip()
    if explicit:
        return explicit
    from urllib.parse import urlparse
    host = (urlparse(WAI_BACKEND_URL).hostname or "") if WAI_BACKEND_URL else ""
    if "-wai-backend" in host:
        return f"http://{host.replace('-wai-backend', '-genai-api', 1)}:8000"
    return ""


GENAI_API_REST_URL = _resolve_genai_api_rest_url()
# Resolved once at import (after the URLs above are known).
TENANT_NAME: str = _resolve_tenant_name()
# USD per 1 agent credit. Mirrors the tenant ``message_limits
# .agent_cost_per_credit`` (cluster default $0.10 = 1 credit). Used to
# DISPLAY execution cost in credits instead of money, both globally and
# per run. Override per-tenant via the env if the rate is customised.
try:
    AGENT_COST_PER_CREDIT: float = float(
        os.environ.get("AGENT_COST_PER_CREDIT", "0.1") or "0.1"
    )
except (TypeError, ValueError):
    AGENT_COST_PER_CREDIT = 0.1
if AGENT_COST_PER_CREDIT <= 0:
    AGENT_COST_PER_CREDIT = 0.1

# ── Agent model catalogue ────────────────────────────────────────────
# The deployer injects the comma-separated model catalogue the tenant is
# allowed to use as ``OPENCODE_DEFAULT_MODEL`` (e.g.
# ``openai/gpt-5.4-mini,openai/gpt-5.4,openai/gpt-5.5``). The FIRST entry
# is the default; the rest are alternatives the operator may pick from
# the Operations page (persisted as app_settings.agent_model and applied
# to every call — chat, channels and autonomous). When unset the list is
# empty and the model selector hides.
def _parse_available_models() -> List[str]:
    raw = os.environ.get("OPENCODE_DEFAULT_MODEL", "").strip()
    out: List[str] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if entry and entry not in out:
            out.append(entry)
    return out


AVAILABLE_MODELS: List[str] = _parse_available_models()
DEFAULT_MODEL: str = AVAILABLE_MODELS[0] if AVAILABLE_MODELS else ""


def modelo_vigente(elegido: str | None) -> str:
    """The model the operator chose (`app_settings.agent_model`) IF the tenant still offers it;
    "" otherwise, which every caller reads as "use the default". A catalogue change (30/09/2026:
    gpt-5.6 → gpt-6) left employees pinned to a retired model: the choice stays stored (the
    person sees it was retired) but no call uses it. With no catalogue (dev), it is kept."""
    v = (elegido or "").strip()
    if not v or not AVAILABLE_MODELS:
        return v
    return v if (v in AVAILABLE_MODELS or f"openai/{v}" in AVAILABLE_MODELS) else ""

# ── Declarative runtime ──────────────────────────────────────────────
WATCHERS: List[Dict[str, Any]] = AGENT_APP.get("watchers") or []
EVENT_TYPES: List[str] = AGENT_APP.get("event_types") or [
    w.get("event_type") for w in WATCHERS if w.get("event_type")
]
EXPOSED_PROJECT_TABLES: List[str] = (
    APP_META.get("exposed_tables")
    or AGENT_APP.get("exposed_tables")
    or []
)
EXPOSED_LOCAL_TABLES: List[str] = AGENT_APP.get("exposed_local_tables") or [
    "events", "agent_runs", "tool_call_log", "event_audit", "watcher_cursors",
]

# ── Retry policy ─────────────────────────────────────────────────────
# Backoff schedule for failed events (seconds). The executor reads
# this to schedule retries with exponential growth and a hard ceiling.
BACKOFF_SECONDS: List[int] = AGENT_APP.get("backoff_seconds") or [30, 120, 600, 3600]

# ── Budget enforcement (R2) ──────────────────────────────────────────
# Daily cost cap (USD). The executor refuses to claim new events when
# the running 24-h sum of agent_runs.cost_usd exceeds this value. A
# stuck agent that fires thousands of events overnight is the
# original motivation. ``0`` (the default) disables the cap.
EXECUTION = AGENT_APP.get("execution") or {}
DAILY_BUDGET_USD: float = float(EXECUTION.get("daily_budget_usd") or 0)

# ── Monthly credit budget (R-BUDGET-1) ───────────────────────────────
# Per-employee spend cap in CREDITS over a rolling 30-day window, set at
# DEPLOY time (the deployer injects ``MONTHLY_CREDITS_BUDGET`` into the app
# pod's env, mirroring AGENT_COST_PER_CREDIT). When the rolling-30d consumed
# credits reach this cap the employee STOPS everything — autonomous runs,
# channel replies AND boss chat — until the window slides back under the cap
# (or a redeploy raises it). ``0`` / unset = unlimited (no cap). See
# ``app/budget.py`` (computation + concept split) and the gates in
# ``autonomous/executor.process_once`` + ``routes/agent_routes.chat_with_agent``.
try:
    MONTHLY_CREDITS_BUDGET: float = float(
        os.environ.get("MONTHLY_CREDITS_BUDGET", "0") or "0"
    )
except (TypeError, ValueError):
    MONTHLY_CREDITS_BUDGET = 0.0
if MONTHLY_CREDITS_BUDGET < 0:
    MONTHLY_CREDITS_BUDGET = 0.0

# ── Executor concurrency (v7.25.0) ───────────────────────────────────
# Max autonomous runs IN FLIGHT at once (channel replies + watcher/cron/
# scheduled). Order is still guaranteed PER CONVERSATION (same person,
# same channel — see the executor's scope guard); concurrency happens
# BETWEEN conversations/background work. Deploy-time env
# ``AGENT_MAX_CONCURRENT_RUNS`` (operator picks it on the deploy form)
# overrides the manifest default ``agent_app.execution.max_concurrent_runs``.
# Clamped to [1, 8] — every extra slot is a live OpenCode session on the
# sister agent pod, so the ceiling protects its CPU/memory.
try:
    MAX_CONCURRENT_RUNS: int = int(
        os.environ.get("AGENT_MAX_CONCURRENT_RUNS")
        or EXECUTION.get("max_concurrent_runs")
        or 2
    )
except (TypeError, ValueError):
    MAX_CONCURRENT_RUNS = 2
MAX_CONCURRENT_RUNS = max(1, min(MAX_CONCURRENT_RUNS, 8))

# ── Conversation threads (v9.1.0) ────────────────────────────────────
# EXISTING is cheap; being ACTIVE is expensive. They are two limits because they are two
# different things, and confusing them is what turns a handy feature into a bill and into
# a pod without memory.
#
# A thread that exists is a row and a name. An ACTIVE one is a live OpenCode session in the
# sister agent pod — the same 1 GiB one that already forces bounding
# ``MAX_CONCURRENT_RUNS``, and for the same reason, written there: "each extra slot is a
# live session in the agent pod".
#
# Going over the cap of active ones rejects nothing and makes nobody wait: the session of
# the least used thread is evicted. The THREAD survives —that is what separates the two
# objects— and it is re-primed when writing in it again.
try:
    MAX_CHAT_THREADS: int = int(os.environ.get("AGENT_MAX_CHAT_THREADS") or 20)
except (TypeError, ValueError):
    MAX_CHAT_THREADS = 20
MAX_CHAT_THREADS = max(1, min(MAX_CHAT_THREADS, 100))

try:
    MAX_CONCURRENT_CHATS: int = int(os.environ.get("AGENT_MAX_CONCURRENT_CHATS") or 3)
except (TypeError, ValueError):
    MAX_CONCURRENT_CHATS = 3
MAX_CONCURRENT_CHATS = max(1, min(MAX_CONCURRENT_CHATS, 8))

# Slots reserved for INBOUND channel messages (a person waiting for a
# reply): those workers only claim ``external_message`` events, so long
# background runs can never occupy the whole pool. 0 = no reservation;
# only meaningful when MAX_CONCURRENT_RUNS > 1.
try:
    RESERVED_INBOUND_SLOTS: int = int(
        os.environ.get("AGENT_RESERVED_INBOUND_SLOTS") or 1
    )
except (TypeError, ValueError):
    RESERVED_INBOUND_SLOTS = 1
RESERVED_INBOUND_SLOTS = (
    max(0, min(RESERVED_INBOUND_SLOTS, MAX_CONCURRENT_RUNS - 1))
    if MAX_CONCURRENT_RUNS > 1 else 0
)

# ── External channels (email + whatsapp + slack; base for outlook/teams) ──
# 7.91.0: channels are a BUILT-IN capability of every employee — ALWAYS
# enabled, ALWAYS configured post-deploy in the app's Canales wizard. The
# manifest no longer carries an ``agent_app.channels`` block (the verifier
# rejects it); a legacy block is read only for the optional defaults below
# so pre-7.91.0 apps keep their tuning until evolved.
CHANNELS_CFG: Dict[str, Any] = AGENT_APP.get("channels") or {}
CHANNELS_ENABLED: bool = True
CHANNELS_ALLOWED_KINDS: List[str] = list(
    CHANNELS_CFG.get("allowed_kinds") or ["email", "whatsapp", "slack", "telegram"]
)
CHANNELS_ALLOWED_AUTH_METHODS: List[str] = list(
    # Email channels are IMAP + App Password ONLY. The Composio/OAuth email
    # adapter was RETIRED (``channels/base.py::get_adapter`` wires imap_smtp /
    # twilio / bot_token and does NOT support Composio as an email channel —
    # Composio is MCP tooling, not a channel adapter). ``oauth_composio`` is no
    # longer a valid auth method; the default set is the supported one.
    CHANNELS_CFG.get("allowed_auth_methods")
    or ["imap_smtp", "twilio", "bot_token", "telegram_bot"]
)
CHANNELS_DEFAULTS: Dict[str, Any] = CHANNELS_CFG.get("defaults") or {}
# Hard floor in the backend so a misconfigured manifest can't blow up
# the upstream rate limits. UI advertises 30 s; backend accepts >= 10 s.
CHANNELS_MIN_POLL_INTERVAL_SEC: int = int(
    CHANNELS_CFG.get("min_poll_interval_sec") or 10
)
# Proactive notification rows stuck in ``processing`` (pod died between
# claim and send) are reclaimed after this many seconds so a retry can
# finish delivery instead of hitting ON CONFLICT and skipping forever.
NOTIFY_STALE_PROCESSING_SEC: int = max(
    30,
    int(os.environ.get("AGENT_NOTIFY_STALE_PROCESSING_SEC") or 120),
)

# Auth-service URLs. Injected by the deployer at deploy time.
#
# * ``AUTH_SERVICE_URL`` — public URL (``https://auth.usewai.com``) used
#   ONLY for browser OAuth redirects (``/composio/{toolkit}/start``).
AUTH_SERVICE_URL: str = (os.environ.get("AUTH_SERVICE_URL") or "").rstrip("/")

# Composio server to server (14.0.0): the employee NO longer talks to the auth-service nor
# has its HMAC secret —a GLOBAL one, with which you act as any identity of any tenant—. It
# talks to ITS tenant's engine (its Composio employees route, see `mcp_connectors`)
# presenting its runtime JWT (`is_agent_runtime`, `sub` = its autonomous email, `agent_id`),
# and the engine signs towards the auth-service after checking the identity is its own.
# Both are injected by the deployer; empty ⇒ the Composio connectors are not available
# (the screen says so), but the employee starts.
WAI_ENGINE_URL: str = (os.environ.get("WAI_ENGINE_URL") or "").strip().rstrip("/")
WAI_AGENT_RUNTIME_JWT: str = (os.environ.get("WAI_AGENT_RUNTIME_JWT") or "").strip()
# Fernet key used to encrypt channel credentials at rest (IMAP App
# Passwords, Twilio Auth Tokens). Generated by the deployer
# (``cryptography.fernet.Fernet.generate_key()``) and mounted as a K8s
# Secret. Without it, the IMAP and WhatsApp auth backends refuse to
# create or read channels (the SPA shows "Falta CHANNEL_ENCRYPTION_KEY").
CHANNEL_ENCRYPTION_KEY: str = os.environ.get("CHANNEL_ENCRYPTION_KEY", "")

# NOTE (7.91.0): deploy-time channel seeding (``CHANNELS_SEED``) was retired.
# Channels are configured exclusively in the app (Canales page wizard) after
# deploy — the employee ships the capability, never pre-wired mailboxes.


def _int_env(name: str, default: int) -> int:
    """Read a positive integer env var, falling back to ``default`` on
    missing / empty / invalid / non-positive values."""
    try:
        v = int((os.environ.get(name) or "").strip())
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


# Inbound email body sizing. Replies carry the whole quoted chain, so a
# long thread is normal and we want to keep it. Two knobs guard the
# context window without losing the conversation:
#   * MAX_INBOUND_BODY_CHARS — hard safety cap when PERSISTING the full
#     body, so a pathological multi-MB mail never bloats the SQLite row.
#   * PROMPT_BODY_BUDGET_CHARS — budget applied when BUILDING the prompt.
#     Only mails above this are trimmed (keeping the most recent top and
#     dropping the oldest quoted tail with a visible marker). Roughly
#     ~4 chars/token, so 48000 ≈ ~12k tokens.
MAX_INBOUND_BODY_CHARS: int = _int_env("AGENT_MAX_INBOUND_BODY_CHARS", 200_000)
PROMPT_BODY_BUDGET_CHARS: int = _int_env("AGENT_PROMPT_BODY_BUDGET_CHARS", 48_000)

# v7.85.0 — CHANNEL MESSAGE RETENTION. Inbound/outbound channel messages (with
# full bodies + staged attachment files on the PVC) otherwise grow WITHOUT limit
# and retain PII indefinitely — there was no TTL for them (unlike memory, which
# has caps + an episode TTL). This opt-in knob prunes settled channel_messages
# (and their staged attachment files) older than N days from the daily
# housekeeping. 0 (default) = keep forever (legacy behaviour); set e.g. 180 to
# age out messages after 6 months. In-flight (pending/processing) rows are never
# pruned regardless of age.
AGENT_CHANNEL_MESSAGE_RETENTION_DAYS: int = _int_env("AGENT_CHANNEL_MESSAGE_RETENTION_DAYS", 0)


def _bool_env(name: str, default: bool) -> bool:
    """Read a boolean env var (``1/true/yes/on`` ⇒ True). Falls back to
    ``default`` on missing / empty values."""
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _float_env(name: str, default: float) -> float:
    """Read a non-negative float env var, falling back to ``default`` on
    missing / empty / invalid / negative values."""
    try:
        v = float((os.environ.get(name) or "").strip())
        return v if v >= 0 else default
    except (TypeError, ValueError):
        return default


# Character budget of the per-user memory block injected (once) into a
# session prime / channel prompt. ~4 chars/token ⇒ 4000 ≈ ~1k tokens.
AGENT_MEMORY_BLOCK_CHARS: int = _int_env("AGENT_MEMORY_BLOCK_CHARS", 4_000)
# Episodic cap: keep at most this many recent episodes per user (older
# ones are pruned; the semantic layer retains what matters).
AGENT_MEMORY_EPISODE_KEEP: int = _int_env("AGENT_MEMORY_EPISODE_KEEP", 50)
# How many rows of each layer enter the WORKING block injected into the
# prompt (the rest stays stored but "not applied"). Section-aware budget
# uses these as per-section caps before the char budget trims further.
AGENT_MEMORY_BLOCK_FACTS: int = _int_env("AGENT_MEMORY_BLOCK_FACTS", 40)
AGENT_MEMORY_BLOCK_EPISODES: int = _int_env("AGENT_MEMORY_BLOCK_EPISODES", 8)
AGENT_MEMORY_BLOCK_FILES: int = _int_env("AGENT_MEMORY_BLOCK_FILES", 8)
# Semantic-layer growth cap: keep at most this many LIVE, non-pinned
# facts per user. When exceeded, compaction auto-supersedes the oldest
# (history is kept; pinned facts are never capped). 0 disables the cap.
AGENT_MEMORY_FACTS_KEEP: int = _int_env("AGENT_MEMORY_FACTS_KEEP", 200)
# Optional time-based episode TTL (days). Episodes older than this are
# pruned on compaction, on top of the count cap. 0 disables the TTL.
AGENT_MEMORY_EPISODE_TTL_DAYS: int = _int_env("AGENT_MEMORY_EPISODE_TTL_DAYS", 0)
# Auto-compaction: when enabled, building the working block at session
# prime / channel prompt enqueues a (deduped) memory_maintenance run if
# the user's live facts or episodes exceed the threshold below. This is
# the only automatic trigger; there is no cron.
AGENT_MEMORY_AUTOCOMPACT: bool = _bool_env("AGENT_MEMORY_AUTOCOMPACT", True)
AGENT_MEMORY_AUTOCOMPACT_OVER: int = _int_env("AGENT_MEMORY_AUTOCOMPACT_OVER", 300)

# ── v7.29.0 — hardening + dense retrieval ────────────────────────────
# Master switch for the dense-retrieval scoring (relevance·importance·
# recency·trust) + trust segregation. OFF = legacy recency-only ordering
# (safe rollback if scoring ever regresses quality).
AGENT_MEMORY_DENSE_RETRIEVAL: bool = _bool_env("AGENT_MEMORY_DENSE_RETRIEVAL", True)
# v7.56.0 — SQLite FTS5 lexical index over memory (facts/episodes/files). Proper
# BM25 keyword ranking, indexed in SQLite, accent-insensitive (Spanish), zero
# extra dependency. Used as the relevance signal for search_memory + the injected
# facts block, falling back to the hashed-embedding cosine when a query has no
# FTS hit or FTS5 is unavailable. OFF = pure embedding/recency (safe rollback).
AGENT_MEMORY_FTS: bool = _bool_env("AGENT_MEMORY_FTS", True)
# Embedding model id (empty = the built-in local, dependency-free provider).
AGENT_MEMORY_EMBED_MODEL: str = os.environ.get("AGENT_MEMORY_EMBED_MODEL", "").strip()
# Max facts one STRUCTURED_RESPONSE.memory block may add per run — bounds a
# hostile inbound flooding memory with quarantined junk.
AGENT_MEMORY_BLOCK_MAX_FACTS: int = _int_env("AGENT_MEMORY_BLOCK_MAX_FACTS", 8)
# Retrieval score weights (relevance, importance, recency, trust). Sensible
# defaults; not all exposed to operators.
AGENT_MEMORY_W_REL: float = _float_env("AGENT_MEMORY_W_REL", 0.40)
AGENT_MEMORY_W_IMP: float = _float_env("AGENT_MEMORY_W_IMP", 0.20)
AGENT_MEMORY_W_REC: float = _float_env("AGENT_MEMORY_W_REC", 0.20)
AGENT_MEMORY_W_TRUST: float = _float_env("AGENT_MEMORY_W_TRUST", 0.20)
# Soft-decay half-life (days) applied at READ time to the recency term;
# reinforced facts (pinned/verified/high access) resist it. 0 disables decay.
AGENT_MEMORY_DECAY_HALF_LIFE_DAYS: float = _float_env("AGENT_MEMORY_DECAY_HALF_LIFE_DAYS", 45.0)

# ── v7.140.0 — AGENT SELF-MEMORY (learning about the ENVIRONMENT) ────
# The SECOND memory axis: not memory of a person (above) but of the system the
# employee operates on ("this sweep taking 40 min is NORMAL here"). Injected in
# EVERY run — including autonomous ones, where the per-user block is empty by
# design. Master switch OFF ⇒ block/tools/routes no-op without a single query
# (clean rollback, and the cheapest possible hot path).
AGENT_SELF_MEMORY_ENABLED: bool = _bool_env("AGENT_SELF_MEMORY_ENABLED", True)
# Own char budget, so it NEVER competes with the person's block.
AGENT_SELF_MEMORY_BLOCK_CHARS: int = _int_env("AGENT_SELF_MEMORY_BLOCK_CHARS", 3_000)
# Per-section caps applied before the char budget trims further. Baselines are
# the section that prevents false positives, so it gets the most room.
AGENT_SELF_MEMORY_BLOCK_BASELINES: int = _int_env("AGENT_SELF_MEMORY_BLOCK_BASELINES", 20)
AGENT_SELF_MEMORY_BLOCK_LESSONS: int = _int_env("AGENT_SELF_MEMORY_BLOCK_LESSONS", 12)
AGENT_SELF_MEMORY_BLOCK_OBSERVATIONS: int = _int_env("AGENT_SELF_MEMORY_BLOCK_OBSERVATIONS", 6)
# Max learnings one STRUCTURED_RESPONSE.self_memory block may add per run.
AGENT_SELF_MEMORY_BLOCK_MAX_ITEMS: int = _int_env("AGENT_SELF_MEMORY_BLOCK_MAX_ITEMS", 8)
# Growth caps: raw observations and own episodes kept (baselines/lessons are the
# distilled layer and are never capped away).
AGENT_SELF_MEMORY_OBSERVATIONS_KEEP: int = _int_env("AGENT_SELF_MEMORY_OBSERVATIONS_KEEP", 120)
AGENT_SELF_MEMORY_EPISODE_KEEP: int = _int_env("AGENT_SELF_MEMORY_EPISODE_KEEP", 80)
# Consolidation: when this many raw observations pile up on ONE subject, the
# block build enqueues a (deduped) ``self_memory_maintenance`` run that distils
# them into a baseline. Same "no cron" discipline as the per-user autocompact.
AGENT_SELF_MEMORY_AUTOCOMPACT: bool = _bool_env("AGENT_SELF_MEMORY_AUTOCOMPACT", True)
# 3 (it was 8 until 7.149.0). MEASURED on the Vigía: 12 learnings spread over 6 subjects, the
# most populated with 4 → the threshold of 8 was NEVER reached, the maintenance run was never
# queued and with it the ONLY path that retires knowledge (`supersedes`/`forget_learning`) never ran.
AGENT_SELF_MEMORY_CONSOLIDATE_AFTER: int = _int_env("AGENT_SELF_MEMORY_CONSOLIDATE_AFTER", 3)
# The LONG TAIL: a subject with 2 observations that no longer grows will never reach the
# threshold by volume, but it is exactly the one that rots (observations of an incident
# already solved). If its most recent observation is older than these days, it is also ripe
# to distil or retire.
AGENT_SELF_MEMORY_CONSOLIDATE_STALE_DAYS: int = _int_env(
    "AGENT_SELF_MEMORY_CONSOLIDATE_STALE_DAYS", 7)
# 7.162.0 — CADENCE of the deterministic cleanup (expire, retire what was never used, retire
# the dated ballast, prune observations and episodes). It was hard-wired in the code as the
# default argument of `_housekeeping_run_forever`, so the only way to touch it was editing
# `main.py`: an employee that learns fast could not sweep more often, and a quiet one could
# not sweep less.
#
# The FLOOR of 1 hour is not decorative. The cleanup writes to the SAME SQLite as the
# watcher, the executor and the poller, and SQLite in WAL admits a single writer: sweeping
# every few minutes is asking for the "database is locked" that already cost us an incident.
# If what you want is to clean NOW, do not lower the interval — use
# `POST /api/agent/self-memory/compact` (admin), which does the same pass on demand and
# without leaving a permanent sweep.
# 7.164.0 — WHAT counts as "important". A single number for BOTH sides, which is exactly
# what was missing: the judgement queue took the important-seen-once with a hand-written
# 0.7, and the reaper retired it without looking at the importance. Result: the system asked
# you for judgement on a row it was itself counting the days of. With the shared constant,
# what you are asked about CANNOT be deleted for disuse.
# 7.164.0 — from how many live facts a subject is considered SATURATED and the agent gets
# the warning before writing ("you already have N of this: reinforce or keep quiet").
# 7.165.0 — after how many days an observation that still has a single sighting while its
# subject has NOT stopped receiving writes is considered IRREINFORCEABLE. It is the MEASURED
# version of the 7.157.0 rule, which detected the same by regex (date in the text) and fell
# short: with the date suffix removed, the content still carried the figures of the moment
# ("maximum of 92.34% on usewai-wk-2") and could never be reinforced either. Measured on the
# Vigía: 50 of 107 entries were still unrepeatable after cleaning the date. Forbidding
# numbers by regex would be worse —"the sweep takes 30-45 min" is a legitimate fact—, so the
# result is measured instead of guessing the cause.
AGENT_SELF_MEMORY_IRREINFORCEABLE_DAYS: int = _int_env(
    "AGENT_SELF_MEMORY_IRREINFORCEABLE_DAYS", 3)
AGENT_SELF_MEMORY_SATURATED_AT: int = _int_env("AGENT_SELF_MEMORY_SATURATED_AT", 8)
AGENT_SELF_MEMORY_IMPORTANT_FLOOR: float = _float_env(
    "AGENT_SELF_MEMORY_IMPORTANT_FLOOR", 0.7)
AGENT_HOUSEKEEPING_INTERVAL_HOURS: float = max(
    1.0, _float_env("AGENT_HOUSEKEEPING_INTERVAL_HOURS", 24.0))
# ── 7.149.0 — make REINFORCEMENT work and dead knowledge die on its own ──
# Diagnosis behind these three blocks: the employee wrote "On 28 July 2026 the control
# covered 18 of 18 tenants…". With the date and the day's figures INSIDE the content, the
# fingerprint changes every time → `reinforce` never fires → everything stays as
# "1 observation" observations → there is no consolidation → there are no baselines → the
# injected block is anecdotes. Content: a TIMELESS and atomic fact. What varies (when, how
# much it measured today, in which run) lives in `observations`/`provenance`, not in the
# text. Above this size it is rejected with a reason: truncating would store half a fact,
# and a half learning is worse than none.
AGENT_SELF_MEMORY_CONTENT_MAX: int = _int_env("AGENT_SELF_MEMORY_CONTENT_MAX", 240)
# Reject content with an absolute date (after trying to remove the leading time phrase).
# Can be switched off in case an environment needs literal dates in the text.
AGENT_SELF_MEMORY_REJECT_DATED: bool = _bool_env("AGENT_SELF_MEMORY_REJECT_DATED", True)
# SEMANTIC dedupe on top of the exact hash: the same fact rewritten opens a new row today.
# Cosine over the embeddings ALREADY stored (local provider, cost 0).
# 0.75 is MEASURED with the default provider (hashed bag of n-grams), not picked by eye:
#   same fact rewritten ................ 0.79 – 0.90
#   DIFFERENT facts of the same subject  0.10 – 0.57
# The gap between 0.57 and 0.79 is where the threshold lives. With 0.90 (first attempt) the
# mechanism would barely have fired — the same failure mode this change comes to fix: code
# that looks right and never runs. WARNING: the bag of n-grams catches REWRITES, not free
# paraphrases ("devuelve 404 persistente" vs "responde Not found" scores 0.21); those are
# merged by the consolidation run, or a real model is configured with AGENT_MEMORY_EMBED_MODEL.
AGENT_SELF_MEMORY_DEDUPE_SIM: float = _float_env("AGENT_SELF_MEMORY_DEDUPE_SIM", 0.75)
# Default validity by type, in days (0 = never expires). An observation is today's; a
# baseline lasts as long as the environment holds. It is applied on the SERVER: trusting the
# model to set `valid_until` is exactly what did not work (none of the 12 real entries had it).
# And reconfirming RENEWS the validity: what is still seen stays alive, what stops being seen
# falls on its own — no cron, no LLM and nobody having to remember to clean.
AGENT_SELF_MEMORY_TTL_OBSERVATION_DAYS: int = _int_env("AGENT_SELF_MEMORY_TTL_OBSERVATION_DAYS", 14)
AGENT_SELF_MEMORY_TTL_BASELINE_DAYS: int = _int_env("AGENT_SELF_MEMORY_TTL_BASELINE_DAYS", 90)
AGENT_SELF_MEMORY_TTL_LESSON_DAYS: int = _int_env("AGENT_SELF_MEMORY_TTL_LESSON_DAYS", 180)
AGENT_SELF_MEMORY_TTL_PROCEDURE_DAYS: int = _int_env("AGENT_SELF_MEMORY_TTL_PROCEDURE_DAYS", 180)
AGENT_SELF_MEMORY_TTL_ENTITY_DAYS: int = _int_env("AGENT_SELF_MEMORY_TTL_ENTITY_DAYS", 180)
# ── 7.153.0 — memory lifecycle (layers 1-4) ─────────────────────────
# INTERMEDIATE similarity band: above DEDUPE_SIM it is "the same fact" (reinforces); between
# REFINE_SIM and DEDUPE_SIM it is "the same thing told in more detail" → the neighbour is
# EXTENDED instead of opening a new row. Below, a different fact. 0.55 measured: the pairs of
# the 23 entries of cluster:tenant-error-coverage fall in that band; different facts of the
# same subject stayed at 0.10-0.57, so the edge goes exactly where it separates.
AGENT_SELF_MEMORY_REFINE_SIM: float = _float_env("AGENT_SELF_MEMORY_REFINE_SIM", 0.55)
# Days an observation has to prove it is useful. If nobody has ever consulted it
# (`access_count == 0`) past that term, it is retired: knowledge that is not used is not
# knowledge, it is weight. What is used survives even if it is old.
AGENT_SELF_MEMORY_UNUSED_GRACE_DAYS: int = _int_env("AGENT_SELF_MEMORY_UNUSED_GRACE_DAYS", 7)
# Similarity from which two entries of the same subject talk about the SAME thing and, if one
# affirms and the other denies, it is a CONTRADICTION to be resolved, not accumulated.
# 0.50 MEASURED: real contradictory pairs score 0.53-0.77 with the local provider.
# It is low on purpose because what discriminates here is the POLARITY (affirms vs denies);
# the similarity only ensures they talk about the SAME subject. Moreover this resolves
# nothing on its own: it flags the pair for consolidation to review, so a false positive
# costs little and a false negative leaves two contradicting entries living together.
AGENT_SELF_MEMORY_CONTRADICTION_SIM: float = _float_env(
    "AGENT_SELF_MEMORY_CONTRADICTION_SIM", 0.50)
# Retention of the NON-ACTION log ("why didn't it warn me?"). The permanent
# total survives in ``signal_suppressions.suppress_count``, so pruning the
# narrative costs no information. 0 disables pruning.
SIGNAL_SILENCE_LOG_RETENTION_DAYS: int = _int_env("SIGNAL_SILENCE_LOG_RETENTION_DAYS", 30)
# How many recent signals the silence "Probar" (dry-run preview) replays.
SIGNAL_CONDITION_PREVIEW_LAST_N: int = _int_env("SIGNAL_CONDITION_PREVIEW_LAST_N", 40)

# ── v7.140.0 — AUTONOMOUS-WORK GOVERNANCE ────────────────────────────
# Daily rollup of ``automation_eval_log`` written BEFORE the 14-day prune, so
# the funnel (checks → met → fired → silenced) stays exact for any range while
# the row-by-row detail stays bounded. Only what the prune destroys is rolled
# up; cost and generated entities are computed live from unpruned tables.
AUTOMATION_STATS_ROLLUP: bool = _bool_env("AUTOMATION_STATS_ROLLUP", True)
# Days of eval-log detail kept (the prune window). The rollup must cover at
# least this, so it back-fills this many complete days on every pass.
AUTOMATION_EVAL_LOG_RETENTION_DAYS: int = _int_env("AUTOMATION_EVAL_LOG_RETENTION_DAYS", 14)

# ── v7.84.0 — DOCUMENT KNOWLEDGE LIBRARY ─────────────────────────────
# File-based knowledge (catalogue, pricing, policies, FAQs…) the employee
# consults for OFFICIAL content WITH source citation. Text is extracted in-app
# (pypdf/python-docx/openpyxl) and indexed in SQLite FTS5 (``knowledge_fts.py``).
# Master switch OFF = the routes/tools/prompt block all no-op (clean rollback).
KNOWLEDGE_LIBRARY_ENABLED: bool = _bool_env("KNOWLEDGE_LIBRARY_ENABLED", True)
# Storage dir for the uploaded originals. Empty ⇒ a ``knowledge`` folder next to
# the app SQLite DB (the app pod's PRIVATE persistent volume). NOT the shared
# agent workspace on purpose: the employee's sidecar can browse that by filesystem
# and would bypass the visibility governance the MCP tools enforce.
KNOWLEDGE_STORAGE_DIR: str = (os.environ.get("KNOWLEDGE_STORAGE_DIR") or "").strip()
# Limits (operator-tunable). Per-file / whole-library size + document count.
KNOWLEDGE_MAX_FILE_MB: int = _int_env("KNOWLEDGE_MAX_FILE_MB", 25)
KNOWLEDGE_MAX_TOTAL_MB: int = _int_env("KNOWLEDGE_MAX_TOTAL_MB", 250)
KNOWLEDGE_MAX_DOCS: int = _int_env("KNOWLEDGE_MAX_DOCS", 150)
# Hard cap on extracted text kept per document (guards a pathological file from
# bloating SQLite / the FTS index). ~4 chars/token ⇒ 400k ≈ ~100k tokens on disk.
KNOWLEDGE_MAX_EXTRACT_CHARS: int = _int_env("KNOWLEDGE_MAX_EXTRACT_CHARS", 400_000)
# Chunking: target chunk size + overlap (chars) for the searchable slices.
KNOWLEDGE_CHUNK_CHARS: int = _int_env("KNOWLEDGE_CHUNK_CHARS", 1_200)
KNOWLEDGE_CHUNK_OVERLAP: int = _int_env("KNOWLEDGE_CHUNK_OVERLAP", 150)
# How many chunk hits ``search_knowledge`` returns by default.
KNOWLEDGE_SEARCH_LIMIT: int = _int_env("KNOWLEDGE_SEARCH_LIMIT", 6)
# Chars of a chunk surfaced as the citation snippet (trimmed on a word boundary).
KNOWLEDGE_SNIPPET_CHARS: int = _int_env("KNOWLEDGE_SNIPPET_CHARS", 320)
