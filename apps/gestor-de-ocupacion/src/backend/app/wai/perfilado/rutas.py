"""The profiling API: `/api/perfilado/…` (my access, the roles matrix, user management and, at
level 3, teams and the rows still without an owner; at level 4, organisations, portfolios, the
delegated administration of each organisation and the sign-in of the external accounts; with the
setting `roles_personalizables`, the custom roles).

INVARIANT FILE — installed by `wai-app-builder/assets/modulos.py`; do not edit it.

Thin handlers: they parse, call `servicio` and answer; the module's errors become HTTP in
`dependencias.como_http` (stable code in `X-WAI-Codigo`).
"""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException

from . import externas, permisos, proyecto, roles_propios, servicio
from .dependencias import como_http, requiere_admin, requiere_gestor, usuario_actual
from .esquemas import (AltaDesdeProyectoIn, AltaDesdeProyectoOut, AltaIn, AsignarSinDuenoIn, AsignarSinOrganizacionIn,
                       CambioDeEquipoIn, CambioDeOrganizacionIn, CambioIn, EntrarIn, EquipoIn, EquipoOut, MatrizOut,
                       MiembrosOut, OrganizacionIn, OrganizacionOut, PaginaDeUsuarios, PedirCodigoIn, RolPropioIn, SesionOut,
                       SinDuenoOut, SinOrganizacionOut, UsuarioBorradoOut, UsuarioOut, YoOut)
from .servicio import ErrorDePerfilado, Usuario

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/perfilado", tags=["perfilado"])


def _sin_plataforma() -> HTTPException:
    return HTTPException(status_code=503, headers={"X-WAI-Codigo": "PERFILADO_PLATAFORMA_NO_RESPONDE", "Retry-After": "5"},
                         detail="the WAI platform does not answer, so project membership cannot be checked now: try again")


def _externas() -> bool:
    """Level 4 with `alta: externas`: the people of an organisation are EXTERNAL accounts."""
    return permisos.nivel() >= 4 and permisos.ajustes().get("alta") == "externas"


def _sin_plataforma_externas() -> HTTPException:
    return HTTPException(status_code=503, headers={"X-WAI-Codigo": "PERFILADO_PLATAFORMA_NO_RESPONDE", "Retry-After": "5"},
                         detail="the WAI platform does not answer, so the external account cannot be handled now: try again")


@router.get("/yo", response_model=YoOut)
async def yo(usuario: Usuario = Depends(usuario_actual)) -> YoOut:
    nombres = {r: str((permisos.rol(r) or {}).get("nombre") or r) for r in usuario.roles}
    nombre_de_equipo = nombre_de_organizacion = None
    if usuario.equipo:
        try:
            nombre_de_equipo = (await servicio.equipo_por_id(usuario.equipo))["nombre"]
        except ErrorDePerfilado:
            nombre_de_equipo = None
    if usuario.organizacion and permisos.nivel() >= 4:
        try:
            nombre_de_organizacion = (await servicio.organizacion_por_id(usuario.organizacion))["nombre"]
        except ErrorDePerfilado:
            nombre_de_organizacion = None
    return YoOut(email=usuario.email, nombre=usuario.nombre, roles=usuario.roles, nombres_de_roles=nombres,
                 administra=usuario.administra,
                 es_dueno=usuario.es_dueno, nivel=permisos.nivel(), alta=str(permisos.ajustes().get("alta") or "proyecto"),
                 permisos=usuario.permisos(), capacidades=permisos.capacidades_con_acciones(),
                 equipo=usuario.equipo, nombre_de_equipo=nombre_de_equipo, alcances=usuario.alcances(),
                 usa_equipos=bool(permisos.capacidades_por_equipo()),
                 organizacion=usuario.organizacion if usuario.es_de_organizacion else None,
                 nombre_de_organizacion=nombre_de_organizacion if usuario.es_de_organizacion else None,
                 es_de_organizacion=usuario.es_de_organizacion, administra_organizacion=usuario.administra_organizacion,
                 cartera=usuario.cartera, externa=usuario.externa,
                 nombre_de_las_organizaciones=permisos.ajustes().get("nombre_organizacion") if permisos.nivel() >= 4 else None)


@router.get("/roles", response_model=MatrizOut)
async def roles(gestor: Usuario = Depends(requiere_gestor)) -> MatrizOut:
    """The matrix. An organisation's administrator sees only the organisation roles (the ones they give)."""
    m = servicio.matriz()
    if not gestor.administra:
        m["roles"] = [r for r in m["roles"] if permisos.ambito_de(r["id"]) == "organizacion"]
    elif m["personalizables"]:
        # Who holds each custom role (internal administrators only: it names people of every organisation).
        uso = await roles_propios.en_uso()
        for r in m["roles"]:
            if r["id"] in uso:
                r["personas"] = uso[r["id"]]
    return MatrizOut(**m)


# ── custom roles (setting `roles_personalizables`, levels 2-4): internal administrators only ─────
@router.get("/roles/propios/piezas")
async def piezas_de_los_roles(_: Usuario = Depends(requiere_admin)) -> dict:
    """What a custom role can be built from: {side: {capability: {action: [scopes]}}} (404 when off)."""
    try:
        roles_propios.exigir_activado()
        return roles_propios.piezas()
    except ErrorDePerfilado as e:
        raise como_http(e) from None


@router.post("/roles/propios", status_code=201)
async def crear_rol(body: RolPropioIn, admin: Usuario = Depends(requiere_admin)) -> dict:
    try:
        return await roles_propios.crear(body.model_dump(), por=admin.email)
    except ErrorDePerfilado as e:
        raise como_http(e) from None


@router.patch("/roles/propios/{rid}")
async def cambiar_rol(rid: str, body: RolPropioIn, admin: Usuario = Depends(requiere_admin)) -> dict:
    """The whole role again (PATCH: the apps' API clients speak GET/POST/PATCH/DELETE)."""
    try:
        return await roles_propios.cambiar(rid, body.model_dump(), por=admin.email)
    except ErrorDePerfilado as e:
        raise como_http(e) from None


@router.delete("/roles/propios/{rid}")
async def borrar_rol(rid: str, pasar_a: Optional[str] = None, admin: Usuario = Depends(requiere_admin)) -> dict:
    """Refused (409) while someone holds it, unless they are moved to `pasar_a` in the same step."""
    try:
        return await roles_propios.borrar(rid, por=admin.email, pasar_a=pasar_a or None)
    except ErrorDePerfilado as e:
        raise como_http(e) from None


@router.get("/usuarios", response_model=PaginaDeUsuarios)
async def usuarios(limit: int = 50, offset: int = 0, q: str = "", activos: Optional[bool] = None,
                   equipo: Optional[str] = None, organizacion: Optional[str] = None,
                   gestor: Usuario = Depends(requiere_gestor)) -> PaginaDeUsuarios:
    if not gestor.administra:          # an organisation's administrator: ONLY their organisation, always
        organizacion, equipo = gestor.organizacion, None
    return PaginaDeUsuarios(**await servicio.listar(limit, offset, q, activos, equipo, organizacion))


@router.get("/proyecto", response_model=MiembrosOut)
async def miembros(authorization: str = Header(""), _: Usuario = Depends(requiere_admin)) -> MiembrosOut:
    lista = await proyecto.miembros_del_proyecto(authorization)
    if lista is None:
        return MiembrosOut(disponible=False, miembros=[])
    return MiembrosOut(disponible=True, miembros=await servicio.marcar_dados_de_alta(lista))


@router.post("/usuarios", response_model=UsuarioOut, status_code=201)
async def alta(body: AltaIn, authorization: str = Header(""),
               admin: Usuario = Depends(requiere_gestor)) -> UsuarioOut:
    try:
        email = servicio.normalizar_correo(body.email)
        origen = "correo"
        nombre = body.nombre
        organizacion = body.organizacion if admin.administra else admin.organizacion
        if organizacion and _externas():
            # The person of an organisation is an EXTERNAL account: the platform creates it (with the
            # caller's own token — it refuses anyone who may not invite into THIS app) BEFORE the record.
            organizacion = await servicio.organizacion_valida(organizacion)
            servicio.exigir_gestion(admin, {"organizacion": organizacion}, body.roles)
            servicio.validar_ambito(servicio.validar_roles(body.roles), organizacion)
            try:
                await externas.invitar(email, nombre=nombre, organizacion=organizacion, autorizacion=authorization)
            except externas.PlataformaNoResponde:
                raise _sin_plataforma_externas() from None
            except externas.Rechazado as r:
                raise HTTPException(status_code=403 if r.estado in (401, 403) else 422, detail=r.detalle or "the platform refused the invitation",
                                    headers={"X-WAI-Codigo": "PERFILADO_INVITACION_RECHAZADA"}) from None
            return UsuarioOut(**await servicio.dar_de_alta(email, body.roles, nombre=nombre, origen="externa", por=admin.email,
                                                          organizacion=organizacion, gestor=admin))
        if permisos.ajustes().get("alta", "proyecto") == "proyecto":
            lista = await proyecto.miembros_del_proyecto(authorization)
            if lista is None:
                raise _sin_plataforma()
            miembro = next((m for m in lista if m["email"] == email), None)
            if miembro is None:
                raise ErrorDePerfilado(422, "PERFILADO_NO_ES_DEL_PROYECTO",
                                       f"{email} is not a member of this project: only project members can be added")
            origen, nombre = "proyecto", nombre or miembro["nombre"] or None
        return UsuarioOut(**await servicio.dar_de_alta(email, body.roles, nombre=nombre, origen=origen, por=admin.email,
                                                      organizacion=organizacion, gestor=admin))
    except ErrorDePerfilado as e:
        raise como_http(e) from None


@router.post("/usuarios/desde-proyecto", response_model=AltaDesdeProyectoOut)
async def alta_desde_proyecto(body: AltaDesdeProyectoIn, authorization: str = Header(""),
                              admin: Usuario = Depends(requiere_admin)) -> AltaDesdeProyectoOut:
    lista = await proyecto.miembros_del_proyecto(authorization)
    if lista is None:
        raise _sin_plataforma()
    try:
        return AltaDesdeProyectoOut(**await servicio.alta_desde_proyecto(body.emails, body.roles, lista, por=admin.email))
    except ErrorDePerfilado as e:
        raise como_http(e) from None


async def _seguir_en_la_plataforma(antes: dict, despues: dict, autorizacion: str) -> None:
    """With external accounts, deactivating / reactivating a person of an organisation also closes /
    reopens their account in the platform. Best effort: the app's record is what lets them in, so a
    platform that does not answer never leaves someone inside (it is logged)."""
    if not (_externas() and despues.get("organizacion") and antes.get("activo") != despues.get("activo")):
        return
    try:
        if despues["activo"]:
            await externas.invitar(despues["email"], nombre=despues.get("nombre"), organizacion=despues["organizacion"],
                                   autorizacion=autorizacion)
        else:
            await externas.retirar(despues["email"], autorizacion=autorizacion)
    except (externas.PlataformaNoResponde, externas.Rechazado):
        logger.warning("profiling: the platform did not follow the change of an external account", exc_info=True)


@router.patch("/usuarios/{uid}", response_model=UsuarioOut)
async def cambiar(uid: int, body: CambioIn, authorization: str = Header(""),
                  gestor: Usuario = Depends(requiere_gestor)) -> UsuarioOut:
    try:
        f = body.model_fields_set
        antes = await servicio.usuario_por_id(uid)
        despues = await servicio.cambiar(
            uid, roles=body.roles, activo=body.activo, nombre=body.nombre, gestor=gestor,
            equipo=body.equipo if "equipo" in f else servicio.SIN_CAMBIO,
            organizacion=body.organizacion if "organizacion" in f else servicio.SIN_CAMBIO,
            cartera=body.cartera if "cartera" in f else servicio.SIN_CAMBIO)
        await _seguir_en_la_plataforma(antes, despues, authorization)
        return UsuarioOut(**despues)
    except ErrorDePerfilado as e:
        raise como_http(e) from None


@router.delete("/usuarios/{uid}", response_model=UsuarioBorradoOut)
async def borrar(uid: int, authorization: str = Header(""), gestor: Usuario = Depends(requiere_gestor)) -> UsuarioBorradoOut:
    """DELETES the person (deactivating is `PATCH {"activo": false}`): the owner and yourself are
    refused (409), an organisation's administrator only deletes people of their organisation (403).
    Their business rows stay and count as «without an owner». An external account is also closed in
    the platform (the same call as deactivating it) — best effort: without the app's record nobody
    gets in, so a platform that does not answer only leaves a dormant account (logged, and said)."""
    try:
        antes = await servicio.borrar(uid, gestor=gestor, por=gestor.email)
    except ErrorDePerfilado as e:
        raise como_http(e) from None
    abierta = False
    if _externas() and antes.get("organizacion"):
        try:
            await externas.retirar(antes["email"], autorizacion=authorization)
        except externas.Rechazado as r:
            abierta = r.estado != 404          # 404: no such account any more (already closed)
            if abierta:
                logger.warning("profiling: the platform refused to close the external account of a deleted user (HTTP %s)", r.estado)
        except externas.PlataformaNoResponde:
            abierta = True
            logger.warning("profiling: the platform did not close the external account of a deleted user", exc_info=True)
    return UsuarioBorradoOut(id=antes["id"], email=antes["email"], nombre=antes.get("nombre"), cuenta_externa_abierta=abierta)


# ── level 3: teams and the rows still without an owner ─────────────────────────────────────
@router.get("/equipos", response_model=List[EquipoOut])
async def equipos(archivados: bool = False, usuario: Usuario = Depends(usuario_actual)) -> List[EquipoOut]:
    """Every active user may read the team NAMES (the app's screens show whose team a record is);
    the archived ones only an administrator."""
    return [EquipoOut(**e) for e in await servicio.listar_equipos(archivados and usuario.administra)]


@router.post("/equipos", response_model=EquipoOut, status_code=201)
async def crear_equipo(body: EquipoIn, admin: Usuario = Depends(requiere_admin)) -> EquipoOut:
    try:
        return EquipoOut(**await servicio.crear_equipo(body.nombre, por=admin.email))
    except ErrorDePerfilado as e:
        raise como_http(e) from None


@router.patch("/equipos/{eid}", response_model=EquipoOut)
async def cambiar_equipo(eid: str, body: CambioDeEquipoIn, _: Usuario = Depends(requiere_admin)) -> EquipoOut:
    try:
        return EquipoOut(**await servicio.cambiar_equipo(eid, nombre=body.nombre, activo=body.activo))
    except ErrorDePerfilado as e:
        raise como_http(e) from None


@router.delete("/equipos/{eid}", response_model=EquipoOut)
async def archivar_equipo(eid: str, _: Usuario = Depends(requiere_admin)) -> EquipoOut:
    """Archives (never deletes: the rows keep its id, and the id is never reused)."""
    try:
        return EquipoOut(**await servicio.cambiar_equipo(eid, activo=False))
    except ErrorDePerfilado as e:
        raise como_http(e) from None


@router.get("/sin-dueno", response_model=List[SinDuenoOut])
async def sin_dueno(_: Usuario = Depends(requiere_admin)) -> List[SinDuenoOut]:
    return [SinDuenoOut(**x) for x in await servicio.sin_dueno()]


@router.post("/sin-dueno/{tabla}", response_model=SinDuenoOut)
async def asignar_sin_dueno(tabla: str, body: AsignarSinDuenoIn, _: Usuario = Depends(requiere_admin)) -> SinDuenoOut:
    try:
        return SinDuenoOut(**await servicio.asignar_sin_dueno(tabla, body.dueno, body.equipo))
    except ErrorDePerfilado as e:
        raise como_http(e) from None


# ── level 4: organisations and the rows still without one ─────────────────────────────────────
@router.get("/organizaciones", response_model=List[OrganizacionOut])
async def organizaciones(archivadas: bool = False, usuario: Usuario = Depends(usuario_actual)) -> List[OrganizacionOut]:
    """The NAMES the person may see (the screens say whose a record is): a person of an organisation
    only theirs, an internal person with a portfolio only those; archived ones only administrators."""
    try:
        servicio._exigir_nivel_4()
    except ErrorDePerfilado as e:
        raise como_http(e) from None
    solo = [usuario.organizacion or ""] if usuario.es_de_organizacion else usuario.cartera
    return [OrganizacionOut(**o) for o in await servicio.listar_organizaciones(archivadas and usuario.administra, solo)]


@router.post("/organizaciones", response_model=OrganizacionOut, status_code=201)
async def crear_organizacion(body: OrganizacionIn, admin: Usuario = Depends(requiere_admin)) -> OrganizacionOut:
    try:
        return OrganizacionOut(**await servicio.crear_organizacion(body.nombre, por=admin.email))
    except ErrorDePerfilado as e:
        raise como_http(e) from None


@router.patch("/organizaciones/{oid}", response_model=OrganizacionOut)
async def cambiar_organizacion(oid: str, body: CambioDeOrganizacionIn, _: Usuario = Depends(requiere_admin)) -> OrganizacionOut:
    try:
        return OrganizacionOut(**await servicio.cambiar_organizacion(oid, nombre=body.nombre, activo=body.activo))
    except ErrorDePerfilado as e:
        raise como_http(e) from None


@router.delete("/organizaciones/{oid}", response_model=OrganizacionOut)
async def archivar_organizacion(oid: str, _: Usuario = Depends(requiere_admin)) -> OrganizacionOut:
    """Archives (never deletes: the rows keep its id, and the id is never reused)."""
    try:
        return OrganizacionOut(**await servicio.cambiar_organizacion(oid, activo=False))
    except ErrorDePerfilado as e:
        raise como_http(e) from None


@router.get("/sin-organizacion", response_model=List[SinOrganizacionOut])
async def sin_organizacion(_: Usuario = Depends(requiere_admin)) -> List[SinOrganizacionOut]:
    return [SinOrganizacionOut(**x) for x in await servicio.sin_organizacion()]


@router.post("/sin-organizacion/{tabla}", response_model=SinOrganizacionOut)
async def asignar_sin_organizacion(tabla: str, body: AsignarSinOrganizacionIn,
                                   _: Usuario = Depends(requiere_admin)) -> SinOrganizacionOut:
    try:
        return SinOrganizacionOut(**await servicio.asignar_sin_organizacion(tabla, body.organizacion))
    except ErrorDePerfilado as e:
        raise como_http(e) from None


# ── level 4 + `alta: externas`: the sign-in of the external accounts (PUBLIC on purpose) ────────
def _exigir_externas() -> None:
    if not _externas():
        raise HTTPException(status_code=404, detail="this application has no external accounts",
                            headers={"X-WAI-Codigo": "PERFILADO_SIN_CUENTAS_EXTERNAS"})


@router.post("/externas/codigo", status_code=202)
async def pedir_codigo(body: PedirCodigoIn) -> dict:
    """Asks the platform to email a sign-in code. ALWAYS the same answer, whether the account exists
    or not (nobody learns who is invited); the app's name is set here, never by the browser."""
    _exigir_externas()
    try:
        email = servicio.normalizar_correo(body.email)
    except ErrorDePerfilado as e:
        raise como_http(e) from None
    try:
        await externas.pedir_codigo(email)
    except externas.PlataformaNoResponde:
        raise _sin_plataforma_externas() from None
    except externas.Rechazado:
        pass
    return {"enviado": True}


@router.post("/externas/entrar", response_model=SesionOut)
async def entrar(body: EntrarIn) -> SesionOut:
    """Exchanges the emailed code for a session of THIS app (the platform's; valid nowhere else)."""
    _exigir_externas()
    try:
        email = servicio.normalizar_correo(body.email)
    except ErrorDePerfilado as e:
        raise como_http(e) from None
    try:
        r = await externas.entrar(email, body.codigo.strip())
    except externas.PlataformaNoResponde:
        raise _sin_plataforma_externas() from None
    except externas.Rechazado:
        raise HTTPException(status_code=401, detail="the code is not valid or has expired: ask for a new one",
                            headers={"X-WAI-Codigo": "PERFILADO_CODIGO_NO_VALIDO"}) from None
    if not r.get("access_token"):
        raise _sin_plataforma_externas()
    return SesionOut(access_token=r["access_token"], token_type=r.get("token_type") or "bearer", expires_in=r.get("expires_in"))
