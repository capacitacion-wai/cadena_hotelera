from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.app_database import get_app_session, get_app_write_session
from app.modules.ocupacion import service
from app.schemas.ocupacion import HotelOut, OcupacionOut, Pagina, HabitacionOut, ReservaOut
from app.wai.perfilado import Usuario, requiere

router = APIRouter()


@router.get("/ocupacion", response_model=OcupacionOut)
async def ocupacion(
    _usuario: Usuario = Depends(requiere("CAP-1", "ver")),
    session: AsyncSession = Depends(get_app_session),
):
    return await service.obtener_ocupacion(session)


@router.get("/hoteles", response_model=Pagina[HotelOut])
async def hoteles(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    _usuario: Usuario = Depends(requiere("CAP-1", "ver")),
    session: AsyncSession = Depends(get_app_session),
):
    return await service.listar_hoteles(session, limit, offset)


@router.get("/hoteles/{hotel_id}", response_model=HotelOut)
async def hotel(
    hotel_id: int,
    _usuario: Usuario = Depends(requiere("CAP-1", "ver")),
    session: AsyncSession = Depends(get_app_session),
):
    return await service.obtener_hotel(session, hotel_id)


@router.get("/hoteles/{hotel_id}/habitaciones", response_model=Pagina[HabitacionOut])
async def habitaciones(
    hotel_id: int,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    _usuario: Usuario = Depends(requiere("CAP-1", "ver")),
    session: AsyncSession = Depends(get_app_session),
):
    return await service.listar_habitaciones(session, hotel_id, limit, offset)


@router.get("/hoteles/{hotel_id}/reservas", response_model=Pagina[ReservaOut])
async def reservas(
    hotel_id: int,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    _usuario: Usuario = Depends(requiere("CAP-1", "ver")),
    session: AsyncSession = Depends(get_app_session),
):
    return await service.listar_reservas(session, hotel_id, limit, offset)


@router.post("/ocupacion/datos-de-ejemplo")
async def cargar_datos_de_ejemplo(
    _usuario: Usuario = Depends(requiere("CAP-4", "crear")),
    session: AsyncSession = Depends(get_app_write_session),
):
    return await service.cargar_datos_de_ejemplo(session)
