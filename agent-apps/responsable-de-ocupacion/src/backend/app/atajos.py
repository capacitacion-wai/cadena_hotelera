"""What is resolved with one call, is resolved with one call.

THE PROBLEM. Whatever you ask, today the same machinery starts: the whole conversation is
resent, the model is given the tool catalog, it looks for which one to use, thinks, calls
and writes. Measured in demo: a "which trips do I have?" is 22 seconds and $0.014. It was
resolved by querying one place.

WHAT THIS DOES. When the agent's definition DECLARES that a capability is resolved with one
call (`resuelve_con`), that request is answered without starting the agent: the tool is
called through the usual permission gate and the answer is written. Two seconds.

FOUR RULES that keep it honest, and none is optional:

1. **Only what is declared exists.** Nobody guesses what is trivial. A person says it in the
   Definition, it shows in its card and it goes into the seal. If one day it is removed, it
   disappears.
2. **Whoever recognizes does not execute, and whoever executes does not interpret.** The
   conversational layer (or, in chat, a small model) only says "this is that capability,
   with this data". The tool is called by the app, with `gate_error` — the SAME gate that
   governs the agent. A shortcut cannot do anything the agent could not.
3. **When in doubt, the normal path.** If an argument is missing, if the request leans on
   the conversation, if the `salvo_si` applies or if whoever recognizes is not sure: normal
   turn. It is not attempted and then corrected — that would be a fallback, and the branch
   that only runs when something fails is the one that fails the day it is needed.
4. **It is recorded.** Every shortcut answer leaves its capability in the turn's line. A
   shortcut that fires where it should not answers something else without anything
   screaming; the log is the only thing that makes it visible.

And a switch in Operations to turn it all off without deploying anything.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

import httpx
from sqlalchemy import select

from app.app_database import app_session
from app.app_models.runtime import AppSetting

logger = logging.getLogger(__name__)

CLAVE_ACTIVOS = "atajos_activos"

#: Cap on what is shown to whoever recognizes. A catalog of twenty capabilities in a small
#: model's prompt stops being cheap, which is precisely its only advantage.
_TOPE_CATALOGO = 12


async def activos() -> bool:
    """The Operations switch. On by default."""
    try:
        async with app_session() as s:
            v = ((await s.execute(
                select(AppSetting.value).where(AppSetting.key == CLAVE_ACTIVOS)
            )).scalar_one_or_none() or "").strip().lower()
    except Exception:  # noqa: BLE001
        return True
    return v not in ("0", "false", "no", "off")


def _definicion() -> dict:
    """The definition of THIS employee. Same source as the voice and the reception: the
    platform's if it has arrived, and if not the manifest's (see `conversacion.declaracion`).
    Three copies of this read were three places to fix the day it changed."""
    from app.conversacion import declaracion
    return declaracion()


def catalogo() -> list[dict[str, Any]]:
    """The capabilities that declare a shortcut: `{id, que, herramienta, argumentos, salvo_si}`.

    Empty = this employee declares none, and then there is none of this: no recognition,
    no extra call, no cost. It is the default state."""
    out: list[dict[str, Any]] = []
    for c in (_definicion().get("hago") or []):
        if not isinstance(c, dict):
            continue
        a = c.get("resuelve_con")
        if isinstance(a, str) and a.strip():
            a = {"herramienta": a.strip()}
        # THE WHOLE ROUTE as a shortcut (`resuelve_con: {"ruta": true}`, 12/10/2026): the same
        # contract as a one-call shortcut, with every call of the capability's route. The
        # platform has already worked out, when sealing, which data to extract from the request
        # (`argumentos`) and the conditions of the steps that are skipped (`no_vale_si`).
        if isinstance(a, dict) and a.get("ruta"):
            rondas = ((c.get("ruta") or {}).get("rondas") or []) if isinstance(c.get("ruta"), dict) else []
            if not rondas:
                continue
            salvo = "; ".join(x for x in [(a.get("salvo_si") or "").strip(),
                                          *[str(y).strip() for y in (a.get("no_vale_si") or [])]] if x)
            out.append({
                "id": str(c.get("id") or "").strip(),
                "que": str(c.get("que") or "").strip(),
                "herramienta": "",
                "ruta": rondas,
                "argumentos": [str(x).strip() for x in (a.get("argumentos") or []) if str(x).strip()],
                "salvo_si": salvo or None,
            })
            if len(out) >= _TOPE_CATALOGO:
                break
            continue
        if not isinstance(a, dict) or not (a.get("herramienta") or "").strip():
            continue
        out.append({
            "id": str(c.get("id") or "").strip(),
            "que": str(c.get("que") or "").strip(),
            "herramienta": str(a["herramienta"]).strip(),
            "argumentos": [str(x).strip() for x in (a.get("argumentos") or []) if str(x).strip()],
            "salvo_si": (a.get("salvo_si") or "").strip() or None,
        })
        if len(out) >= _TOPE_CATALOGO:
            break
    return [c for c in out if c["id"]]


def buscar(capacidad: str) -> Optional[dict[str, Any]]:
    cid = (capacidad or "").strip()
    if not cid:
        return None
    return next((c for c in catalogo() if c["id"] == cid), None)


def solo_los_declarados(atajo: dict[str, Any], argumentos: Any) -> dict[str, Any]:
    """The arguments the capability DECLARES, and not one more.

    Whoever recognizes the request is a model, and a model fills gaps: asked for the data of
    a capability that needs none, it invented `peticion` and the tool died with "got an
    unexpected keyword argument 'peticion'" — measured on 20/09/2026 with the shortcut
    already firing.

    The declaration rules: it says which data must be extracted from what the person says,
    so whatever is not there does not travel. It is deterministic and does not depend on the
    model behaving well."""
    dados = argumentos if isinstance(argumentos, dict) else {}
    declarados = atajo.get("argumentos") or []
    return {k: dados[k] for k in declarados if k in dados}


def faltan_argumentos(atajo: dict[str, Any], argumentos: Any) -> list[str]:
    """The ones the declaration asks for that have not arrived.

    A shortcut launched with a gap answers SOMETHING ELSE —the trips of the wrong person,
    the wrong month— and that is worse than taking twenty seconds. Without all of them,
    there is no shortcut."""
    dados = argumentos if isinstance(argumentos, dict) else {}
    return [a for a in atajo.get("argumentos") or []
            if dados.get(a) in (None, "", [], {})]


# ── Ejecutar ────────────────────────────────────────────────────────────────

async def ejecutar(atajo: dict[str, Any], argumentos: dict[str, Any],
                   run_id: int | None) -> dict[str, Any]:
    """Calls the declared tool, through the usual permission gate.

    TWO PLACES, and knowing it is half of the matter: the employee's own tools
    (`upsert_alert`, `remember_fact`…) live in `mcp_server.TOOLS`, and those of its
    CONNECTORS —which are almost always the ones a shortcut declares— live behind the MCP
    proxy. Looking only at the former is the failure measured on 20/09/2026: "the tool
    'wai-viajes_consultar_contexto_usuario' does not exist in this employee" about one that
    did exist, and the shortcut fell to the normal path on every question.

    `gate_error` is the SAME function that governs the agent when it calls that tool: a
    shortcut cannot do anything the agent could not do for that person. If the gate says
    no, that "no" is returned —it is a legitimate answer— and it is not retried another way.
    """
    if atajo.get("ruta"):
        return await _ejecutar_ruta(atajo, argumentos, run_id)
    from app.mcp_server import TOOLS, ToolCall

    nombre = atajo["herramienta"]
    handler = TOOLS.get(nombre)

    # EACH GATE IN ITS PLACE, and this was done wrong the first time. The employee's LOCAL
    # tools are governed by `gate_error` with `TOOL_MIN_ROLE`; those of a CONNECTOR are
    # governed by the proxy's write policy. Passing a connector one through `gate_error`
    # ALWAYS denies it —it is not declared in that table, and what is undeclared is denied—,
    # and the user reads "that capability is not authorized in this employee" about something
    # that is authorized. Measured on 20/09/2026 with the shortcut already firing.
    if handler is not None:
        from app.mcp_actor import gate_error, resolve_actor
        denegado = await gate_error(nombre, await resolve_actor(run_id))
        if denegado is not None:
            return {"ok": False, "denegado": denegado}
        call = ToolCall(run_id=run_id, step_idx=0, name=nombre,
                        arguments=dict(argumentos or {}), mode="chat")
        try:
            return {"ok": True, "resultado": await handler(call)}
        except Exception as exc:  # noqa: BLE001 — a failure of its own is an answer
            logger.warning("atajo %s: la herramienta local falló: %s", nombre, exc)
            return {"ok": False, "error": str(exc)}

    # From a connector: `{server}_{tool}`, split on the FIRST underscore —slugs carry
    # underscores inside, so splitting on the last one breaks them—.
    servidor, _, slug = nombre.partition("_")
    if not servidor or not slug:
        return {"ok": False, "error": f"la herramienta {nombre!r} no existe en este empleado"}
    # The CONNECTOR's gate: the same write policy that governs the agent when it calls that
    # tool. A shortcut is declared on queries, so normally it vetoes nothing; but if the
    # operator put the connector in read-only and someone declared a write shortcut, it
    # stops here — as the agent would be stopped.
    try:
        from app import mcp_policy
        veto = await mcp_policy.veto_for_call(servidor, slug, dict(argumentos or {}))
    except Exception:  # noqa: BLE001 — without a policy the gate is not opened: it lets it go
        veto = None                                    # to the normal path, which does apply it
    if veto:
        return {"ok": False, "denegado": {"error": veto}}
    # WHO IS ASKING travels with the call, the same as on the normal path. The agent passes
    # `run_token` and the proxy turns it into the person (`run_serving_user`) before calling
    # the connector; here it was called with nobody, so WAI Viajes answered as if nobody
    # were talking: "No information available about your trips" to someone who had five
    # (21/09/2026, with the agent listing them one turn later). The identity is derived
    # from the run —server-side, not from the model—, through the same gate as the proxy.
    from app.mcp_actor import run_serving_user
    persona = str(await run_serving_user(run_id) or "").strip().lower()
    try:
        from app.routes.mcp_proxy_routes import llamar_herramienta
        if _es_de_composio(servidor):
            # A COMPOSIO CONNECTOR GOES THROUGH ITS ROUTER, and in the router a toolkit tool
            # does NOT exist as such: only its meta-tools. Calling it by its name gave
            # "Tool GOOGLE_MAPS_GEOCODE_ADDRESS_WITH_QUERY not found" (24/09/2026, "take me
            # to the Prado"). It is executed through `COMPOSIO_MULTI_EXECUTE_TOOL`, which is
            # exactly what the agent does, and its result is unwrapped.
            crudo = await llamar_herramienta(
                servidor, "COMPOSIO_MULTI_EXECUTE_TOOL",
                {"tools": [{"tool_slug": slug, "arguments": dict(argumentos or {})}],
                 "thought": f"atajo {atajo.get('id') or slug}"},
                acting_user=persona)
            salida = _desenvolver_composio(crudo)
        else:
            salida = await llamar_herramienta(servidor, slug, dict(argumentos or {}),
                                              acting_user=persona)
    except Exception as exc:  # noqa: BLE001
        logger.warning("atajo %s: el conector %s falló: %s", nombre, servidor, exc)
        return {"ok": False, "error": str(exc)}
    fallo = _fallo_de(salida)
    if fallo:
        # AN ERROR IS AN ERROR, even if it comes inside a "good" answer. The MCP protocol
        # marks a tool's failures with `isError` INSIDE the result, not as a call error;
        # without looking at it, the shortcut wrote up the failure as if it were the data
        # ("The Prado Museum could not be found"). With this it falls to the normal path,
        # which is where a failure can be solved another way.
        logger.warning("atajo %s: la herramienta devolvió un error: %s", nombre, fallo[:300])
        return {"ok": False, "error": fallo}
    return {"ok": True, "resultado": salida}


# ── The whole route ─────────────────────────────────────────────────────────

def _json_de(resultado: Any) -> Any:
    """A tool's result as data: the JSON inside its MCP text if there is one, else the text."""
    texto = _texto_de(resultado) if isinstance(resultado, dict) else str(resultado or "")
    try:
        return json.loads(texto)
    except (ValueError, TypeError):
        return texto


def _pasos_del_campo(campo: str) -> Optional[list[tuple[str, str]]]:
    """`places[].displayName.text` → [("k","places"), ("i","*"), ("k","displayName"), ("k","text")]."""
    import re
    pasos: list[tuple[str, str]] = []
    for parte in [p for p in (campo or "").split(".") if p]:
        m = re.fullmatch(r"([^\[\]]*)((?:\[\d*\])*)", parte)
        if not m:
            return None
        if m.group(1):
            pasos.append(("k", m.group(1)))
        pasos += [("i", i or "*") for i in re.findall(r"\[(\d*)\]", m.group(2))]
    return pasos


def valor_en(datos: Any, campo: str) -> Any:
    """`results[0].location.lat` inside `datos`; `places[].displayName.text` = that field of EVERY
    element (a flat list). `None` if nothing is there — then the route cannot go on without the
    agent, and the turn takes the normal path. The path is written inside the TOOL's result (the
    Composio wrapper is already removed here; the platform refuses a path that starts with it)."""
    pasos = _pasos_del_campo(campo)
    if pasos is None:
        return None

    def anda(x: Any, n: int) -> Any:
        if n == len(pasos):
            return x
        tipo, v = pasos[n]
        if tipo == "k":
            return anda(x[v], n + 1) if isinstance(x, dict) and v in x else None
        if not isinstance(x, list):
            return None
        if v != "*":
            return anda(x[int(v)], n + 1) if int(v) < len(x) else None
        # ALL the elements: the rest of the path on each, the ones that have it, flattened.
        vals: list[Any] = []
        for e in x:
            r = anda(e, n + 1)
            if r is None:
                continue
            vals.extend(r if isinstance(r, list) and any(t == "i" and w == "*" for t, w in pasos[n + 1:]) else [r])
        return vals or None

    return anda(datos, 0)


async def _ejecutar_ruta(atajo: dict[str, Any], argumentos: dict[str, Any],
                         run_id: int | None) -> dict[str, Any]:
    """Runs the capability's route WITHOUT the agent: round by round, the calls of a round at
    the same time, each through `ejecutar` (the same gates as the agent), and the data of each
    call from the request (`argumentos`) or from an earlier call's result (`{de, campo}`).

    Conditional calls (`solo_si`) are not made: the recognizer was told that the shortcut does
    not apply when their condition holds. ANY doubt —a value that is not there, a call that
    fails— returns not-ok, and the turn takes the normal path (rule 3); a denial is returned as
    such (it is an answer)."""
    import asyncio
    resultados: dict[str, Any] = {}
    for i, ronda in enumerate(atajo.get("ruta") or [], 1):
        llamadas = [ll for ll in (ronda.get("llamadas") or []) if isinstance(ll, dict)
                    and ll.get("herramienta") and not ll.get("solo_si")]
        preparadas = []
        for ll in llamadas:
            args = dict(ll.get("fijos") or {})
            for k, origen in (ll.get("rellena") or {}).items():
                if isinstance(origen, dict):
                    v = valor_en(resultados.get(origen.get("de")), str(origen.get("campo") or ""))
                else:
                    v = (argumentos or {}).get(k)
                if v in (None, "", [], {}):
                    return {"ok": False, "error": f"ruta: sin valor para `{k}` en la ronda {i}"}
                args[k] = v
            preparadas.append((ll["herramienta"], args))
        salidas = await asyncio.gather(*(
            ejecutar({"herramienta": h, "id": f"{atajo.get('id')}#{i}"}, a, run_id)
            for h, a in preparadas))
        for (h, _a), salida in zip(preparadas, salidas):
            if not salida.get("ok"):
                return salida
            resultados[h] = _json_de(salida.get("resultado"))
    return {"ok": True, "resultado": resultados}


def _es_de_composio(servidor: str) -> bool:
    try:
        from app.mcp_runtime import composio_connectors
        return servidor in {(c.get("name") or "").strip() for c in composio_connectors()}
    except Exception:  # noqa: BLE001 — without a manifest there is no Composio
        return False


def _texto_de(resultado: Any) -> str:
    """The text of an MCP result (`content[].text`), joined."""
    if not isinstance(resultado, dict):
        return ""
    return "\n".join(str(c.get("text") or "") for c in (resultado.get("content") or [])
                     if isinstance(c, dict))


def _fallo_de(salida: Any) -> str:
    """The reason if the output is a failure; empty string if it is a result."""
    if not isinstance(salida, dict):
        return ""
    if salida.get("error"):
        return str(salida["error"])
    if salida.get("isError"):
        return _texto_de(salida) or "la herramienta devolvió un error"
    return ""


def _desenvolver_composio(crudo: Any) -> Any:
    """From the `COMPOSIO_MULTI_EXECUTE_TOOL` answer to THE tool's result.

    Composio wraps: `content[0].text` is a JSON with `data.results[0].response`, and there
    `successful`/`data`/`error`. It is returned as an MCP result so the rest (failure or
    write-up) treats it like any connector's."""
    if _fallo_de(crudo) and not _texto_de(crudo):
        return crudo
    try:
        d = json.loads(_texto_de(crudo) or "{}")
        r = (((d.get("data") or {}).get("results") or [{}])[0].get("response")) or {}
    except (ValueError, AttributeError, IndexError):
        return {"isError": True, "content": [{"type": "text", "text": _texto_de(crudo)[:500]}]}
    if not r.get("successful"):
        motivo = r.get("error") or (r.get("data") or {}).get("message") or d.get("error")
        return {"isError": True, "content": [{"type": "text", "text": str(motivo or "falló")}]}
    return {"content": [{"type": "text",
                         "text": json.dumps(r.get("data"), ensure_ascii=False)}]}


# ── Recognize and write up (small model, chat only) ─────────────────────────
#
# By voice it is not needed: the conversational layer is already reading and names the
# capability itself. This is for written chat, which has no conversational layer in front.

async def _modelito(mensajes: list[dict], *, max_tokens: int = 400) -> str:
    """A short call to the tenant's small model. Returns text or ``""``."""
    from app.config import TRANSCRIPTION_API_KEY, TRANSCRIPTION_BASE_URL
    modelo = await _modelo_pequeno()
    if not TRANSCRIPTION_API_KEY or not modelo:
        return ""
    try:
        async with httpx.AsyncClient(timeout=20) as cli:
            r = await cli.post(
                f"{TRANSCRIPTION_BASE_URL.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {TRANSCRIPTION_API_KEY}"},
                json={"model": modelo, "messages": mensajes,
                      "max_completion_tokens": max_tokens},
            )
        if r.status_code >= 400:
            logger.debug("atajos: el modelo pequeño contestó %s", r.status_code)
            return ""
        return ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    except Exception:  # noqa: BLE001 — without recognition it goes the normal path
        logger.debug("atajos: no se pudo llamar al modelo pequeño", exc_info=True)
        return ""


async def _modelo_pequeno() -> str:
    """Which model recognizes a request.

    ONE SINGLE PLACE, and that is why it is like this: before, this read
    `AGENT_SMALL_MODEL`/`GENAI_MODEL`, two variables the deployer does NOT inject into the
    employee's pod. Result measured on 20/09/2026: it returned `''`, the call was skipped
    and by VOICE a shortcut was never recognized — while in writing it worked, because the
    reception uses the employee's model on its own. One path with a model and another
    without it, for the same decision.

    `AGENT_SMALL_MODEL` is still respected: it is the way for an operator to put something
    cheaper here if one day it pays off. Empty —the norm— the employee's is used, the same
    that gives it a voice and serves the written conversation."""
    import os
    m = (os.environ.get("AGENT_SMALL_MODEL") or "").strip()
    if m:
        return m.split("/", 1)[-1]
    from app.recepcion import _modelo_del_operador
    return await _modelo_del_operador()


_RECONOCER = (
    "You are a classifier. You do NOT answer anyone: you decide whether a request is EXACTLY "
    "one of the capabilities in the list and extract its data.\n\n"
    "Reply ONLY with a JSON: {\"capacidad\": \"<id>\", \"argumentos\": {...}} or "
    "{\"capacidad\": null}.\n\n"
    "Return null if: the request asks to compare, decide, recommend or plan; if it "
    "leans on what was said before (\"and the next one\", \"the same but…\", in any "
    "language); if any of the data the capability asks for is missing; or if you have any "
    "doubt. Erring towards null costs nothing; erring towards yes answers something else."
)


async def reconocer(texto: str) -> dict[str, Any]:
    """Is this request a capability with a shortcut? Returns `{capacidad, argumentos}`.

    It costs about 300 ms and a ten-thousandth of a dollar, and it is only paid when the
    employee declares some shortcut. Against the 22 seconds of the full turn, it pays off
    even if it fails half the time."""
    cat = catalogo()
    if not cat or not (texto or "").strip():
        return {"capacidad": None}
    lista = [{"id": c["id"], "es": c["que"], "datos": c["argumentos"],
              **({"no_vale_si": c["salvo_si"]} if c["salvo_si"] else {})} for c in cat]
    salida = await _modelito([
        {"role": "system", "content": _RECONOCER},
        {"role": "user", "content": json.dumps(
            {"capacidades": lista, "peticion": texto[:2000]}, ensure_ascii=False)},
    ], max_tokens=300)
    try:
        d = json.loads((salida or "").strip().strip("`").removeprefix("json").strip())
    except Exception:  # noqa: BLE001
        return {"capacidad": None}
    cid = d.get("capacidad") if isinstance(d, dict) else None
    if not cid or not isinstance(cid, str):
        return {"capacidad": None}
    return {"capacidad": cid.strip(),
            "argumentos": d.get("argumentos") if isinstance(d.get("argumentos"), dict) else {}}


async def redactar(texto: str, resultado: Any, idioma: str = "es") -> str:
    """Turns what the tool returned into an answer for a person.

    Writing up is NOT deciding: it cannot add anything that is not in the result. If the
    small model is not available, `""` is returned and the caller goes the normal path —
    rather than showing a JSON."""
    crudo = resultado if isinstance(resultado, str) else json.dumps(
        resultado, ensure_ascii=False, default=str)
    salida = await _modelito([
        {"role": "system", "content":
            "Write the answer to the person using ONLY the data below. Do not add "
            "anything that is not there, do not assume and do not apologise. Brief and "
            f"natural, in {'Spanish' if idioma != 'en' else 'English'}. If the data is "
            "empty, say so naturally.\n\n"
            # A tool's raw data is full of ids, and saying them is showing the kitchen:
            # nobody asks for "trip number 4".
            "DO NOT SAY IDENTIFIERS (\"id\", \"ID 4\", internal references): they mean "
            "nothing to whoever reads you. Name things by their name; if they were needed "
            "to tell two identical ones apart, use the dates or the place."},
        {"role": "user", "content": f"Request: {texto[:500]}\n\nData: {crudo[:6000]}"},
    ], max_tokens=700)
    return (salida or "").strip()
