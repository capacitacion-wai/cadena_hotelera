"""Records EVERYTHING the employee touched in a run.

A run's «Herramientas» tab read `tool_call_log`, which is the IDEMPOTENCY ledger of the
app's own writes: it serves to not repeat an `upsert_alert` on retry and knows nothing
about what the agent queried outside. So a turn with six calls —two to the PMS, two to a
colleague, two to the app— showed ONE line, and the manager believed they were seeing
what their employee did.

This records what is seen going through the agent's stream. Three decisions that are
better not undone:

* **One row per invocation**, keyed by the `call_id` opencode gives. The same call is
  re-emitted as it progresses (pending → running → finished); without that key three rows
  come out and the screen counts three times the work there was.
* **It is a record, not control.** Nothing written here decides whether something runs,
  so a failure writing can NOT bring a turn down: it is swallowed and logged. It is the
  only reasonable exception to "one single path": the alternative —the employee falling
  over because it could not note down what it did— is worse than missing the annotation.
* **What is stored is trimmed.** A tool's arguments and result can be a whole table
  dump; storing them raw turns the employee's database into an archive of answers.
  Just enough is stored to recognise the call.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select

from app.app_database import app_session, app_write
from app.app_models.runtime import ToolUseLog

logger = logging.getLogger(__name__)

# How much of the arguments and the result fits. Enough to recognise the call
# —"consultar_ocupacion with date 2026-08-18"— and not to archive its answer.
_TOPE = 4000


def servidor_de(tool_name: str) -> str | None:
    """Which connector it comes from, per the `{server}_{tool}` convention.

    Returns `None` when it does not follow it (opencode's native ones: `bash`, `read`,
    `skill`…), which is information in itself: they are its own, not any connector's."""
    nombre = (tool_name or "").strip()
    if "_" not in nombre:
        return None
    izquierda = nombre.split("_", 1)[0]
    # A one-letter prefix or a name without dashes is rarely a server; we prefer not to
    # guess over mislabelling (`list_alerts` does not come from a "list" server).
    return izquierda if len(izquierda) >= 3 else None


def _recorta(valor: Any) -> str | None:
    if valor is None:
        return None
    try:
        texto = valor if isinstance(valor, str) else json.dumps(valor, default=str,
                                                               ensure_ascii=False)
    except Exception:  # noqa: BLE001 — never over an odd value
        texto = str(valor)
    return texto[:_TOPE] + ("…" if len(texto) > _TOPE else "")


async def anotar(run_id: int | None, *, call_id: str, tool_name: str, step_idx: int = 0,
                 args: Any = None, result: Any = None, terminada: bool = False,
                 estado: str = "ok") -> None:
    """Records an invocation, or completes it if it was already recorded.

    `terminada=True` is the closing event: it fills in result, status and duration —
    `estado` tells apart the one that went well from the one that failed, because a tool
    that blows up and one that answers cannot look the same on the card. Without `run_id`
    nothing is recorded — a call that belongs to no run cannot be shown on any screen."""
    if not run_id or not (tool_name or "").strip():
        return
    llave = (call_id or "").strip() or f"{step_idx}:{tool_name}"
    try:
        # Look up and write in the SAME write session: splitting it in two opened a gap
        # between the "does not exist" and the INSERT, and two consecutive notices of the
        # same call —which is the norm, not the exception— collided with the uniqueness.
        async with app_write() as s:
            fila = (await s.execute(
                select(ToolUseLog).where(ToolUseLog.run_id == run_id,
                                         ToolUseLog.call_id == llave)
            )).scalar_one_or_none()
            if fila is None:
                s.add(ToolUseLog(
                    run_id=run_id, call_id=llave, step_idx=step_idx,
                    tool_name=tool_name, server=servidor_de(tool_name),
                    status=(estado if terminada else "running"),
                    args_json=_recorta(args), result_json=_recorta(result),
                    finished_at=datetime.utcnow() if terminada else None,
                ))
            elif terminada:
                fila.status = estado
                fila.result_json = _recorta(result)
                fila.finished_at = datetime.utcnow()
                if fila.started_at:
                    fila.duration_ms = int(
                        (fila.finished_at - fila.started_at).total_seconds() * 1000)
            elif args is not None and not fila.args_json:
                # The arguments arrive complete in the second notice, not in the first.
                fila.args_json = _recorta(args)
            await s.commit()
    except Exception as exc:  # noqa: BLE001 — recording cannot bring a turn down
        logger.warning("no se pudo anotar el uso de «%s» del run %s: %s",
                       tool_name, run_id, exc)


async def usos_de(run_id: int, *, run_terminado: bool = False) -> list[dict]:
    """What was recorded in a run, in the order it happened.

    A call still `running` in a run that already ENDED never got its closing notice —the
    turn was replaced by a newer message, ran out of time or was stopped— and is returned as
    `interrupted`. Decided here, when reading, and not in each of the five places that close
    a run: the one that forgets it leaves a «pending» forever (cobsa, 27/10/2026)."""
    async with app_session() as s:
        filas = (await s.execute(
            select(ToolUseLog).where(ToolUseLog.run_id == run_id)
            .order_by(ToolUseLog.id)
        )).scalars().all()
    return [
        {
            "step_idx": f.step_idx,
            "call_id": f.call_id,
            "tool_name": f.tool_name,
            "server": f.server,
            "status": ("interrupted" if run_terminado and f.status == "running" else f.status),
            "arguments": f.args_json,
            "result_json": f.result_json,
            "duration_ms": f.duration_ms,
            "executed_at": (f.finished_at or f.started_at).isoformat() + "Z",
        }
        for f in filas
    ]
