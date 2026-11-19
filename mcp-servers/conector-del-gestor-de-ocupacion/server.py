"""MCP server built from WAI — skeleton.

What is here is not an example: it is the contract. The lock, the shape of the responses
and the health check come in place because they are the three things that, when missing,
are not discovered until an employee gives a customer a wrong answer.

What to touch and what not:

* `TOOLS` and the functions that implement them — **this is yours**.
* `_quien_llama`, `/health` and the JSON-RPC routing — **do not touch**. The platform
  checks them before allowing publication (two probes: a made-up key must be
  rejected and the good one must work).

Where the configuration comes from:

* `WAI_MCP_ACCESS_KEY_HASHES` — who may call, as hashes `{sha256: label}`. The platform
  writes it when issuing or revoking a grant. There are NO keys here: the hash of what
  arrives is compared, so not even stealing the Secret gets you in.
* The other variables — the ones you declared as deployment configuration (in plain
  sight: host, port, database) and as deployment secret (in a Secret: user,
  password). They arrive as environment variables, read the same way.

And if the connection to the customer's system is PER USER (`credential_mode = user`), this
connector is a TRANSLATOR and the credential ARRIVES, it is not requested:

    employee → Composio → (Authorization: Bearer <custodied key>) → here
             → the call to the customer's system, with the credential wherever its API requires

It works because **the URL the customer registers when connecting is this connector's**, not
their system's: Composio calls here and injects on the way the credential it custodies. Your
job is to put it wherever that API asks for it — header, body, whatever — and not store it.
Declare a `cabeceras: dict` parameter in your function and the routing gives you the request's.

Careful: Composio injects ONLY into headers. If the API authenticates via the body (Odoo
17/18 XML-RPC, for instance) the translator is exactly the piece needed; if it authenticates
via a header, you probably do not need a connector.

And if the target is a WAI APPLICATION (`credential_mode = wai_app`), there is no credential
to declare nor key to set: `Authorization` carries the token of the PERSON asking,
signed by the platform, and `app_wai.py` forwards it as-is to the application, which
applies its usual permissions. Declare `cabeceras: dict` in your tools and pass them to
`app_wai.llamar()`; do not read the token, do not log it and do not store it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any, Callable

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

import ficheros
import limites
import app_wai

logging.basicConfig(
    level=logging.INFO,
    format='{"ts": "%(asctime)s", "level": "%(levelname)s", "msg": "%(message)s"}',
)
log = logging.getLogger("mcp")

app = FastAPI(title=os.environ.get("MCP_NAME", "servidor-mcp"))

# ── The lock ──────────────────────────────────────────────────────────────────────────
# Hashes, never keys. And each one's label, which is what makes the logs say WHO called
# instead of "someone".
_HASHES: dict[str, str] = json.loads(os.environ.get("WAI_MCP_ACCESS_KEY_HASHES", "{}"))
# Our own header and not `Authorization`: that is where the target system's credential
# travels when there is one, and putting both in the same place forces guessing which is which.
CABECERA = "x-wai-mcp-key"


def _quien_llama(clave: str) -> str | None:
    """The label of the key's owner, or None if it is not valid.

    If `_HASHES` is empty, this server does NOT require a key (`access_key_mode = none`) and
    authenticates with the in-house Bearer, like the rest of the internal MCPs."""
    if not _HASHES:
        return "sin cerradura"
    return _HASHES.get(hashlib.sha256((clave or "").encode()).hexdigest())


# ── The tools ─────────────────────────────────────────────────────────────────────────
# Each one: what it does, what arguments it accepts, what it returns and — MANDATORY —
# whether it READS or CHANGES something. The description is READ by a model, so write for
# it: "returns a customer's unpaid invoices" works, "invoices endpoint" does not.
#
# `annotations` is NOT decoration. It is the only thing that tells whoever uses you whether
# your tool touches the customer's system or not, and the employee's permissions depend on it:
#
#   "annotations": {"readOnlyHint": True}       ← query: changes nothing
#   "annotations": {"readOnlyHint": False}      ← write: changes something
#   "annotations": {"destructiveHint": True}    ← also, hard to undo
#
# **If you do not declare it, whoever uses you has to GUESS it from the name, and fails closed.**
# Measured on 18/08/2026 at the restaurants workshop: a tool called
# `detectar_sobreaforo` — a textbook query — matched no known verb, so it
# was treated as a write and the employee PARKED A QUERY waiting for a person's
# approval: "the query is pending your approval". Nothing failed, nothing was
# logged as an error, and the floor manager was left without an answer.
TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_occupancy_summary",
        "description": "Returns the current hotel-chain occupancy summary, including sold rooms, rooms out of service, and overbooking.",
        "annotations": {"readOnlyHint": True},
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_hotels",
        "description": "Lists hotels with room counts, availability, occupancy percentage, nightly rate, and distance from central Valencia.",
        "annotations": {"readOnlyHint": True},
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Optional page size."},
                "offset": {"type": "integer", "description": "Optional page offset."},
            },
        },
    },
    {
        "name": "get_relocation_options",
        "description": "Returns relocation proposals, history, guests who need relocation, and destination hotels with availability.",
        "annotations": {"readOnlyHint": True},
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Optional page size for proposals and relocation candidates."},
                "offset": {"type": "integer", "description": "Optional page offset for proposals and relocation candidates."},
                "historial_limit": {"type": "integer", "description": "Optional page size for relocation history."},
                "historial_offset": {"type": "integer", "description": "Optional page offset for relocation history."},
            },
        },
    },
    {
        "name": "propose_relocation",
        "description": "Records a relocation proposal for a reservation at a destination hotel, with a reason. It does not approve the proposal.",
        "annotations": {"readOnlyHint": False},
        "inputSchema": {
            "type": "object",
            "properties": {
                "reserva_id": {"type": "integer", "description": "Reservation id to relocate."},
                "hotel_destino_id": {"type": "integer", "description": "Destination hotel id."},
                "motivo": {"type": "string", "description": "Reason for the proposed relocation."},
            },
            "required": ["reserva_id", "hotel_destino_id", "motivo"],
        },
    },
]


async def get_occupancy_summary(cabeceras: dict) -> Any:
    return await app_wai.llamar(cabeceras, "/api/ocupacion")


async def list_hotels(cabeceras: dict, limit: int | None = None,
                     offset: int | None = None) -> Any:
    params = {k: v for k, v in {"limit": limit, "offset": offset}.items() if v is not None}
    return await app_wai.llamar(cabeceras, "/api/hoteles", params=params)


async def get_relocation_options(cabeceras: dict, limit: int | None = None,
                                 offset: int | None = None,
                                 historial_limit: int | None = None,
                                 historial_offset: int | None = None) -> Any:
    params = {
        k: v for k, v in {
            "limit": limit,
            "offset": offset,
            "historial_limit": historial_limit,
            "historial_offset": historial_offset,
        }.items() if v is not None
    }
    return await app_wai.llamar(cabeceras, "/api/reubicaciones", params=params)


async def propose_relocation(cabeceras: dict, reserva_id: int,
                             hotel_destino_id: int, motivo: str) -> dict[str, Any]:
    return await app_wai.llamar(
        cabeceras,
        "/api/reubicaciones",
        metodo="POST",
        cuerpo={
            "reserva_id": reserva_id,
            "hotel_destino_id": hotel_destino_id,
            "motivo": motivo,
        },
    )


IMPLEMENTACIONES: dict[str, Callable] = {
    "get_occupancy_summary": get_occupancy_summary,
    "list_hotels": list_hotels,
    "get_relocation_options": get_relocation_options,
    "propose_relocation": propose_relocation,
}


# BUILT-IN TESTING (2.9.0) — only a connector to a WAI application: the platform's three test tools (seed the app's
# sample data marked with the run, list its datasets, sweep the run), the same in every connector. Do not remove it.
def _modo_de_credencial() -> str:
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp.json"), encoding="utf-8") as f:
            return str(json.load(f).get("credential_mode") or "")
    except (OSError, ValueError):
        return ""


if _modo_de_credencial() == "wai_app":
    import pruebas_de_serie
    pruebas_de_serie.instalar(TOOLS, IMPLEMENTACIONES)


def _argumentos_que_no_encajan(fn: Callable, args: dict) -> tuple[list, list]:
    """What is extra and what is missing with respect to the tool's signature.

    The signature is inspected rather than trying the call, to tell a CALLER's error apart
    from a failure of OURS: both would be a TypeError and only one is their fault."""
    import inspect

    firma = inspect.signature(fn)
    acepta_todo = any(p.kind is inspect.Parameter.VAR_KEYWORD
                      for p in firma.parameters.values())
    nombres = {n for n, p in firma.parameters.items()
               if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                             inspect.Parameter.KEYWORD_ONLY)}
    obligatorios = {n for n, p in firma.parameters.items()
                    if p.default is inspect.Parameter.empty
                    and p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                   inspect.Parameter.KEYWORD_ONLY)}
    sobran = [] if acepta_todo else sorted(set(args) - nombres)
    faltan = sorted(obligatorios - set(args))
    return sobran, faltan


def _contenido_de(resultado) -> list[dict]:
    """The response blocks: files through their own path and everything else through text's.

    Accepts a single file, a list of files, or a dict carrying some inside
    — the usual case: "here is the invoice AND this data" —. Whatever is not a file is
    serialized with the usual caps."""
    if isinstance(resultado, ficheros.Fichero):
        return resultado.contenido_mcp()

    if isinstance(resultado, (list, tuple)) and any(
            isinstance(x, ficheros.Fichero) for x in resultado):
        bloques: list[dict] = []
        resto = []
        for x in resultado:
            (bloques.extend(x.contenido_mcp()) if isinstance(x, ficheros.Fichero)
             else resto.append(x))
        if resto:
            bloques.append({"type": "text", "text": limites.serializar(resto)})
        return bloques

    if isinstance(resultado, dict) and any(
            isinstance(v, ficheros.Fichero) for v in resultado.values()):
        bloques = []
        resto = {}
        for k, v in resultado.items():
            (bloques.extend(v.contenido_mcp()) if isinstance(v, ficheros.Fichero)
             else resto.update({k: v}))
        if resto:
            bloques.append({"type": "text", "text": limites.serializar(resto)})
        return bloques

    return [{"type": "text", "text": limites.serializar(resultado)}]


# ── JSON-RPC: the protocol, not to be touched ─────────────────────────────────────────
#: Answered without a key: introducing itself and listing its tools. Nothing that reads data.
_SIN_LLAVE = {"initialize", "notifications/initialized", "tools/list", "ping"}


@app.post("/mcp")
async def mcp(request: Request, x_wai_mcp_key: str = Header(default=""),
              x_wai_user: str = Header(default=""), x_wai_agent: str = Header(default="")):
    cuerpo = await request.json()
    metodo = cuerpo.get("method")
    idr = cuerpo.get("id")

    # Introducing itself and listing its tools needs no key; USING a tool does. `initialize`
    # and `tools/list` touch no customer data — they describe the connector — and the
    # platform needs them to show it in the catalogue WITHOUT anyone's identity. Everything
    # else (`tools/call`, which reaches the customer's system) requires the key.
    dueno = _quien_llama(x_wai_mcp_key)
    if dueno is None and metodo not in _SIN_LLAVE:
        # 401 and not 403: what is missing is identification. And with no detail of why:
        # someone trying keys gets no help. The body stays in Spanish: validation case O17-15
        # asserts the word "credencial" in the 401 of connectors built from this scaffold.
        return JSONResponse({"error": "credencial no válida"}, status_code=401)

    if metodo == "tools/list":
        return {"jsonrpc": "2.0", "id": idr, "result": {"tools": TOOLS}}

    if metodo == "tools/call":
        params = cuerpo.get("params") or {}
        nombre = params.get("name")
        args = params.get("arguments") or {}
        fn = IMPLEMENTACIONES.get(nombre)
        if not fn:
            return {"jsonrpc": "2.0", "id": idr,
                    "error": {"code": -32601, "message": f"Tool does not exist: {nombre}"}}
        # Arguments are checked BEFORE calling, against the signature. Wrapping the
        # call in an `except TypeError` looks the same and is not: a TypeError from
        # INSIDE the function would count as "wrong arguments" and send the
        # model to fix the only thing that was right. It happened with the Odoo connector: four
        # tools broken by an internal arity bug, reported as the caller's
        # fault.
        # Whom this call serves. Only the tools that declare it receive it, and
        # the PLATFORM sets it in the header: if the agent set it, getting the email
        # wrong would be enough to read a colleague's system.
        if "usuario" in getattr(fn, "__annotations__", {}):
            # With the employee attached to the user: an employee's credential is ITS OWN
            # (`…+a{id}#{toolkit}`), not that of the person who installed it.
            args = {**args, "usuario": f"{x_wai_user}|{x_wai_agent}" if x_wai_agent
                    else x_wai_user}
        # And the headers exactly as they arrive, for the TRANSLATOR: that is where the credential
        # Composio injects on the way comes. They are passed only to whoever declares them, and
        # NEVER logged: a log with a customer's key is exactly what this design
        # exists to prevent.
        if "cabeceras" in getattr(fn, "__annotations__", {}):
            args = {**args, "cabeceras": dict(request.headers)}

        sobran, faltan = _argumentos_que_no_encajan(fn, args)
        if sobran or faltan:
            detalle = []
            if faltan:
                detalle.append("missing: " + ", ".join(faltan))
            if sobran:
                detalle.append("unexpected: " + ", ".join(sobran))
            return {"jsonrpc": "2.0", "id": idr,
                    "error": {"code": -32602,
                              "message": f"Wrong arguments — {'; '.join(detalle)}"}}
        try:
            resultado = await fn(**args)
        except Exception as e:  # noqa: BLE001 — any failure here is OURS
            log.error("%s falló para «%s»: %s", nombre, dueno, e, exc_info=True)
            return {"jsonrpc": "2.0", "id": idr,
                    "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}}
        log.info("%s · llamada de «%s»", nombre, dueno)
        # The size cap is applied HERE and not in each tool: a tool that forgets
        # the limit does not fail, it works — and wipes out the model's context window
        # and the bill. A limit that depends on someone remembering is not a
        # limit. Placed in the routing, the tool written tomorrow cannot forget it
        # either.
        # A FILE does not go through the text cap. It is the exception and it lives here, in the
        # routing, for the same reason as the cap: so it does not depend on the tool
        # remembering. Before this branch, a 200 KB PDF returned as base64 came out truncated
        # to 2,000 characters — 0.7 %, and not even valid base64 — with a 200 response and not
        # a single log line. `ficheros.py` caps files on its own, and its cap
        # BLOWS UP instead of truncating: a truncated file is indistinguishable from a good one until
        # someone tries to open it, far from here.
        contenido = _contenido_de(resultado)
        return {"jsonrpc": "2.0", "id": idr, "result": {"content": contenido}}

    if metodo == "initialize":
        return {"jsonrpc": "2.0", "id": idr, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": app.title, "version": os.environ.get("MCP_VERSION", "0.1.0")},
        }}

    return {"jsonrpc": "2.0", "id": idr,
            "error": {"code": -32601, "message": f"Method not supported: {metodo}"}}


@app.get("/health")
async def health():
    """Without this Kubernetes does not know whether the pod serves, and a broken start passes for good."""
    return {"status": "ok", "tools": len(TOOLS), "cerradura": bool(_HASHES)}
