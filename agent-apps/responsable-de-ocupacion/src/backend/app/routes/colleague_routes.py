"""`/mcp/colleague` — the door through which ANOTHER employee talks to this one.

It is a surface DISTINCT from `/mcp` (`app-tools`), and the separation is deliberate:
`app-tools` is this employee's internal toolbox (alerts, recommendations, memory) and
handing that to a colleague would be handing over our database, not a conversation.
Here we expose the only thing that makes sense between colleagues:

| tool         | what it does                                              |
|--------------|-----------------------------------------------------------|
| `consultar`  | answers with its persona, its skills and its connectors   |
| `encargar`   | ACTS — subject to ITS write policy and approvals          |
| `capacidades`| returns its card, so the caller knows when to call it     |

**What keeps this from being a back door:** every call carries a delegation token with
the USER who originated it. This employee resolves that user in ITS directory and applies
ITS rules to THAT person. The relation (platform) authorises the employee; the permission
is decided by the user. Both have to say yes, and if the user is not in our directory the
answer is NO, stated with names and with whom to ask — never a silence nor a
"guest" treatment.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, Request

from app.auth import require_admin

from app.colleagues import (
    MODE_CONSULT, MODE_DELEGATE, card_to_text, colleague_card, resolve_caller_user,
    verify_delegation,
)

router = APIRouter(prefix="/mcp/colleague", tags=["colleague"])
logger = logging.getLogger(__name__)

# Ceiling of the turn granted to a colleague. Shorter than a human chat on purpose:
# whoever waits on the other side is another agent with its own user in front of it.
COLLEAGUE_TIMEOUT_S = 180.0


def _rpc_result(req_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _rpc_error(req_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _texto(mensaje: str, es_error: bool = False) -> Dict[str, Any]:
    """MCP response. `isError` flags the failure, but the text is ALWAYS sent: the
    caller has to be able to tell its user why it could not be done."""
    return {"content": [{"type": "text", "text": mensaje}], "isError": es_error}


async def _tool_descriptors() -> list[dict]:
    card = await colleague_card()
    ficha = card_to_text(card)
    quien = card.get("nombre") or card.get("app_name") or "this colleague"
    return [
        {
            "name": "consultar",
            "description": (
                f"Ask {quien} and wait for the answer. Use it when you need something "
                f"it knows and you don't.\n\n{ficha}\n\n"
                "Ask as you would ask a colleague: in business language and with the "
                "data it needs (see above). Do not ask it to execute anything: that is "
                "what `encargar` is for."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "pregunta": {"type": "string", "description": "What you are asking."},
                    "contexto": {"type": "string", "description": "Data that helps it answer."},
                },
                "required": ["pregunta"],
            },
        },
        {
            "name": "encargar",
            "description": (
                f"Ask {quien} to DO something in its systems. ITS write policy and ITS "
                "approvals apply: it may execute it or leave it parked awaiting sign-off, "
                "and in that case it will tell you. That is not a failure: it is its "
                "governance."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "tarea": {"type": "string", "description": "What you are asking it to do, concrete and bounded."},
                    "contexto": {"type": "string", "description": "Data needed to do it."},
                },
                "required": ["tarea"],
            },
        },
        {
            "name": "capacidades",
            "description": (
                f"Card of {quien}: what it solves, which systems it reaches, what it needs "
                "to answer well and what it CANNOT do right now. Check it if you are unsure "
                "whether it is the one to ask."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]


async def _ejecutar(nombre: str, args: Dict[str, Any], quien: Any,
                    delegacion: Dict[str, Any]) -> Dict[str, Any]:
    """Runs the tool with the user ALREADY resolved in our directory."""
    if nombre == "capacidades":
        card = await colleague_card()
        return _texto(card_to_text(card))

    if nombre not in ("consultar", "encargar"):
        return _texto(f"Unknown tool \"{nombre}\".", es_error=True)

    modo = MODE_CONSULT if nombre == "consultar" else MODE_DELEGATE
    peticion = (args.get("pregunta") or args.get("tarea") or "").strip()
    if not peticion:
        return _texto("You have not said what you need.", es_error=True)
    contexto = (args.get("contexto") or "").strip()

    # The budget rules here too: if the employee cannot work for its own boss, it cannot
    # work for a colleague either. Skipping it would be a hole through which money leaks.
    from app.budget import budget_block_reason
    bloqueo = await budget_block_reason()
    if bloqueo:
        return _texto(
            "I cannot take requests right now: this employee's budget is exhausted "
            f"({bloqueo}). Ask its owner to increase it.", es_error=True)

    from app.agent_proxy import get_agent_proxy
    from app.agent_sessions import resolve_operator_model, resolve_tool_permissions

    quien_pide = (delegacion.get("on_behalf_of_app") or "another employee")
    encabezado = (
        f"[COLLEAGUE REQUEST] The employee \"{quien_pide}\" is writing to you on behalf of "
        f"{quien.email}, who is {'an administrator' if (quien.role or '') == 'admin' else 'a member'} "
        "of your directory. Answer as you would answer that person: same business "
        "tone, same permissions, same caution.\n"
        + ("Treat it as a TASK: if it requires writing to a system, apply your normal "
           "policy (execute or park for approval) and SAY which of the two happened.\n"
           if modo == MODE_DELEGATE else
           "It is a QUESTION: answer with data, do not execute writes.\n")
        + "Answer directly and briefly — someone is waiting on the other side. "
        "Reply in the language the request is written in.\n\n"
    )
    # The run is opened BEFORE the turn and its id travels in the prompt. Not cosmetic: it
    # is what lets THIS employee's tools (including those of its own colleagues) know which
    # CHAIN they belong to. Without it, a second hop would start a new chain at depth 1 and
    # the depth cap would be decorative.
    run_id = await _abrir_run(peticion, quien, delegacion, modo)
    mensaje = (encabezado
               + (f"MCP `app-tools` (run_id={run_id}) — pass it on every call.\n\n" if run_id else "")
               + peticion + (f"\n\nContext provided:\n{contexto}" if contexto else ""))

    proxy = get_agent_proxy()
    session_id = await proxy.create_session(
        # The session is bound to the impersonated USER, not to the calling employee:
        # that is what keeps per-person permissions applying INSIDE the turn (the
        # impersonation plugin scopes every `wai-*` tool to that user).
        #
        # Role `interactive` and not `autonomous` for that same reason: `autonomous` is the
        # role of background work, which runs under the system user. Here there is a
        # concrete person behind it, even if they speak through a colleague.
        user_email=quien.email,
        role="interactive",
        model=await resolve_operator_model(),
        tool_permissions=await resolve_tool_permissions(),
    )
    try:
        # `require_structured=False`: we do not ask for the JSON tail of autonomous work —
        # what comes back here is an answer for another employee, and demanding a format
        # this turn has no reason to produce would be inventing a failure.
        crudo = await proxy.send_autonomous(
            session_id, mensaje, timeout=COLLEAGUE_TIMEOUT_S, require_structured=False,
        )
    finally:
        # Abort before deleting: a turn that timed out has to really STOP, or we keep
        # paying for it with nobody reading it.
        await proxy.abort_session(session_id)
        await proxy.delete_session(session_id)

    respuesta = (crudo.get("text") or "").strip() or "(no answer)"
    # TRACE ON THE CALLEE SIDE. Without this, serving a colleague would leave no trace in
    # THIS employee's history: its owner would see tokens spent and no run explaining
    # them. With `mode='colleague'` it shows up in /runs like any other turn, but saying
    # who asked and on whose behalf.
    await _cerrar_run(run_id, respuesta, crudo)
    return {
        "content": [{"type": "text", "text": respuesta}],
        "isError": False,
        # Metadata the caller uses to close the hop in the chain ledger.
        "_wai": {
            "cost_usd": crudo.get("cost"),
            "tokens_in": crudo.get("tokens_input"),
            "tokens_out": crudo.get("tokens_output"),
            "model": crudo.get("model"),
        },
    }


@router.post("")
@router.post("/")
async def colleague_mcp(
    request: Request,
    x_wai_delegation: str = Header(default=""),
):
    """MCP (JSON-RPC) endpoint for colleagues.

    Authentication is NOT a static Bearer: it is the delegation token, because what we
    need to know is not "who is this pod?" but "on WHOSE behalf is it speaking?". Without
    that answer not a single permission can be applied.
    """
    msg = await request.json()
    metodo = msg.get("method") or ""
    req_id = msg.get("id")
    params = msg.get("params") or {}

    if metodo.startswith("notifications/"):
        return None
    if metodo == "initialize":
        return _rpc_result(req_id, {
            "protocolVersion": params.get("protocolVersion") or "2025-03-26",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "colleague", "version": "1.0.0"},
        })
    if metodo == "ping":
        return _rpc_result(req_id, {})

    # From here on we need to know on whose behalf the caller speaks — even to LIST:
    # the card says which systems we reach, and that is not shown to just anyone.
    try:
        delegacion = verify_delegation(x_wai_delegation)
    except ValueError as exc:
        return _rpc_error(req_id, -32001, f"Invalid delegation: {exc}")
    # An APPLICATION's delegation opens the chat, not this door (each door, its own key).
    if delegacion.get("typ") == "WAI-DELEG-APP":
        return _rpc_error(req_id, -32001, "Invalid delegation: it is an application's, not a colleague's.")

    if metodo == "tools/list":
        return _rpc_result(req_id, {"tools": await _tool_descriptors()})

    if metodo == "tools/call":
        nombre = (params.get("name") or "").strip()
        args = params.get("arguments") or {}
        quien, motivo = await resolve_caller_user(delegacion.get("sub") or "")
        if motivo:
            # EXPLICIT rejection, with names: the colleague can repeat it to its user
            # verbatim, which is what avoids the "I don't know why it doesn't answer me".
            logger.info("colega rechazado: %s", motivo)
            return _rpc_result(req_id, _texto(motivo, es_error=True))
        inicio = time.monotonic()
        try:
            resultado = await _ejecutar(nombre, args, quien, delegacion)
        except Exception as exc:  # pragma: no cover — never return a silent 500
            logger.exception("fallo atendiendo a un compañero")
            return _rpc_result(req_id, _texto(
                f"I could not handle the request: {exc}", es_error=True))
        resultado.setdefault("_wai", {})["duration_ms"] = int((time.monotonic() - inicio) * 1000)
        return _rpc_result(req_id, resultado)

    return _rpc_error(req_id, -32601, f"Unsupported method: {metodo}")


@router.get("/card")
async def card():
    """The card, as JSON. The platform reads it to show it BEFORE confirming a relation:
    whoever asks to talk to this employee sees what they will be able to ask of it."""
    return await colleague_card()


# ── Surface for the employee's screens ────────────────────────────────────────────────
# An employee's day-to-day governance lives INSIDE the employee (Kommo is connected here,
# the write policy is set here and the directory is managed here), so its colleagues do
# too. The platform network and these screens are two doors to the SAME model: both call
# `/backend/agent-relations`, and what can be done is decided by the role.
@router.get("/relations")
async def my_relations(direction: str = "all", user=Depends(require_admin)):
    """THIS employee's relations. `outgoing` = whom it talks to; `incoming` = who talks
    to it. The platform is asked, as it owns the data: keeping a local copy would mean two
    truths about the same fact, and we know how that ends."""
    import httpx
    from app.config import AGENT_ID, BACKEND_API_SECRET, WAI_BACKEND_URL

    url = f"{(WAI_BACKEND_URL or '').rstrip('/')}/backend/agent-relations/"
    async with httpx.AsyncClient(timeout=12.0) as client:
        # `agent_id`, NOT `app_id`: what this pod knows is the id of the AGENT it runs,
        # and the deployed EMPLOYEE id is another number. Sending it as `app_id` does not
        # fail — it returns another employee's relations, or none — which is worse than
        # failing: the screen stayed empty or in error with nothing misconfigured.
        # The translation is done by the platform, which owns the link.
        r = await client.get(url, headers={"X-API-Secret": BACKEND_API_SECRET},
                             params={"agent_id": AGENT_ID, "direction": direction})
        r.raise_for_status()
        return r.json()


@router.get("/relations/{relation_id}/calls")
async def relation_calls(relation_id: int, limit: int = 50, user=Depends(require_admin)):
    """The REQUESTS that travelled through a collaboration, with their answer.

    The aggregate figure ("7 calls today") tells you something is happening and nothing
    more: it does not say who asked, on behalf of which person, what they asked nor why
    one ended badly. And it misleads: of those 7, 5 may never have arrived — denied by
    mode, cut by the loop guard — and another may be left unclosed, so the callee shows
    ONE execution and it looks like a contradiction. Debugging requires opening and
    reading."""
    import httpx
    from app.config import BACKEND_API_SECRET, WAI_BACKEND_URL

    base = (WAI_BACKEND_URL or "").rstrip("/")
    async with httpx.AsyncClient(timeout=12.0) as client:
        r = await client.get(f"{base}/backend/agent-relations/{relation_id}/calls",
                             headers={"X-API-Secret": BACKEND_API_SECRET},
                             params={"limit": limit})
        r.raise_for_status()
        return r.json()


@router.delete("/relations/{relation_id}")
async def delete_relation(relation_id: int, user=Depends(require_admin)):
    """Deletes a FINISHED collaboration and its requests.

    Only revoked or denied ones — the platform checks it —: a live one is cut by revoking
    it, which records the why. Finished ones do not come back, and piled up they hide the
    ones that matter."""
    import httpx
    from fastapi import HTTPException
    from app.config import BACKEND_API_SECRET, WAI_BACKEND_URL

    base = (WAI_BACKEND_URL or "").rstrip("/")
    async with httpx.AsyncClient(timeout=12.0) as client:
        r = await client.delete(f"{base}/backend/agent-relations/{relation_id}",
                                headers={"X-API-Secret": BACKEND_API_SECRET,
                                         "X-User-Email": getattr(user, "email", "") or ""})
        if r.status_code >= 400:
            raise HTTPException(r.status_code, (r.json() or {}).get("detail") or r.text)
        return r.json()


@router.post("/relations/{relation_id}/{accion}")
async def decide_relation(relation_id: int, accion: str, body: dict | None = None,
                          user=Depends(require_admin)):
    """Accept, deny, pause, resume or revoke — from the employee.

    Whoever accepts that another employee talks to this one is THIS employee's owner:
    that is why the action exists here and not only on the platform screen."""
    import httpx
    from fastapi import HTTPException
    from app.config import BACKEND_API_SECRET, WAI_BACKEND_URL

    if accion not in ("accept", "deny", "pause", "resume", "revoke"):
        raise HTTPException(400, f"Acción no reconocida: {accion}")
    base = (WAI_BACKEND_URL or "").rstrip("/")
    payload = dict(body or {})
    # Whoever decides is recorded with their real email, not as "the employee".
    payload.setdefault("reason", "")
    async with httpx.AsyncClient(timeout=12.0) as client:
        r = await client.post(
            f"{base}/backend/agent-relations/{relation_id}/{accion}",
            headers={"X-API-Secret": BACKEND_API_SECRET,
                     "X-User-Email": getattr(user, "email", "") or ""},
            json=payload,
        )
        if r.status_code >= 400:
            raise HTTPException(r.status_code, (r.json() or {}).get("detail") or r.text)
        return r.json()


async def _abrir_run(peticion: str, quien: Any, delegacion: Dict[str, Any],
                     modo: str) -> Optional[int]:
    """Records the turn in `agent_runs` BEFORE running it, with the chain inside.

    Two things at once: the trace on the CALLEE side (without it its owner would see
    tokens spent and no run explaining them) and the chain context its own colleague
    tools need so as not to restart the chain.

    Best-effort: if it cannot be recorded, better the trace is missing than the colleague
    left without an answer — so we carry on, but with a warning."""
    import hashlib
    import json as _json
    from datetime import datetime

    try:
        from app.app_database import app_write
        from app.app_models.runtime import AgentRun

        cadena = str(delegacion.get("chain_id") or "")
        clave = hashlib.sha256(
            f"colleague:{cadena}:{delegacion.get('depth')}:{peticion[:500]}".encode()
        ).hexdigest()
        async with app_write() as s:
            fila = AgentRun(
                idempotency_key=clave,
                trace_id=cadena or None,
                status="RUNNING",
                # Neither `chat` nor `autonomous`: it is a third origin and deserves its own
                # name, or the history would mix apples and oranges.
                mode="colleague",
                # v12.7.0 — who and through where. In a colleague call the requester is the
                # PERSON on whose behalf the calling employee acts, not the employee: that
                # is what makes "what did So-and-so ask for?" answerable even when the
                # request took two hops between employees.
                solicitante=(getattr(quien, "email", "") or None),
                origen="companero",
                prompt=peticion[:20000],
                request_meta_json=_json.dumps({
                    "colleague_of": delegacion.get("on_behalf_of_app"),
                    "on_behalf_of": getattr(quien, "email", ""),
                    "chain_id": cadena,
                    "depth": int(delegacion.get("depth") or 1),
                    "mode": modo,
                }, ensure_ascii=False),
                started_at=datetime.utcnow(),
            )
            s.add(fila)
            await s.commit()
            return int(fila.id)
    except Exception:  # pragma: no cover
        logger.warning("no se pudo abrir el run de la consulta de un compañero", exc_info=True)
        return None


async def _cerrar_run(run_id: Optional[int], respuesta: str, crudo: Dict[str, Any]) -> None:
    """Closes the run with what it cost and what it answered."""
    import json as _json
    from datetime import datetime

    if not run_id:
        return
    try:
        from sqlalchemy import update
        from app.app_database import app_write
        from app.app_models.runtime import AgentRun

        async with app_write() as s:
            await s.execute(update(AgentRun).where(AgentRun.id == int(run_id)).values(
                status="COMPLETED",
                response_md=(respuesta or "")[:20000],
                tokens_in=int(crudo.get("tokens_input") or 0) or None,
                tokens_out=int(crudo.get("tokens_output") or 0) or None,
                cost_usd=str(crudo.get("cost") or "") or None,
                model=crudo.get("model") or None,
                finished_at=datetime.utcnow(),
            ))
            await s.commit()
    except Exception:  # pragma: no cover
        logger.warning("no se pudo cerrar el run de la consulta de un compañero", exc_info=True)
