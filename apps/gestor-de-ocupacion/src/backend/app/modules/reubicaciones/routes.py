from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.app_database import get_app_session, get_app_write_session
from app.modules.reubicaciones import service
from app.schemas.reubicaciones import AprobacionOut, DatosReubicacionesOut, PropuestaCreadaOut, PropuestaIn
from app.wai.perfilado import Usuario, requiere

router = APIRouter()


@router.get("/reubicaciones", response_model=DatosReubicacionesOut)
async def reubicaciones(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    historial_limit: int = Query(20, ge=1, le=100),
    historial_offset: int = Query(0, ge=0),
    _usuario: Usuario = Depends(requiere("CAP-2", "ver")),
    session: AsyncSession = Depends(get_app_session),
):
    return await service.listar_reubicaciones(session, limit, offset, historial_limit, historial_offset)


@router.post("/reubicaciones", response_model=PropuestaCreadaOut, status_code=status.HTTP_201_CREATED)
async def proponer(
    payload: PropuestaIn,
    usuario: Usuario = Depends(requiere("CAP-2", "crear")),
    session: AsyncSession = Depends(get_app_write_session),
):
    proposal = await service.proponer_reubicacion(
        session,
        payload.reserva_id,
        payload.hotel_destino_id,
        payload.motivo,
        usuario.email,
    )
    return {"id": proposal.id, "estado": proposal.estado}


@router.post("/reubicaciones/{propuesta_id}/aprobar", response_model=AprobacionOut)
async def aprobar(
    propuesta_id: int,
    usuario: Usuario = Depends(requiere("CAP-3", "aprobar")),
    session: AsyncSession = Depends(get_app_write_session),
):
    proposal = await service.aprobar_reubicacion(
        session,
        propuesta_id,
        usuario.email,
        list(usuario.roles),
    )
    return {"estado": proposal.estado}
