"""The FastAPI dependencies an application route uses to declare who may call it.

INVARIANT FILE — installed by `wai-app-builder/assets/modulos.py`; do not edit it.

    from app.wai.perfilado import requiere, Usuario

    @router.post("/pedidos")
    async def crear_pedido(body: PedidoIn, usuario: Usuario = Depends(requiere("CAP-2", "crear"))):
        ...

* `requiere(capacidad, accion)` — the person is an ACTIVE user of the app AND one of their roles
  allows that action on that capability (level 1: any active user). The capability is a `CAP-n` of
  the Definition and the action one of `ver crear editar borrar aprobar exportar`; anything else
  fails when the route is DEFINED (at import), not on the first request.
* `requiere_admin` — an active administrator (user management).
* `usuario_actual` — any active user, no capability (only for what every user can see, like «my
  access»); at level 2 the verifier does not accept it as a route's permission.
* `exigir_sobre(usuario, registro, capacidad, accion)` — level 3: 403 (`PERFILADO_FUERA_DE_ALCANCE`)
  when the record is outside the person's scope (another owner's, another team's); level 4: 403
  (`PERFILADO_OTRA_ORGANIZACION`) when it is another organisation's. Lists use `usuario.filtrar(...)`;
  see `servicio.py`.
* `requiere_gestor` — an internal administrator OR an organisation's administrator (level 4: they
  manage the people of THEIR organisation; the service checks whom).
* `marcar_dueno(registro, usuario)` — on create: owner, team and (level 4) organisation.

The identity is the platform's (`app.auth.require_project_access`: introspection, unchanged); the
role is read from the app's database by that identity's email (`servicio.resolver`). An EXTERNAL
account (`externa`, level 4) is accepted only when the identity says it is of THIS app.
"""
from __future__ import annotations

from typing import Any, Callable

import os

from fastapi import Depends, HTTPException

from app.auth import require_project_access

from . import permisos, servicio
from .servicio import ErrorDePerfilado, Usuario, resolver


def como_http(e: ErrorDePerfilado) -> HTTPException:
    """ONE translation of the module's errors: the message for whoever reads it, the stable code in
    `X-WAI-Codigo` for the screens (they say it in the app's language)."""
    return HTTPException(status_code=e.estado, detail=e.mensaje, headers={"X-WAI-Codigo": e.codigo})


async def usuario_actual(identidad=Depends(require_project_access)) -> Usuario:
    externa = getattr(identidad, "externa", False) is True
    # Defence in depth: `auth.py` already refuses an external session of another app; the module
    # never trusts that it was written so.
    if externa and (getattr(identidad, "app", None) or "") != (os.environ.get("APP_NAME") or "").strip():
        raise HTTPException(status_code=403, detail="this session is for another application",
                            headers={"X-WAI-Codigo": "PERFILADO_SESION_DE_OTRA_APLICACION"})
    try:
        return await resolver(getattr(identidad, "email", "") or "", externa=externa)
    except ErrorDePerfilado as e:
        raise como_http(e) from None


async def requiere_admin(usuario: Usuario = Depends(usuario_actual)) -> Usuario:
    if not usuario.administra:
        raise HTTPException(status_code=403, detail="only an administrator of this application can do this",
                            headers={"X-WAI-Codigo": "PERFILADO_SOLO_ADMINISTRADORES"})
    return usuario


async def requiere_gestor(usuario: Usuario = Depends(usuario_actual)) -> Usuario:
    if not (usuario.administra or usuario.administra_organizacion):
        raise HTTPException(status_code=403, detail="only an administrator of this application can do this",
                            headers={"X-WAI-Codigo": "PERFILADO_SOLO_ADMINISTRADORES"})
    return usuario


def requiere(capacidad: str, accion: str = "ver") -> Callable:
    """The dependency of a route that serves `capacidad` with `accion`. Validated against
    `permisos.json` when the route is defined."""
    if accion not in permisos.ACCIONES:
        raise ValueError(f"requiere({capacidad!r}, {accion!r}): the action is one of {list(permisos.ACCIONES)}")
    if capacidad not in permisos.ids_de_capacidades():
        raise ValueError(f"requiere({capacidad!r}, {accion!r}): {capacidad} is not a capability of the Definition "
                         f"({permisos.ids_de_capacidades()}); regenerate permisos.json with modulos.py if it is new")

    async def _requiere(usuario: Usuario = Depends(usuario_actual)) -> Usuario:
        if not usuario.puede(capacidad, accion):
            raise HTTPException(status_code=403,
                                detail=f"your role does not allow «{accion}» on {capacidad} in this application",
                                headers={"X-WAI-Codigo": "PERFILADO_SIN_PERMISO"})
        return usuario

    _requiere.wai_requiere = (capacidad, accion)  # type: ignore[attr-defined]
    _requiere.__name__ = f"requiere_{capacidad}_{accion}".replace("-", "_")
    return _requiere


def exigir_sobre(usuario: Usuario, registro: Any, capacidad: str, accion: str = "ver") -> None:
    """403 unless `usuario.puede_sobre(registro, …)`: `X-WAI-Codigo: PERFILADO_OTRA_ORGANIZACION` for
    another organisation's record (level 4), `PERFILADO_FUERA_DE_ALCANCE` otherwise. For the routes
    that load ONE record of a table with an owner / of the organisation (detail, edit, delete, approve)."""
    motivo = usuario.motivo_sobre(registro, capacidad, accion)
    if motivo == "organizacion":
        raise HTTPException(status_code=403, detail="this record is of another organisation",
                            headers={"X-WAI-Codigo": "PERFILADO_OTRA_ORGANIZACION"})
    if motivo is not None:
        raise HTTPException(status_code=403,
                            detail=f"this record is outside what your role may «{accion}» on {capacidad} (scope: "
                                   f"{usuario.alcance(capacidad, accion) or 'none'})",
                            headers={"X-WAI-Codigo": "PERFILADO_FUERA_DE_ALCANCE"})


def marcar_dueno(registro: Any, usuario: Usuario, **kw: Any) -> Any:
    """`servicio.marcar_dueno` with its refusals as HTTP (403 `PERFILADO_OTRA_ORGANIZACION`, 422
    `PERFILADO_FALTA_ORGANIZACION`): what an app route calls on create."""
    try:
        return servicio.marcar_dueno(registro, usuario, **kw)
    except ErrorDePerfilado as e:
        raise como_http(e) from None
