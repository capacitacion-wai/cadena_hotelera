from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.app_database import get_app_session, get_app_write_session
from app.modules.ocupacion import service
from app.schemas.ocupacion import (
    HotelOut,
    OcupacionOut,
    Pagina,
    HabitacionOut,
    ReservaOut,
    ReservaCrearIn,
    ReservaEditarIn,
    ReservaCancelarIn,
)
from app.wai.perfilado import Usuario, requiere

router = APIRouter()


def exigir_operador_reservas(usuario: Usuario) -> None:
    if not {"admin", "direccion", "recepcion"}.intersection(usuario.roles):
        raise HTTPException(
            status_code=403,
            detail="Solo Administrador, Dirección y Recepción pueden gestionar reservas.",
            headers={"X-WAI-Codigo": "PERFILADO_SIN_PERMISO"},
        )


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
    periodo: Literal["actuales", "futuras", "pasadas", "canceladas", "todas"] = "actuales",
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    _usuario: Usuario = Depends(requiere("CAP-1", "ver")),
    session: AsyncSession = Depends(get_app_session),
):
    return await service.listar_reservas(session, hotel_id, periodo, limit, offset)


@router.post(
    "/hoteles/{hotel_id}/reservas",
    response_model=ReservaOut,
    status_code=status.HTTP_201_CREATED,
)
async def crear_reserva(
    hotel_id: int,
    payload: ReservaCrearIn,
    usuario: Usuario = Depends(requiere("CAP-5", "crear")),
    session: AsyncSession = Depends(get_app_write_session),
):
    exigir_operador_reservas(usuario)
    return await service.crear_reserva(
        session,
        hotel_id,
        payload.huesped,
        payload.habitacion_id,
        payload.entrada,
        payload.salida,
    )


@router.patch("/reservas/{reserva_id}", response_model=ReservaOut)
async def editar_reserva(
    reserva_id: int,
    payload: ReservaEditarIn,
    usuario: Usuario = Depends(requiere("CAP-5", "editar")),
    session: AsyncSession = Depends(get_app_write_session),
):
    exigir_operador_reservas(usuario)
    return await service.editar_reserva(session, reserva_id, payload.model_dump(exclude_unset=True))


@router.post("/reservas/{reserva_id}/cancelar", response_model=ReservaOut)
async def cancelar_reserva(
    reserva_id: int,
    payload: ReservaCancelarIn,
    usuario: Usuario = Depends(requiere("CAP-5", "editar")),
    session: AsyncSession = Depends(get_app_write_session),
):
    exigir_operador_reservas(usuario)
    return await service.cancelar_reserva(session, reserva_id, payload.motivo, usuario.email)


@router.post("/ocupacion/datos-de-ejemplo")
async def cargar_datos_de_ejemplo(
    _usuario: Usuario = Depends(requiere("CAP-4", "crear")),
    session: AsyncSession = Depends(get_app_write_session),
):
    return await service.cargar_datos_de_ejemplo(session)
