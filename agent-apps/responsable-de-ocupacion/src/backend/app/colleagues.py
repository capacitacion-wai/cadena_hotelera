"""Employees that talk to each other — the half that lives inside the employee.

The other half (who MAY talk to whom) belongs to the platform: table `agent_relations`,
API `/backend/agent-relations`. Here is what happens at the moment of talking, which is
where what really matters is decided:

**The relation authorizes the EMPLOYEE; the permission is decided by the USER.**
An active relation A→B existing does not mean anyone can use it. When A calls B, the call
carries inside it the user who originated it, and B resolves them in ITS directory and
applies ITS roles, ITS write policy and ITS approvers. Both have to say yes. Without this
the callee's governance evaporates as soon as someone asks it through a colleague, which is
the easiest back door imaginable.

Three pieces:

* **The card** (`colleague_card`) — what B publishes about itself so A knows when to call it
  and with what. It is GENERATED from the manifest and from the really connected connectors;
  it is not written by hand, because a hand-written card lies within two months.
* **The delegation token** — who travels in the call. HS256 with the secret the employee
  already shares with the platform; short-lived and tied to the chain.
* **The client** (`ask_colleague`) — what A uses: it opens the hop in the platform's ledger,
  calls, and closes it with its verdict. If the platform says no, A finds out BEFORE spending
  and with a reason it can repeat to its user as is.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.config import (
    AGENT_APP, AGENT_ID, BACKEND_API_SECRET, TENANT_NAME, WAI_BACKEND_URL,
)

# Name of this app. `AGENT_APP` comes from the manifest, which is the only source that
# exists both in the pod and in the published package.
APP_NAME = str(AGENT_APP.get("name") or AGENT_APP.get("slug") or "").strip()

logger = logging.getLogger(__name__)

# Lifetime of the delegation token. Short on purpose: it only has to survive ONE call. If
# it leaks, it expires before anyone has time to use it twice.
DELEGATION_TTL_S = 300
MODE_CONSULT = "consultar"
MODE_DELEGATE = "encargar"


# ── Delegation token ──────────────────────────────────────────────────────────────────
def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _secret() -> bytes:
    """The SAME secret this employee already uses to talk to the platform.

    No new mechanism is introduced: of the four that could exist, the one invented today is
    the one nobody knows which applies in six months' time."""
    return (BACKEND_API_SECRET or "").encode()


def mint_delegation(user_email: str, chain_id: str, depth: int,
                    origin_project_id: Optional[int] = None) -> str:
    """Signs who travels in this call. `sub` is the USER, not the employee: it is the
    data the whole permission hangs from on the other side."""
    ahora = int(time.time())
    payload = {
        "sub": (user_email or "").strip().lower(),
        "on_behalf_of_app": APP_NAME,
        "agent_id": AGENT_ID,
        "tenant": TENANT_NAME,
        "chain_id": chain_id,
        "depth": int(depth),
        "origin_project_id": origin_project_id,
        "iat": ahora,
        "exp": ahora + DELEGATION_TTL_S,
    }
    cabecera = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    cuerpo = _b64(json.dumps(payload, separators=(",", ":")).encode())
    firma = _b64(hmac.new(_secret(), f"{cabecera}.{cuerpo}".encode(), hashlib.sha256).digest())
    return f"{cabecera}.{cuerpo}.{firma}"


def verify_delegation(token: str) -> Dict[str, Any]:
    """Returns the payload or raises ValueError with a readable reason.

    The signature is checked BEFORE the expiry on purpose: a tampered token does not
    deserve to be told whether it had also expired."""
    try:
        cabecera, cuerpo, firma = (token or "").split(".")
    except ValueError:
        raise ValueError("The delegation token does not have the expected format.")
    esperada = _b64(hmac.new(_secret(), f"{cabecera}.{cuerpo}".encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(esperada, firma):
        raise ValueError("The delegation token's signature is not valid.")
    payload = json.loads(_unb64(cuerpo))
    if int(payload.get("exp") or 0) < int(time.time()):
        raise ValueError("The delegation token has expired (it lasts 5 minutes).")
    if not payload.get("sub"):
        raise ValueError("The token does not say on whose behalf the call is made.")
    return payload


# ── The card: what B publishes about itself ───────────────────────────────────────────
def _texto_loc(valor: Any, lang: str = "es") -> str:
    """Manifest texts are bilingual (`{"es": …, "en": …}`) or plain strings. Always
    treating them as a string is what made the ROLE disappear from the card: it was a
    dictionary and it was silently discarded."""
    if isinstance(valor, dict):
        return str(valor.get(lang) or valor.get("es") or valor.get("en") or "").strip()
    return str(valor or "").strip()


def _presentacion_legible(p: Any) -> Any:
    """The definition, in the shape a colleague wants to read it.

    It accepts both shapes because both exist. The structured one is returned whole —the
    caller can reason about `hago` and `necesita` instead of about a paragraph— and the free
    text is returned as it was."""
    if isinstance(p, dict):
        caps = [c for c in (p.get("hago") or []) if isinstance(c, dict) and c.get("que")]
        return {
            "soy": str(p.get("soy") or ""),
            "hago": [
                {"que": c.get("que"),
                 # The detail travels: whoever decides whether to call you needs to know
                 # when you apply and what you leave out, not just the headline.
                 "como": str(c.get("detalle") or "")[:1200] or None,
                 "necesita": c.get("necesita"),
                 # Where this capability ends: a caller deciding whether to delegate needs
                 # it as much as the headline.
                 "no_hace": [str(x) for x in (c.get("no_hace") or []) if str(x).strip()],
                 # The examples go ATTACHED to their capability, which is where they come
                 # from: a loose list forces the reader to guess which one each belongs to.
                 "asi_se_pide": [str(x) for x in (c.get("ejemplos") or [])][:3],
                 "necesita_aprobacion": bool(c.get("aprobacion"))}
                for c in caps
            ],
            "no_hago": [str(x) for x in (p.get("no_hago") or [])],
        }
    return _texto_loc(p)


async def colleague_card() -> Dict[str, Any]:
    """Card of THIS employee, so a colleague knows when to call it.

    Two halves that do not mix:

    * what it IS — its `presentation`, written when designing it: what it exists for, when
      to call it, what data it needs and what it does NOT do. It is the only thing that
      cannot be derived from anything;
    * what it HAS now — really connected connectors with their policy, and the entities it
      manages. This is derived live on purpose: written by hand it ages and ends up
      promising a system that was taken away.
    """
    from app.config import APP_META

    meta = APP_META if isinstance(APP_META, dict) else {}
    agent_app = meta.get("agent_app") or {}
    persona = agent_app.get("persona") or {}

    conectados: List[str] = []
    try:
        # ALL the systems of the manifest, not just the ones to connect by hand: the
        # Connectors screen excludes WAI's native ones (`wai-odoo`, `composio-odoo`) because
        # there is nothing to plug in, and with that same calculation the Odoo Employee
        # introduced itself saying it reaches no system. A colleague decides whether to call
        # you by what you REACH. The policy is read live, connector by connector.
        from app import mcp_policy
        from app.mcp_runtime import sistemas_de_negocio
        for c in sistemas_de_negocio():
            nombre_c = (c.get("display_name") or c.get("name") or "").strip()
            if not nombre_c:
                continue
            politica = await mcp_policy.effective_policy((c.get("name") or "").strip())
            if politica == getattr(mcp_policy, "POLICY_READ_ONLY", "read_only"):
                conectados.append(f"{nombre_c} (solo consulta)")
            elif politica == getattr(mcp_policy, "POLICY_APPROVAL", "write_with_approval"):
                conectados.append(f"{nombre_c} (escribe con tu visto bueno)")
            else:
                conectados.append(nombre_c)
    except Exception:  # pragma: no cover — the card can never bring a call down
        logger.debug("no se pudieron leer los conectores para la ficha", exc_info=True)

    gestiona: List[str] = []
    for e in (agent_app.get("domain_entities") or []):
        etiqueta = _texto_loc(e.get("label") or e.get("slug"))
        if etiqueta:
            gestiona.append(etiqueta)

    nombre = (_texto_loc(persona.get("name"))
              or _texto_loc(agent_app.get("agent_display_name"))
              or _texto_loc(meta.get("display_name"))
              or APP_NAME)

    return {
        "app_name": APP_NAME,
        "agent_id": AGENT_ID,
        "nombre": nombre,
        "rol": _texto_loc(persona.get("role")),
        # The DEFINITION. Since 12.14.0 it can come STRUCTURED —what it does, what each thing
        # needs, what it does not do— and then it is served as is: a colleague deciding
        # whether to call you needs to know WHAT you are for, not just what you are called.
        # The old free text is still accepted and read as `soy`.
        "presentacion": _presentacion_legible(agent_app.get("presentation")),
        "que_resuelve": _texto_loc(agent_app.get("description") or meta.get("description")),
        "sistemas": conectados,
        "gestiona": gestiona[:8],
        "modos": [MODE_CONSULT, MODE_DELEGATE],
        "nota_escritura": (
            "If you ask it to do something that writes to a system, ITS policy and ITS "
            "approvals apply: it may execute it or leave it parked awaiting sign-off."
        ),
    }


def _presentacion_en_prosa(p: Any) -> str:
    """The presentation as a model reads it, whatever shape it comes in.

    `_presentacion_legible` delivers the whole STRUCTURED definition (soy / hago / no hago)
    for whoever reasons about it; here it is written as prose. Without this, a structured
    card went into the lines as is and the final `strip()` brought `/mcp/colleague` down
    with a 500: no colleague could consult that employee."""
    if not p:
        return ""
    if not isinstance(p, dict):
        return str(p).strip()
    partes: List[str] = []
    soy = str(p.get("soy") or "").strip()
    if soy:
        partes.append(soy)
    hago = [c for c in (p.get("hago") or []) if isinstance(c, dict) and c.get("que")]
    if hago:
        partes.append("I do:")
        for c in hago:
            linea = f"- {str(c['que']).strip()}"
            if c.get("como"):
                linea += f" — {str(c['como']).strip()}"
            if c.get("necesita_aprobacion"):
                linea += " (asks for approval before doing it)"
            partes.append(linea)
            ejemplos = [str(x).strip() for x in (c.get("asi_se_pide") or []) if str(x).strip()]
            if ejemplos:
                partes.append("  How to ask: " + " · ".join(f"«{e}»" for e in ejemplos))
    no_hago = [str(x).strip() for x in (p.get("no_hago") or []) if str(x).strip()]
    if no_hago:
        partes.append("I do not: " + "; ".join(no_hago) + ".")
    return "\n".join(partes)


def card_to_text(card: Dict[str, Any]) -> str:
    """The card in prose, which is how a model reads it.

    Deliberate order: first who it is and what it says about itself —what decides WHETHER you
    call it— and then what it reaches and manages, which is what decides HOW you ask. And no
    empty lines: a card with gaps (`**  **`, a bare "Resuelve:") reads like an employee with
    nothing to offer, which is exactly what happened the first time it was tried."""
    nombre = (card.get("nombre") or card.get("app_name") or "").strip()
    rol = (card.get("rol") or "").strip()
    lineas: List[str] = []
    if nombre and rol:
        lineas.append(f"**{nombre}** — {rol}")
    elif nombre:
        lineas.append(f"**{nombre}**")
    presentacion = _presentacion_en_prosa(card.get("presentacion"))
    if presentacion:
        lineas.append(presentacion)
    elif card.get("que_resuelve"):
        # Without a written presentation the description is used and IT IS SAID that it is
        # missing: that is what gets someone to write it, instead of living with a poor card forever.
        lineas.append(card["que_resuelve"])
        lineas.append("(This colleague has not written its presentation yet; ask its "
                      "manager for it to know when it is worth calling.)")
    if card.get("sistemas"):
        lineas.append("Reaches: " + ", ".join(card["sistemas"]) + ".")
    if card.get("gestiona"):
        lineas.append("Manages: " + ", ".join(card["gestiona"]) + ".")
    if card.get("nota_escritura"):
        lineas.append(card["nota_escritura"])
    return "\n".join(l for l in lineas if l.strip())


# ── Resolve the user in OUR directory (the callee's side) ─────────────────────────────
async def resolve_caller_user(email: str) -> Tuple[Optional[Any], Optional[str]]:
    """Who is this person HERE? Returns `(user, rejection_reason)`.

    No alternative paths: if they are not in our directory, it is SAID and it stops. They are
    not served as a guest nor does it fall back to the system user — an employee that answers
    whoever it does not know is an employee without permissions, and on top of that it does
    not show.
    """
    from sqlalchemy import select
    from app.app_database import app_session
    from app.app_models.users import AppUser

    limpio = (email or "").strip().lower()
    if not limpio:
        return None, "The call does not say on whose behalf it is made."
    async with app_session() as s:
        fila = (await s.execute(
            select(AppUser).where(AppUser.email == limpio)
        )).scalars().first()
        if not fila or not getattr(fila, "active", 1):
            admins = (await s.execute(
                select(AppUser.email).where(AppUser.role == "admin", AppUser.active == 1).limit(3)
            )).scalars().all()
            a_quien = (" They can ask for access from: " + ", ".join(a for a in admins if a)) if admins else ""
            return None, (
                f"«{limpio}» is not in my directory, so I cannot serve them: my "
                f"permissions are applied per person and I do not know who they are.{a_quien}"
            )
        return fila, None


# ── The client: what A uses to talk to B ─────────────────────────────────────────────
def new_chain_id() -> str:
    return uuid.uuid4().hex


async def _post_platform(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{(WAI_BACKEND_URL or '').rstrip('/')}/backend/agent-relations{path}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(url, headers={"X-API-Secret": BACKEND_API_SECRET}, json=body)
        r.raise_for_status()
        return r.json()


async def open_hop(callee_app_id: int, on_behalf_of: str, mode: str, chain_id: str,
                   depth: int, question: str = "",
                   origin_project_id: Optional[int] = None) -> Dict[str, Any]:
    """Opens the hop in the platform's ledger BEFORE talking.

    The platform checks relation, mode, depth, cycle and caps, and returns `allowed` with its
    reason. Asking before spending is the whole point of this: if the relation is paused or
    the chain bites its own tail, we never get to pay for the turn.
    """
    return await _post_platform("/calls/open", {
        "chain_id": chain_id,
        "depth": int(depth),
        # Asymmetric on purpose, and better not to "fix" it: about itself the pod only
        # knows its AGENT id (the platform translates it to the deployed employee); about
        # the colleague, instead, it already received the EMPLOYEE id in the relations list.
        # Each end sends what it really knows, instead of guessing what it does not.
        "caller_agent_id": int(AGENT_ID or 0),
        "callee_app_id": int(callee_app_id),
        "on_behalf_of": on_behalf_of,
        "mode": mode,
        "question": (question or "")[:4000],
        "origin_project_id": origin_project_id,
    })


async def close_hop(call_id: int, status: str, reason: str = "", answer: str = "",
                    cost_usd: Optional[float] = None,
                    duration_ms: Optional[int] = None) -> None:
    """Closes the hop with its verdict. Best-effort: losing the close cannot bring down the
    answer the user is already waiting for — but it warns, because a hop that stays
    `running` is, by itself, the symptom that something got cut."""
    try:
        await _post_platform(f"/calls/{int(call_id)}/close", {
            "status": status,
            "reason": reason or None,
            "answer": (answer or "")[:8000] or None,
            "cost_usd": cost_usd,
            "duration_ms": duration_ms,
        })
    except Exception:  # pragma: no cover
        logger.warning("no se pudo cerrar el salto %s de la cadena", call_id, exc_info=True)
