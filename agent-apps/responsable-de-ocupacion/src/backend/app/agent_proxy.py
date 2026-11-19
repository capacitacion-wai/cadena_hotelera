"""Thin client for the WAI OpenCode Simplified Agent API (``/v1/*``).

COPIED VERBATIM by ``wai-agent-app-builder`` into every generated
agent-app at ``src/backend/app/agent_proxy.py``. Do NOT rewrite or
rename — the routes, executor and tests all depend on the exact
public surface below.

Architecture (post v7 simple-API):
==================================
The agent-app pod NEVER speaks the raw OpenCode HTTP API anymore. It
talks to the **sidecar** that ``opencode-server`` exposes on port
``4097`` (override with ``OPENCODE_SIMPLE_URL``). The sidecar handles
all the SSE plumbing, structured-response splitting, identity
binding (autonomous vs interactive) and permission grants — this
client just makes 1–2 HTTP calls per turn::

    POST /v1/sessions                       → {"session_id": "..."}
    POST /v1/sessions/{id}/messages         → {"text", "structured", ...}
    GET  /v1/sessions/{id}/stream?prompt=…  → SSE stream
    DELETE /v1/sessions/{id}                → 204

Authentication is HTTP Basic with the literal username ``opencode``
and the password held in ``AGENT_SHARED_TOKEN`` (same secret the old
proxy used). The token is reused for every call.

The dual-identity contract:
* **Autonomous runs** (watchers / event-driven) create sessions with
  ``role="autonomous"`` and let the simple API append the
  STRUCTURED_RESPONSE contract; the returned ``structured`` dict is
  persisted on ``agent_runs.structured_json``.
* **Interactive chats** create sessions with ``role="interactive"``
  and pass the logged-in user's email so the OpenCode
  wai-impersonation plugin can scope every wai-* MCP tool call to
  that user. No structured tail is requested or expected.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, AsyncIterator, Optional

import httpx

logger = logging.getLogger(__name__)


def _resolve_simple_base() -> str:
    """Order of preference for the simple-API base URL:

    1. ``OPENCODE_SIMPLE_URL`` — set by the deployer at deploy time.
    2. ``AGENT_OPENCODE_URL`` — legacy var, still pointing at the
       native OpenCode HTTP API for backwards compatibility with
       pre-v7 deployments. We coerce ``:4096`` → ``:4097`` so an
       operator who hasn't updated the deployment manifest yet still
       gets a sane default.
    """
    explicit = os.environ.get("OPENCODE_SIMPLE_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    legacy = os.environ.get("AGENT_OPENCODE_URL", "").strip().rstrip("/")
    if legacy.endswith(":4096"):
        return legacy[: -len(":4096")] + ":4097"
    return legacy


SIMPLE_BASE_URL = _resolve_simple_base()
AGENT_SHARED_TOKEN = os.environ.get("AGENT_SHARED_TOKEN", "")
# Email of the WAI user the agent impersonates on autonomous runs.
# Set by the deployer from agent_apps.autonomous_user_email.
AUTONOMOUS_USER_EMAIL = (
    os.environ.get("AGENT_AUTONOMOUS_USER_EMAIL")
    or os.environ.get("OPENCODE_AUTONOMOUS_USER_EMAIL")
    or ""
).strip().lower()
# Legacy aliases kept for code that still references them (tests, etc.)
AGENT_OPENCODE_URL = SIMPLE_BASE_URL
AGENT_NAME = (
    os.environ.get("AGENT_SLUG")
    or os.environ.get("AGENT_NAME")
    or "build"
)


def _resolve_model(role: str) -> str:
    """Pick the single model id the sidecar should bind for ``role``.

    R-MODEL-1 (post v7.1): the simple-API ``/v1/sessions`` endpoint
    expects ``model`` to be a *single* model id (e.g.
    ``"openai/gpt-5.4-mini"``). The legacy ``OPENCODE_DEFAULT_MODEL``
    env var has always been documented as a single model, but the
    deployer historically injected the WHOLE comma-separated catalog
    (``openai/gpt-5.4-mini,openai/gpt-5.4,openai/gpt-5.5``). When that
    comma-string arrives at the sidecar, the model layer silently
    fails the lookup and falls back to its own hard-coded default
    (today ``gpt-5.3-chat-latest``), which is NOT what the operator
    intended. Symptom on the Operations UI: "Modelo:
    gpt-5.3-chat-latest" for every autonomous run even though
    ``OPENCODE_DEFAULT_MODEL`` lists 5.4 / 5.5 family models.

    Resolution order:

      1. ``AGENT_AUTONOMOUS_MODEL`` (when role=="autonomous") —
         dedicated env var the deployer wires from
         ``agent_versions.model_config.autonomous_model``. Takes
         absolute priority so an operator can pin the autonomous
         model independently of the interactive one.
      2. ``AGENT_INTERACTIVE_MODEL`` (when role=="interactive") —
         analogous override for chats.
      3. First non-empty entry of ``OPENCODE_DEFAULT_MODEL`` (split
         on commas, whitespace trimmed). Backwards-compatible with
         every old deployment.
      4. Empty string — the sidecar then falls back to its own
         configured default. We let that case through silently so a
         partial config doesn't break create_session.

    Always returns a stripped string. Never raises.
    """
    if role == "autonomous":
        explicit = os.environ.get("AGENT_AUTONOMOUS_MODEL", "").strip()
        if explicit:
            return explicit
    elif role == "interactive":
        explicit = os.environ.get("AGENT_INTERACTIVE_MODEL", "").strip()
        if explicit:
            return explicit
    raw = os.environ.get("OPENCODE_DEFAULT_MODEL", "").strip()
    if not raw:
        return ""
    # Take the first comma-separated entry; this is the documented
    # contract since v7.1 but historical deployments passed the full
    # catalogue, so be defensive here instead of in every call site.
    for candidate in raw.split(","):
        candidate = candidate.strip()
        if candidate:
            return candidate
    return ""


# Module-level cache for the common case (chat path); the autonomous
# path goes through _resolve_model("autonomous") on every create so
# operator env edits + ``kubectl set env`` take effect without restart.
AGENT_DEFAULT_MODEL = _resolve_model("interactive")

def _float_env(name: str, default: float) -> float:
    """Positive float from the environment, else ``default``. Never raises."""
    try:
        v = float(os.environ.get(name, "").strip())
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


DEFAULT_TIMEOUT = 30.0
SESSION_TIMEOUT = 600.0

# R-TIMEOUT-1 (v6.29.0): how long an AUTONOMOUS run may take before the
# sidecar returns 504 and we surface "simple API gateway timeout". The
# old hard-coded 300 s was too tight for heavy deliverables — e.g. a
# watcher that builds and exports a PPTX/PDF presentation routinely
# needs 4–8 minutes — so those runs failed all retries with a gateway
# timeout while lighter runs (a stock check) squeaked in at ~240 s. The
# sidecar honours whatever ``timeout_s`` we pass (no server-side cap),
# so the budget lives here and is operator-tunable without a rebuild via
# ``AGENT_AUTONOMOUS_TIMEOUT_SEC``. Default 900 s (15 min).
# 15.6.0 — this env is now only the DEPLOY-CONFIG layer of the live knob
# ``autonomous_run_max_minutes`` (``app/execution_limits.py``): the operator moves it in
# Operations without a redeploy, and ``send_autonomous`` reads the effective value per run.
AUTONOMOUS_TIMEOUT_SEC = _float_env("AGENT_AUTONOMOUS_TIMEOUT_SEC", 900.0)

_BASIC_AUTH = httpx.BasicAuth("opencode", AGENT_SHARED_TOKEN) if AGENT_SHARED_TOKEN else None

# Raw NATIVE OpenCode base (:4096). The sidecar (:4097) only fronts prompts;
# the on-demand compaction endpoint is native-only, so it is derived from the
# sidecar base by swapping the port (override with AGENT_OPENCODE_NATIVE_URL).
def _resolve_native_base() -> str:
    explicit = os.environ.get("AGENT_OPENCODE_NATIVE_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    if SIMPLE_BASE_URL.endswith(":4097"):
        return SIMPLE_BASE_URL[: -len(":4097")] + ":4096"
    return SIMPLE_BASE_URL


NATIVE_BASE_URL = _resolve_native_base()
# Compaction is a full LLM turn over the WHOLE context (it is used precisely
# when the context is huge) — far beyond the default control-plane timeout.
COMPACT_TIMEOUT = 300.0


class RecoverableAgentError(RuntimeError):
    """Transient errors (timeout, 5xx, simple-API unreachable).

    The autonomous executor catches these and schedules a retry."""


class FatalAgentError(RuntimeError):
    """Non-transient errors (bad request, malformed agent reply).

    Indicates a programming or configuration bug rather than a
    transient failure; do NOT retry blindly."""


class AgentProxy:
    """Async client for the WAI Simplified Agent API.

    Instantiate ONCE in the FastAPI lifespan (``set_agent_proxy``)
    and reuse across requests.
    """

    def __init__(self, base_url: Optional[str] = None) -> None:
        self.base_url = (base_url or SIMPLE_BASE_URL).rstrip("/")
        if not self.base_url:
            raise RuntimeError(
                "OPENCODE_SIMPLE_URL (or legacy AGENT_OPENCODE_URL) is not "
                "set — refusing to create an unbound AgentProxy."
            )
        if _BASIC_AUTH is None:
            raise RuntimeError(
                "AGENT_SHARED_TOKEN is empty — refusing to create an "
                "unauthenticated AgentProxy."
            )
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=DEFAULT_TIMEOUT,
            auth=_BASIC_AUTH,
        )

    async def close(self) -> None:
        await self._client.aclose()

    # ── Health ─────────────────────────────────────────────────────
    async def check_health(self) -> bool:
        """Return True iff the sidecar reports the native server is healthy."""
        try:
            r = await self._client.get("/v1/health", timeout=5.0)
            if r.status_code != 200:
                return False
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            return bool(data.get("ok"))
        except Exception:
            return False

    # ── MCP discovery ──────────────────────────────────────────────
    async def list_mcp_tools(self) -> list[dict]:
        """Return the live catalogue of MCP servers + their tools as the
        sidecar reports them (``GET /v1/mcp/tools``). Used by the
        Operations → Conectores page to render a per-tool permission
        editor with the EXACT tool names the runtime enforces. Returns an
        empty list (never raises) when the sidecar is unavailable so the
        UI can fall back gracefully."""
        try:
            r = await self._client.get("/v1/mcp/tools", timeout=15.0)
        except Exception as exc:  # noqa: BLE001 — discovery is best-effort
            logger.warning("list_mcp_tools transport error: %s", exc)
            return []
        if r.status_code >= 400:
            logger.warning("list_mcp_tools %s: %s", r.status_code, r.text[:200])
            return []
        try:
            data = r.json()
        except json.JSONDecodeError:
            return []
        servers = data.get("servers") if isinstance(data, dict) else None
        return servers if isinstance(servers, list) else []

    async def list_skills(self) -> dict:
        """The skills the agent has LOADED, as its pod sees them
        (``GET /v1/skills``).

        It tells "there are none" apart from "I don't know": if the sidecar does not
        answer it returns ``{"available": False}``, never an empty list. The screen
        that showed this took the list from `wai-app.json` —a snapshot of the day the
        application was generated— and that is why it showed old versions, did not
        see a skill added later and kept showing the removed ones. Stale data without
        a warning is worse than a gap."""
        try:
            # 3 s, not 15. It is an informative block of the manifest: waiting fifteen
            # seconds for a sidecar that does not answer brings no better data, it only
            # holds the request —and its database connection with it— while the pool drains.
            r = await self._client.get("/v1/skills", timeout=3.0)
        except Exception as exc:  # noqa: BLE001 — informative, never critical
            logger.warning("list_skills transport error: %s", exc)
            return {"available": False, "reason": "sidecar unreachable", "skills": []}
        if r.status_code >= 400:
            logger.warning("list_skills %s: %s", r.status_code, r.text[:200])
            return {"available": False, "reason": f"HTTP {r.status_code}", "skills": []}
        try:
            data = r.json()
        except json.JSONDecodeError:
            return {"available": False, "reason": "invalid response", "skills": []}
        return data if isinstance(data, dict) else {
            "available": False, "reason": "invalid response", "skills": []}

    # ── Sessions ───────────────────────────────────────────────────
    async def create_session(
        self,
        *,
        user_email: Optional[str] = None,
        role: str = "autonomous",
        agent: Optional[str] = None,
        model: Optional[str] = None,
        tool_permissions: Optional[dict[str, str]] = None,
        write_grant: Optional[str] = None,
        permission: Optional[list[dict[str, str]]] = None,
    ) -> str:
        """Create an OpenCode session bound to a user identity.

        Args:
            user_email: Email of the user this session represents.
                For ``role="autonomous"`` defaults to
                ``AUTONOMOUS_USER_EMAIL``; for ``role="interactive"``
                must be provided by the caller (typically the logged-in
                user's email).
            role: ``"autonomous"`` for background runs (structured tail
                expected), ``"interactive"`` for chats.
            agent: Optional override for the OpenCode agent slug. The
                sidecar passes it to ``POST /session/{id}/prompt_async``
                on every message.
            model: Optional explicit model id. When set (and non-empty)
                it OVERRIDES the env-derived default — this is how the
                operator's Operations model selection (app_settings.
                agent_model) is applied to every call. When empty/None we
                fall back to ``_resolve_model(role)``.
            tool_permissions: Optional per-tool permission overrides
                (``{tool_name: 'allow'|'ask'|'deny'}``) the operator chose
                on the Operations → Conectores page (app_settings.
                mcp_tool_permissions). Forwarded to the sidecar, persisted
                on the session ACL and enforced by the wai-impersonation
                plugin: 'deny' blocks the tool, 'ask' blocks it on
                autonomous runs. Empty/None ⇒ no per-tool restriction.
        """
        if role not in ("autonomous", "interactive"):
            raise ValueError(f"invalid role {role!r}; expected autonomous|interactive")
        email = (user_email or "").strip().lower()
        if not email and role == "autonomous":
            email = AUTONOMOUS_USER_EMAIL
        if not email:
            raise FatalAgentError(
                f"cannot create a {role} session without user_email — "
                f"set AGENT_AUTONOMOUS_USER_EMAIL or pass user_email explicitly"
            )

        body: dict[str, Any] = {
            "user": {"email": email, "role": role},
        }
        if agent:
            body["agent"] = agent
        elif AGENT_NAME and AGENT_NAME != "build":
            body["agent"] = AGENT_NAME
        # R-MODEL-1: resolve a SINGLE model id (the sidecar fails the
        # lookup on a comma-separated catalogue). An explicit ``model``
        # (the operator's Operations selection) wins; otherwise see
        # _resolve_model for the precedence rules and the historical bug
        # it cures.
        model_id = (model or "").strip() or _resolve_model(role)
        if model_id:
            body["model"] = model_id
        if tool_permissions:
            body["tool_permissions"] = tool_permissions
        # v7.35.0 — a run-scoped write grant for an approved reaction; the sidecar
        # stores it on the session ACL and the wai-impersonation plugin injects it
        # into connector calls so the proxy coalesces the write approval.
        if write_grant:
            body["write_grant"] = write_grant
        # 15.8.0 — opencode's own per-session ruleset (a CONTROLLED test chat, `prueba_controlada`).
        if permission:
            body["permission"] = permission

        try:
            r = await self._client.post("/v1/sessions", json=body)
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise RecoverableAgentError(f"cannot reach simple API: {exc}") from exc
        if r.status_code >= 500:
            raise RecoverableAgentError(f"/v1/sessions 5xx: {r.status_code}")
        if r.status_code == 404:
            raise FatalAgentError(
                f"user {email!r} not found on this tenant — refusing to "
                f"create a session it cannot run as"
            )
        if r.status_code >= 400:
            raise FatalAgentError(
                f"/v1/sessions {r.status_code}: {r.text[:300]!r}"
            )
        try:
            data = r.json()
        except json.JSONDecodeError as exc:
            raise RecoverableAgentError(
                f"/v1/sessions returned non-JSON ({exc})"
            )
        sid = data.get("session_id")
        if not sid:
            raise RecoverableAgentError(
                f"/v1/sessions returned no session_id: {data}"
            )
        if permission and data.get("permission_rules") != len(permission):
            # An older agent pod ignores the ruleset: that session would never ask. Fail closed.
            from app.prueba_controlada import NoDisponible
            await self.delete_session(str(sid))      # never raises
            raise NoDisponible("the agent pod does not apply a session's permission rules")
        return str(sid)

    async def answer_permission(self, session_id: str, permission_id: str, permitir: bool, motivo: str) -> None:
        """The PERSON's answer to a pending ask of a controlled test session (15.8.0): run it once, or reject it
        with its reason (the call fails with it and the agent goes on)."""
        cuerpo = ({"response": "once"} if permitir else
                  {"response": "reject", "message": motivo or "The person did not allow this change."})
        r = await self._client.post(f"/v1/sessions/{session_id}/permissions/{permission_id}", json=cuerpo)
        if r.status_code >= 400:
            raise FatalAgentError(f"permission answer {r.status_code}: {r.text[:200]!r}")

    async def delete_session(self, session_id: str) -> None:
        try:
            r = await self._client.delete(f"/v1/sessions/{session_id}")
            if r.status_code not in (200, 204, 404):
                logger.warning(
                    "delete_session(%s) -> %s: %s",
                    session_id, r.status_code, r.text[:200],
                )
        except Exception as exc:
            logger.warning("delete_session(%s) raised: %s", session_id, exc)

    async def compact_session(self, session_id: str, model: str) -> str:
        """Compact a chat session IN PLACE (native ``POST /session/{id}/summarize``).

        OpenCode generates a concise summary and continues the SAME thread on
        top of it: the context tokens drop without losing the conversation —
        the cheap alternative to «Limpiar contexto» when the operator just
        wants the next messages to cost less. ``model`` (flat
        ``provider/model-id``) is REQUIRED: OpenCode's SummarizePayload has no
        "use the session's model" default (same contract the platform
        engine-api fixed in 7.89.x). Blocking until the summarize turn ends,
        hence ``COMPACT_TIMEOUT``.

        Returns ``"ok"`` on success, ``"gone"`` when the native session no
        longer exists (404 — OpenCode's session store lives in the agent
        CONTAINER, not the PVC, so a redeploy/restart wipes it while the
        registry row survives; the chat path self-heals via
        ``resolve(force_new=True)`` on its own 404), ``"error"`` otherwise.
        """
        provider_id, _, model_id = (model or "").partition("/")
        if not model_id:
            logger.warning("compact_session %s: model %r is not provider/model-id", session_id, model)
            return "error"
        try:
            async with httpx.AsyncClient(timeout=COMPACT_TIMEOUT, auth=_BASIC_AUTH) as client:
                r = await client.post(
                    f"{NATIVE_BASE_URL}/session/{session_id}/summarize",
                    json={"providerID": provider_id, "modelID": model_id, "auto": False},
                )
                if r.status_code == 404:
                    logger.info("compact_session %s: session gone (agent pod restarted?)", session_id)
                    return "gone"
                r.raise_for_status()
                return "ok"
        except Exception as exc:  # noqa: BLE001 — surfaced to the operator as "could not"
            logger.warning("compact_session %s failed: %s", session_id, exc)
            return "error"

    async def abort_session(self, session_id: str) -> bool:
        """Best-effort: tell the sidecar to stop the in-flight generation on
        ``session_id`` (operator kill switch / abort-before-retry / watchdog).
        Stops a runaway OpenCode run so it frees compute immediately instead
        of churning until the gateway timeout. Never raises."""
        if not session_id:
            return False
        try:
            r = await self._client.post(
                f"/v1/sessions/{session_id}/abort", timeout=15.0
            )
        except Exception as exc:
            logger.warning("abort_session(%s) raised: %s", session_id, exc)
            return False
        if r.status_code in (200, 204):
            return True
        logger.warning(
            "abort_session(%s) -> %s: %s",
            session_id, r.status_code, r.text[:200],
        )
        return False

    # ── Autonomous (synchronous, structured) ───────────────────────
    async def send_autonomous(
        self,
        session_id: str,
        prompt: str,
        *,
        timeout: float | None = None,
        require_structured: bool = True,
    ) -> dict:
        """Send a prompt and wait for the full structured response.

        Returns::

            {
              "text": "<clean text without the JSON tail>",
              "structured": {"summary": ..., "metrics": {...}, "next_step": ...},
              "tokens_input": int,
              "tokens_output": int,
              "cost": float,
              "model": str,
              "duration_sec": float,
            }

        Raises ``RecoverableAgentError`` on transient failures (the
        executor retries) and ``FatalAgentError`` on programming bugs.

        When ``require_structured=True`` and the agent did NOT emit a
        parsable STRUCTURED_RESPONSE tail, the sidecar returns 422 and
        we raise ``FatalAgentError`` — the executor records the run as
        a degraded failure so the operator can investigate the prompt
        contract, not retry forever.

        ``timeout`` omitted ⇒ the LIVE autonomous ceiling (``execution_limits.
        autonomous_run_seconds``: Operations → manifest → env), read per run. Callers with
        a deliberately shorter budget of their own (a colleague, a capability probe) pass it.
        """
        if timeout is None:
            from app.execution_limits import autonomous_run_seconds
            timeout = await autonomous_run_seconds()
        body = {
            "prompt": prompt,
            "mode": "autonomous",
            "require_structured": require_structured,
            "timeout_s": timeout,
            "auto_grant_permissions": True,
        }
        try:
            r = await self._client.post(
                f"/v1/sessions/{session_id}/messages",
                json=body,
                timeout=httpx.Timeout(timeout + 30.0, connect=10.0),
            )
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise RecoverableAgentError(f"simple API transport: {exc}") from exc
        if r.status_code == 504:
            # The sidecar returns 504 BOTH for a genuine wall-clock timeout
            # AND for an in-run watchdog abort (step ceiling / no-progress
            # loop), which can fire in seconds. Surfacing a hard-coded
            # "gateway timeout ({timeout}s)" hid the real reason and made
            # every aborted run look like a 900s hang. Propagate the
            # sidecar's actual ``detail`` so the run error is truthful.
            try:
                detail = r.json().get("detail")
            except Exception:
                detail = None
            if not detail:
                detail = (r.text or "").strip()[:300] or f"gateway timeout ({timeout}s)"
            raise RecoverableAgentError(f"simple API 504: {detail}")
        if r.status_code == 422:
            try:
                detail = r.json().get("detail", {})
            except Exception:
                detail = {}
            raise FatalAgentError(
                f"agent did not emit STRUCTURED_RESPONSE tail "
                f"(session_id={session_id!r}): {detail}"
            )
        if r.status_code >= 500:
            raise RecoverableAgentError(
                f"/v1/sessions/.../messages 5xx: {r.status_code}"
            )
        if r.status_code >= 400:
            raise FatalAgentError(
                f"/v1/sessions/.../messages {r.status_code}: {r.text[:300]!r}"
            )

        data = r.json()
        return {
            "text": data.get("text") or "",
            "structured": data.get("structured"),
            "tokens_input": int(data.get("tokens_input") or 0),
            "tokens_output": int(data.get("tokens_output") or 0),
            "cost": float(data.get("cost_usd") or 0.0),
            "model": data.get("model") or "",
            "duration_sec": float(data.get("duration_sec") or 0.0),
            # Which tools it used. The autonomous path is not a stream —the sidecar
            # returns the turn already finished— so without this list the run's detail
            # could only show what the employee WROTE in its database, which out of six
            # calls is one or two. Empty with a sidecar older than 7.217.0.
            "tools": data.get("tools") or [],
        }

    # Legacy alias — many generated apps still call ``send_and_wait``.
    async def send_and_wait(
        self, session_id: str, message: str, timeout: float | None = None,
    ) -> dict:
        return await self.send_autonomous(
            session_id, message, timeout=timeout, require_structured=True,
        )

    # ── Interactive (streaming, no structured) ─────────────────────
    async def stream_interactive(
        self,
        session_id: str,
        prompt: str,
        *,
        timeout: float | None = None,
        request_id: str | None = None,
        nivel: str | None = None,
        auto_grant: bool = True,
    ) -> AsyncIterator[dict]:
        """Yield normalised events from a chat-mode session.

        Each event is a dict with at least ``{"type": ...}`` where
        ``type`` is one of:

        * ``delta`` — incremental text fragment in ``delta``.
        * ``tool`` — tool invocation (``tool``, ``state``).
        * ``step_done`` — step finished (``cost``, ``tokens``, ``model``).
        * ``question`` — the agent asked the user a clarifying question
          (``request_id``, ``questions``); the UI renders a widget and the
          answer comes back as a follow-up turn carrying ``request_id``.
        * ``message_done`` — message finalised.
        * ``error`` — agent or transport error (``error``).
        * ``done`` — terminal sentinel.

        The sidecar strips any rogue ``STRUCTURED_RESPONSE:`` tail
        before forwarding deltas, so chat UIs never see the JSON.

        When ``request_id`` is set this turn ANSWERS a pending clarifying
        question (``prompt`` carries the chosen option) instead of starting a
        new prompt; the sidecar relays it to OpenCode and streams the
        continuation.

        ``nivel`` is the reasoning level of THIS turn (``app/razonamiento.py``):
        the id of a model VARIANT the agent pod declared in its opencode config,
        so the same model thinks more or less for this single message. Empty ⇒
        nothing is sent and the model runs with whatever the employee has
        configured.

        ``timeout`` omitted ⇒ the LIVE chat ceiling (``execution_limits.chat_turn_seconds``).
        The sidecar enforces it as a wall clock and answers ``{"type": "error", "error":
        "stream timeout"}`` when it runs out — WITHOUT stopping the opencode run: aborting
        it is the caller's job (see ``is_turn_time_limit``).
        """
        if timeout is None:
            from app.execution_limits import chat_turn_seconds
            timeout = await chat_turn_seconds()
        params = {
            "prompt": prompt,
            "mode": "interactive",
            "require_structured": "false",
            "timeout_s": str(timeout),
        }
        if request_id:
            params["request_id"] = request_id
        if nivel:
            params["variant"] = nivel
        if not auto_grant:
            # 15.8.0 — a controlled test chat: its asks come as `permission` events and wait for the person.
            params["auto_grant"] = "false"
        url = f"{self.base_url}/v1/sessions/{session_id}/stream"
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout + 30.0, connect=10.0),
                auth=_BASIC_AUTH,
            ) as client:
                async with client.stream("GET", url, params=params) as resp:
                    if resp.status_code >= 400:
                        body = await resp.aread()
                        raise RecoverableAgentError(
                            f"stream {resp.status_code}: {body[:300]!r}"
                        )
                    buffer = ""
                    async for chunk in resp.aiter_text():
                        buffer += chunk
                        while "\n\n" in buffer:
                            block, buffer = buffer.split("\n\n", 1)
                            for line in block.split("\n"):
                                if line.startswith("data:"):
                                    raw = line[5:].strip()
                                    if not raw:
                                        continue
                                    try:
                                        event = json.loads(raw)
                                    except json.JSONDecodeError:
                                        continue
                                    yield event
                                    if event.get("type") == "done":
                                        return
        except RecoverableAgentError:
            raise
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise RecoverableAgentError(f"stream transport: {exc}") from exc
        except Exception as exc:
            logger.error("stream_interactive crashed: %s", exc, exc_info=True)
            yield {"type": "error", "error": str(exc)}
            yield {"type": "done"}


#: What the sidecar (``stream_normalised``) yields when the turn's wall clock runs out. A
#: contract with the sidecar, matched in ONE place: the chat turns it into the coded error
#: ``TURN_TIME_LIMIT``, and ``autonomous/executor._is_heavy_failure`` keeps matching it.
SIDECAR_STREAM_TIMEOUT = "stream timeout"


def is_turn_time_limit(error: str) -> bool:
    """True when a stream ``error`` is the turn's ceiling running out (not a transport
    failure, not the 10-s «SSE connection timeout» of the sidecar's own attach)."""
    return (error or "").strip().lower() == SIDECAR_STREAM_TIMEOUT


# ── Singleton wiring (lifespan-managed) ────────────────────────────
_PROXY: AgentProxy | None = None


def set_agent_proxy(proxy: AgentProxy) -> None:
    global _PROXY
    _PROXY = proxy


def get_agent_proxy() -> AgentProxy:
    if _PROXY is None:
        raise RuntimeError(
            "AgentProxy not initialised — wire it in main.py's lifespan "
            "with `set_agent_proxy(AgentProxy())`."
        )
    return _PROXY


__all__ = [
    "AgentProxy",
    "RecoverableAgentError",
    "FatalAgentError",
    "AGENT_NAME",
    "AGENT_DEFAULT_MODEL",
    "AUTONOMOUS_USER_EMAIL",
    "SIDECAR_STREAM_TIMEOUT",
    "is_turn_time_limit",
    "set_agent_proxy",
    "get_agent_proxy",
]
