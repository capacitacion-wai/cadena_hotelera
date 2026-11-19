"""The employee's LOCATION requests to the person it is talking to (v12.1.0).

The employee does not receive the location with every message —that would be a continuous
trail nobody decided on—. It asks for it **when the task needs it**, and the client resolves
it if its owner has allowed it. The waiting lives here.

Why opencode's native question (the one for clarifying questions), which already exists and
blocks the run, is NOT used: that one is answered with the chosen LABEL
(``reply_question(request_id, [[mensaje]])``) and the reply path sends the text verbatim
so opencode matches the option. Putting coordinates there forces a choice between breaking
the matching or storing them as MESSAGE TEXT — and that would leave them in
``agent_runs.prompt`` forever, visible when the conversation is repainted in any client.
The location is data of the turn, not something the person wrote.

So the request waits HERE, inside the tool call: the coordinates come back as the tool's
RESULT and do not touch the message. Three properties being sought:

* **It never hangs.** Every wait has a cap; exhausting it returns "not available", which is a
  valid answer the employee keeps working with. A turn blocked waiting for something that is
  not coming is worse than not having the feature: it keeps billing.
* **Fresh by construction.** The client takes the position on receiving the request, so there
  is no way to serve one from half an hour ago with the instruction "use it as is".
* **Its owner's.** Whoever answers has to be the SAME person who was asked: the request's
  identifier is not enough as a permission.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: How long we wait for the client to answer. A phone with a recent position answers
#: instantly; one that has to wake the GPS —or that shows the permission dialog, which on
#: iOS with "allow once" comes up every session— takes longer. With 8 s it often ran out on
#: mobile coverage and the employee carried on without it (measured on 24/09/2026: the server
#: delivered the request and the phone arrived late). 20 s covers a cold GPS and the dialog;
#: beyond that, the honest thing is to carry on without it and say so (see `pedir_ubicacion`).
ESPERA_S = 20.0

#: Live requests: ``id -> {"futuro", "email"}``. In the process's memory, like the turn
#: buffer (`chat_runtime`): the generated app runs ONE uvicorn, and a request only makes
#: sense while its turn is alive in that same process.
_PENDIENTES: Dict[str, Dict[str, Any]] = {}


def abrir(email: str) -> tuple[str, asyncio.Future]:
    """Registers a request for ``email`` and returns ``(id, future)``."""
    peticion_id = secrets.token_urlsafe(16)
    futuro: asyncio.Future = asyncio.get_running_loop().create_future()
    _PENDIENTES[peticion_id] = {"futuro": futuro,
                                "email": (email or "").strip().lower()}
    return peticion_id, futuro


async def esperar(peticion_id: str, futuro: asyncio.Future,
                  espera_s: float = ESPERA_S) -> Optional[Dict[str, Any]]:
    """Waits for the client's answer. ``None`` if it does not arrive in time.

    Whatever happens the request is removed: an orphan entry is a late answer that would
    arrive when nobody is listening any more, and a dictionary that only grows."""
    try:
        return await asyncio.wait_for(asyncio.shield(futuro), timeout=espera_s)
    except asyncio.TimeoutError:
        logger.info("ubicacion: la petición %s no se contestó en %.0fs", peticion_id, espera_s)
        return None
    except Exception:  # noqa: BLE001 — a broken wait cannot bring the turn down
        logger.debug("ubicacion: espera fallida en %s", peticion_id, exc_info=True)
        return None
    finally:
        _PENDIENTES.pop(peticion_id, None)


def resolver(peticion_id: str, email: str, datos: Dict[str, Any]) -> bool:
    """Answers a live request. ``False`` if it does not exist, has expired or is NOT this person's.

    Checking the owner is the reason for storing the email: the identifier travels to the
    client and cannot, on its own, be a permission to inject a position into the turn of
    another person on the same pod.
    """
    fila = _PENDIENTES.get(peticion_id)
    if fila is None:
        return False
    if fila["email"] != (email or "").strip().lower():
        logger.warning("ubicacion: %s intentó contestar una petición que no es suya", email)
        return False
    futuro: asyncio.Future = fila["futuro"]
    if futuro.done():
        return False
    futuro.set_result(datos)
    return True


def describir(datos: Optional[Dict[str, Any]]) -> str:
    """The line that stays IN THE CONVERSATION when it is shared (text for the person).

    It exists because being automatic removes the natural brake: if nobody sees it was asked
    for, it can be asked twenty times and nobody notices. It goes in the turn's TEXT —not as a
    tool call— because the history does not carry tools: when repainting the conversation in
    another client, a tool call is not visible and "I know where you are" would remain without
    any explanation."""
    if not datos:
        return "📍 Se te pidió la ubicación y no estaba disponible."
    precision = datos.get("precision")
    trozo = f", ±{int(precision)} m" if isinstance(precision, (int, float)) and precision else ""
    return f"📍 Compartiste tu ubicación con el empleado{trozo}."
