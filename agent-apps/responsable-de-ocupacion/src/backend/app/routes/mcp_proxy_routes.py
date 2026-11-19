"""Universal MCP policy proxy — the app-pod PEP for EVERY external
connector (v7.26.0).

The agent pod carries ZERO connector credentials: its bridges point at
``/mcp-proxy/{token}/{connector}`` on THIS pod, which

  1. VETOES ``tools/call`` per the platform's permissions table for THIS
     employee (``app.mcp_policy``) BEFORE anything leaves the cluster,
  2. FILTERS ``tools/list`` responses (a tool the table denies simply does
     not exist for the agent), and
  3. attaches the connector's real credentials (Composio master key /
     BYO bearer / api-key header) and forwards to the upstream MCP.

Auth: the shared ``APP_MCP_TOKEN`` travels IN THE PATH because the
``streaming-http-proxy`` bridge can only be given a URL. Leaking that
token to the model is harmless by design: it only buys access to THIS
proxy, which enforces the same policy for any caller.

Protocol: streamable-HTTP MCP — JSON-RPC over POST (responses either
``application/json`` or ``text/event-stream``), optional GET SSE
channel, DELETE to close. The relay is transparent except for the two
interventions above; ``Mcp-Session-Id`` round-trips untouched.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
from typing import Any, AsyncIterator, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app import mcp_policy, prueba_controlada, prueba_solo_lectura

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/mcp-proxy", tags=["mcp-proxy"])

APP_MCP_TOKEN = os.environ.get("APP_MCP_TOKEN", "")
# Composio master key — since v7.26.0 it lives in THIS pod only (the
# deployer no longer injects it into the agent pod).
COMPOSIO_API_KEY = (
    os.environ.get("COMPOSIO_API_KEY") or os.environ.get("API_KEY") or ""
).strip()

# Serialises the cold-miss upstream warm-up so a burst of concurrent first
# requests doesn't fan out into duplicate resolves / router sessions.
import asyncio as _asyncio
_COLD_RESOLVE_LOCK = _asyncio.Lock()

_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
    "content-length", "authorization",
}


def _check_token(token: str) -> None:
    # Constant-time compare (avoid a timing side-channel on the proxy token).
    if not APP_MCP_TOKEN or not hmac.compare_digest(str(token or ""), APP_MCP_TOKEN):
        raise HTTPException(status_code=401, detail="bad proxy token")


async def upstream_for(connector: str, acting_user: str = "") -> dict[str, Any]:
    """Resolve the upstream MCP for ``connector``:

    * a Composio connector → its persisted per-identity signed URL (the
      binding ``resolve_runtime_mcp_block`` maintains) + the master key
      header;
    * a BYO external MCP → ``MCP_UPSTREAMS`` (deploy-time env, shape
      ``{"<name>": {"url": ..., "headers": {...}}}`` — credentials the
      platform used to bake into the AGENT config and now land here);
    * an IN-HOUSE connector (built in WAI) → its LIVE record, which
      carries its URL and the header with THIS employee's key.

    ``acting_user`` (the person of the turn) only affects the connectors that DELEGATE identity
    (``wai_app``): their header is signed FOR THAT PERSON instead of for the owner. For the rest
    it is harmless.
    """
    name = (connector or "").strip().lower()

    # Connector with its own key (one built in WAI, or an external one with its api-key): it goes
    # FIRST and through the LIVE record, not through the deployment env. The deployer copies
    # these connectors into ``MCP_UPSTREAMS`` —it does so with every entry that carries
    # ``auth_type``— but WITHOUT a header, because the key of an in-house connector does not
    # belong to the deployment: it belongs to THIS employee's connection, which is pasted (or
    # rotated) much later. That credential-less copy won, the connector answered 401 and the
    # agent was left without a single tool. Asking here is also what makes connecting take
    # effect without redeploying. The connector is only claimed if it can be served: Composio
    # ones carry no record with URL+header and keep to their own path, untouched.
    from app import mcp_runtime as _mr
    propio = _mr.upstream_propio(name, acting_user)
    if propio:
        return propio
    if _mr.espera_llave(name):
        raise HTTPException(
            status_code=503,
            detail=(f"el conector {name!r} está sin conectar: pega su llave de acceso en "
                    f"Conectores y vuelve a intentarlo"),
        )

    # BYO externals (explicit registry).
    try:
        upstreams = json.loads(os.environ.get("MCP_UPSTREAMS") or "{}")
    except Exception:
        upstreams = {}
    for k, v in (upstreams or {}).items():
        if str(k).strip().lower() == name and isinstance(v, dict) and v.get("url"):
            return {"url": str(v["url"]), "headers": dict(v.get("headers") or {})}

    # Composio connector: the live upstream registry ``mcp_runtime`` fills
    # on every resolve (boot + the agent's periodic refresher). The signed
    # URL is already scoped to the connector's own identity and — in router
    # mode — its filtered session. On a cold miss (proxy hit before any
    # resolve), resolve the block ONCE to warm it — serialised by a lock so
    # a burst of concurrent cold requests doesn't fan out into duplicate
    # resolves + redundant Composio router sessions (thundering herd).
    from app import mcp_runtime
    url = mcp_runtime.upstream_url_for(name)
    if not url:
        async with _COLD_RESOLVE_LOCK:
            url = mcp_runtime.upstream_url_for(name)  # re-check inside the lock
            if not url:
                try:
                    await mcp_runtime.resolve_runtime_mcp_block()
                except Exception:  # pragma: no cover — resolution is best-effort
                    logger.debug("cold upstream resolve failed", exc_info=True)
                url = mcp_runtime.upstream_url_for(name)
    if not url:
        raise HTTPException(status_code=503, detail=f"connector {name!r} not resolved")
    headers = {"x-api-key": COMPOSIO_API_KEY} if COMPOSIO_API_KEY else {}
    return {"url": url, "headers": headers}


def _rpc_error(rpc_id: Any, message: str) -> dict[str, Any]:
    # Tool-level failure (not a protocol error): MCP expects a RESULT with
    # isError=true so the model reads the veto text and adapts, instead of
    # a JSON-RPC error some clients surface as a hard crash.
    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "result": {"content": [{"type": "text", "text": message}], "isError": True},
    }


def _parse_sse_payloads(body: bytes) -> list[dict]:
    """JSON-RPC messages out of a streamable-HTTP SSE body."""
    out: list[dict] = []
    for block in body.decode("utf-8", "replace").split("\n\n"):
        data_lines = [l[5:].strip() for l in block.split("\n") if l.startswith("data:")]
        if not data_lines:
            continue
        try:
            out.append(json.loads("\n".join(data_lines)))
        except Exception:
            continue
    return out


def _encode_sse(payloads: list[dict]) -> bytes:
    return b"".join(
        b"event: message\ndata: " + json.dumps(p, ensure_ascii=False).encode() + b"\n\n"
        for p in payloads
    )


def _add_run_id_param(tool: dict) -> None:
    """Declares `run_id` (optional) in the schema of ONE tool of a `wai_app` connector, so
    the model fills it with its turn's run —same as the employee's own tools—. The relay
    reads it to DERIVE server-side who the person is (`run_serving_user`) and STRIPS it
    before it reaches the connector. Without declaring it, the model does not pass it
    reliably: it is what separates "save MY profile" from the owner's profile."""
    schema = tool.get("inputSchema")
    if not isinstance(schema, dict):
        schema = {"type": "object", "properties": {}}
        tool["inputSchema"] = schema
    props = schema.get("properties")
    if not isinstance(props, dict):
        props = {}
        schema["properties"] = props
    props.setdefault("run_token", {
        "type": "string",
        "description": (
            "INTERNAL: pass the EXACT run_token from this turn's [INTERNO] line. It identifies "
            "the PERSON speaking, so their data (profile, preferences…) is THEIRS and not "
            "the employee owner's. Do not invent it or omit it."
        ),
    })


def _filter_list_payloads(connector: str, payloads: list[dict], table: Optional[dict],
                          es_wai_app: bool = False) -> list[dict]:
    for p in payloads:
        result = p.get("result") if isinstance(p, dict) else None
        if isinstance(result, dict) and isinstance(result.get("tools"), list):
            tools = mcp_policy.filter_tools_list(connector, result["tools"], table)
            if es_wai_app:
                for t in tools:
                    if isinstance(t, dict):
                        _add_run_id_param(t)
            result["tools"] = tools
    return payloads


def _tools_from_body(raw: bytes, ctype: str) -> Optional[list]:
    """Pull a ``result.tools`` list out of a JSON or SSE MCP response body."""
    if "text/event-stream" in (ctype or ""):
        msgs: list = _parse_sse_payloads(raw)
    else:
        try:
            parsed = json.loads(raw or b"null")
        except Exception:
            return None
        msgs = parsed if isinstance(parsed, list) else [parsed] if isinstance(parsed, dict) else []
    for m in msgs:
        res = m.get("result") if isinstance(m, dict) else None
        if isinstance(res, dict) and isinstance(res.get("tools"), list):
            return res["tools"]
    return None


_PROBE_INIT = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "wai-readiness-probe", "version": "1"},
    },
}
_PROBE_LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}


async def probe_upstream_tools(connector: str) -> Optional[int]:
    """Best-effort readiness probe: how many tools the connector's upstream MCP
    session serves RIGHT NOW (``>=1`` ⇒ warm). Returns ``None`` on any transport/
    protocol error — the boot ``/agent-runtime/ready`` gate treats ``None`` (and
    ``0``) as "not warm yet" and keeps polling until its cap, so a probe hiccup
    can only DELAY the boot up to the cap, never wrongly disable a connector.

    A freshly-minted Composio tool-router session does not serve its meta-tools
    the instant it is created; this is what lets the entrypoint hold ``opencode
    serve`` until the bridge is real, so opencode never binds a cold session."""
    name = (connector or "").strip().lower()
    try:
        upstream = await upstream_for(name)
    except HTTPException:
        return None
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        **(upstream.get("headers") or {}),
    }
    url = upstream["url"]
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=5.0)) as client:
            # streamable-HTTP MCP handshake: initialize → carry Mcp-Session-Id →
            # tools/list. (Composio round-trips Mcp-Session-Id, see module docstring.)
            init = await client.post(url, json=_PROBE_INIT, headers=headers)
            sid = init.headers.get("mcp-session-id")
            if sid:
                headers["Mcp-Session-Id"] = sid
                try:
                    await client.post(
                        url, headers=headers,
                        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                    )
                except Exception:  # noqa: BLE001 — notification is best-effort
                    pass
            lst = await client.post(url, json=_PROBE_LIST, headers=headers)
            tools = _tools_from_body(lst.content, lst.headers.get("content-type", ""))
            if tools is None:
                # Fallback for servers that answer tools/list without a handshake.
                bare = await client.post(url, json=_PROBE_LIST,
                                         headers={k: v for k, v in headers.items()
                                                  if k != "Mcp-Session-Id"})
                tools = _tools_from_body(bare.content, bare.headers.get("content-type", ""))
            prueba_solo_lectura.anotar_herramientas(name, tools)
            return len(tools) if tools is not None else None
    except Exception:  # noqa: BLE001 — never raise out of a readiness probe
        logger.debug("readiness probe for %s failed", name, exc_info=True)
        return None


@router.api_route("/{token}/{connector}", methods=["POST", "GET", "DELETE"])
async def relay(token: str, connector: str, request: Request):
    _check_token(token)
    name = (connector or "").strip().lower()
    tabla = await mcp_policy.load_table()
    from app import mcp_runtime as _mr
    # Only a connector that DELEGATES identity to a WAI app (`wai_app`) signs its token for
    # the person of the turn; for the rest (Composio, api_key…) the person is harmless. Once.
    es_wai_app = _mr.es_wai_app(name)
    # The person of the turn is DERIVED SERVER-SIDE from the `run_id` the model passes in a
    # `tools/call` —same as the employee's memory—, NEVER from an identity argument
    # (forgeable). That is why `upstream_for` is resolved AFTER looking at the body, not before.
    acting_user = ""

    body = await request.body()
    is_tools_list = False
    if request.method == "POST" and body:
        try:
            rpc = json.loads(body)
        except Exception:
            rpc = None
        messages = rpc if isinstance(rpc, list) else [rpc] if isinstance(rpc, dict) else []
        for msg in messages:
            method = str(msg.get("method") or "")
            if method == "tools/list":
                is_tools_list = True
            elif method == "tools/call":
                params = msg.get("params") or {}
                tool_name = str(params.get("name") or "")
                # COALESCENCE (v7.35.0): the trusted sidecar plugin injects
                # ``__wai_write_grant`` into connector calls of an already-
                # approved reaction run. Read + VALIDATE it, then STRIP it from
                # the body so it never reaches the upstream and the signature/
                # replay stay clean. The LLM never sets this (same trust
                # boundary as acting_user).
                msg_grant = False
                # 15.7.0 — the same slot carries the MARKER of a read-only test run
                # (`prueba_solo_lectura`): never a write grant, it TIGHTENS the verdict.
                solo_lectura = ""
                # 15.8.0 — or the MARKER of a controlled test chat (`prueba_controlada`): never a write
                # grant either; a write passes only if the person was asked and said yes.
                controlada = ""
                args = params.get("arguments")
                if isinstance(args, dict) and "__wai_write_grant" in args:
                    _g = str(args.pop("__wai_write_grant") or "")
                    body = json.dumps(rpc).encode("utf-8")   # rebuild WITHOUT the grant
                    if prueba_solo_lectura.es_marca(_g):
                        solo_lectura = _g
                    elif prueba_controlada.es_marca(_g):
                        controlada = _g
                    else:
                        msg_grant = await _valid_write_grant(_g)
                # PERSON IDENTITY (only `wai_app` connectors). The model passes the
                # `run_id` of ITS turn —declared in the schema of these tools, see
                # `_filter_list_payloads`— and the server DERIVES who it is from the run row
                # (`run_serving_user` → `agent_runs.request_meta_json.memory_user`, stamped by
                # the chat endpoint when the turn starts). The model does NOT choose the person;
                # it only supplies the run_id. It is the SAME anchor as the employee's memory, and
                # it is robust precisely because the model does not set the identity. Without a
                # run_id (or if it does not resolve), the token stays the owner's —the previous
                # behaviour—.
                if isinstance(args, dict) and "acting_user" in args:
                    # A raw `acting_user` (e.g. from a prompt injection) is NOT trustworthy:
                    # it is ignored and stripped. Identity is derived from the run, not from here.
                    args.pop("acting_user", None)
                    body = json.dumps(rpc).encode("utf-8")
                if es_wai_app and isinstance(args, dict) and (
                        "run_token" in args or "run_id" in args):
                    # v12.0.0 — `run_token` (chat, unguessable) through the single gate
                    # `resolver_run`; an integer `run_id` is only valid for autonomous runs (no
                    # person), so in chat it cannot be used to impersonate anyone.
                    _tok = args.pop("run_token", None)
                    _rid = args.pop("run_id", None)
                    body = json.dumps(rpc).encode("utf-8")   # the connector expects neither
                    from app.mcp_actor import resolver_run, run_serving_user
                    _run = await resolver_run(_tok, _rid)
                    persona = await run_serving_user(_run) if _run else None
                    if persona:
                        acting_user = str(persona).strip().lower()
                if controlada and not prueba_controlada.activa(controlada):
                    # The test is over, or this pod restarted and forgot it: nothing of it runs.
                    decision, slugs, reason = "deny", [tool_name], mcp_policy.CONTROLLED_TEST
                elif controlada:
                    decision, slugs, reason = await mcp_policy.decide_controlled_test(
                        name, tool_name, params.get("arguments"), prueba_controlada.preguntadas(controlada))
                elif solo_lectura and not prueba_solo_lectura.activa(solo_lectura):
                    # The test this session was opened for is over (or never existed here):
                    # nothing of it runs any more. Fail-closed.
                    decision, slugs, reason = "deny", [tool_name], mcp_policy.READ_ONLY_TEST
                elif solo_lectura:
                    decision, slugs, reason = await mcp_policy.decide_read_only_test(
                        name, tool_name, params.get("arguments"))
                else:
                    decision, slugs, reason = await mcp_policy.decide_call(
                        name, tool_name, params.get("arguments"), write_granted=msg_grant,
                    )
                if decision == "deny":
                    return JSONResponse(_rpc_error(
                        msg.get("id"), mcp_policy.deny_message(name, reason)))
                if decision == "approve":
                    # LOOP-GUARD: if this EXACT write was just executed via an
                    # approval (replay), return its cached result instead of
                    # re-parking — otherwise the APPROVAL_GRANTED follow-up run
                    # re-attempts it and loops.
                    sig = mcp_policy.write_signature(name, tool_name, params.get("arguments"))
                    cached = await _recent_execution(sig)
                    if cached is not None:
                        return JSONResponse({"jsonrpc": "2.0", "id": msg.get("id"), "result": cached})
                    # Park the EXACT request server-side so approval replays it
                    # verbatim (no schema guessing). The agent only records a
                    # human-readable approval referencing it by request_ref.
                    # Covers BOTH a write-approval and an operator "ask" permission.
                    ref = await _stage_pending_request(name, body, slugs)
                    from app.agent_locale import resolve_agent_language
                    return JSONResponse(_rpc_error(
                        msg.get("id"),
                        mcp_policy.approval_message(name, slugs, ref, reason,
                                                    await resolve_agent_language())))

    # Now, with the person of the turn known, the upstream is resolved. For a
    # `wai_app` its identity header is signed FOR THAT PERSON; for the rest, same
    # as before.
    upstream = await upstream_for(name, acting_user)

    fwd_headers = {
        k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP
    }
    fwd_headers.update(upstream["headers"])

    client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0))
    upstream_req = client.build_request(
        request.method, upstream["url"], content=body or None, headers=fwd_headers,
    )
    upstream_resp = await client.send(upstream_req, stream=True)

    # SELF-HEAL: a Composio router-session URL that has expired / been revoked
    # answers 401/403 (bad auth) or 404 (session gone). Drop the cached session
    # so the next resolve MINTS a fresh one instead of the agent staying stuck on
    # a dead upstream (the reuse cache would otherwise keep handing back the dead
    # url). Best-effort — this request still fails; the retry lands on the new one.
    if upstream_resp.status_code in (401, 403, 404):
        from app import mcp_runtime
        mcp_runtime.invalidate_router_session(name)

    resp_headers = {
        k: v for k, v in upstream_resp.headers.items() if k.lower() not in _HOP_BY_HOP
    }
    ctype = upstream_resp.headers.get("content-type", "")

    # tools/list responses are small and must be inspected: buffer, filter,
    # re-emit in the SAME encoding. Everything else streams through.
    if is_tools_list:
        raw = await upstream_resp.aread()
        await upstream_resp.aclose()
        await client.aclose()
        # What this connector really serves, BEFORE filtering: a read-only test session denies by
        # opencode key the tools the table does not classify (`prueba_solo_lectura.permisos`).
        prueba_solo_lectura.anotar_herramientas(name, _tools_from_body(raw, ctype))
        if "text/event-stream" in ctype:
            payloads = _filter_list_payloads(name, _parse_sse_payloads(raw), tabla, es_wai_app)
            content = _encode_sse(payloads)
        else:
            try:
                parsed = json.loads(raw or b"null")
            except Exception:
                parsed = None
            if parsed is not None:
                msgs = parsed if isinstance(parsed, list) else [parsed]
                msgs = _filter_list_payloads(name, msgs, tabla, es_wai_app)
                content = json.dumps(
                    msgs if isinstance(parsed, list) else msgs[0],
                    ensure_ascii=False,
                ).encode()
            else:
                content = raw
        resp_headers.pop("content-encoding", None)
        return Response(
            content=content, status_code=upstream_resp.status_code,
            headers=resp_headers, media_type=ctype or "application/json",
        )

    async def _stream() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream_resp.aiter_raw():
                yield chunk
        finally:
            await upstream_resp.aclose()
            await client.aclose()

    return StreamingResponse(
        _stream(), status_code=upstream_resp.status_code,
        headers=resp_headers, media_type=ctype or None,
    )


async def _stage_pending_request(connector: str, raw_body: bytes,
                                 slugs: list[str]) -> str:
    """Persist the EXACT parked JSON-RPC request so approval can replay it
    verbatim. Returns its ``request_ref`` (a hash of the body — the integrity
    binding). Best-effort TTL-sweep of stale rows on the way in."""
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import delete as _delete
    from app.app_database import app_write
    from app.app_models.runtime import PendingToolRequest
    from sqlalchemy.dialects.sqlite import insert as _insert
    body_str = (raw_body or b"").decode("utf-8", "replace")
    ref = __import__("hashlib").sha256(
        (connector + "|" + body_str).encode("utf-8")).hexdigest()[:32]
    async with app_write() as s:
        await s.execute(_delete(PendingToolRequest).where(
            PendingToolRequest.created_at < datetime.now(timezone.utc) - timedelta(days=14)
        ))
        await s.execute(_insert(PendingToolRequest).values(
            request_ref=ref, connector=connector,
            tool_slugs=",".join(slugs)[:500] or None, raw_body=body_str,
            created_at=datetime.now(timezone.utc),
        ).on_conflict_do_nothing(index_elements=[PendingToolRequest.request_ref]))
        await s.commit()
    return ref


# ── Loop-guard: cache of just-executed approved writes (v7.34.0) ──────
_EXEC_TTL_MIN = 15  # a re-attempt of an approved write within this window is a loop


async def _record_execution(signature: str, connector: str, result: dict) -> None:
    """Remember that this exact write was executed (via an approval replay), so
    a re-attempt is recognised as a loop and short-circuited. Best-effort."""
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import delete as _delete
    from app.app_database import app_write
    from app.app_models.runtime import RecentToolExecution
    from sqlalchemy.dialects.sqlite import insert as _insert
    try:
        async with app_write() as s:
            await s.execute(_delete(RecentToolExecution).where(
                RecentToolExecution.created_at < datetime.now(timezone.utc) - timedelta(minutes=_EXEC_TTL_MIN)
            ))
            await s.execute(_insert(RecentToolExecution).values(
                signature=signature, connector=connector,
                result=json.dumps(result, ensure_ascii=False, default=str)[:8000],
                created_at=datetime.now(timezone.utc),
            ).on_conflict_do_update(
                index_elements=[RecentToolExecution.signature],
                set_={"result": json.dumps(result, ensure_ascii=False, default=str)[:8000],
                      "created_at": datetime.now(timezone.utc)}))
            await s.commit()
    except Exception:  # noqa: BLE001 — the guard must never break a call
        logger.debug("loop-guard record failed", exc_info=True)


async def _recent_execution(signature: str) -> Optional[dict]:
    """Return the cached result of a recently-executed approved write matching
    ``signature`` (within the TTL), or None. Never raises."""
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import select as _select
    from app.app_database import app_session
    from app.app_models.runtime import RecentToolExecution
    try:
        async with app_session() as s:
            row = (await s.execute(_select(RecentToolExecution).where(
                RecentToolExecution.signature == signature,
                RecentToolExecution.created_at >= datetime.now(timezone.utc) - timedelta(minutes=_EXEC_TTL_MIN),
            ))).scalars().first()
        if row is None:
            return None
        return json.loads(row.result or "null")
    except Exception:  # noqa: BLE001
        logger.debug("loop-guard lookup failed", exc_info=True)
        return None


_GRANT_TTL_MIN = 20  # a pre-approved reaction run's write budget


async def _valid_write_grant(grant_id: str) -> bool:
    """True when ``grant_id`` is an unexpired run-scoped write grant (minted by
    approvals_routes when the operator approved an automation reaction). Never
    raises. The grant lets a ``write_with_approval`` write proceed for that run
    without a SECOND approval — read_only/permission still enforce."""
    if not grant_id:
        return False
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import select as _select
    from app.app_database import app_session
    from app.app_models.runtime import WriteGrant
    try:
        async with app_session() as s:
            row = (await s.execute(_select(WriteGrant).where(
                WriteGrant.grant_id == grant_id,
                WriteGrant.created_at >= datetime.now(timezone.utc) - timedelta(minutes=_GRANT_TTL_MIN),
            ))).scalars().first()
        return row is not None
    except Exception:  # noqa: BLE001 — a grant lookup error must never allow bypass
        logger.debug("write-grant lookup failed", exc_info=True)
        return False


async def llamar_herramienta(connector: str, tool: str, arguments: dict,
                             acting_user: str = "") -> dict[str, Any]:
    """One `tools/call` to the connector, through its usual upstream.

    It exists because a SHORTCUT (`app/atajos.py`) must be able to run the tool the
    definition declares, and that one is almost never one of the employee's LOCAL tools: the
    connector's live here. Measured on 20/09/2026: the shortcut looked in `mcp_server.TOOLS`
    —the 70 local ones— and failed with "the tool does not exist in this employee" on a
    `wai-viajes_consultar_contexto_usuario` that did exist, just somewhere else.

    It is the same path as `replay_approved_request`: handshake and `tools/call`. It does not
    repeat the write policy because the caller already went through the permissions gate,
    and because a shortcut is only declared over queries."""
    upstream = await upstream_for(connector, acting_user)
    headers = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
        **upstream["headers"],
    }
    cuerpo = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
              "params": {"name": tool, "arguments": dict(arguments or {})}}
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0)) as client:
        r1 = await client.post(upstream["url"], headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "wai-atajo", "version": "1.0"}},
        })
        session_id = r1.headers.get("mcp-session-id")
        if session_id:
            headers["mcp-session-id"] = session_id
        await client.post(upstream["url"], headers=headers, json={
            "jsonrpc": "2.0", "method": "notifications/initialized"})
        r2 = await client.post(upstream["url"], headers=headers, json=cuerpo)
    if "text/event-stream" in r2.headers.get("content-type", ""):
        for pay in _parse_sse_payloads(r2.content):
            if isinstance(pay, dict) and pay.get("result") is not None:
                return pay["result"]
            if isinstance(pay, dict) and pay.get("error") is not None:
                return {"error": pay["error"]}
        return {"error": "no response payload"}
    try:
        d = r2.json()
    except Exception:  # noqa: BLE001
        return {"error": f"non-JSON response from the connector ({r2.status_code})"}
    if isinstance(d, dict) and d.get("error") is not None:
        return {"error": d["error"]}
    return (d or {}).get("result") or {}


async def replay_approved_request(connector: str, request_ref: str) -> dict[str, Any]:
    """Deterministic execution of an APPROVED write: REPLAY the agent's exact
    original request (staged at veto time) to the upstream — no hand-built
    Composio call, so we never guess a meta-tool schema. The ``request_ref``
    is the integrity binding (maps to one exact body) and the connector must
    match. Consumes the pending row. Returns the tool result / an error dict."""
    from sqlalchemy import delete as _delete, select as _select
    from app.app_database import app_session, app_write
    from app.app_models.runtime import PendingToolRequest
    async with app_session() as s:
        row = (await s.execute(_select(PendingToolRequest).where(
            PendingToolRequest.request_ref == request_ref,
            PendingToolRequest.connector == connector,
        ))).scalars().first()
        if row is None:
            return {"error": "approved request not found or expired (request_ref)"}
        raw_body = row.raw_body

    upstream = await upstream_for(connector)
    headers = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
        **upstream["headers"],
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0)) as client:
        # MCP handshake, then replay the ORIGINAL tools/call body verbatim.
        r1 = await client.post(upstream["url"], headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "wai-approved-exec", "version": "1.0"}},
        })
        session_id = r1.headers.get("mcp-session-id")
        if session_id:
            headers["mcp-session-id"] = session_id
        await client.post(upstream["url"], headers=headers, json={
            "jsonrpc": "2.0", "method": "notifications/initialized"})
        r2 = await client.post(upstream["url"], headers=headers, content=raw_body.encode("utf-8"))
        raw = r2.content

    result: dict[str, Any]
    if "text/event-stream" in r2.headers.get("content-type", ""):
        result = {"error": "no response payload"}
        for p in _parse_sse_payloads(raw):
            if isinstance(p, dict) and p.get("result") is not None:
                result = p["result"]; break
            if isinstance(p, dict) and p.get("error") is not None:
                result = {"error": p["error"]}; break
    else:
        try:
            parsed = json.loads(raw or b"{}")
            result = parsed.get("result") or {"error": parsed.get("error")}
        except Exception:
            result = {"error": f"unparseable response (HTTP {r2.status_code})"}

    succeeded = isinstance(result, dict) and not result.get("error") and not result.get("isError")

    # v7.85.0 — consume the staged row ONLY on success. Deleting it unconditionally
    # made an approved write UN-retryable when the upstream call errored: the
    # single-use exact body was gone, so the action was lost (approved but never
    # executed, no safe retry). Keeping it on failure lets the APPROVAL_GRANTED
    # follow-up run (or a re-approve) re-replay the identical body. An MCP tool
    # error (error/isError) means the write did NOT take effect, so this cannot
    # double-execute.
    if succeeded:
        async with app_write() as s:
            await s.execute(_delete(PendingToolRequest).where(
                PendingToolRequest.request_ref == request_ref))
            await s.commit()
        # Loop-guard: cache the SUCCESSFUL execution by its metadata-stable
        # signature, so a re-attempt of the same write is short-circuited.
        try:
            _b = json.loads(raw_body); _p = _b.get("params") or {}
            sig = mcp_policy.write_signature(connector, str(_p.get("name") or ""), _p.get("arguments"))
            await _record_execution(sig, connector, result)
        except Exception:  # noqa: BLE001
            logger.debug("loop-guard record on replay failed", exc_info=True)
    return result
