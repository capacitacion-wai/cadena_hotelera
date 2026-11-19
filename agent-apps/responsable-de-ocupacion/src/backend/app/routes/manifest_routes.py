# src/backend/app/routes/manifest_routes.py
import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.tarjetas import (
    TIPOS as _TIPOS_DE_TARJETA, VERSION as _VERSION_DE_TARJETA,
)
from typing import Any, Iterable, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import desc, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import selectinload

from app.app_database import (
    app_session, app_write, get_app_session, write_with_retry,
)
from app.app_models.runtime import (
    ActionOverride,
    AgentPersona,
    AgentRun,
    AppSetting,
    Event,
    WatcherSchedule,
)
from app.auth import require_project_access, require_admin, TokenPayload
from app.config import APP_TIMEZONE as _APP_TIMEZONE, WAI_APP_JSON_PATH, WATCHERS

router = APIRouter(prefix="/api/agent", tags=["agent"])

logger = logging.getLogger(__name__)


# R-TZ-1 (v7.1): timestamps written to SQLite by the legacy ORM
# defaults (``default=datetime.utcnow``) lose their tzinfo, so by
# the time we read them back they're naive Python ``datetime``s
# whose ``.isoformat()`` yields ``"2026-05-13T15:49:19.888498"``
# — no ``Z`` suffix. The SPA then parses that with ``new Date()``
# which interprets bare ISO strings in the BROWSER's local TZ,
# producing wildly wrong "started at" timestamps for any operator
# whose timezone isn't UTC. To fix once at the boundary we route
# every datetime that ships in the manifest payload through
# ``_iso_utc``: aware datetimes are normalised to UTC, naive ones
# are assumed UTC (this is correct because both ORM defaults and
# the executor write UTC). The ISO output always carries an
# explicit offset (``+00:00``), which ``new Date()`` then converts
# to the browser-local instant deterministically.
def _iso_utc(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat()

_MANIFEST_CACHE: dict | None = None

# Runtime / audit tables that MUST NEVER appear under the "Domain"
# sidebar group — they live under the dedicated /events and /runs
# audit pages instead. Filtered server-side from
# ``manifest.domain_entities`` so a misconfigured ``wai-app.json`` can't
# leak event-queue rows into the business-data navigation.
RUNTIME_BLACKLIST: set[str] = {
    "events",
    "agent_runs",
    "tool_call_log",
    "event_audit",
    "watcher_cursor",
    "watcher_schedules",
    # External-channel state — surfaced through the dedicated
    # /channels page in the SPA, never under the Domain group.
    "channels",
    "channel_messages",
    # v7.30.0 — WORK-COORDINATION substrate that already has a functional
    # home in "Trabajo del empleado" (calendar/agenda, encargos, approvals).
    # They are agent-written too, but listing them under "Lo que genero"
    # duplicated the surface — keep them ONLY in the work group.
    "scheduled_items",
    "approvals",
}


def _is_runtime_entity(entity: dict) -> bool:
    """Return True when an entity points at one of the platform's
    operational tables. Checks both ``slug`` and ``table`` so the rule
    holds whether the manifest names the entity ``events`` (slug ==
    table) or aliases it (slug=``activity`` table=``events``)."""
    slug = (entity.get("slug") or "").strip().lower()
    table = (entity.get("table") or "").strip().lower()
    return slug in RUNTIME_BLACKLIST or table in RUNTIME_BLACKLIST


def _load_wai_app() -> dict:
    global _MANIFEST_CACHE
    if _MANIFEST_CACHE is None:
        _MANIFEST_CACHE = json.loads(Path(WAI_APP_JSON_PATH).read_text())
    return _MANIFEST_CACHE


# ── «Capacidades declaradas»: the truth, not the manifest ────────────────
# v7.158.2 — this block described the security posture from the MANIFEST (what was
# declared when generating the app) and from the server's NAME. Both sources were false in
# production:
#
#   · `_classify_mcp` returned "local write" for everything that was not wai-data, so a
#     read-only observation MCP (clusterwai) and a remote SaaS one (composio-github) were
#     both announced as "local write" — they neither write, nor are they local.
#   · `_summarise_permissions` read `permissions` from the manifest and went as far as
#     saying "Any tool without asking for confirmation" while the effective floor was
#     `read_only` (the default since 7.113.0): the panel promised a free-for-all in an
#     employee that cannot write anything outside. Lying towards the unsafe side is the
#     worst thing a security screen can do.
#
# Now both come from `mcp_policy`, the SAME module that decides a real call: since 14.8.0
# it reads the platform's permissions table for this employee (one calculation, in the
# platform) and this block is a reading of that table. The per-tool detail lives in the
# Capabilities view; this is the headline.

_POLICY_UNKNOWN = "read_only"


async def _mcp_classifier():
    """Returns `name → headline write policy` (`read_only` | `write_with_approval` |
    `write`) read from the permissions table, autonomy applied."""
    from app import mcp_policy
    try:
        tabla = await mcp_policy.load_table()
        autonomia = await mcp_policy._por_autonomia()
    except Exception:  # pragma: no cover — never break the manifest over this
        logger.debug("permissions table unreadable", exc_info=True)
        tabla, autonomia = None, mcp_policy.POLICY_WRITE

    def classify(name: str) -> str:
        n = (name or "").strip().lower()
        # wai-data is read-only BY CONSTRUCTION (it is the project's data view): it does not
        # depend on the policy, it cannot write even if the agent's authority is opened.
        if n == "wai-data" or n.startswith("wai-data."):
            return "read_only"
        if tabla is None:
            return _POLICY_UNKNOWN
        return mcp_policy.connector_policy(tabla, n, autonomia)

    return classify


async def _permissions_summariser():
    """Honest headline of the posture, read from the permissions table: how far the
    employee can WRITE in its connectors. It never promises more than the table allows."""
    from app import mcp_policy
    try:
        tabla = await mcp_policy.load_table()
        autonomia = await mcp_policy._por_autonomia()
    except Exception:  # pragma: no cover
        logger.debug("permissions table unreadable", exc_info=True)
        tabla = None
    if tabla is None:
        def ilegible(_perms) -> str:
            return "No se ha podido leer la tabla de permisos de la plataforma."
        ilegible.codigo = {"kind": "unreadable", "own_rules": 0}
        return ilegible
    filas = [r for c in tabla.get("connectors") or [] for r in c.get("tools") or []]
    escrituras = [mcp_policy._perm(r.get("permission")) for r in filas
                  if str(r.get("io_kind") or "").lower() != "read"]
    if autonomia == mcp_policy.POLICY_APPROVAL:
        escrituras = [mcp_policy.PERM_ASK if p == mcp_policy.PERM_ALLOW else p for p in escrituras]
    propias = sum(1 for r in filas if r.get("source") in ("custom", "employee"))

    # The SAME verdict as a code, so the screen says it in the person's language (14.16.0):
    # the text below is Spanish and showed as is inside English screens.
    if not escrituras:
        kind = "no_writes"
    elif all(p == mcp_policy.PERM_DENY for p in escrituras):
        kind = "read_only"
    elif all(p != mcp_policy.PERM_ALLOW for p in escrituras):
        kind = "all_approval"
    elif all(p == mcp_policy.PERM_ALLOW for p in escrituras):
        kind = "all_free"
    else:
        kind = "mixed"

    def summarise(_manifest_perms) -> str:
        if not escrituras:
            base = ("Usa sus herramientas de consulta sin pedir permiso; "
                    "no tiene herramientas que escriban en sistemas externos.")
        elif all(p == mcp_policy.PERM_DENY for p in escrituras):
            base = ("Usa sus herramientas de consulta sin pedir permiso; "
                    "no puede escribir en ningún sistema externo.")
        elif all(p != mcp_policy.PERM_ALLOW for p in escrituras):
            base = ("Consulta sin pedir permiso; toda escritura en un sistema "
                    "externo espera tu aprobación.")
        elif all(p == mcp_policy.PERM_ALLOW for p in escrituras):
            base = "Usa cualquier herramienta, incluidas las escrituras, sin pedir confirmación."
        else:
            base = ("Consulta sin pedir permiso; unas escrituras las hace solo y otras "
                    "esperan tu aprobación o no las puede hacer.")
        if propias:
            base += f" {propias} con regla propia."
        return base

    summarise.codigo = {"kind": kind, "own_rules": propias}
    return summarise


def _coerce_locale_dict(value: Any) -> Optional[dict[str, str]]:
    """Coerce ``business_description`` payloads to a clean
    ``{lang: text}`` dict. Accepts:

      * dict[str, str] — used as-is (only non-empty strings kept).
      * str            — legacy single-locale value; promoted to
                         ``{"es": value}`` for backward-compat with
                         manifests authored before R-OPS-1.
      * None / empty   — returned as ``None``.

    Anything else returns ``None`` so we never echo garbage back to
    the SPA. Locale codes are lower-cased and clipped to 8 chars to
    prevent accidental injection of huge keys from manifest typos.
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return {"es": text} if text else None
    if not isinstance(value, dict):
        return None
    out: dict[str, str] = {}
    for k, v in value.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        key = k.strip().lower()[:8]
        text = v.strip()
        if key and text:
            out[key] = text
    return out or None


def _technical_description_payload(
    w: dict,
    *,
    interval_sec: int | None = None,
    cron_expr: str | None = None,
) -> dict:
    """Return the structured payload the SPA needs to render the
    short "how this watcher works technically" sentence in the user's
    locale. The frontend has the i18n templates
    (``operations.triggers.sentences.<type>``) so we just hand it the
    discrete fields. Keeping the data and the localized copy on the
    frontend keeps the backend locale-agnostic (a stated goal of
    R-OPS-1: stop emitting Spanish paragraphs from the API).

    R-OPS-6: callers in the watcher-list path pass the EFFECTIVE
    cadence (operator override applied) so the sentence stays in
    sync with the chip / editor instead of showing the manifest
    default forever.
    """
    return {
        "kind": w.get("type"),
        "interval_sec": (
            interval_sec if interval_sec is not None else w.get("interval_sec")
        ),
        "cron_expr":    (
            cron_expr if cron_expr is not None else w.get("cron_expr")
        ),
        "event_type":   w.get("event_type"),
        "table":        w.get("table"),
        "column":       w.get("column"),
        "threshold":    w.get("threshold"),
    }


# Legacy fallback (Spanish single-language paragraph). Older SPAs and
# older manifests still expect ``description`` to be a string, so we
# build a minimal one only when the manifest has not provided either
# ``business_description`` nor an explicit ``description``. New
# frontend code MUST use ``technical`` + ``business_description``
# instead.
def _legacy_description(w: dict) -> str:
    if w.get("description"):
        return w["description"]
    kind = w.get("type")
    every = (
        f"cada {w.get('interval_sec')}s"
        if w.get("interval_sec") else "según cadencia configurada"
    )
    table = w.get("table") or "—"
    column = w.get("column")
    event_type = w.get("event_type") or "evento"
    if kind == "row_count_delta":
        threshold = w.get("threshold") or 1
        return (
            f"Mira {every} la tabla «{table}» y, si han aparecido al menos "
            f"{threshold} filas nuevas, levanta un evento «{event_type}»."
        )
    if kind == "value_change":
        return (
            f"Vigila {every} si la columna «{column}» de «{table}» cambia. "
            f"Cuando cambia emite «{event_type}»."
        )
    if kind == "threshold":
        return (
            f"Comprueba {every} si «{column}» de «{table}» supera el umbral. "
            f"Si se rebasa emite «{event_type}»."
        )
    if kind == "cron":
        cron = w.get("cron_expr") or every
        return (
            f"Programación recurrente {cron}. Emite «{event_type}» en cada tick."
        )
    if kind == "local_event":
        return f"Reacciona a eventos locales «{event_type}»."
    return f"Watcher «{kind or 'desconocido'}» — emite «{event_type}»."


# Persona ("AI employee") presentation block. The marketing framing of
# every generated app is that it's a teammate, not a faceless tool — so
# the manifest may carry a ``persona`` describing a friendly name, a
# mascot avatar, a one-line role, a short first-person bio and a few
# personality traits. Everything is OPTIONAL and the SPA degrades to the
# functional ``display_name`` / ``tagline`` when absent, so older
# manifests keep rendering. We sanitise hard (known keys only, length
# caps, locale-dict coercion) so a malformed ``wai-app.json`` can never
# inject huge or arbitrary payloads into the browser.
_PERSONA_MAX_TRAITS = 6
# Generous cap so a small inlined mascot data-URI is allowed, but a
# pathological manifest can't bloat the manifest endpoint unbounded.
_PERSONA_AVATAR_URL_MAX = 300_000


def _persona_str_or_locale(value: Any, max_len: int) -> Any:
    """Coerce a persona copy node to a clean ``str`` or ``{lang: str}``
    dict (length-capped), or ``None``. Mirrors ``_coerce_locale_dict``
    but also accepts plain strings verbatim so single-language personas
    stay terse in the manifest."""
    if isinstance(value, str):
        text = value.strip()
        return text[:max_len] if text else None
    if isinstance(value, dict):
        out: dict[str, str] = {}
        for k, v in value.items():
            if not isinstance(k, str) or not isinstance(v, str):
                continue
            key = k.strip().lower()[:8]
            text = v.strip()
            if key and text:
                out[key] = text[:max_len]
        return out or None
    return None


def _persona_fallback(agent_app: dict) -> Optional[dict]:
    """Synthesize a minimal persona from the app's display name / tagline
    when the manifest carries no (or an empty) ``persona`` block. Without
    this the Identity editor and the welcome hero load BLANK on every
    agent generated before the persona block became standard — the
    operator opens "Identidad" and sees "Sin nombre" with nothing to
    edit. The display name is the agent's name; the tagline (if any)
    seeds the role. Returns ``None`` only when even those are absent."""
    out: dict[str, Any] = {}
    name = agent_app.get("agent_display_name") or agent_app.get("name")
    if isinstance(name, str) and name.strip():
        out["name"] = name.strip()[:80]
    role = _persona_str_or_locale(
        agent_app.get("agent_tagline") or agent_app.get("tagline"), 200
    )
    if role:
        out["role"] = role
    return out or None


def _persona_block(agent_app: dict) -> Optional[dict]:
    raw = agent_app.get("persona")
    if not isinstance(raw, dict):
        raw = {}

    out: dict[str, Any] = {}

    name = raw.get("name")
    if isinstance(name, str) and name.strip():
        out["name"] = name.strip()[:80]

    role = _persona_str_or_locale(raw.get("role"), 200)
    if role:
        out["role"] = role

    # ---- avatar (mascot) ------------------------------------------------
    avatar_in = raw.get("avatar") if isinstance(raw.get("avatar"), dict) else {}
    avatar: dict[str, str] = {}
    img = avatar_in.get("image_url")
    if isinstance(img, str) and img.strip():
        # Accept a path ("/persona-mascot.png"), an http(s) URL or an
        # inlined data: URI — capped so a runaway base64 blob can't
        # balloon the manifest.
        avatar["image_url"] = img.strip()[:_PERSONA_AVATAR_URL_MAX]
    emoji = avatar_in.get("emoji")
    if isinstance(emoji, str) and emoji.strip():
        avatar["emoji"] = emoji.strip()[:8]
    color = avatar_in.get("color")
    if isinstance(color, str) and color.strip():
        avatar["color"] = color.strip()[:32]
    if avatar:
        out["avatar"] = avatar

    # ---- personality ----------------------------------------------------
    pers_in = raw.get("personality") if isinstance(raw.get("personality"), dict) else {}
    personality: dict[str, Any] = {}
    tone = _persona_str_or_locale(pers_in.get("tone"), 200)
    if tone:
        personality["tone"] = tone
    bio = _persona_str_or_locale(pers_in.get("bio"), 1000)
    if bio:
        personality["bio"] = bio
    traits_raw = pers_in.get("traits")
    if isinstance(traits_raw, list):
        traits: list[Any] = []
        for item in traits_raw[:_PERSONA_MAX_TRAITS]:
            coerced = _persona_str_or_locale(item, 40)
            if coerced:
                traits.append(coerced)
        if traits:
            personality["traits"] = traits
    if personality:
        out["personality"] = personality

    greeting = _persona_str_or_locale(raw.get("greeting"), 400)
    if greeting:
        out["greeting"] = greeting

    # A persona block that forgot the name still loads blank in the
    # editor hero — backfill it from the app display name.
    if out and "name" not in out:
        fb = _persona_fallback(agent_app)
        if fb and fb.get("name"):
            out["name"] = fb["name"]

    return out or _persona_fallback(agent_app)


# ── Persona DB override (Operations → "Identidad del empleado IA") ───
# The manifest persona is the INITIAL identity; the operator can refine
# it without a redeploy. We persist per-field overrides in the
# ``agent_persona`` table and merge them over the manifest persona,
# field-by-field, so an unedited field keeps the manifest value.

def _loads_or_none(raw: Optional[str]) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        # Legacy / hand-written value stored as a bare string.
        return raw


def _db_persona_to_dict(row: "AgentPersona") -> dict:
    """Project a stored override row into the same shape ``_persona_block``
    emits (so the two can be merged), sanitising exactly the same way."""
    out: dict[str, Any] = {}
    if row.name and row.name.strip():
        out["name"] = row.name.strip()[:80]

    role = _persona_str_or_locale(_loads_or_none(row.role_json), 200)
    if role:
        out["role"] = role

    avatar: dict[str, str] = {}
    if row.avatar_image_url and row.avatar_image_url.strip():
        avatar["image_url"] = row.avatar_image_url.strip()[:_PERSONA_AVATAR_URL_MAX]
    if row.avatar_emoji and row.avatar_emoji.strip():
        avatar["emoji"] = row.avatar_emoji.strip()[:8]
    if row.avatar_color and row.avatar_color.strip():
        avatar["color"] = row.avatar_color.strip()[:32]
    if avatar:
        out["avatar"] = avatar

    personality: dict[str, Any] = {}
    tone = _persona_str_or_locale(_loads_or_none(row.tone_json), 200)
    if tone:
        personality["tone"] = tone
    bio = _persona_str_or_locale(_loads_or_none(row.bio_json), 1000)
    if bio:
        personality["bio"] = bio
    traits_raw = _loads_or_none(row.traits_json)
    if isinstance(traits_raw, list):
        traits: list[Any] = []
        for item in traits_raw[:_PERSONA_MAX_TRAITS]:
            coerced = _persona_str_or_locale(item, 40)
            if coerced:
                traits.append(coerced)
        if traits:
            personality["traits"] = traits
    if personality:
        out["personality"] = personality

    greeting = _persona_str_or_locale(_loads_or_none(row.greeting_json), 400)
    if greeting:
        out["greeting"] = greeting

    return out


def _merge_persona(base: Optional[dict], override: Optional[dict]) -> Optional[dict]:
    """Deep-merge ``override`` over ``base`` for the persona shape. Scalars
    and lists are replaced wholesale; the ``avatar`` and ``personality``
    sub-objects merge key-by-key so the operator can set just an emoji or
    just the bio without wiping the manifest's other fields."""
    if not base and not override:
        return None
    if not override:
        return dict(base or {})
    if not base:
        base = {}
    merged: dict[str, Any] = dict(base)
    for key, val in override.items():
        if key in ("avatar", "personality") and isinstance(val, dict):
            sub = dict(merged.get(key) or {})
            sub.update(val)
            merged[key] = sub
        else:
            merged[key] = val
    return merged or None


async def _resolve_persona(agent_app: dict, session) -> Optional[dict]:
    """Manifest persona with the DB override applied (override wins)."""
    base = _persona_block(agent_app)
    row = (await session.execute(
        select(AgentPersona).where(AgentPersona.id == "default")
    )).scalar_one_or_none()
    override = _db_persona_to_dict(row) if row is not None else None
    return _merge_persona(base, override)


async def _agent_block(agent_app: dict, summarise_perms, classify) -> dict:
    from app.mcp_runtime import fichas_de_conectores

    perms = agent_app.get("permissions") or {}
    mcps = []
    for mcp in agent_app.get("mcps", []) or []:
        is_dict = isinstance(mcp, dict)
        name = mcp.get("name") if is_dict else str(mcp)
        if not name:
            continue
        # What this connector IS is said by the catalog card, same as on the connectors
        # screen. The manifest keeps a copy frozen when building the image, and deducing the
        # toolkit from it —from the NAME, moreover— is what made this same employee declare
        # "odoo-17-18" as toolkit and Composio answer, rightly, that that toolkit does not
        # exist. A piece of data said differently on two screens of the same employee is a
        # piece of data that can no longer be trusted.
        ficha = fichas_de_conectores().get(name) or {}
        auth_type = (ficha.get("auth_type") or (mcp.get("auth_type") if is_dict else "") or "")
        toolkit = (ficha.get("composio_toolkit")
                   or (mcp.get("composio_toolkit") if is_dict else "") or "")
        external = auth_type.lower() == "composio" or name.startswith("composio-")
        if external and not toolkit and name.startswith("composio-"):
            toolkit = name[len("composio-"):]   # catalog convention, not a deduction
        entry = {
            "name": name,
            "kind": classify(name),
            "display_name": (mcp.get("display_name") if is_dict else None) or name,
            "external": external,
        }
        if external:
            entry["auth_type"] = "composio"
            entry["composio_toolkit"] = toolkit.strip().lower()
        mcps.append(entry)
    return {
        "id":             agent_app.get("agent_id"),
        "slug":           agent_app.get("agent_slug"),
        "name":           agent_app.get("agent_display_name") or agent_app.get("name"),
        "display_name":   agent_app.get("agent_display_name") or agent_app.get("name"),
        "version":        agent_app.get("agent_version"),
        "description":    agent_app.get("description") or agent_app.get("agent_description"),
        # Marketing one-liner for the Welcome ("AI employee") page. May
        # be a plain string or a {es,en} locale dict — kept verbatim so
        # the SPA resolves it to the active language. Falls back to the
        # description on the frontend when absent.
        "tagline":        agent_app.get("agent_tagline") or agent_app.get("tagline"),
        # Optional "AI employee" persona (name, mascot avatar, role,
        # personality, greeting). Sanitised; ``None`` when the manifest
        # doesn't define one so the SPA falls back to display_name.
        "persona":        _persona_block(agent_app),
        "capabilities":   agent_app.get("capabilities", []),
        "example_prompts": agent_app.get("example_prompts", []),
        # v7.169.0 — the skills are asked to the POD, not to the manifest. `agent_app.skills`
        # was a snapshot written when generating the application: it showed `wai-odoo@v3.0.0`
        # while the pod ran 3.3.0, did not see a skill added later and kept showing removed
        # ones — on a screen called «Capacidades declaradas». Moreover it only covered ONE of
        # the three origins: the functional ones the operator attaches and the ones the
        # deployer injects (`wai-agent-core`, `wai-autonomous-delivery`), which cannot be
        # disabled and did not appear anywhere, were missing.
        # CAREFUL: `skills` is still a LIST. Changing it to an object broke the screen
        # silently —"No declared skills" with the eleven loaded— because the consumer
        # expected an array. The flag goes separately.
        **(await _skills_bloque()),
        "mcps":           mcps,
        "permissions": {
            "raw":     perms,
            "summary": summarise_perms(perms),
            "summary_code": getattr(summarise_perms, "codigo", None),
        },
    }


def _presentation_block(agent_app: dict) -> dict:
    """Non-technical "cover letter" copy for the Welcome page, sourced
    from the Step-0 discovery questionnaire (``notes.discovery``).

    These fields are authored once at generation time and are mostly
    single-language; the SPA shows them as supporting accent text while
    all section headings/labels come from the bilingual i18n bundle, and
    the richer per-item copy (watchers/actions ``business_description``,
    KPI/entity labels) is resolved per-locale on the frontend.

    Only a known, sanitised subset is echoed so a malformed manifest
    cannot leak large or arbitrary keys to the browser."""
    notes = agent_app.get("notes") or {}
    disc = notes.get("discovery") or {}

    def _clean_str(v: Any) -> Optional[str]:
        return v.strip() if isinstance(v, str) and v.strip() else None

    def _clean_list(v: Any) -> list[str]:
        if not isinstance(v, list):
            return []
        return [x.strip() for x in v if isinstance(x, str) and x.strip()]

    return {
        "objective": _clean_str(disc.get("objective")),
        "outputs":   _clean_list(disc.get("outputs")),
        "triggers":  _clean_list(disc.get("triggers")),
        "channels":  _clean_list(disc.get("channels")),
        "entities":  _clean_list(disc.get("entities")),
    }


def _manual_block(agent_app: dict) -> dict:
    """Employee manual for the "Manual" page, two simple LINKS:
    (1) ``global_url`` — the generic GLOBAL manual (platform config
        ``EMPLOYEE_MANUAL_URL``, the single guide of how ANY WAI employee works);
    (2) ``specific_url`` — THIS employee's SPECIFIC manual (the ZIP the publisher uploads to
        the marketplace), built with ``EMPLOYEE_MANUAL_BASE_URL`` + the employee's
        marketplace slug: ``agent_app.marketplace_slug`` from the manifest (written by the
        install) or, failing that, the ``MARKETPLACE_SLUG`` env (injected by the deployer
        from the platform — it covers the publisher's original employees).
    Either of the two is ``None`` if its config/slug is missing — the page hides that section."""
    from app.config import EMPLOYEE_MANUAL_URL, EMPLOYEE_MANUAL_BASE_URL, MARKETPLACE_SLUG
    slug = str(agent_app.get("marketplace_slug") or "").strip() or MARKETPLACE_SLUG.strip()
    specific_url = None
    if EMPLOYEE_MANUAL_BASE_URL and slug:
        specific_url = f"{EMPLOYEE_MANUAL_BASE_URL.rstrip('/')}/{slug}/manual/index.html"
    return {
        "global_url": EMPLOYEE_MANUAL_URL or None,
        "specific_url": specific_url,
    }


# Short cache + ONE single flight for the query to the sidecar. The manifest is an
# apparently CHEAP endpoint that inside makes an HTTP call to another pod, and it is
# requested by clients that do not know that: on 31/08/2026 the mobile app requested it ~6
# times per second (one of its effects relaunched on every render while the answer arrived
# streaming) and each request held a database connection waiting for the sidecar until the
# pool ran out. The WHOLE employee degraded: 2 s writes, readiness timing out and the pod
# out of its Service. The client was fixed, but a chatty client being able to bring the
# server down is a server fault: that is what this closes.
#
# 10 s of cache do not lie —it is still "what the pod has loaded", and it was this or the
# manifest's frozen snapshot, which is what was removed for showing old versions—. The lock
# makes N simultaneous requests produce ONE call, not N.
_SKILLS_TTL = 10.0
_skills_cache: dict = {"t": 0.0, "datos": None}
_skills_lock = asyncio.Lock()


async def _skills_bloque() -> dict:
    """What the pod has loaded right now. It never falls back to the manifest's snapshot: if
    it cannot be queried, it is said — stale data presented as current is exactly what had
    to be fixed."""
    from app.agent_proxy import get_agent_proxy
    ahora = time.monotonic()
    if _skills_cache["datos"] is not None and (ahora - _skills_cache["t"]) < _SKILLS_TTL:
        datos = _skills_cache["datos"]
    else:
        async with _skills_lock:
            # Re-checked INSIDE the lock: the ones waiting already have a fresh answer from
            # the first and must not repeat the call.
            ahora = time.monotonic()
            if _skills_cache["datos"] is not None and (ahora - _skills_cache["t"]) < _SKILLS_TTL:
                datos = _skills_cache["datos"]
            else:
                try:
                    datos = await get_agent_proxy().list_skills()
                except Exception:  # noqa: BLE001
                    logger.debug("skills vivas: fallo consultando al sidecar", exc_info=True)
                    datos = {"available": False, "reason": "sidecar unreachable", "skills": []}
                # The failure is cached too: if the sidecar is down, retrying on every request
                # is exactly what turns an outage into an avalanche.
                _skills_cache.update({"t": time.monotonic(), "datos": datos})
    return {
        "skills": datos.get("skills") or [],
        # "Could not be queried" is NOT "it does not have": the screen must be able to tell
        # them apart. An empty list presented as the truth is the same failure as the old
        # snapshot, just the other way round.
        "skills_available": bool(datos.get("available")),
        "skills_unavailable_reason": datos.get("reason"),
    }


from app.config import (  # noqa: E402 — next to its use, like the rest of this module
    RECEPTION_ENABLED as _RECEPTION_ENABLED,
    VOICE_ENABLED as _VOICE_ENABLED,
)


async def _bloque_de_su_aplicacion() -> dict | None:
    """The WAI application associated with this employee, or `None`.

    It is asked to the platform (it does not travel in an environment variable) because the
    association is stored WITHOUT deploying: see `app/mi_aplicacion.py`."""
    try:
        from app.mi_aplicacion import cual_es
        d = await cual_es()
        return {"url": d["url"], "nombre": d.get("nombre") or ""} if d.get("tiene") else None
    except Exception:  # noqa: BLE001 — the manifest cannot fall over because of this
        return None


@router.get("/manifest")
async def get_manifest(
    user: TokenPayload = Depends(require_project_access),
    session=Depends(get_app_session),
) -> dict:
    cfg = _load_wai_app()
    agent_app = cfg.get("agent_app", {})
    now = datetime.now(timezone.utc)

    # Persona ("AI employee") — manifest identity with the operator's
    # DB override applied so edits made in Operations show up everywhere
    # (welcome hero, chat header, chat reply avatar) without a redeploy.
    persona_resolved = await _resolve_persona(agent_app, session)

    # ---- Status counters ------------------------------------------------
    rows = (await session.execute(
        select(Event.status, func.count()).group_by(Event.status)
    )).all()
    by_status = {"PENDING": 0, "CLAIMED": 0, "COMPLETED": 0, "FAILED": 0}
    by_status.update({status: int(n) for status, n in rows})

    # ---- "Today" buckets (autonomous only) -----------------------------
    # R-OPS-2: see note in recent_runs above; the dashboard "Hoy"
    # tile must show autonomous activity only so chat doesn't
    # inflate the success-count when no events have fired.
    since_today = now - timedelta(hours=24)
    today_rows = (await session.execute(
        select(AgentRun.status, func.count())
        .where(AgentRun.started_at >= since_today)
        .where(AgentRun.mode == "autonomous")
        .group_by(AgentRun.status)
    )).all()
    today = {"runs": 0, "completed": 0, "failed": 0}
    for status, n in today_rows:
        today["runs"] += int(n)
        if status == "COMPLETED": today["completed"] = int(n)
        if status == "FAILED":    today["failed"]    = int(n)
    # Lightweight chat counter for the "Conversaciones (24h)" tile.
    chat_today = (await session.execute(
        select(func.count())
        .select_from(AgentRun)
        .where(AgentRun.started_at >= since_today)
        .where(AgentRun.mode == "chat")
    )).scalar_one() or 0

    # ---- Recent runs (deep links) — split by mode ----------------------
    # R-OPS-2: the Operations page is a control plane for the
    # AUTONOMOUS half of the agent. Mixing chat sessions in
    # ``recent_runs`` made the "Last run" tile show a chat
    # message whose ``mode = 'chat'`` and whose ``event_id`` is NULL
    # (so the operator couldn't even click through to an event).
    # Chat traffic lives at /runs?mode=chat and on the dedicated
    # ``recent_chat_runs`` block below so operators can still see
    # human activity at a glance without it polluting autonomous
    # KPIs.
    autonomous_rows = (await session.execute(
        select(AgentRun)
        .options(selectinload(AgentRun.event))
        .where(AgentRun.mode == "autonomous")
        .order_by(desc(AgentRun.started_at))
        .limit(5)
    )).scalars().all()
    chat_rows = (await session.execute(
        select(AgentRun)
        .where(AgentRun.mode == "chat")
        .order_by(desc(AgentRun.started_at))
        .limit(5)
    )).scalars().all()

    def _serialise_autonomous(r: AgentRun) -> dict:
        return {
            "id":           r.id,
            "status":       r.status,
            "event_type":   r.event.type if r.event else None,
            "started_at":   _iso_utc(r.started_at),
            "duration_sec": r.duration_sec,
            "model":        r.model or None,
            "mode":         r.mode,
        }

    def _serialise_chat(r: AgentRun) -> dict:
        return {
            "id":           r.id,
            "status":       r.status,
            "started_at":   _iso_utc(r.started_at),
            "duration_sec": r.duration_sec,
            "model":        r.model or None,
            "mode":         r.mode,
        }

    recent_autonomous_runs = [_serialise_autonomous(r) for r in autonomous_rows]
    recent_chat_runs       = [_serialise_chat(r)       for r in chat_rows]
    # Legacy alias kept so older SPAs that consume ``recent_runs``
    # without the autonomous suffix keep working. New code MUST use
    # ``recent_autonomous_runs`` so chat never sneaks back in.
    recent_runs = recent_autonomous_runs
    last_run = recent_autonomous_runs[0] if recent_autonomous_runs else None

    last_err = (await session.execute(
        select(AgentRun)
        .options(selectinload(AgentRun.event))
        .where(AgentRun.status == "FAILED")
        .where(AgentRun.mode == "autonomous")
        .order_by(desc(AgentRun.started_at))
        .limit(1)
    )).scalar_one_or_none()
    last_error = None
    if last_err is not None:
        last_error = {
            "id":         last_err.id,
            "started_at": _iso_utc(last_err.started_at),
            "message":    (last_err.error or "")[:280],
            "event_type": last_err.event.type if last_err.event else None,
        }

    # ---- Latency p50/p95 (only over completed AUTONOMOUS runs) ---------
    # R-OPS-2: chat latency drags p50/p95 in opposite directions
    # (interactive turns are short, autonomous runs are long); the
    # Operations latency tile should reflect the autonomous SLO so
    # we filter by mode.
    durations = (await session.execute(
        select(AgentRun.duration_sec)
        .where(AgentRun.status == "COMPLETED")
        .where(AgentRun.mode == "autonomous")
        .where(AgentRun.duration_sec.is_not(None))
        .order_by(desc(AgentRun.id))
        .limit(1000)
    )).scalars().all()
    p50, p95 = _percentiles([d * 1000 for d in durations if d is not None], [50, 95])

    # ---- Watchers — enriched -------------------------------------------
    schedules = {
        s.watcher_name: s
        for s in (await session.execute(select(WatcherSchedule))).scalars().all()
    }
    # R-OPS-1: load action overrides once so we can splice them into
    # the actions block below without N+1.
    action_overrides = {
        ov.event_type: ov
        for ov in (await session.execute(select(ActionOverride))).scalars().all()
    }
    watchers = []
    for w in agent_app.get("watchers", []):
        name = w.get("name")
        last = (await session.execute(
            select(AgentRun.started_at, AgentRun.status, AgentRun.duration_sec)
            .join(Event, AgentRun.event_id == Event.id)
            .where(Event.type == w.get("event_type"))
            .order_by(desc(AgentRun.id))
            .limit(1)
        )).first()
        last_event = (await session.execute(
            select(Event.detected_at, Event.status, Event.id)
            .where(Event.source == name)
            .order_by(desc(Event.id))
            .limit(1)
        )).first()
        sched = schedules.get(name)
        in_flight = bool(last and last[1] in ("RUNNING", "CLAIMED"))
        # R-OPS-6: cron and interval are now MUTUALLY EXCLUSIVE
        # at save time (the editor forces a mode). If the operator
        # saved a cron, ``sched.cron_expr`` is set and
        # ``sched.interval_sec`` is None — and we must honour that,
        # which means falling back to the manifest only when BOTH
        # override fields are None. The legacy "OR with manifest"
        # behaviour bled the manifest's interval back into a
        # cron-mode override, producing the "they overwrite each other" duplication
        # the operator reported.
        if sched is not None:
            effective_interval = sched.interval_sec
            effective_cron = sched.cron_expr
            if effective_interval is None and effective_cron is None:
                # Either the schedule row exists for a different
                # field (e.g. enabled/biz description) or the
                # operator hasn't picked a cadence yet — fall back
                # to the manifest defaults so the watcher still
                # ticks.
                effective_interval = w.get("interval_sec")
                effective_cron = w.get("cron_expr")
        else:
            effective_interval = w.get("interval_sec")
            effective_cron = w.get("cron_expr")
        # R-OPS-1: business_description follows operator-override
        # precedence. Manifest authors set the initial copy at
        # generation time (``agent_app.watchers[].business_description``
        # — a per-locale dict). On-call operators can rewrite it
        # without redeploy via PATCH /watchers/{name}, persisted in
        # ``watcher_schedules.business_description_json``. Frontend
        # gets a single ``business_description`` (dict {lang: text})
        # plus a flag so it can render an "edited by operator" badge.
        manifest_biz = _coerce_locale_dict(w.get("business_description"))
        override_biz: Optional[dict[str, str]] = None
        if sched and sched.business_description_json:
            try:
                override_biz = _coerce_locale_dict(
                    json.loads(sched.business_description_json)
                )
            except (TypeError, ValueError):
                override_biz = None
        watchers.append({
            **w,
            # Locale-agnostic source of truth (manifest + override).
            "business_description":          override_biz or manifest_biz,
            "business_description_source":   (
                "override" if override_biz else ("manifest" if manifest_biz else None)
            ),
            # Structured payload for the SPA to render the technical
            # sentence in the user's locale (i18n templates live in
            # the frontend, not on the backend — see R-OPS-1).
            "technical":         _technical_description_payload(
                w,
                interval_sec=effective_interval,
                cron_expr=effective_cron,
            ),
            # Legacy single-locale field for older SPAs. Keep it
            # populated so deployed-but-not-rebuilt frontends keep
            # showing *something*. Newer SPAs ignore this.
            "description":       _legacy_description(w),
            "enabled":           bool(sched.enabled) if sched else True,
            "interval_sec":      effective_interval,
            "cron_expr":         effective_cron,
            "schedule_overridden": sched is not None and (
                bool(sched.interval_sec) or bool(sched.cron_expr)
                or not bool(sched.enabled)
            ),
            "last_run_at":       _iso_utc(last[0]) if last else None,
            "last_status":       last[1] if last else None,
            "last_duration_sec": int(last[2]) if last and last[2] is not None else None,
            "in_flight":         in_flight,
            "last_event_at":     _iso_utc(last_event[0]) if last_event else None,
            "last_event_status": last_event[1] if last_event else None,
            "last_event_id":     last_event[2] if last_event else None,
        })

    # IS7 / R8 — derive the next watcher tick (for the Operations
    # standby card) and the latest watcher heartbeat (for the
    # liveness badge). Wrapped in a try/except so a single bad
    # timestamp never 500s the entire Operations page (which is the
    # only diagnostic surface the operator has).
    #
    # NOTE: Event.detected_at is persisted via datetime.utcnow()
    # which returns NAIVE datetimes (no tzinfo). Comparing those
    # against a tz-aware ``now_utc`` raises TypeError, so we coerce
    # parsed strings to UTC before any comparison.
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    now_utc = _dt.now(_tz.utc)
    next_tick_at = None
    last_watcher_pulse = None
    max_interval = 0
    watcher_stale = False
    try:
        for w in watchers:
            ival = w.get("interval_sec")
            if ival:
                try:
                    max_interval = max(max_interval, int(ival))
                except (TypeError, ValueError):
                    pass
            if not w.get("enabled"):
                continue
            last_at = w.get("last_event_at")
            if last_at:
                try:
                    last_dt = _dt.fromisoformat(str(last_at).replace("Z", "+00:00"))
                except ValueError:
                    continue
                # Coerce naive → aware (UTC) so comparisons below
                # don't raise TypeError.
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=_tz.utc)
                if last_watcher_pulse is None or last_dt > last_watcher_pulse:
                    last_watcher_pulse = last_dt
                if ival:
                    tick = last_dt + _td(seconds=int(ival))
                    if tick < now_utc:
                        tick = now_utc + _td(seconds=int(ival))
                    if next_tick_at is None or tick < next_tick_at:
                        next_tick_at = tick
            elif ival:
                tick = now_utc + _td(seconds=int(ival))
                if next_tick_at is None or tick < next_tick_at:
                    next_tick_at = tick

        watcher_stale = bool(
            last_watcher_pulse
            and max_interval
            and (now_utc - last_watcher_pulse).total_seconds() > 2 * max_interval
        )
    except Exception:
        # Best-effort metric. Never let it kill the Operations page.
        import logging as _logging
        _logging.getLogger(__name__).exception(
            "watcher heartbeat computation failed; serving Operations without it"
        )
        next_tick_at = None
        last_watcher_pulse = None
        watcher_stale = False

    # ---- Actions (the real headline) -----------------------------------
    actions = _build_actions_block(agent_app, action_overrides)

    # Strip runtime entities before exposing them to the SPA. The
    # generator may have inadvertently declared ``events`` /
    # ``agent_runs`` / etc. under ``domain_entities`` (older skills
    # encouraged it). v6.4.0 makes Domain strictly business-data; the
    # operational tables are reachable via ``/events`` and ``/runs``.
    from app.routes.domain_routes import enrich_entity
    raw_domain_entities = agent_app.get("domain_entities", []) or []
    filtered_domain_entities = []
    skipped_runtime = []
    for entity in raw_domain_entities:
        if _is_runtime_entity(entity):
            skipped_runtime.append(entity.get("slug") or entity.get("table"))
            continue
        # Enrich with the resolved lifecycle (so the SPA knows which
        # operator transitions to offer) AND the auto-derived filters
        # (status/severity/priority/decision/date) for local entities.
        filtered_domain_entities.append(enrich_entity(entity))
    if skipped_runtime:
        # Deliberately a logger.warning instead of an exception: an old
        # generated app should keep booting, just with a cleaner sidebar.
        import logging
        logging.getLogger(__name__).warning(
            "manifest: dropping runtime entities from domain_entities (use /events|/runs instead): %s",
            skipped_runtime,
        )

    from app.config import CHANNELS_ENABLED as _channels_enabled
    from app.config import MAX_CHAT_THREADS as _max_threads
    from app.config import MAX_CONCURRENT_CHATS as _max_active_chats
    # Skill version this agent was generated/last-evolved from. Source of truth
    # is version.py (byte-checked == skill.json); the manifest may also carry a
    # ``skill_version`` stamp. Surfaced so Operations → Estado shows it.
    try:
        from app.version import SKILL_VERSION as _skill_version
    except Exception:  # pragma: no cover — never break the manifest
        _skill_version = ""
    # v7.39.0 — the business timezone is operator-editable (Identity page); use
    # the EFFECTIVE value (override → deploy default) so the SPA re-renders in the
    # new wall clock without a redeploy.
    from app.business_timezone import effective_timezone as _effective_tz
    _tz = await _effective_tz()
    # v7.41.0 — the EFFECTIVE signal policy (axes, bands, reason codes + this app's
    # tuning params) so the SPA guide/welcome show the real policy, not hardcoded copy.
    try:
        from app.signal_policy import public_policy as _public_policy
        _signal_policy = _public_policy("es")
    except Exception:  # pragma: no cover — never break the manifest over this
        _signal_policy = None
    # v7.117.0 — the employee's default theme (editable in Identity admin). The SPA applies
    # it at start ONLY if the user has not chosen a theme by hand. '' or absent = system.
    try:
        _dcm_row = (await session.execute(
            select(AppSetting.value).where(AppSetting.key == "default_color_mode")
        )).scalar_one_or_none()
        _default_color_mode = (str(_dcm_row or "system")).strip().lower()
        if _default_color_mode not in ("system", "light", "dark"):
            _default_color_mode = "system"
    except Exception:  # pragma: no cover — never break the manifest over this
        _default_color_mode = "system"
    return {
        "app_name":         cfg.get("name") or agent_app.get("name"),
        "skill_version":    cfg.get("skill_version") or _skill_version,
        "default_color_mode": _default_color_mode,
        # Feature flag for the SPA sidebar: when channels are disabled
        # at the manifest level the "Canales" nav entry is hidden so
        # operators don't land on a permanently-empty page.
        "channels_enabled": bool(_channels_enabled),
        # Conversation threads (v9.1.0). It is the compatibility contract with the employees
        # that are NOT updated: there is ONE web frontend and ONE mobile app talking to
        # employees of many versions, so the client asks instead of deducing by version
        # number —which is fragile and ages—.
        #
        # THE ABSENCE OF THIS KEY MEANS "NO". An old employee does not publish it and the
        # client behaves exactly as until now: without a thread selector and without calling
        # any new endpoint.
        # v10.2.0 — which CARDS this employee can emit, so the client knows what to ask for.
        # The absence of the flag means "none": there are clients talking to employees of
        # several generations and deducing it by version number is fragile.
        "tarjetas": {
            "soportado": True,
            "tipos": list(_TIPOS_DE_TARJETA),
            "v": _VERSION_DE_TARJETA,
        },
        "hilos": {
            "soportado": True,
            "max": _max_threads,
            "max_activos": _max_active_chats,
        },
        # WHAT this employee HAS (platform V101, skill 12.27.0). It is decided WHEN DEPLOYING
        # it and both are in beta, so normally it does not have them.
        #
        # The manifest publishing it is what allows the web and the mobile app to NOT PAINT
        # the talk button: absence = no, same as with cards and threads. Showing a button
        # that errors when pressed is worse than not offering it, and deducing it by version
        # number is fragile — there are clients talking to employees of several generations
        # at once.
        "conversacion": {
            "voz": bool(_VOICE_ENABLED),
            "recepcion": bool(_RECEPTION_ENABLED),
        },
        # ITS WAI APPLICATION, if it has one associated (platform V102, 12.51.0). The frontend
        # needs it to recognize a link AS ITS OWN —the host is compared— and open it with the
        # session resolved instead of dropping the person on a sign-in screen. `null` = it has
        # none, or it has one not deployed yet: without an address there is nowhere to take
        # them and nothing is painted.
        "aplicacion":       await _bloque_de_su_aplicacion(),
        "agent":            {
            **(await _agent_block(agent_app, await _permissions_summariser(), await _mcp_classifier())),
            # DB override merged over the manifest persona.
            "persona":        persona_resolved,
        },
        # Business timezone (IANA name) so the SPA renders every
        # timestamp in the operator's wall clock instead of the
        # browser's. Resolved from APP_TIMEZONE / presentation.locale.
        "timezone":         _tz,
        "locale": {
            "timezone": _tz,
            "currency": (cfg.get("presentation", {}).get("locale", {}) or {}).get("currency")
                        or agent_app.get("locale", {}).get("currency"),
        },
        # Non-technical cover-letter copy for the Welcome page.
        "presentation":     _presentation_block(agent_app),
        # Employee manual: link to the global manual + specific manual (es/en).
        "manual":           _manual_block(agent_app),
        "business_kpis":    agent_app.get("business_kpis", []),
        "trend_charts":     agent_app.get("trend_charts", []),
        "domain_entities":  filtered_domain_entities,
        "signal_policy":    _signal_policy,
        "permissions":      agent_app.get("permissions", {}),
        "event_types":      [w.get("event_type") for w in agent_app.get("watchers", []) if w.get("event_type")],
        "watchers":         watchers,
        "actions":          actions,
        "stats": {
            "by_status":  by_status,
            "today":      today,
            "chat_today": int(chat_today),
            "last_run":   last_run,
            "last_error": last_error,
            "latency_ms": {"p50": p50, "p95": p95, "samples": len(durations)},
            # R-OPS-2: split-mode views. ``recent_runs`` stays for
            # back-compat with older SPAs; new code consumes the
            # explicit ``recent_autonomous_runs`` / ``recent_chat_runs``.
            "recent_runs":            recent_runs,
            "recent_autonomous_runs": recent_autonomous_runs,
            "recent_chat_runs":       recent_chat_runs,
            "next_tick_at":     _iso_utc(next_tick_at),
            "watcher_pulse_at": _iso_utc(last_watcher_pulse),
            "watcher_stale": watcher_stale,
        },
    }


def _build_actions_block(
    agent_app: dict,
    overrides: Optional[dict[str, ActionOverride]] = None,
) -> list[dict]:
    """Read `agent_app.event_type_specs` from wai-app.json and enrich with
    the inferred MCP kind for each tool. The skill is responsible for
    populating `event_type_specs` at generation time — see SKILL.md §0.i
    for the contract.

    R-OPS-1: ``business_description`` follows the same override
    precedence as watchers — operator-edited value wins over the
    manifest copy. ``summary`` is kept for backward-compat with
    older SPAs (it is the single-locale fallback the manifest used
    historically) but the new SPA should prefer
    ``business_description``.
    """
    overrides = overrides or {}
    specs = agent_app.get("event_type_specs") or []
    out: list[dict] = []
    for spec in specs:
        reads = spec.get("reads", {}) or {}
        writes = spec.get("writes", {}) or {}
        event_type = spec["type"]
        manifest_biz = _coerce_locale_dict(spec.get("business_description"))
        ov = overrides.get(event_type)
        override_biz: Optional[dict[str, str]] = None
        if ov and ov.business_description_json:
            try:
                override_biz = _coerce_locale_dict(
                    json.loads(ov.business_description_json)
                )
            except (TypeError, ValueError):
                override_biz = None
        out.append({
            "type":    event_type,
            "label":   spec.get("label") or event_type,
            "summary": spec.get("summary", ""),
            "business_description":        override_biz or manifest_biz,
            "business_description_source": (
                "override" if override_biz else ("manifest" if manifest_biz else None)
            ),
            "reads": {
                "project_tables": reads.get("project_tables", []),
                "tools": [
                    {"name": t, "mcp": t.split(".")[0], "kind": "read_only"}
                    for t in reads.get("tools", [])
                ],
            },
            "writes": {
                "local_tables": writes.get("local_tables", []),
                "tools": [
                    {"name": t, "mcp": t.split(".")[0], "kind": "local_write"}
                    for t in writes.get("tools", [])
                ],
            },
            "ui_effects": spec.get("ui_effects", []),
            "structured_output": spec.get("structured_output", []),
            "system": False,
        })

    # F0: ALWAYS surface the platform SYSTEM events the baked backend emits
    # and handles via prompts_defaults — even when the manifest forgot to
    # declare them. Otherwise Operaciones hides real behaviour (the contract
    # would be incomplete). Operator overrides still apply to their text.
    declared = {e["type"] for e in out}
    for ev_type, meta in _SYSTEM_EVENT_SPECS.items():
        if ev_type in declared:
            continue
        ov = overrides.get(ev_type)
        override_biz: Optional[dict[str, str]] = None
        if ov and ov.business_description_json:
            try:
                override_biz = _coerce_locale_dict(json.loads(ov.business_description_json))
            except (TypeError, ValueError):
                override_biz = None
        out.append({
            "type": ev_type,
            "label": meta["label"],
            "summary": "",
            "business_description": override_biz or meta["business_description"],
            "business_description_source": "override" if override_biz else "system",
            "reads": {"project_tables": [], "tools": []},
            "writes": {
                "local_tables": meta.get("local_tables", []),
                "tools": [
                    {"name": t, "mcp": t.split(".")[0], "kind": "local_write"}
                    for t in meta.get("tools", [])
                ],
            },
            "ui_effects": meta.get("ui_effects", []),
            "structured_output": [],
            "system": True,
        })
    return out


# Platform events the BAKED backend emits + handles via prompts_defaults.
# Surfaced in Operaciones regardless of what the manifest declares so the
# employee's real behaviour is never hidden.
# ``triggerable`` (v7.134.0): whether this system event is offered as a
# business_event SIGNAL in the automation editor. DELEGATED_TASK / APPROVAL_GRANTED
# are internal plumbing (the employee already runs a delegated task / an approved
# action automatically) — offering them as "when" triggers is redundant and
# confusing, so they are hidden from the trigger catalog. They STAY here so the
# capability manifest keeps documenting the behaviour; only the trigger offering is
# filtered (see routes/automations_routes.catalog + mcp_server.get_automation_catalog).
_SYSTEM_EVENT_SPECS: dict[str, dict] = {
    "SCHEDULED_TASK_DUE": {
        "triggerable": True,
        "label": "Tarea programada vencida",
        "business_description": {
            "es": "Cuando un encargo o seguimiento llega a su hora, el empleado lo atiende y lo cierra.",
            "en": "When a scheduled task or follow-up comes due, the employee handles and closes it.",
        },
        "local_tables": ["scheduled_items"],
        "tools": ["complete_scheduled_item"],
        # ui_effects MUST be {page,label} objects (the SPA renders
        # <Link to={page}>{label}</Link>); plain strings render as broken,
        # empty links. Point only to pages that ALWAYS exist (Encargos /
        # Actividad), never to conditional /domain/* pages.
        "ui_effects": [{"page": "/encargos", "label": "Encargos"}],
    },
    "DELEGATED_TASK": {
        "triggerable": False,
        "label": "Encargo delegado",
        "business_description": {
            "es": "Cuando le delegas una tarea, el empleado la ejecuta y deja constancia.",
            "en": "When you delegate a task, the employee carries it out and records the outcome.",
        },
        "local_tables": ["recommendations", "communications", "decisions", "scheduled_items", "approvals"],
        "tools": ["complete_scheduled_item", "log_communication", "log_decision", "request_approval"],
        "ui_effects": [
            {"page": "/encargos", "label": "Encargos"},
            {"page": "/actividad", "label": "Actividad"},
        ],
    },
    "APPROVAL_GRANTED": {
        "triggerable": False,
        "label": "Aprobación concedida",
        "business_description": {
            "es": "Cuando apruebas una acción pendiente, el empleado la ejecuta y cierra la aprobación.",
            "en": "When you approve a pending action, the employee executes it and closes the approval.",
        },
        "local_tables": ["communications", "decisions", "approvals"],
        "tools": ["mark_approval_executed", "log_communication", "log_decision"],
        "ui_effects": [{"page": "/actividad", "label": "Actividad"}],
    },
}


@router.get("/watchers/{name}")
async def get_watcher(
    name: str,
    user: TokenPayload = Depends(require_project_access),
):
    cfg = _load_wai_app()
    agent_app = cfg.get("agent_app", {})
    declared = next(
        (w for w in agent_app.get("watchers", []) if w.get("name") == name),
        None,
    )
    if declared is None:
        raise HTTPException(status_code=404, detail=f"unknown watcher {name!r}")
    async with app_session() as s:
        sched = (await s.execute(
            select(WatcherSchedule).where(WatcherSchedule.watcher_name == name)
        )).scalar_one_or_none()
    manifest_biz = _coerce_locale_dict(declared.get("business_description"))
    override_biz: Optional[dict[str, str]] = None
    if sched and sched.business_description_json:
        try:
            override_biz = _coerce_locale_dict(
                json.loads(sched.business_description_json)
            )
        except (TypeError, ValueError):
            override_biz = None
    # R-OPS-6: apply the same mutual-exclusion logic the
    # list endpoint uses so /watchers/{name} doesn't merge a
    # manifest interval back into a cron-mode override.
    if sched is not None:
        eff_interval = sched.interval_sec
        eff_cron = sched.cron_expr
        if eff_interval is None and eff_cron is None:
            eff_interval = declared.get("interval_sec")
            eff_cron = declared.get("cron_expr")
    else:
        eff_interval = declared.get("interval_sec")
        eff_cron = declared.get("cron_expr")
    return {
        "name": name,
        "declared": declared,
        "description":          _legacy_description(declared),
        "technical":            _technical_description_payload(
            declared, interval_sec=eff_interval, cron_expr=eff_cron,
        ),
        "business_description": override_biz or manifest_biz,
        "business_description_source": (
            "override" if override_biz else ("manifest" if manifest_biz else None)
        ),
        "schedule": {
            "enabled":      bool(sched.enabled) if sched else True,
            "interval_sec": eff_interval,
            "cron_expr":    eff_cron,
            "notes":        sched.notes if sched else None,
            "updated_by":   sched.updated_by if sched else None,
            "updated_at":   _iso_utc(sched.updated_at) if sched else None,
            "overridden":   sched is not None,
        },
    }


@router.patch("/watchers/{name}")
async def update_watcher_schedule(
    name: str,
    body: dict[str, Any] = Body(...),
    user: TokenPayload = Depends(require_admin),
):
    """Operator-controlled override for a watcher's cadence, on/off
    and business description.

    The manifest stays the source of truth for *what* a watcher does
    technically (table, column, threshold, event_type) — those fields
    are deliberately frozen here. The editable surface is:

      * ``enabled``               (bool)   — pause/resume
      * ``interval_sec``          (int>0)  — override cadence
      * ``cron_expr``             (str)    — override cron schedule
      * ``notes``                 (str)    — operator's free notes
      * ``business_description``  (dict)   — per-locale business copy
                                              ({"es": "...", "en": "..."})

    R-OPS-1: ``business_description`` is what the operator sees on the
    Operations card. Manifest authors set the initial value at
    generation time; on-call ops can rewrite it without a redeploy.
    """
    cfg = _load_wai_app()
    agent_app = cfg.get("agent_app", {})
    declared = next(
        (w for w in agent_app.get("watchers", []) if w.get("name") == name),
        None,
    )
    if declared is None:
        raise HTTPException(status_code=404, detail=f"unknown watcher {name!r}")

    enabled = body.get("enabled")
    interval_sec = body.get("interval_sec")
    cron_expr = body.get("cron_expr")
    notes = body.get("notes")
    business_description = body.get("business_description")

    if enabled is not None and not isinstance(enabled, bool):
        raise HTTPException(status_code=400, detail="`enabled` must be boolean")
    if interval_sec is not None:
        try:
            interval_sec = int(interval_sec)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="`interval_sec` must be int")
        if interval_sec <= 0:
            raise HTTPException(status_code=400, detail="`interval_sec` must be > 0")

    business_description_json: Optional[str] = None
    if business_description is not None:
        coerced = _coerce_locale_dict(business_description)
        # Treat ``{}`` and ``None`` from the SPA as "clear the
        # override" so the manifest copy reasserts itself.
        business_description_json = (
            json.dumps(coerced, ensure_ascii=False) if coerced else None
        )

    now = datetime.now(timezone.utc)
    values = {
        "watcher_name": name,
        "enabled":      1 if (enabled if enabled is not None else True) else 0,
        "interval_sec": interval_sec,
        "cron_expr":    cron_expr,
        "notes":        notes,
        "business_description_json": business_description_json,
        "updated_by":   getattr(user, "email", None),
        "updated_at":   now,
    }
    set_ = {k: v for k, v in values.items() if k != "watcher_name"}
    stmt = sqlite_insert(WatcherSchedule).values(**values).on_conflict_do_update(
        index_elements=[WatcherSchedule.watcher_name], set_=set_,
    )
    async with app_write() as s:
        await s.execute(stmt)
        await s.commit()
    return {"ok": True, "name": name, **set_}


# ── Budget / consumption (R-BUDGET-1) ───────────────────────────────


@router.get("/budget")
async def get_budget(user: TokenPayload = Depends(require_admin)):
    """This employee's spend vs its monthly credit budget (rolling 30 days),
    split by concept (autónomo vs petición). Single source of truth — the same
    computation the enforcement gates use. Rendered by the Operations "Consumo"
    section (same-origin, in this app); the WAI platform does NOT aggregate this
    cross-pod (kept deliberately simple)."""
    from app.budget import budget_status
    async with app_session() as s:
        return await budget_status(s)


@router.get("/budget/consumption")
async def get_budget_consumption(
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = Query(None),
    user: TokenPayload = Depends(require_admin),
):
    """Date-ranged consumption analytics for the rich Consumo section: a daily
    credits time-series split by concept (autónomo / petición), by-concept
    totals, and the by-user breakdown of petición spend (boss chat operators +
    channel senders). Defaults to the last 30 days; span clamped to 366."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from app.budget import consumption

    def _parse(s: Optional[str], default: _dt) -> _dt:
        if not s:
            return default
        try:
            return _dt.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return default

    now = _dt.now(_tz.utc)
    dt_to = _parse(to, now)
    dt_from = _parse(from_, now - _td(days=30))
    if dt_to <= dt_from:
        dt_from = dt_to - _td(days=30)
    if (dt_to - dt_from).days > 366:
        dt_from = dt_to - _td(days=366)
    async with app_session() as s:
        return await consumption(s, dt_from, dt_to)


# ── Operator settings (app_settings key/value) ──────────────────────

AGENT_LANGUAGES = ("es", "en")


@router.get("/settings")
async def get_settings(user: TokenPayload = Depends(require_project_access)):
    """Operator-editable app settings:

    * ``agent_language`` — the language the agent uses for human-facing
      output in autonomous runs (trigger summaries, alerts/recommendations
      it writes, and proactive notifications). Channel replies are NOT
      affected — they adapt to the sender's language.
    * ``agent_model`` — the model id the agent uses for EVERY call (chat,
      channels, autonomous). Must be one of ``available_models`` (the
      tenant catalogue). Empty string means "use the deploy default".

    ``available_models``/``default_model`` are read-only and come from the
    deploy-time catalogue (``OPENCODE_DEFAULT_MODEL``)."""
    from app.config import AVAILABLE_MODELS, DEFAULT_MODEL, modelo_vigente
    from app import razonamiento as _razonamiento
    from app import atajos as _atajos
    from app.triggers_state import manifest_triggers_start_active, SETTING_KEY as _TRIG_KEY
    from app.autonomy_state import (
        manifest_autonomy_level, LEVELS as _AUTONOMY_LEVELS,
        SETTING_KEY as _AUTON_KEY, _coerce as _coerce_autonomy,
    )
    from app.business_timezone import (
        manifest_timezone as _biz_tz_default, COMMON_TIMEZONES as _BIZ_TIMEZONES,
    )
    async with app_session() as s:
        rows = (await s.execute(select(AppSetting))).scalars().all()
    settings = {r.key: r.value for r in rows}
    # Triggers master switch: stored choice wins; otherwise the manifest default
    # (which defaults to OFF). ``triggers_default`` is read-only context for the UI.
    trig_default = manifest_triggers_start_active()
    trig_raw = settings.get(_TRIG_KEY)
    triggers_active = (
        str(trig_raw).strip().lower() in ("1", "true", "yes", "on")
        if trig_raw is not None else trig_default
    )
    # Autonomy level: stored choice wins; otherwise the manifest default
    # (``propose``). The catalogue is read-only context for the selector.
    auton_default = manifest_autonomy_level()
    auton_raw = settings.get(_AUTON_KEY)
    autonomy = _coerce_autonomy(auton_raw) if auton_raw is not None else auton_default
    # Self-scheduling bounds: stored choice wins; otherwise manifest defaults.
    from app.scheduling_policy import (
        manifest_self_schedule, manifest_max_open_tasks,
        SELF_SCHEDULE_KEY as _SS_KEY, MAX_OPEN_TASKS_KEY as _MOT_KEY,
    )
    ss_default = manifest_self_schedule()
    ss_raw = settings.get(_SS_KEY)
    self_schedule = (
        str(ss_raw).strip().lower() in ("1", "true", "yes", "on")
        if ss_raw is not None else ss_default
    )
    mot_default = manifest_max_open_tasks()
    mot_raw = settings.get(_MOT_KEY)
    try:
        max_open = max(0, int(mot_raw)) if mot_raw is not None else mot_default
    except (TypeError, ValueError):
        max_open = mot_default
    # Execution concurrency (v7.45.0) — effective (stored → manifest → config),
    # applied LIVE. The ceiling + current in-flight counts are read-only context.
    from app.execution_limits import (
        effective_limits as _eff_limits, manifest_max_runs as _mmr,
        CEILING as _EXEC_CEILING, snapshot as _exec_snapshot,
    )
    _mt, _ma = await _eff_limits()
    # 15.6.0 — the turn TIME ceilings, same path and same liveness as the concurrency.
    from app.execution_limits import (
        effective_time_limits as _eff_times, manifest_chat_turn_minutes as _mct,
        manifest_autonomous_run_minutes as _mar, TIME_LIMIT_MIN as _TL_MIN,
        TIME_LIMIT_MAX as _TL_MAX,
    )
    _tc, _ta = await _eff_times()
    # v7.108.0 — the master switch ALONE lies. It is the operator's INTENT ("let the
    # employee act on its own"), not the reality: with the switch ON and every
    # automation paused, the employee watches NOTHING while Comportamiento paints
    # "Ejecución autónoma activada". Whoever reads the panel believes there is an
    # autonomous employee and there is a chatbot (INC-KOM-49). Pausing routines is
    # legitimate; ANNOUNCING it as active autonomy is not. So the surface now carries
    # the EFFECTIVE truth — the switch AND something to fire — plus the counts that
    # let any UI say "0 rutinas activas" instead of a green light.
    autos_total, autos_enabled = 0, 0
    try:
        from app import automations_service as _asvc
        _items = await _asvc.list_automations()
        autos_total = len(_items)
        autos_enabled = sum(1 for a in _items if a.get("enabled"))
    except Exception:  # pragma: no cover — never break settings on a listing error
        import logging as _lg
        _lg.getLogger(__name__).debug("settings: automations count failed", exc_info=True)
    from app.agent_locale import resolve_agent_language as _resolve_agent_language
    return {
        # Without the operator's choice, the EFFECTIVE one (the Definition's primary language).
        "agent_language": settings.get("agent_language") or await _resolve_agent_language(),
        # v7.117.0 — the employee's default theme (Identity admin): system|light|dark.
        "default_color_mode": (settings.get("default_color_mode") or "system"),
        # A choice the catalogue no longer offers counts as unset (config.modelo_vigente); it is
        # reported apart so the screen can say it was retired instead of silently changing it.
        "agent_model": modelo_vigente(settings.get("agent_model")),
        "agent_model_retirado": ("" if modelo_vigente(settings.get("agent_model"))
                                 else (settings.get("agent_model") or "")),
        "available_models": AVAILABLE_MODELS,
        "default_model": DEFAULT_MODEL,
        # v7.87.0 — session-level reasoning effort ('' = model default). Applied to
        # the sidecar config; changing it takes effect when the employee's runtime
        # refresher restarts opencode (config-only, no per-prompt field).
        "agent_effort": settings.get("agent_effort") or "",
        "available_efforts": ["none", "low", "medium", "high", "xhigh"],
        # 12.23.0 — the SAME levels whoever writes in the chat sees, with the effort each one
        # corresponds to. Operations chose between `none|low|medium|high|xhigh`: the
        # provider's names, in monospace, to decide exactly the same thing the chat calls
        # Rápido/Normal/Profundo. Two vocabularies for one single thing.
        "niveles_de_razonamiento": _razonamiento.catalogo(),
        # What NOT setting any is equivalent to, for this employee's model. "Not set" sounds
        # like "let the model decide" and that does not exist: the effort is always set by the
        # caller, and if nobody sets it the layer below does (opencode: `medium` for the whole
        # gpt-5 family). Saying so avoids the question that came up live — "which is the
        # option where the model chooses?" — whose honest answer is: there is none.
        "razonamiento_sin_fijar": _razonamiento.equivalente_sin_fijar(
            modelo_vigente(settings.get("agent_model")) or DEFAULT_MODEL or ""
        ),
        # How much it WRITES by default. Empty = whatever the model brings and, in that case,
        # the channel rules (by voice, brief).
        "agent_verbosity": settings.get("agent_verbosity") or "",
        "longitudes_de_respuesta": _razonamiento.catalogo_de_longitudes(),
        # What the machine decides on its own, and its two switches.
        "atajos_activos": str(settings.get("atajos_activos") or "1").strip().lower()
        not in ("0", "false", "no", "off"),
        "conversador_elige_nivel": str(
            settings.get("conversador_elige_nivel") or "1").strip().lower()
        not in ("0", "false", "no", "off"),
        # The conversation also when writing: before starting the agent, someone reads and
        # decides (answers the general things, asks for the missing piece of data, or delegates).
        "recepcion_activa": str(settings.get("recepcion_activa") or "1").strip().lower()
        not in ("0", "false", "no", "off"),
        # How many capabilities declare a shortcut today: with none, the shortcuts switch
        # governs nothing and the screen can say so instead of pretending it does something.
        "atajos_declarados": len(_atajos.catalogo()),
        # v7.89.0 — FUNCTIONAL (global) skills attached by the operator, applied
        # in caliente (the sidecar writes them + restarts opencode). Slim shape
        # here (files stripped); the catalog for the picker is a separate call.
        "agent_extra_skills": _slim_extra_skills(settings.get("agent_extra_skills")),
        "triggers_active": bool(triggers_active),
        "triggers_default": bool(trig_default),
        # The operator's INTENT (``triggers_active``) vs the REALITY: autonomous work
        # only happens when the master switch is on AND at least one routine is armed.
        # A surface that shows only the switch announces autonomy that does not exist.
        "automations_total": int(autos_total),
        "automations_enabled": int(autos_enabled),
        "autonomous_work_active": bool(triggers_active) and autos_enabled > 0,
        "autonomy_level": autonomy,
        "autonomy_default": auton_default,
        "autonomy_levels": [
            {"key": l["key"], "label": l["label"], "description": l["description"]}
            for l in _AUTONOMY_LEVELS
        ],
        "self_schedule": bool(self_schedule),
        "self_schedule_default": bool(ss_default),
        "max_open_tasks": int(max_open),
        "max_open_tasks_default": int(mot_default),
        # v7.45.0 — execution concurrency (live, no redeploy).
        "max_concurrent_runs": int(_mt),
        "max_concurrent_runs_default": int(_mmr()),
        "max_concurrent_ceiling": int(_EXEC_CEILING),
        "max_autonomous_concurrent": int(_ma),
        "execution_inflight": _exec_snapshot(),
        # 15.6.0 — how long ONE chat reply / ONE background job may run, in minutes (live).
        "chat_turn_max_minutes": int(_tc),
        "chat_turn_max_minutes_default": int(_mct()),
        "autonomous_run_max_minutes": int(_ta),
        "autonomous_run_max_minutes_default": int(_mar()),
        "time_limit_range": {"min": int(_TL_MIN), "max": int(_TL_MAX)},
        # Business timezone (v7.39.0) — part of the employee's identity. Stored
        # choice wins; the deploy default is read-only context for the selector.
        "business_timezone": (settings.get("business_timezone") or _biz_tz_default()),
        "business_timezone_default": _biz_tz_default(),
        "timezones": _BIZ_TIMEZONES,
    }


def _slim_extra_skills(raw: Any) -> list[dict[str, Any]]:
    """Parse the stored ``agent_extra_skills`` JSON into a slim UI shape
    ``[{id, name, display_name}]`` (files stripped). Tolerant of empty/bad rows."""
    import json as _json
    if not raw:
        return []
    try:
        parsed = _json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    out: list[dict[str, Any]] = []
    for sk in (parsed if isinstance(parsed, list) else []):
        if not isinstance(sk, dict) or not sk.get("name"):
            continue
        out.append({
            "id": sk.get("id"),
            "name": sk.get("name"),
            "display_name": sk.get("display_name") or sk.get("name"),
        })
    return out


@router.get("/available-skills")
async def available_skills(user: TokenPayload = Depends(require_project_access)):
    """FUNCTIONAL (global) skills the operator can attach to this agent. Slim
    list proxied read-only from the platform skill catalog."""
    from app.skills_catalog import list_functional_skills
    return {"skills": await list_functional_skills()}


@router.put("/extra-skills")
async def set_extra_skills(
    body: dict[str, Any] = Body(...),
    user: TokenPayload = Depends(require_admin),
):
    """Attach/detach FUNCTIONAL skills to the autonomous agent EN CALIENTE.

    ADMIN ONLY since v7.146.0: attaching a skill changes what the employee KNOWS how to do
    and how it does it in ALL its runs (also the autonomous ones, also those of other
    people). It was open to any member of the project, which in effect allowed
    reprogramming the employee without governing it.

    Body: ``{"skill_ids": [int, ...]}`` — the FULL desired set (not a delta).
    Each selected skill's file tree is fetched from the platform and embedded in
    the ``agent_extra_skills`` AppSetting so ``/agent-runtime`` stays
    self-contained; the sidecar picks up the change on its next refresh."""
    import json as _json
    from app.skills_catalog import fetch_skill_payload

    ids_raw = body.get("skill_ids")
    if not isinstance(ids_raw, list):
        raise HTTPException(status_code=400, detail="skill_ids must be a list")
    # De-dup, keep ints, cap the number of attached skills.
    ids: list[int] = []
    for v in ids_raw:
        try:
            iv = int(v)
        except (TypeError, ValueError):
            continue
        if iv not in ids:
            ids.append(iv)
    if len(ids) > 25:
        raise HTTPException(status_code=400, detail="too many skills (max 25)")

    payloads: list[dict[str, Any]] = []
    missing: list[int] = []
    for sid in ids:
        payload = await fetch_skill_payload(sid)
        if payload:
            payloads.append(payload)
        else:
            missing.append(sid)

    await _upsert_setting(
        "agent_extra_skills",
        _json.dumps(payloads, ensure_ascii=False),
        user,
    )
    return {"agent_extra_skills": _slim_extra_skills(payloads), "missing": missing}


async def _upsert_setting(key: str, value: str, user: TokenPayload) -> None:
    """Persist one setting. Goes through the SERIALIZED write queue + retry.

    INC-VIG-59: this used to write straight to SQLite via ``app_session``, so it
    had to out-race whoever held the file's single write lock. During an autonomous
    round (8 workers claiming + the channel poller) it lost that race past the
    busy timeout and raised 'database is locked' — a 500 on the one action the
    operator most needs to succeed, ``triggers_active=false``. Two changes make it
    reliable: ``app_write`` puts it in the FIFO queue (it waits its turn instead of
    competing), and ``write_with_retry`` covers the residual so a governance write
    is never simply lost. A single-row upsert holds the slot for microseconds, so
    queueing here costs nothing to anyone else.
    """
    now = datetime.now(timezone.utc)
    values = {
        "key": key,
        "value": value,
        "updated_by": getattr(user, "email", None),
        "updated_at": now,
    }
    stmt = sqlite_insert(AppSetting).values(**values).on_conflict_do_update(
        index_elements=[AppSetting.key],
        set_={k: v for k, v in values.items() if k != "key"},
    )

    async def _do() -> None:
        async with app_write() as s:
            await s.execute(stmt)
            await s.commit()

    await write_with_retry(_do, what=f"el ajuste «{key}»")


# v7.108.0 — the employee's GOVERNANCE keys: they decide what it can do ALONE (raising the
# dial to `act_autonomous` also makes automations be born without approval). They went
# through `require_project_access` like the language or the model, so ANY user of the
# employee could change them — while the write policy and the per-tool permissions did
# require admin. Governance incoherence: now these two require admin.
# v7.146.0 — GOVERNANCE = everything that decides how the employee behaves, how much it can
# spend and with which brain it thinks. Until now it only covered the autonomy and the
# triggers switch, so a ``member`` could change its MODEL (and with it the cost per token
# and the quality of everything it does), its output language, its concurrency, its
# self-scheduling and the BUSINESS time zone — decisions of the employee's manager, not of
# anyone with access to the panel. The only setting that is still each person's is the
# colour theme, which is a visual preference and changes nobody's behaviour.
_GOVERNANCE_KEYS = (
    "autonomy_level", "triggers_active", "agent_model", "agent_effort",
    "agent_verbosity", "atajos_activos", "conversador_elige_nivel",
    "recepcion_activa",
    "agent_language", "business_timezone", "self_schedule", "max_open_tasks",
    "max_concurrent_runs", "max_autonomous_concurrent",
    "chat_turn_max_minutes", "autonomous_run_max_minutes",
)


@router.get("/app-map")
async def app_map(
    q: str = Query("", description="Qué busca la persona; vacío = el mapa entero"),
    user: TokenPayload = Depends(require_project_access),
):
    """The map of THIS application, told for whoever is asking (v7.146.0).

    It is the SAME function the employee uses through chat (`describe_app` →
    `self_knowledge.describe`), with the same filtering by role. It is exposed over REST for
    two reasons that reinforce each other: the help screen can read from the single source
    of the map instead of maintaining its own text, and the continuous validation can check
    against the DEPLOYED employee that the map is its app's —including its domain screens,
    which come from ITS manifest— without spending a token.

    Read-only, and the role is set by the employee's DIRECTORY (not the platform superuser,
    INC-AGO-39): a member must not see here, listed and explained, the screens they cannot
    open.
    """
    from app.mcp_actor import Actor
    from app.users_directory import can_approve as _can_approve, is_admin as _is_admin
    from app import self_knowledge

    email = (user.email or "").strip()
    actor = Actor(
        email=email,
        surface="chat",
        role="admin" if await _is_admin(email) else "member",
        can_approve=await _can_approve(email),
    )
    out = self_knowledge.describe(q or "", actor=actor)
    out["menu"] = [{"ruta": r, "etiqueta_i18n": k}
                   for r, k in self_knowledge.menu_routes().items()]
    return out


@router.patch("/settings")
async def patch_settings(
    body: dict[str, Any] = Body(...),
    user: TokenPayload = Depends(require_project_access),
):
    from app.config import AVAILABLE_MODELS
    from app import razonamiento as _razonamiento

    # The role is governed by the EMPLOYEE's DIRECTORY (same as `require_admin`), not by the
    # platform superuser: a WAI admin can be a plain member here (INC-AGO-39).
    gov = [k for k in _GOVERNANCE_KEYS if k in body]
    if gov:
        from app.users_directory import is_admin as _is_employee_admin
        if not await _is_employee_admin(user.email):
            raise HTTPException(
                status_code=403,
                detail=f"Solo un administrador del empleado puede cambiar {', '.join(gov)}: "
                       "definen qué hace por su cuenta.",
            )
    lang = body.get("agent_language")
    if lang is not None:
        lang = str(lang).strip().lower()
        if lang not in AGENT_LANGUAGES:
            raise HTTPException(
                status_code=400,
                detail=f"agent_language must be one of {AGENT_LANGUAGES}",
            )
        await _upsert_setting("agent_language", lang, user)

    if "agent_model" in body:
        model = str(body.get("agent_model") or "").strip()
        # Empty string clears the override (falls back to deploy default).
        if model and AVAILABLE_MODELS and model not in AVAILABLE_MODELS:
            raise HTTPException(
                status_code=400,
                detail=f"agent_model must be one of {AVAILABLE_MODELS}",
            )
        await _upsert_setting("agent_model", model, user)

    if "agent_effort" in body:
        # Session-level reasoning effort ('' = model default). Validated against
        # the fixed set the reasoning models accept.
        effort = str(body.get("agent_effort") or "").strip().lower()
        _VALID_EFFORTS = ("none", "low", "medium", "high", "xhigh")
        if effort and effort not in _VALID_EFFORTS:
            raise HTTPException(
                status_code=400,
                detail=f"agent_effort must be one of {list(_VALID_EFFORTS)} (or empty)",
            )
        await _upsert_setting("agent_effort", effort, user)

    if "agent_verbosity" in body:
        # How much it WRITES by default ('' = whatever the model brings, and then the channel
        # decides: by voice, brief). It is the employee's and not each message's: the register
        # it writes in is part of its character, like the language.
        largo = str(body.get("agent_verbosity") or "").strip().lower()
        _VALID_LARGOS = tuple(x["id"] for x in _razonamiento.catalogo_de_longitudes())
        if largo and largo not in _VALID_LARGOS:
            raise HTTPException(
                status_code=400,
                detail=f"agent_verbosity must be one of {list(_VALID_LARGOS)} (or empty)",
            )
        await _upsert_setting("agent_verbosity", largo, user)

    # The two switches of what the machine decides on its own. On by default and switchable
    # off without deploying anything: what they replace is a blind constant, but if one day
    # odd answers come out it has to be possible to cut it on the spot.
    for clave in ("atajos_activos", "conversador_elige_nivel", "recepcion_activa"):
        if clave in body:
            val = body.get(clave)
            encendido = str(val).strip().lower() not in ("0", "false", "no", "off")
            await _upsert_setting(clave, "1" if encendido else "0", user)

    if "default_color_mode" in body:
        # v7.117.0 — the employee's default theme (Identity). system|light|dark.
        mode = str(body.get("default_color_mode") or "").strip().lower()
        _VALID_MODES = ("system", "light", "dark")
        if mode and mode not in _VALID_MODES:
            raise HTTPException(
                status_code=400,
                detail=f"default_color_mode must be one of {list(_VALID_MODES)}",
            )
        await _upsert_setting("default_color_mode", mode or "system", user)

    if "triggers_active" in body:
        from app.triggers_state import SETTING_KEY as _TRIG_KEY
        active = bool(body.get("triggers_active"))
        await _upsert_setting(_TRIG_KEY, "1" if active else "0", user)

    if "autonomy_level" in body:
        from app.autonomy_state import SETTING_KEY as _AUTON_KEY, _BY_KEY
        level = str(body.get("autonomy_level") or "").strip().lower()
        if level not in _BY_KEY:
            raise HTTPException(
                status_code=400,
                detail=f"autonomy_level must be one of {sorted(_BY_KEY)}",
            )
        await _upsert_setting(_AUTON_KEY, level, user)
        # 7.182.0 — same as with the write policy: what the employee had learned about its
        # own posture stops being valid as soon as you change it.
        from app.agent_memory_utils import expire_config_state_memories
        await expire_config_state_memories("cambio de nivel de autonomía")

    if "business_timezone" in body:
        # v7.39.0 — the business wall clock, editable from the Identity page.
        # Empty string clears the override (falls back to the deploy default).
        from app.business_timezone import (
            SETTING_KEY as _BIZ_TZ_KEY, is_valid_tz as _valid_tz, refresh_cache as _refresh_tz,
        )
        tz = str(body.get("business_timezone") or "").strip()
        if tz and not _valid_tz(tz):
            raise HTTPException(status_code=400, detail=f"business_timezone {tz!r} is not a valid IANA name")
        await _upsert_setting(_BIZ_TZ_KEY, tz, user)
        await _refresh_tz()  # warm the in-memory cache so timeutil sees it now

    if "self_schedule" in body:
        from app.scheduling_policy import SELF_SCHEDULE_KEY as _SS_KEY
        await _upsert_setting(_SS_KEY, "1" if bool(body.get("self_schedule")) else "0", user)

    if "max_open_tasks" in body:
        from app.scheduling_policy import MAX_OPEN_TASKS_KEY as _MOT_KEY
        try:
            n = max(0, int(body.get("max_open_tasks")))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="max_open_tasks must be a non-negative integer (0 = no cap)")
        await _upsert_setting(_MOT_KEY, str(n), user)

    # v7.45.0 — execution concurrency (both LIVE; invalidate the cache so the
    # executor's next claim sees the new value at once).
    if "max_concurrent_runs" in body or "max_autonomous_concurrent" in body:
        from app.execution_limits import (
            MAX_RUNS_KEY as _MR_KEY, MAX_AUTO_KEY as _MA_KEY,
            CEILING as _EXEC_CEILING, invalidate_cache as _inval_limits,
        )
        if "max_concurrent_runs" in body:
            try:
                n = int(body.get("max_concurrent_runs"))
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"max_concurrent_runs must be an integer in [1, {_EXEC_CEILING}]")
            await _upsert_setting(_MR_KEY, str(max(1, min(_EXEC_CEILING, n))), user)
        if "max_autonomous_concurrent" in body:
            try:
                n = int(body.get("max_autonomous_concurrent"))
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"max_autonomous_concurrent must be an integer in [1, {_EXEC_CEILING}]")
            # Clamp to [1, ceiling]; effective_limits further clamps to ≤ total.
            await _upsert_setting(_MA_KEY, str(max(1, min(_EXEC_CEILING, n))), user)
        _inval_limits()

    # 15.6.0 — the turn TIME ceilings (minutes). Clamped to the range like the concurrency,
    # and the cache dropped so the NEXT message already runs with the new ceiling.
    if "chat_turn_max_minutes" in body or "autonomous_run_max_minutes" in body:
        from app.execution_limits import (
            CHAT_TURN_KEY as _CT_KEY, AUTONOMOUS_RUN_KEY as _AR_KEY,
            TIME_LIMIT_MIN as _TL_MIN, TIME_LIMIT_MAX as _TL_MAX,
            invalidate_cache as _inval_times,
        )
        for _clave, _fila in (("chat_turn_max_minutes", _CT_KEY),
                              ("autonomous_run_max_minutes", _AR_KEY)):
            if _clave not in body:
                continue
            try:
                n = int(body.get(_clave))
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail=(
                    f"{_clave} must be an integer number of minutes in [{_TL_MIN}, {_TL_MAX}]"))
            await _upsert_setting(_fila, str(max(_TL_MIN, min(_TL_MAX, n))), user)
        _inval_times()

    return await get_settings(user)


# ── Persona ("AI employee" identity) ────────────────────────────────
# Operator-editable identity of the AI employee, persisted in the
# ``agent_persona`` table. The manifest persona is the initial value;
# edits here override it field-by-field without a redeploy.


def _persona_payload(agent_app: dict, row: Optional["AgentPersona"]) -> dict:
    """Build the GET/PATCH response: the resolved persona shown
    everywhere, the manifest defaults (so the editor can show what a
    field falls back to), the raw override and audit metadata."""
    manifest_default = _persona_block(agent_app)
    override = _db_persona_to_dict(row) if row is not None else None
    return {
        "resolved":         _merge_persona(manifest_default, override),
        "manifest_default": manifest_default,
        "override":         override,
        "overridden":       row is not None,
        "updated_by":       getattr(row, "updated_by", None) if row else None,
        "updated_at":       _iso_utc(getattr(row, "updated_at", None)) if row else None,
    }


@router.get("/persona")
async def get_persona(
    user: TokenPayload = Depends(require_project_access),
    session=Depends(get_app_session),
):
    cfg = _load_wai_app()
    agent_app = cfg.get("agent_app", {})
    row = (await session.execute(
        select(AgentPersona).where(AgentPersona.id == "default")
    )).scalar_one_or_none()
    return _persona_payload(agent_app, row)


def _coerce_persona_copy_json(value: Any, max_len: int) -> Optional[str]:
    """Sanitise a persona copy node (str | {lang: str}) and serialise to
    JSON text for storage, or ``None`` to clear the override."""
    coerced = _persona_str_or_locale(value, max_len)
    return json.dumps(coerced, ensure_ascii=False) if coerced else None


@router.patch("/persona")
async def patch_persona(
    body: dict[str, Any] = Body(...),
    user: TokenPayload = Depends(require_admin),
    session=Depends(get_app_session),
):
    """Upsert the AI-employee identity override.

    Editable fields (all optional — omit a field to leave it unchanged,
    send ``null``/empty to clear it back to the manifest default):

      * ``name``      (str)              — friendly first name ("Fina")
      * ``role``      (str | {lang})     — one-line role
      * ``greeting``  (str | {lang})     — first-person chat greeting
      * ``personality.tone``  (str|{lang})
      * ``personality.bio``   (str|{lang})  — short first-person bio
      * ``personality.traits``(list)        — adjectives (max 6)
      * ``avatar.emoji`` (str)              — assignable "icono"
      * ``avatar.color`` (str)              — monogram gradient seed
      * ``avatar.image_url`` (str)          — mascot path/URL/data-URI
    """
    cfg = _load_wai_app()
    agent_app = cfg.get("agent_app", {})

    row = (await session.execute(
        select(AgentPersona).where(AgentPersona.id == "default")
    )).scalar_one_or_none()
    if row is None:
        row = AgentPersona(id="default")
        session.add(row)

    avatar = body.get("avatar") if isinstance(body.get("avatar"), dict) else None
    personality = (
        body.get("personality") if isinstance(body.get("personality"), dict) else None
    )

    if "name" in body:
        name = body.get("name")
        row.name = name.strip()[:80] if isinstance(name, str) and name.strip() else None
    if "role" in body:
        row.role_json = _coerce_persona_copy_json(body.get("role"), 200)
    if "greeting" in body:
        row.greeting_json = _coerce_persona_copy_json(body.get("greeting"), 400)

    if personality is not None:
        if "tone" in personality:
            row.tone_json = _coerce_persona_copy_json(personality.get("tone"), 200)
        if "bio" in personality:
            row.bio_json = _coerce_persona_copy_json(personality.get("bio"), 1000)
        if "traits" in personality:
            traits_raw = personality.get("traits")
            traits: list[Any] = []
            if isinstance(traits_raw, list):
                for item in traits_raw[:_PERSONA_MAX_TRAITS]:
                    coerced = _persona_str_or_locale(item, 40)
                    if coerced:
                        traits.append(coerced)
            row.traits_json = (
                json.dumps(traits, ensure_ascii=False) if traits else None
            )

    if avatar is not None:
        if "emoji" in avatar:
            emoji = avatar.get("emoji")
            row.avatar_emoji = (
                emoji.strip()[:8] if isinstance(emoji, str) and emoji.strip() else None
            )
        if "color" in avatar:
            color = avatar.get("color")
            row.avatar_color = (
                color.strip()[:32] if isinstance(color, str) and color.strip() else None
            )
        if "image_url" in avatar:
            img = avatar.get("image_url")
            row.avatar_image_url = (
                img.strip()[:_PERSONA_AVATAR_URL_MAX]
                if isinstance(img, str) and img.strip() else None
            )

    row.updated_by = getattr(user, "email", None)
    row.updated_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(row)
    return _persona_payload(agent_app, row)


@router.delete("/persona")
async def reset_persona(
    user: TokenPayload = Depends(require_admin),
    session=Depends(get_app_session),
):
    """Clear the override so the manifest persona reasserts itself.

    ADMIN ONLY since v7.146.0, same as editing it (``PATCH /persona``): the employee's
    identity is the same for everyone, so deleting it is a governance change. That EDITING
    required admin and DELETING did not was an oversight, not a design.
    """
    cfg = _load_wai_app()
    agent_app = cfg.get("agent_app", {})
    row = (await session.execute(
        select(AgentPersona).where(AgentPersona.id == "default")
    )).scalar_one_or_none()
    if row is not None:
        await session.delete(row)
        await session.commit()
    return _persona_payload(agent_app, None)


@router.patch("/actions/{event_type}")
async def update_action_description(
    event_type: str,
    body: dict[str, Any] = Body(...),
    # SECURITY: admin only. It changes a text ALL users see on the operations cards; writable
    # by a member it was a way to slip text to the others.
    user: TokenPayload = Depends(require_admin),
):
    """Operator-edited business description for an action card.

    Body shape (R-OPS-1)::

      { "business_description": { "es": "...", "en": "..." } }

    The manifest's ``event_type_specs[]`` stays the technical
    contract (label, reads, writes, ui_effects, structured_output);
    only the human-facing business sentence is overridable here so
    the operations card can be tuned without a redeploy.
    """
    cfg = _load_wai_app()
    agent_app = cfg.get("agent_app", {})
    declared = next(
        (s for s in (agent_app.get("event_type_specs") or [])
         if s.get("type") == event_type),
        None,
    )
    if declared is None:
        raise HTTPException(
            status_code=404,
            detail=f"unknown event_type {event_type!r}",
        )
    business_description = body.get("business_description")
    coerced = _coerce_locale_dict(business_description) if business_description else None
    business_description_json = (
        json.dumps(coerced, ensure_ascii=False) if coerced else None
    )
    now = datetime.now(timezone.utc)
    values = {
        "event_type": event_type,
        "business_description_json": business_description_json,
        "updated_by": getattr(user, "email", None),
        "updated_at": now,
    }
    set_ = {k: v for k, v in values.items() if k != "event_type"}
    stmt = sqlite_insert(ActionOverride).values(**values).on_conflict_do_update(
        index_elements=[ActionOverride.event_type], set_=set_,
    )
    async with app_write() as s:
        await s.execute(stmt)
        await s.commit()
    return {"ok": True, "event_type": event_type, **set_}


@router.post("/watchers/{name}/fire")
async def fire_watcher(
    name: str,
    body: dict[str, Any] = Body(default_factory=dict),
    user: TokenPayload = Depends(require_admin),
):
    """Manually enqueue the watcher's event right now (R-OPS-4).

    ADMIN ONLY since v7.146.0: firing a watcher by hand SPENDS a run of the employee (real
    credits) and can launch reactions to the outside. Editing the watcher already required
    admin; firing it, which is what costs money, did not — and so any member could consume
    the month's budget at the push of a button.

    Why this endpoint exists
    ------------------------
    The cron / row_count_delta watchers tick on their own cadence
    (``interval_sec`` from the manifest, defaulting to **86400** for
    daily-rollup style alerts in the wai-agent-app-builder
    examples). Operators legitimately need to:

      * **Smoke-test a fresh deployment** without waiting for the
        next aligned bucket — especially when ``interval_sec`` is a
        day and the agent has just been built.
      * **Re-run a watcher after fixing a bug** in the agent prompt
        or the action handler — the previous event already
        completed so the natural fingerprint dedup would NO-OP a
        retry.
      * **Trigger a demo / training run** on demand for a
        stakeholder review.

    Idempotency contract
    --------------------
    Each manual fire mints a unique fingerprint by mixing the
    canonical watcher fingerprint with the calling user's email
    and the current monotonic millisecond stamp; two clicks within
    the same millisecond will dedup but consecutive clicks always
    produce distinct events. The event carries
    ``payload.trigger="manual"`` so dashboards and audit views can
    filter manual vs. scheduled traffic. Manual events go through
    the SAME event machine (PENDING → CLAIMED → IN_PROGRESS →
    COMPLETED) as automatic ones — they're not a separate code
    path, which keeps observability uniform.

    Returns the created event id so the SPA can deep-link to
    ``/events/{id}`` for live progress.
    """
    declared = next(
        (w for w in WATCHERS or [] if w.get("name") == name),
        None,
    )
    if declared is None:
        raise HTTPException(status_code=404, detail=f"unknown watcher {name!r}")

    async with app_write() as s:
        # R-OPS-4 (v7.4): a paused watcher used to 409 here on the
        # theory that silently dispatching a manual fire on a
        # disabled schedule would surprise the next operator. In
        # practice this blocked the most common smoke-test workflow
        # — "pause the noisy daily watcher, fire one manually, look
        # at the audit trail" — and forced operators to enable +
        # immediately re-disable, which is racy: the executor might
        # tick a scheduled event in the window. Manual fires are
        # explicit, audit-logged (``payload.trigger='manual'``,
        # ``payload.actor=<user>``) and unique-fingerprinted, so the
        # paused-but-fireable contract is safe. The button stays in
        # the UI for paused watchers; only the autonomous scheduler
        # respects ``enabled=False``.
        event_type = declared.get("event_type") or (
            "CRON" if declared.get("type") == "cron" else f"MANUAL::{name}"
        )
        now = datetime.now(timezone.utc)
        actor = getattr(user, "email", None) or "unknown"
        # ms-resolution suffix is enough to differentiate two
        # clicks; pure monotonic time without dates would survive
        # a clock skew but isn't human-debuggable in the events
        # table, so we keep an ISO stamp.
        unique_seed = f"manual::{actor}::{now.isoformat()}::{id(body)}"
        payload = {
            "tick":     now.isoformat(),
            "trigger":  "manual",
            "actor":    actor,
            "watcher":  name,
            "notes":    (body or {}).get("notes") or None,
            "_unique":  unique_seed,
        }
        fp = hashlib.sha256(
            f"{event_type}::{name}::{unique_seed}".encode("utf-8")
        ).hexdigest()
        stmt = sqlite_insert(Event).values(
            fingerprint=fp,
            type=event_type,
            source=name,
            payload_json=json.dumps(payload, default=str),
            status="PENDING",
            attempts=0,
            detected_at=now,
        ).on_conflict_do_nothing(index_elements=[Event.fingerprint])
        result = await s.execute(stmt)
        await s.commit()

        if not result.rowcount:
            # The fingerprint already exists — shouldn't happen
            # because we mixed in a ms stamp, but defensively
            # report it instead of returning a misleading "ok".
            existing = (await s.execute(
                select(Event.id, Event.status)
                .where(Event.fingerprint == fp)
            )).first()
            return {
                "ok": False,
                "duplicate": True,
                "event_id": existing[0] if existing else None,
                "status":   existing[1] if existing else None,
                "name":     name,
            }

        created = (await s.execute(
            select(Event.id, Event.detected_at)
            .where(Event.fingerprint == fp)
        )).first()
        return {
            "ok":         True,
            "name":       name,
            "event_id":   created[0] if created else None,
            "event_type": event_type,
            "status":     "PENDING",
            "detected_at": _iso_utc(created[1] if created else now),
            "actor":      actor,
        }


def _percentiles(values: list[float], ps: list[int]) -> tuple[int, int]:
    if not values:
        return 0, 0
    s = sorted(values)
    out = []
    for p in ps:
        k = min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1))))
        out.append(int(s[k]))
    return out[0], out[1]
