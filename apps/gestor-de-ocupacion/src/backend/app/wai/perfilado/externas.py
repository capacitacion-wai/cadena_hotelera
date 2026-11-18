"""External accounts (level 4, setting `alta: externas`): the WAI platform's side, asked LIVE.

INVARIANT FILE — installed by `wai-app-builder/assets/modulos.py`; do not edit it.

An external account is a platform identity LIMITED TO THIS APPLICATION (a supplier's or a customer's
person: no access to the platform, to other apps or to the project's data). The platform creates it,
emails the sign-in code and issues its session; the app only:

* invites (`POST {WAI_BACKEND_URL}/backend/apps/{APP_NAME}/externos/invitar`) with the CALLER's own
  token — an internal administrator, or an organisation's administrator whose external session is of
  this app: the platform refuses anyone else, and an external session of another app;
* deactivates (`DELETE …/externos/{email}`) the same way;
* relays the sign-in (`/backend/auth/externos/codigo` and `/entrar`) with ITS name, set here by the
  server (`APP_NAME`, from the deployer) — never by the browser, so a page cannot ask for a session of
  another app.

The token that comes back is valid only for this app: introspection says so only when the asker sends
`app = APP_NAME` (patterns/authentication.md «External accounts»).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)


class PlataformaNoResponde(Exception):
    """The platform cannot be asked now (no URL, no name, network, 5xx): the caller says so (503)."""


class Rechazado(Exception):
    def __init__(self, estado: int, detalle: str = ""):
        super().__init__(detalle or f"HTTP {estado}")
        self.estado, self.detalle = estado, detalle


def nombre_de_la_app() -> str:
    return (os.environ.get("APP_NAME") or "").strip()


def _base() -> str:
    base = (os.environ.get("WAI_BACKEND_URL") or "").rstrip("/")
    if not base or not nombre_de_la_app():
        raise PlataformaNoResponde("WAI_BACKEND_URL or APP_NAME is not set in this pod")
    return base


async def _llamar(metodo: str, ruta: str, *, autorizacion: Optional[str] = None,
                  cuerpo: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cabeceras = {"Authorization": autorizacion} if autorizacion else {}
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.request(metodo, f"{_base()}{ruta}", json=cuerpo, headers=cabeceras)
    except httpx.HTTPError as exc:
        raise PlataformaNoResponde(str(exc)) from exc
    if r.status_code >= 500 or r.status_code == 429:
        raise PlataformaNoResponde(f"HTTP {r.status_code}")
    if r.status_code >= 400:
        try:
            detalle = r.json().get("detail")
        except ValueError:
            detalle = ""
        raise Rechazado(r.status_code, str(detalle or ""))
    try:
        datos = r.json()
    except ValueError:
        datos = {}
    return datos if isinstance(datos, dict) else {}


async def invitar(email: str, *, nombre: Optional[str], organizacion: Optional[str], autorizacion: str) -> Dict[str, Any]:
    return await _llamar("POST", f"/backend/apps/{quote(nombre_de_la_app(), safe='')}/externos/invitar",
                         autorizacion=autorizacion, cuerpo={"email": email, "nombre": nombre, "organizacion": organizacion})


async def retirar(email: str, *, autorizacion: str) -> Dict[str, Any]:
    return await _llamar("DELETE", f"/backend/apps/{quote(nombre_de_la_app(), safe='')}/externos/{quote(email, safe='')}",
                         autorizacion=autorizacion)


async def pedir_codigo(email: str) -> None:
    """The platform answers 202 whether the account exists or not (no enumeration): so does the app."""
    await _llamar("POST", "/backend/auth/externos/codigo", cuerpo={"email": email, "app": nombre_de_la_app()})


async def entrar(email: str, codigo: str) -> Dict[str, Any]:
    """{access_token, token_type, expires_in}; `Rechazado(401)` for any wrong code/account."""
    return await _llamar("POST", "/backend/auth/externos/entrar",
                         cuerpo={"email": email, "app": nombre_de_la_app(), "codigo": codigo})
