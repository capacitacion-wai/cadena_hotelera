"""The turn context: what almost every turn reads, read BEFORE the model starts.

THE PROBLEM. Measured on 07/10/2026 in the travel employee: «where can I have dinner near the
hotel?» spent three of its eighteen round trips asking who is talking, which trip is active and
what time it is — the same three reads, in almost every turn, each one a whole round trip to
the model (2-3 s) just to decide to make them.

WHAT THIS DOES. The agent's definition declares `contexto_de_turno`: READ-ONLY calls that need
nothing from the request. Before handing the turn to the agent, the employee makes them all in
parallel —through the SAME gate as a shortcut (`atajos.ejecutar`), as the person of the turn—
and appends their results to the turn under `[TURN CONTEXT]`. The agent starts with them
already in hand; its AGENTS.md says not to call them again.

THREE RULES:

1. **Only what is declared.** Nothing is guessed here; the platform's integrity rejects a
   context call that writes or is not classified as a read (`CONTEXTO_ROTO`), because this
   runs blind on every turn.
2. **A failure is said, never hidden.** A call that fails goes into the block as failed, so
   the agent knows to make it itself. It is not retried and it does not stop the turn.
3. **Bounded.** Each result is trimmed and the whole step has a deadline: the context exists
   to save time, and a slow read must not cost more than the round trip it saves.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

#: Per-call cap of what reaches the model. The context is paid on every turn.
_TOPE_RESULTADO = 4000
#: Deadline for the whole step. Reads in parallel take ~1 s; past this, the agent does it.
_PLAZO_S = 8.0


def declarado() -> list[dict[str, Any]]:
    """The `contexto_de_turno` of THIS employee's definition (same source as the shortcuts)."""
    try:
        from app.conversacion import declaracion
        crudo = (declaracion() or {}).get("contexto_de_turno") or []
    except Exception:  # noqa: BLE001 — without a definition there is no context
        logger.debug("contexto_de_turno: no se pudo leer la definición", exc_info=True)
        return []
    fuera = []
    for x in crudo:
        if isinstance(x, dict) and str(x.get("herramienta") or "").strip():
            fuera.append({"herramienta": str(x["herramienta"]).strip(),
                          "fijos": dict(x.get("fijos") or {}),
                          "para": str(x.get("para") or "").strip()})
    return fuera


def _como_texto(resultado: Any) -> str:
    from app.atajos import _texto_de
    texto = _texto_de(resultado) if isinstance(resultado, dict) else ""
    if not texto:
        try:
            texto = json.dumps(resultado, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            texto = str(resultado)
    if len(texto) > _TOPE_RESULTADO:
        texto = texto[:_TOPE_RESULTADO] + " …[trimmed]"
    return texto


async def _uno(llamada: dict[str, Any], run_id: int | None) -> dict[str, Any]:
    from app.atajos import ejecutar
    t0 = time.monotonic()
    try:
        r = await ejecutar({"herramienta": llamada["herramienta"], "id": "contexto"},
                           llamada["fijos"], run_id)
    except Exception as exc:  # noqa: BLE001 — a failure is said in the block, never raised
        r = {"ok": False, "error": str(exc)}
    return {**llamada, "ok": bool(r.get("ok")), "ms": int((time.monotonic() - t0) * 1000),
            "texto": _como_texto(r.get("resultado")) if r.get("ok")
            else str(r.get("error") or (r.get("denegado") or {}).get("error") or "failed")[:300]}


async def bloque(run_id: int | None) -> tuple[str, dict[str, Any]]:
    """The `[TURN CONTEXT]` block to append to the turn, and its measurement.

    Empty string if nothing is declared. The measurement (`ms`, `n`, `fallos`) goes into the
    turn's account so the saving —or the cost— can be seen next to the steps."""
    llamadas = declarado()
    if not llamadas:
        return "", {}
    t0 = time.monotonic()
    try:
        hechas = await asyncio.wait_for(
            asyncio.gather(*(_uno(x, run_id) for x in llamadas)), timeout=_PLAZO_S)
    except asyncio.TimeoutError:
        logger.warning("contexto_de_turno: superado el plazo de %.0f s", _PLAZO_S)
        hechas = [{**x, "ok": False, "ms": int(_PLAZO_S * 1000),
                   "texto": f"not fetched: it took longer than {_PLAZO_S:.0f} s"} for x in llamadas]
    partes = ["", "[TURN CONTEXT — already fetched by the platform for THIS turn. Use it; do not "
                  "call these tools again in this turn unless one says it failed.]"]
    for h in hechas:
        cabecera = f"### `{h['herramienta']}`" + (f" — {h['para']}" if h.get("para") else "")
        partes.append(cabecera if h["ok"] else cabecera + " — FAILED, call it yourself if you need it")
        partes.append(h["texto"])
    medida = {"ms": int((time.monotonic() - t0) * 1000), "n": len(hechas),
              "fallos": sum(1 for h in hechas if not h["ok"])}
    return "\n".join(partes) + "\n[/TURN CONTEXT]", medida
