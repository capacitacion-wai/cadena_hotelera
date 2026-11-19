"""The WAI APPLICATION THAT IS "ITS OWN", if it has one associated.

An employee and an application are often two faces of the same product —the travel
assistant and the travel application— and the person experiences them as one. The
association is decided by whoever deploys, in the platform (V102), and it serves two purposes:

  1. that the employee can LINK to it on purpose ("your trip is here"), instead of
     writing an address it knows by heart and that may be wrong;
  2. that the link opens THE RIGHT WAY: inside the WAI mobile app if you are on mobile, and
     on the web without asking for the session again.

WHY IT IS ASKED AND NOT INJECTED. The association is saved WITHOUT deploying: it is a link
of the platform, not something the container needs. With an environment variable, choosing
the application would not take effect until the next publication —and nobody would connect
one thing with the other—. So the pod asks by itself, with its own secret and by its public
address, which is the only thing both ends know equally: the pod does NOT know its
`agent_app_id`, it never has.

It is cached for a few minutes because the answer changes at most once a day and this is
queried on every chat turn.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import httpx

from app.config import APP_PUBLIC_BASE_URL, BACKEND_API_SECRET, WAI_BACKEND_URL

logger = logging.getLogger(__name__)

#: What is kept hot. The association changes with one click of whoever deploys, so
#: five minutes is the balance between not asking on every turn and not having to
#: restart anything for it to show.
_VIDA_CACHE = 300.0

_cache: dict[str, object] = {"cuando": 0.0, "valor": None}


def _vacia() -> dict:
    return {"tiene": False}


async def cual_es(refrescar: bool = False) -> dict:
    """`{"tiene": True, "url": …, "nombre": …}` or `{"tiene": False}`.

    It NEVER raises: not knowing which its application is cannot bring down a chat turn.
    With no answer it behaves like one that has none, which is the normal state.
    """
    ahora = time.monotonic()
    if not refrescar and _cache["valor"] is not None and ahora - float(_cache["cuando"]) < _VIDA_CACHE:
        return dict(_cache["valor"])  # type: ignore[arg-type]
    valor = _vacia()
    if WAI_BACKEND_URL and BACKEND_API_SECRET and APP_PUBLIC_BASE_URL:
        url = f"{WAI_BACKEND_URL.rstrip('/')}/backend/agent-apps/mi-aplicacion"
        try:
            async with httpx.AsyncClient(timeout=8.0) as cli:
                r = await cli.get(url, params={"host": APP_PUBLIC_BASE_URL},
                                  headers={"X-API-Secret": BACKEND_API_SECRET})
            if r.status_code < 400:
                d = r.json() or {}
                if d.get("tiene") and d.get("url"):
                    valor = {"tiene": True, "url": str(d["url"]),
                             "nombre": str(d.get("nombre") or "")}
        except Exception:  # noqa: BLE001 — see the promise above
            logger.debug("mi_aplicacion: no se pudo preguntar a la plataforma", exc_info=True)
    _cache["cuando"] = ahora
    _cache["valor"] = valor
    return dict(valor)


async def url() -> Optional[str]:
    d = await cual_es()
    return str(d["url"]) if d.get("tiene") else None


async def origen() -> Optional[str]:
    """`https://host[:port]` of its application, or None. It is what the CSP needs to allow in
    `form-action` so the chat link can redirect to it (12.54.0)."""
    from urllib.parse import urlsplit
    u = await url()
    if not u:
        return None
    p = urlsplit(u if "//" in u else f"https://{u}")
    return f"{p.scheme or 'https'}://{p.netloc}" if p.netloc else None


async def para_el_prompt() -> str:
    """What the EMPLOYEE is told about its application. Empty string if it has none.

    It gives the address and says how to link it: normal markdown, without any special
    syntax —the surface recognises the link by its HOST, so it does not depend on the model
    remembering anything—.

    And it says to link to the SPECIFIC PAGE when it knows it. It knows its routes from its
    own skills —the travel one has them written down— and writes them correctly: measured on
    23/09/2026 in its history, `/#/viajes/4`, `/#/viajes/3/agenda`, `/#/viajes/5/
    experiencias`. At one point a "do not invent routes" was put here that was superfluous
    and would have taken away the good part: the rule is not to GUESS one it does not know,
    not to stop using the ones it does. A route example is left out on purpose —each
    application has its own—.
    """
    d = await cual_es()
    if not d.get("tiene"):
        return ""
    nombre = d.get("nombre") or "your application"
    return (
        f"YOUR APPLICATION: «{nombre}» — {d['url']}\n"
        "It is the web application of this same product, the one used by the person you\n"
        "are talking to. When what you have done is SEEN better there (a trip, a record, a list),\n"
        f"link it in markdown: [open {nombre}]({d['url']}) — with the link text in the\n"
        "person's language.\n"
        "\n"
        "And TO THE SPECIFIC PAGE whenever you know which one it is: if your instructions include\n"
        "the routes of this application, use them — taking them to the home page forces them to\n"
        "search again for what you have just done. What you do not do is GUESS a route you do\n"
        "not know: that one leads to a page that does not exist, and that IS worse than the home page.\n"
        "\n"
        "Do not explain how to get in or warn that they will have to sign in: whoever clicks is\n"
        "already inside, and the application takes care of that."
    )
