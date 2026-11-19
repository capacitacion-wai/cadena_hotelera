from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.ocupacion.models import Habitacion, Hotel, Reserva


def _esta_esta_noche():
    hoy = datetime.now(ZoneInfo("Europe/Madrid")).date()
    return (
        Reserva.estado == "confirmada",
        Reserva.fecha <= hoy,
        Reserva.salida > hoy,
    )


async def resumen_hotel(session: AsyncSession, hotel: Hotel) -> dict:
    fuera_servicio = await session.scalar(
        select(func.count()).select_from(Habitacion).where(
            Habitacion.hotel_id == hotel.id,
            Habitacion.estado == "fuera_servicio",
        )
    ) or 0
    vendidas = await session.scalar(
        select(func.count()).select_from(Reserva).where(
            Reserva.hotel_id == hotel.id,
            *_esta_esta_noche(),
        )
    ) or 0
    disponibles = max(hotel.habitaciones_totales - fuera_servicio, 0)
    libres = max(disponibles - vendidas, 0)
    ocupacion = round(vendidas * 100 / disponibles, 1) if disponibles else 0.0
    motivo = await session.scalar(
        select(Habitacion.detalle).where(
            Habitacion.hotel_id == hotel.id,
            Habitacion.estado == "fuera_servicio",
            Habitacion.detalle.is_not(None),
        ).limit(1)
    )
    return {
        "id": hotel.id,
        "nombre": hotel.nombre,
        "tarifa_noche_eur": hotel.tarifa_noche_eur,
        "distancia_centro_valencia_km": hotel.distancia_centro_valencia_km,
        "habitaciones_totales": hotel.habitaciones_totales,
        "fuera_servicio": fuera_servicio,
        "vendidas": vendidas,
        "disponibles": disponibles,
        "libres": libres,
        "ocupacion_porcentaje": ocupacion,
        "motivo_fuera_servicio": motivo,
    }


async def obtener_ocupacion(session: AsyncSession) -> dict:
    hoteles = (await session.execute(select(Hotel))).scalars().all()
    filas = [await resumen_hotel(session, hotel) for hotel in hoteles]
    return {
        "hoteles": len(filas),
        "vendidas": sum(fila["vendidas"] for fila in filas),
        "fuera_servicio": sum(fila["fuera_servicio"] for fila in filas),
        "sobreventa": sum(max(fila["vendidas"] - fila["disponibles"], 0) for fila in filas),
    }


async def listar_hoteles(session: AsyncSession, limit: int, offset: int) -> dict:
    total = await session.scalar(select(func.count()).select_from(Hotel)) or 0
    hoteles = (await session.execute(
        select(Hotel).order_by(Hotel.id).limit(limit).offset(offset)
    )).scalars().all()
    return {
        "items": [await resumen_hotel(session, hotel) for hotel in hoteles],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


async def obtener_hotel(session: AsyncSession, hotel_id: int) -> dict:
    hotel = await session.get(Hotel, hotel_id)
    if hotel is None:
        raise HTTPException(status_code=404, detail="No se encontró el hotel.")
    return await resumen_hotel(session, hotel)


async def listar_habitaciones(session: AsyncSession, hotel_id: int, limit: int, offset: int) -> dict:
    if await session.get(Hotel, hotel_id) is None:
        raise HTTPException(status_code=404, detail="No se encontró el hotel.")
    filtro = Habitacion.hotel_id == hotel_id
    total = await session.scalar(
        select(func.count()).select_from(Habitacion).where(filtro)
    ) or 0
    habitaciones = (await session.execute(
        select(Habitacion).where(filtro).order_by(Habitacion.numero).limit(limit).offset(offset)
    )).scalars().all()
    return {"items": habitaciones, "total": total, "limit": limit, "offset": offset}


async def listar_reservas(session: AsyncSession, hotel_id: int, limit: int, offset: int) -> dict:
    if await session.get(Hotel, hotel_id) is None:
        raise HTTPException(status_code=404, detail="No se encontró el hotel.")
    filtros = (Reserva.hotel_id == hotel_id, *_esta_esta_noche())
    total = await session.scalar(
        select(func.count()).select_from(Reserva).where(*filtros)
    ) or 0
    filas = (await session.execute(
        select(Reserva, Habitacion.numero)
        .outerjoin(Habitacion, Reserva.habitacion_id == Habitacion.id)
        .where(*filtros)
        .order_by(Reserva.id)
        .limit(limit)
        .offset(offset)
    )).all()
    items = [{
        "id": reserva.id,
        "huesped": reserva.huesped,
        "habitacion": numero or "Pendiente de asignación",
        "entrada": reserva.fecha.isoformat(),
        "salida": reserva.salida.isoformat(),
        "estado": reserva.estado.capitalize(),
    } for reserva, numero in filas]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


async def cargar_datos_de_ejemplo(session: AsyncSession) -> dict:
    from app import datos_de_ejemplo

    filas = await datos_de_ejemplo.cargar_si_vacia(session, incluir_usuarios_de_prueba=False)
    if not filas:
        raise HTTPException(status_code=409, detail="La aplicación ya contiene datos.")
    await session.commit()
    return {"cargados": True}
