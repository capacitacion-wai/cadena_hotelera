"""NOTIFY THE PHONE that the employee has finished (12.55.0).

A chat turn lasts from seconds to minutes, and whoever asks from their phone does what they
would do with any person: they go to another app in the meantime. iOS then freezes the WAI
app —the JavaScript stops running and the stream is cut—, so the phone cannot find out by
itself that the turn finished: a LOCAL notification has nobody to fire it. It has to be sent
by whoever is still awake, which is this pod.

How it works, and why this way:

· **The token travels with the message** (`ChatRequest.aviso_push`), it is not stored in any
  table. The notification is for THIS turn and THIS phone; storing it would force us to manage
  expired tokens, several devices and removals, for something the message itself already carries.
· **It is ALWAYS sent at the end**, and it is the phone that does not show it if it is in the
  foreground (`setNotificationHandler`). Guessing here whether the phone is frozen is not
  possible: a suspended app keeps the socket open for a good while and to the server it looks
  connected.
· **The text is generic.** The notification is seen on the LOCKED screen, in view of anyone:
  "it has your answer", never the answer.
· **What the person stopped** with "Stop" is not notified: they already know it finished.

It is sent through Expo's push service (the mobile app is an Expo app and its token is an Expo
token). If it fails, it is logged and that is it: a lost notification can never spoil the turn.
The notification texts are for the PERSON and stay in Spanish.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

_EXPO_PUSH = "https://exp.host/--/api/v2/push/send"

#: The exact format of an Expo token. Whatever does not match is sent nowhere: the
#: field is filled in by a client, and without this anyone could use the pod to send
#: requests with whatever they wanted inside.
_TOKEN = re.compile(r"^Expo(nent)?PushToken\[[A-Za-z0-9_\-]{10,200}\]$")


def token_valido(token: str | None) -> str | None:
    t = (token or "").strip()
    return t if _TOKEN.match(t) else None


def _nombre_del_empleado() -> str:
    """Its name for the title, from the manifest. Without it, «Tu asistente»."""
    try:
        from app.config import WAI_APP_JSON_PATH
        d = json.loads(Path(WAI_APP_JSON_PATH).read_text())
        aa = d.get("agent_app") or {}
        return str(aa.get("agent_display_name") or aa.get("name") or d.get("name") or "").strip()
    except Exception:  # noqa: BLE001 — the title is decoration; without it the notice still goes out
        return ""


def texto_del_aviso(eventos: list[dict], estado: str) -> str | None:
    """What to say, or None if there is nothing to notify.

    It looks at the END of the turn: a pending question asks for something different from an
    answer, and an error is told as an error so nobody opens the app expecting what is not there."""
    tipos = {str(e.get("type") or "") for e in eventos[-20:]}
    if "aborted" in tipos:
        return None
    if any(str(e.get("type") or "") in ("question", "pregunta") for e in eventos[-5:]):
        return "Te pregunta algo para poder seguir. Toca para contestar."
    if estado != "complete":
        return "No ha podido terminar. Toca para ver qué ha pasado."
    return "Ya tiene tu respuesta. Toca para verla."


async def avisar(token: str, eventos: list[dict], estado: str, hilo: str | None = None) -> bool:
    """Send the end-of-turn notification. Never raises. Returns whether Expo accepted it."""
    token = token_valido(token)
    cuerpo = texto_del_aviso(eventos, estado) if token else None
    if not token or not cuerpo:
        return False
    nombre = _nombre_del_empleado()
    mensaje = {
        "to": token,
        "title": nombre or "Tu asistente",
        "body": cuerpo,
        "sound": "default",
        # What the phone needs to open the right conversation when tapped. Nothing of the
        # content: it travels through a third-party service.
        "data": {"que": "turno_terminado", "hilo": hilo or ""},
        "priority": "high",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as cli:
            r = await cli.post(_EXPO_PUSH, json=mensaje,
                               headers={"Accept": "application/json"})
        ok = r.status_code < 400 and (r.json() or {}).get("data", {}).get("status") != "error"
        if not ok:
            logger.warning("aviso_push: Expo no aceptó el aviso (%s): %s",
                           r.status_code, r.text[:200])
        return ok
    except Exception:  # noqa: BLE001 — a lost notification cannot spoil anything
        logger.warning("aviso_push: no se pudo mandar el aviso", exc_info=True)
        return False


async def enviar(mensajes: list[dict]) -> list[dict]:
    """Send several notices in one call (Expo takes up to 100) and return Expo's ticket for each,
    in the same order (``{}`` when it could not be sent). Never raises: a lost notice spoils
    nothing. The caller reads ``details.error == "DeviceNotRegistered"`` to forget a phone."""
    validos = [m for m in mensajes if token_valido(m.get("to"))]
    if not validos:
        return [{} for _ in mensajes]
    tickets: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=15.0) as cli:
            for i in range(0, len(validos), 100):
                r = await cli.post(_EXPO_PUSH, json=validos[i:i + 100],
                                   headers={"Accept": "application/json"})
                datos = (r.json() or {}).get("data") if r.status_code < 400 else None
                if not isinstance(datos, list):
                    logger.warning("aviso_push: Expo did not accept the notices (%s): %s",
                                   r.status_code, r.text[:200])
                    datos = [{} for _ in validos[i:i + 100]]
                tickets.extend(datos)
    except Exception:  # noqa: BLE001 — a lost notice cannot spoil anything
        logger.warning("aviso_push: the notices could not be sent", exc_info=True)
    por_token = {m["to"]: t for m, t in zip(validos, tickets)}
    return [por_token.get(m.get("to"), {}) for m in mensajes]
