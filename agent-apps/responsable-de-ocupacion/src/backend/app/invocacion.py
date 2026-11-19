"""Invoking a CAPABILITY by API: arguments in, a VALIDATED structured output out (14.23.0).

COPIED VERBATIM by ``wai-agent-app-builder`` into every generated agent-app. INVARIANT (imported
by ``routes/agent_routes.py``, which is invariant: registered in ``INVARIANT_FILES``).

WHY. An application that called this employee «by API» only had the chat: text in, text out. On
09/10/2026 the suppliers portal of COBSA had to paste a file sample into a free message and
describe the JSON it wanted in words, and the employee answered with a question («which field is
PVR without VAT?»). A program cannot answer questions nor read prose. So a capability of the
Definition can declare a CONTRACT (`hago[].entrada` / `hago[].salida`, a closed JSON Schema subset
checked by the platform when sealing) and `POST /api/agent/invocar` honours it:

  1. the arguments are validated against `entrada` BEFORE a turn is spent (400 with the errors);
  2. one turn of the agent runs that capability, as the person, with an instruction that forbids
     asking and asks for ONLY the JSON (what is doubtful goes INSIDE the output, as the schema says);
  3. the JSON is extracted from the final answer and validated against `salida`; if it does not
     conform, ONE retry carrying the validation errors; if it still does not, 422 SALIDA_NO_CUMPLE.

Everything here is pure (no I/O): the route in ``agent_routes.py`` runs the turn through the same
machinery as the chat. The schema subset is the platform's (`contrato_de_capacidad.py`): what is
not checked is not accepted there, so a schema reaching here only uses what ``validar`` checks.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

#: The stable codes of the route (what the application reads in `detail.code`).
CAPACIDAD_NO_EXISTE = "CAPACIDAD_NO_EXISTE"
CAPACIDAD_SIN_SALIDA = "CAPACIDAD_SIN_SALIDA"
ENTRADA_NO_VALIDA = "ENTRADA_NO_VALIDA"
SALIDA_NO_CUMPLE = "SALIDA_NO_CUMPLE"
INVOCACION_FALLIDA = "INVOCACION_FALLIDA"
PRESUPUESTO_AGOTADO = "PRESUPUESTO_AGOTADO"

#: How many errors travel back (to the agent in the retry, to the application in the 422). Ten
#: errors already say what is wrong; two hundred are noise that costs tokens.
MAX_ERRORES = 10
#: An answer longer than this is not parsed: the employee is asked for a JSON, not a novel.
_MAX_TEXTO = 400_000


def capacidad(cid: str) -> Optional[dict[str, Any]]:
    """The capability `cid` of THIS employee's Definition (same source as the shortcuts and the
    reception: `conversacion.declaracion`), or None."""
    from app.conversacion import declaracion
    buscada = (cid or "").strip()
    if not buscada:
        return None
    for c in (declaracion().get("hago") or []):
        if isinstance(c, dict) and str(c.get("id") or "").strip() == buscada:
            return c
    return None


# ── The JSON Schema subset ──────────────────────────────────────────────────────────────────────

def _es_del_tipo(valor: Any, tipo: str) -> bool:
    if tipo == "string":
        return isinstance(valor, str)
    if tipo == "boolean":
        return isinstance(valor, bool)
    if tipo == "integer":
        # A bool IS an int in Python; in JSON it is not. And 3.0 is an integer in JSON Schema.
        return (isinstance(valor, int) and not isinstance(valor, bool)) or (
            isinstance(valor, float) and valor.is_integer())
    if tipo == "number":
        return isinstance(valor, (int, float)) and not isinstance(valor, bool)
    if tipo == "array":
        return isinstance(valor, list)
    if tipo == "object":
        return isinstance(valor, dict)
    if tipo == "null":
        return valor is None
    return False


def _nombre_de_tipo(valor: Any) -> str:
    if valor is None:
        return "null"
    if isinstance(valor, bool):
        return "boolean"
    if isinstance(valor, int):
        return "integer"
    if isinstance(valor, float):
        return "number"
    if isinstance(valor, str):
        return "string"
    if isinstance(valor, list):
        return "array"
    if isinstance(valor, dict):
        return "object"
    return type(valor).__name__


def validar(valor: Any, esquema: Any, ruta: str = "$") -> list[str]:
    """The errors of `valor` against `esquema` (empty = it conforms), each with WHERE (`$.a[2].b`).

    Only the subset the platform admits: type (one or a list, `null` included), properties,
    required, additionalProperties (bool or schema), items, enum. Anything else in the schema was
    refused when sealing, so it cannot reach here."""
    if not isinstance(esquema, dict):
        return []
    errores: list[str] = []
    t = esquema.get("type")
    tipos = [t] if isinstance(t, str) else list(t) if isinstance(t, list) else []
    if tipos and not any(_es_del_tipo(valor, x) for x in tipos):
        return [f"{ruta}: expected {' or '.join(tipos)}, got {_nombre_de_tipo(valor)}"]
    if "enum" in esquema and isinstance(esquema["enum"], list) and valor not in esquema["enum"]:
        errores.append(f"{ruta}: must be one of {json.dumps(esquema['enum'], ensure_ascii=False)}")
    if isinstance(valor, dict):
        props = esquema.get("properties") if isinstance(esquema.get("properties"), dict) else {}
        for k in (esquema.get("required") or []):
            if k not in valor:
                errores.append(f"{ruta}: missing required property `{k}`")
        extra = esquema.get("additionalProperties", True)
        for k, v in valor.items():
            if k in props:
                errores += validar(v, props[k], f"{ruta}.{k}")
            elif extra is False:
                errores.append(f"{ruta}: property `{k}` is not allowed")
            elif isinstance(extra, dict):
                errores += validar(v, extra, f"{ruta}.{k}")
    if isinstance(valor, list) and isinstance(esquema.get("items"), dict):
        for i, v in enumerate(valor):
            errores += validar(v, esquema["items"], f"{ruta}[{i}]")
    return errores


# ── Getting the JSON out of the answer ──────────────────────────────────────────────────────────

_VALLA = re.compile(r"```(?:json|JSON)?\s*\n(.*?)\n?```", re.DOTALL)


def extraer_json(texto: str) -> tuple[bool, Any]:
    """`(True, value)` with the JSON the agent answered, or `(False, why)`.

    The agent is told to answer ONLY the JSON, and usually does; but a turn's text also carries
    what it said while working («I'll open the file…»), and sometimes it fences the JSON. So, in
    this order: the whole text; the LAST fenced block that parses; the LAST top-level JSON object
    or array in the text. The last one because the answer comes at the end — a JSON quoted
    earlier (a tool result it commented on) is not the answer."""
    t = (texto or "").strip()
    if not t:
        return False, "the answer is empty"
    if len(t) > _MAX_TEXTO:
        return False, f"the answer is longer than {_MAX_TEXTO} characters"
    try:
        return True, json.loads(t)
    except ValueError:
        pass
    for bloque in reversed(_VALLA.findall(t)):
        try:
            return True, json.loads(bloque.strip())
        except ValueError:
            continue
    dec = json.JSONDecoder()
    ultimo: tuple[bool, Any] = (False, "the answer contains no JSON object or array")
    i = 0
    while i < len(t):
        if t[i] in "{[":
            try:
                valor, fin = dec.raw_decode(t, i)
            except ValueError:
                i += 1
                continue
            ultimo = (True, valor)
            i = fin        # what is INSIDE a value already parsed is not another candidate
            continue
        i += 1
    return ultimo


# ── What the agent reads (English: docs/idioma-de-los-agentes.md) ───────────────────────────────

def _idioma() -> str:
    from app.conversacion import declaracion
    return str(declaracion().get("idioma_principal") or "").strip()


#: The caller's REJECTION of a previous output (04/11/2026): `{motivos: [{elemento, motivo}], resultado}`. The caller
#: validates what points back at its input (citations, ids) and the schema cannot: with this it asks ONE correction
#: (docs/aplicaciones-que-llaman-a-empleados.md). Bounded, because it travels into the model's turn.
RECHAZO_MOTIVOS_MAX = 20
RECHAZO_ELEMENTO_MAX = 200
RECHAZO_MOTIVO_MAX = 500
RECHAZO_RESULTADO_MAX_BYTES = 64 * 1024


def validar_rechazo_previo(valor: Any) -> dict[str, Any] | None:
    """The caller's `rechazo_previo`, checked, or None if it did not send one. Raises ValueError saying what is wrong."""
    if valor is None:
        return None
    if not isinstance(valor, dict):
        raise ValueError("rechazo_previo is an object: {motivos: [{elemento, motivo}], resultado}")
    motivos = valor.get("motivos")
    if not isinstance(motivos, list) or not motivos or len(motivos) > RECHAZO_MOTIVOS_MAX:
        raise ValueError(f"rechazo_previo.motivos is a list of 1 to {RECHAZO_MOTIVOS_MAX} reasons")
    limpios = []
    for m in motivos:
        if not isinstance(m, dict) or not str(m.get("elemento") or "").strip() or not str(m.get("motivo") or "").strip():
            raise ValueError("each reason of rechazo_previo.motivos is {elemento, motivo}, both non-empty text")
        limpios.append({"elemento": str(m["elemento"]).strip()[:RECHAZO_ELEMENTO_MAX],
                        "motivo": str(m["motivo"]).strip()[:RECHAZO_MOTIVO_MAX]})
    resultado = valor.get("resultado")
    if resultado is not None and len(json.dumps(resultado, ensure_ascii=False).encode()) > RECHAZO_RESULTADO_MAX_BYTES:
        raise ValueError(f"rechazo_previo.resultado exceeds {RECHAZO_RESULTADO_MAX_BYTES // 1024} KiB")
    return {"motivos": limpios, **({"resultado": resultado} if resultado is not None else {})}


def encargo(cap: dict[str, Any], argumentos: dict[str, Any], *, pregunto: str = "",
            instrucciones: str = "", rechazo_previo: dict[str, Any] | None = None,
            solo_lectura: bool = False) -> str:
    """The message of the invocation turn. It goes through the chat's own path (session as the
    person, memory, permissions gate), so here is only what makes THIS turn different."""
    idioma = _idioma()
    partes = [
        f"[API INVOCATION] An application is invoking your capability `{cap.get('id')}` "
        f"({str(cap.get('que') or '').strip()}) on behalf of the person of this session. This is "
        "NOT a conversation: a program parses your final message, and nobody reads prose.",
        "Arguments (already validated against the capability's input schema):\n"
        + json.dumps(argumentos or {}, ensure_ascii=False, indent=2),
        "Do the work of that capability with your usual tools and skills. Then reply with ONLY "
        "one JSON value that conforms to this JSON Schema — no text before or after it, no "
        "Markdown fences:\n" + json.dumps(cap.get("salida") or {}, ensure_ascii=False, indent=2),
        "Rules for this turn:\n"
        "- NEVER ask a question and never wait for an answer: nobody is there to answer. Decide "
        "with what you have.\n"
        "- What you cannot decide, or are not sure of, goes INSIDE the JSON, where the schema "
        "provides for it (for example a list of undecided items, each with its reason). Never "
        "invent a value to fill a field: use what the schema allows for «unknown».\n"
        "- The chat rule against ending with JSON does not apply to this turn: here the JSON IS "
        "the answer.",
    ]
    if (instrucciones or "").strip():
        # The caller's APPROVED instructions for this use (docs/red-de-integraciones.md): the platform resolved them
        # from the template the person approved and the arguments. They narrow the work; they never widen the
        # capability nor change the output schema.
        partes.append("Instructions approved for this use (follow them WITHIN this capability; they never change "
                      "what the capability does nor its output schema):\n" + instrucciones.strip()[:4000])
    if rechazo_previo:
        lista = "\n".join(f"- {m['elemento']}: {m['motivo']}" for m in rechazo_previo["motivos"])
        partes.append(
            "The application already received your output for these same arguments and REJECTED these elements (it "
            "checks them against the input it sent you):\n" + lista
            + ("\n\nYour previous output:\n" + json.dumps(rechazo_previo["resultado"], ensure_ascii=False, indent=2)
               if "resultado" in rechazo_previo else "")
            + "\n\nReturn the WHOLE output again with ONLY those elements fixed — replace or remove them; keep the "
              "rest as it was. An element that cannot rest on what the input gives you goes where the schema provides "
              "for undecided items, never as a reference to something that is not in the input.")
    if solo_lectura:
        # 15.7.0 — the platform enforces it (`prueba_solo_lectura`); this only tells the agent why a
        # write is refused, so it does not fight the refusal nor present a refused change as done.
        partes.append(
            "READ-ONLY TEST: someone is previewing an application that calls you, with real data. In this "
            "run nothing may be changed: every tool that writes (in a connector or in your own tools) will "
            "be refused. Read what you need, never retry a refused call, and where the work would change "
            "something, say in the JSON (wherever the schema lets you) what you WOULD have done instead of "
            "claiming it was done.")
    if idioma:
        partes.append(f"Write any human-readable text inside the JSON in {idioma}.")
    if pregunto:
        partes.append(
            "In a previous attempt at this invocation you asked a question instead of answering "
            f"({pregunto[:400]}). You must not ask: decide, and put what is undecided inside the "
            "JSON.")
    return "\n\n".join(partes)


def correccion(cap: dict[str, Any], errores: list[str]) -> str:
    """The ONE retry: same session (the work is already done), only the format to fix."""
    lista = "\n".join(f"- {e}" for e in errores[:MAX_ERRORES])
    return (
        f"[API INVOCATION — CORRECTION] Your last reply to the invocation of `{cap.get('id')}` "
        f"does not conform to its output schema:\n{lista}\n\n"
        "Reply again with ONLY the corrected JSON value (no text, no fences) conforming to the "
        "same schema. Do not redo the work unless an error requires it, and do not ask anything.")
