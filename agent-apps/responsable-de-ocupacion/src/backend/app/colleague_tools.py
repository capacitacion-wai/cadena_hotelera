"""Which colleagues this employee needs and which ones it is missing.

It NO LONGER talks to them from here: each callable employee is an MCP server of the
catalog attached to the agent's version, and the call is governed by the platform
(relation, depth, cycle, caps and the chain ledger). What remains here is the only thing
that is still the employee's: knowing which ROLES it declared it needs and saying out loud
the ones it does not have covered, which is what travels in its instructions and stops it
from discovering it in the middle of a conversation.

OLD (retired):

They lived in `app-tools` (the app's own MCP) and not in the pod configuration, and the
reason is what holds up the whole feature: **an MCP configured in the pod carries a static
token and does not know who is asking.** Here, instead, the call is born inside the app,
which does know the run and therefore the person — and it is that person who travels to the
other employee.

One tool per colleague and mode, named after the colleague (`consultar_a_vera`), not a
generic verb: in the agent's tool list they have to read as colleagues, not as an API.

The cycle of each call:

1. resolve WHO is asking (from the run, not from the model: the model cannot choose an identity);
2. open the hop in the platform's ledger — which checks relation, mode, depth, cycle and
   caps BEFORE spending;
3. talk to the colleague carrying the delegation token;
4. close the hop with the verdict, the cost and what it answered.

If step 2 says no, step 3 does not happen and the reason is returned as is so the agent can
tell its user.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List

import httpx

logger = logging.getLogger(__name__)
# Caching the colleague list avoids asking the platform on every `tools/list`, which
# happens every turn. Short: a newly accepted relation has to show up on its own.
_CACHE_TTL_S = 60.0
_cache: Dict[str, Any] = {"ts": 0.0, "colegas": []}


def _slug(nombre: str) -> str:
    """Tool name from the colleague's name. Stable: if it changed between turns, the
    agent would see colleagues appear and disappear."""
    limpio = re.sub(r"[^a-z0-9]+", "_", (nombre or "").strip().lower()).strip("_")
    return limpio or "colega"


async def _colegas() -> List[Dict[str, Any]]:
    """This employee's ACTIVE colleagues, with their card. Source: the platform — which is
    who knows which relations are alive; asking our own database would mean having two
    truths for the same fact."""
    ahora = time.monotonic()
    if ahora - float(_cache["ts"]) < _CACHE_TTL_S:
        return _cache["colegas"]

    from app.config import AGENT_ID, BACKEND_API_SECRET, WAI_BACKEND_URL
    colegas: List[Dict[str, Any]] = []
    try:
        url = f"{(WAI_BACKEND_URL or '').rstrip('/')}/backend/agent-relations/"
        async with httpx.AsyncClient(timeout=10.0) as client:
            # `agent_id` and NOT `app_id`: the pod knows the id of the AGENT it runs, not
            # that of the deployed employee. The platform translates (it has the link); having
            # each employee translate it would guarantee that one day they diverge.
            r = await client.get(url, headers={"X-API-Secret": BACKEND_API_SECRET},
                                 params={"agent_id": AGENT_ID, "direction": "outgoing"})
            r.raise_for_status()
            for rel in r.json() or []:
                if rel.get("status") != "active":
                    continue
                colegas.append({
                    "relation_id": rel.get("id"),
                    "callee_app_id": rel.get("callee_app_id"),
                    "nombre": rel.get("callee_name") or "",
                    "slug": _slug(rel.get("callee_name") or ""),
                    "modes": rel.get("modes") or [],
                    "purpose": rel.get("purpose") or "",
                    # WHICH of the roles declared by the design this collaboration covers. It
                    # is the data that turns "do I have the ERP covered?" into an exact answer
                    # instead of a guess by text similarity.
                    "role": rel.get("role") or "",
                })
    except Exception:  # pragma: no cover — no platform, no colleagues (and it is said)
        logger.warning("no se pudo leer la lista de colegas", exc_info=True)
        return _cache["colegas"]

    _cache["ts"] = ahora
    _cache["colegas"] = colegas
    return colegas


async def missing_colleagues() -> List[Dict[str, Any]]:
    """Colleague ROLES this employee DECLARES it needs and does not have covered.

    It comes from the manifest (`agent_app.colleagues`), which is what travels when it is
    published to the marketplace: the package declares ROLES, and in each installation they
    are assigned to a concrete employee. If that assignment was not made —or the
    collaboration was revoked—, the employee has to KNOW it so it can say so, instead of
    discovering it in the middle of a conversation.
    """
    from app.config import AGENT_APP
    declarados = AGENT_APP.get("colleagues") or []
    if not isinstance(declarados, list) or not declarados:
        return []
    colegas = await _colegas()
    # Covered = there is an ACTIVE collaboration that says it covers THAT role. It used to be
    # guessed by looking for the role's text inside the purpose, and on top of that everything
    # was considered covered if there were as many collaborations as roles: two heuristics
    # that were too generous —an employee believed it had a role covered that nobody served
    # and found out mid-conversation—. Now the relation CARRIES the role (`role`), so it is
    # compared and that is it.
    papeles_cubiertos = {(c.get("role") or "").strip().lower() for c in colegas}
    papeles_cubiertos.discard("")
    faltan = []
    for d in declarados:
        if not isinstance(d, dict):
            continue
        papel = (d.get("role") or "").strip()
        if not papel:
            continue
        if papel.lower() not in papeles_cubiertos:
            faltan.append({"role": papel, "required": bool(d.get("required")),
                           "purpose": d.get("purpose") or ""})
    return faltan


async def colleagues_note() -> str:
    """WHO your colleagues are and how to talk to them. Empty if you have none.

    Without this, an employee with colleagues attached has them in front of it and does not
    use them: their tools appear in the list like any other —`empleado_algo_preguntar`— and
    nothing tells it that behind them there is a colleague WORTH asking before giving up or
    making up an answer. It is the positive half of `missing_colleagues_note`: one says what
    you cannot do, this one says what you can and with whom.

    It goes in the identity preamble —what ALL surfaces read— and not in the prompt of the
    tasks: a rule that only one surface sees produces an employee that behaves differently
    depending on where you talk to it, and that is not governable (we paid for it with
    AGO-136)."""
    colegas = await _colegas()
    if not colegas:
        return ""
    lineas = [
        "YOUR COLLEAGUES. You are not alone: these employees of the organisation serve you and "
        "you have them among your tools. When something falls in THEIR area, ask them "
        "BEFORE answering that you do not know or improvising:",
    ]
    for c in colegas:
        modos = c.get("modes") or []
        puede = []
        if "consultar" in modos:
            puede.append("`preguntar` (answers you with THEIR data)")
        if "encargar" in modos:
            puede.append("`encargar` (acts in THEIR systems, with THEIR permissions)")
        lineas.append(
            f"  · {c['nombre']} — for: {c.get('purpose') or 'collaborating'}. "
            f"You can: {', '.join(puede) or 'nothing yet'}."
        )
    lineas.append(
        "How to talk to them: their tools start with the colleague's name. Start "
        "with `presentate` if you do not know what it can solve for you — it returns its live "
        "card (which systems it reaches TODAY and what it can touch). ALWAYS tell it what you "
        "need it for and with which concrete data: the colleague answers on behalf of whoever "
        "asked you, applies ITS own permissions and may need approval from its manager. If "
        "it says no, report it with its reason; do not work around it on your own."
    )
    return "\n".join(lineas)


async def missing_colleagues_note() -> str:
    """Notice for the employee's instructions. Empty if none is missing.

    It is the "explicit degradation": without an assigned colleague the employee does NOT
    improvise or stay silent — it says which part of its work it cannot do and whom to tell."""
    faltan = await missing_colleagues()
    if not faltan:
        return ""
    lineas = [
        "NOTICE ABOUT YOUR COLLEAGUES: nobody has been assigned yet to cover these roles, so "
        "there are things you CANNOT do right now:",
    ]
    for f in faltan:
        marca = "ESSENTIAL" if f.get("required") else "optional"
        lineas.append(f"  · «{f['role']}» ({marca}). {f.get('purpose') or ''}".rstrip())
    lineas.append(
        "If you are asked for something in that area, SAY it clearly: you have no colleague "
        "assigned for that and it has to be configured in the employee network. Do not try it "
        "on your own or make up the answer."
    )
    return "\n".join(lineas)


