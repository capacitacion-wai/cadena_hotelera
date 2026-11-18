"""Profiling module (`perfilado`): users, roles and permissions of THIS application.

INVARIANT FILE — installed and refreshed by `wai-app-builder/assets/modulos.py`; the verifier
compares it byte by byte with the skill's copy. Do not edit it: what is per app is `permisos.json`
(generated from the Definition) and the frontend adapter.

What an application uses:

    from app.wai.perfilado import requiere, requiere_admin, usuario_actual, Usuario

    @router.get("/pedidos")
    async def pedidos(usuario: Usuario = Depends(requiere("CAP-1", "ver"))): ...

level 3 («by function and data»), on the tables marked «with an owner» (`dueno`, `equipo` columns):

    from app.wai.perfilado import exigir_sobre, marcar_dueno
    stmt = usuario.filtrar(select(Pedido), "CAP-1", "ver", dueno_col=Pedido.dueno, equipo_col=Pedido.equipo)
    exigir_sobre(usuario, pedido, "CAP-1", "editar")     # one record: 403 outside the scope
    marcar_dueno(pedido, usuario)                         # on create

level 4 («by organisation»), on the tables marked «of the organisation» (`organizacion` column):

    stmt = usuario.filtrar(select(Pedido), "CAP-1", "ver", dueno_col=Pedido.dueno,
                           equipo_col=Pedido.equipo, org_col=Pedido.organizacion)

and, in each data set of `datos_de_ejemplo.py` (so the test users exist whatever the set):

    from app.wai.perfilado import restaurar_usuarios_de_prueba
    await restaurar_usuarios_de_prueba(session)

Guide: `wai-app-builder/patterns/perfilado.md`.
"""
from __future__ import annotations

from typing import Any

from . import db
from .db import restaurar_usuarios_de_prueba
from .dependencias import exigir_sobre, marcar_dueno, requiere, requiere_admin, requiere_gestor, usuario_actual
from .servicio import ErrorDePerfilado, Usuario

__all__ = ["requiere", "requiere_admin", "requiere_gestor", "usuario_actual", "Usuario", "ErrorDePerfilado",
           "exigir_sobre", "marcar_dueno", "restaurar_usuarios_de_prueba", "dar_de_alta_para_pruebas", "montar",
           "preparar"]


def montar(app: Any) -> None:
    """Called by `app.wai.montar_modulos(app)`: mounts `/api/perfilado`."""
    from .rutas import router
    app.include_router(router)


async def preparar() -> None:
    """Migrations, the owner's record and (test mode) the test users. Idempotent."""
    await db.preparar()


async def dar_de_alta_para_pruebas(email: str, roles: Any = ("admin",), sesion: Any = None,
                                   equipo: Any = None, organizacion: Any = None) -> None:
    """For the app's OWN unit tests (conftest): gives `email` an active record with `roles` (and, at
    level 3, `equipo`: a team id; at level 4, `organizacion`: an organisation id — both created if
    missing), so the test token's user (`test@example.com`) can use routes protected by `requiere`."""
    await db.preparar()
    if equipo:
        await db.escribir([("INSERT OR IGNORE INTO perfilado_equipos (id, nombre, activo, creado_por, creado_en, "
                            "actualizado_en) VALUES (:i, :i, 1, 'pruebas', :t, :t)", {"i": equipo, "t": db.ahora()})],
                          sesion=sesion)
    if organizacion:
        await db.escribir([("INSERT OR IGNORE INTO perfilado_organizaciones (id, nombre, activo, creado_por, creado_en, "
                            "actualizado_en) VALUES (:i, :i, 1, 'pruebas', :t, :t)", {"i": organizacion, "t": db.ahora()})],
                          sesion=sesion)
    await db.asegurar_usuario(email, list(roles), origen="pruebas", reactivar=True, sesion=sesion,
                              equipo=equipo, organizacion=organizacion)
