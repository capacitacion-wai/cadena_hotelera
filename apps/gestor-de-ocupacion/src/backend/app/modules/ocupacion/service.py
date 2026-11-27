from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.ocupacion.models import Habitacion, Hotel, Reserva


def _hoy() -> date:
    return datetime.now(ZoneInfo("Europe/Madrid")).date()


def _esta_esta_noche():
    hoy = _hoy()
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
    ids = [habitacion.id for habitacion in habitaciones]
    ocupadas = set()
    if ids:
        ocupadas = set((await session.scalars(select(Reserva.habitacion_id).where(
            Reserva.habitacion_id.in_(ids),
            *_esta_esta_noche(),
        ))).all())
    items = [{
        "id": habitacion.id,
        "numero": habitacion.numero,
        "estado": "fuera_servicio" if habitacion.estado == "fuera_servicio" else (
            "ocupada" if habitacion.id in ocupadas else "libre"
        ),
        "detalle": habitacion.detalle,
    } for habitacion in habitaciones]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


async def listar_reservas(
    session: AsyncSession, hotel_id: int, periodo: str, limit: int, offset: int,
) -> dict:
    if await session.get(Hotel, hotel_id) is None:
        raise HTTPException(status_code=404, detail="No se encontró el hotel.")
    filtros = [Reserva.hotel_id == hotel_id]
    hoy = _hoy()
    if periodo == "actuales":
        filtros.extend(_esta_esta_noche())
    elif periodo == "futuras":
        filtros.extend((Reserva.estado == "confirmada", Reserva.fecha > hoy))
    elif periodo == "pasadas":
        filtros.extend((Reserva.estado == "confirmada", Reserva.salida <= hoy))
    elif periodo == "canceladas":
        filtros.append(Reserva.estado == "cancelada")
    total = await session.scalar(
        select(func.count()).select_from(Reserva).where(*filtros)
    ) or 0
    filas = (await session.execute(
        select(Reserva, Habitacion.numero)
        .outerjoin(Habitacion, Reserva.habitacion_id == Habitacion.id)
        .where(*filtros)
        .order_by(Reserva.fecha, Reserva.id)
        .limit(limit)
        .offset(offset)
    )).all()
    items = [{
        "id": reserva.id,
        "huesped": reserva.huesped,
        "habitacion_id": reserva.habitacion_id,
        "habitacion": numero or "Pendiente de asignación",
        "entrada": reserva.fecha.isoformat(),
        "salida": reserva.salida.isoformat(),
        "estado": reserva.estado,
        "motivo_cancelacion": reserva.motivo_cancelacion,
        "cancelada_por": reserva.cancelada_por,
        "cancelada_en": reserva.cancelada_en,
    } for reserva, numero in filas]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


async def _validar_habitacion_y_disponibilidad(
    session: AsyncSession,
    hotel_id: int,
    habitacion_id: int,
    entrada: date,
    salida: date,
    *,
    excluir_reserva_id: int | None = None,
) -> Habitacion:
    if salida <= entrada:
        raise HTTPException(status_code=422, detail="La salida debe ser posterior a la entrada.")
    habitacion = await session.get(Habitacion, habitacion_id)
    if habitacion is None or habitacion.hotel_id != hotel_id:
        raise HTTPException(status_code=422, detail="La habitación no pertenece a este hotel.")
    if habitacion.estado == "fuera_servicio":
        raise HTTPException(status_code=409, detail="La habitación está fuera de servicio.")

    conflicto = select(Reserva.id).where(
        Reserva.habitacion_id == habitacion_id,
        Reserva.estado == "confirmada",
        Reserva.fecha < salida,
        Reserva.salida > entrada,
    )
    if excluir_reserva_id is not None:
        conflicto = conflicto.where(Reserva.id != excluir_reserva_id)
    if await session.scalar(conflicto.limit(1)) is not None:
        raise HTTPException(status_code=409, detail="La habitación ya está reservada durante alguna de esas noches.")
    return habitacion


async def _reserva_out(session: AsyncSession, reserva: Reserva) -> dict:
    numero = None
    if reserva.habitacion_id is not None:
        habitacion = await session.get(Habitacion, reserva.habitacion_id)
        numero = habitacion.numero if habitacion else None
    return {
        "id": reserva.id,
        "huesped": reserva.huesped,
        "habitacion_id": reserva.habitacion_id,
        "habitacion": numero or "Pendiente de asignación",
        "entrada": reserva.fecha.isoformat(),
        "salida": reserva.salida.isoformat(),
        "estado": reserva.estado,
        "motivo_cancelacion": reserva.motivo_cancelacion,
        "cancelada_por": reserva.cancelada_por,
        "cancelada_en": reserva.cancelada_en,
    }


async def crear_reserva(
    session: AsyncSession,
    hotel_id: int,
    huesped: str,
    habitacion_id: int,
    entrada: date,
    salida: date,
) -> dict:
    hotel = await session.get(Hotel, hotel_id)
    if hotel is None:
        raise HTTPException(status_code=404, detail="No se encontró el hotel.")
    await _validar_habitacion_y_disponibilidad(session, hotel_id, habitacion_id, entrada, salida)
    reserva = Reserva(
        hotel_id=hotel_id,
        habitacion_id=habitacion_id,
        huesped=huesped.strip(),
        estado="confirmada",
        fecha=entrada,
        salida=salida,
    )
    session.add(reserva)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="No se pudo guardar la reserva; vuelve a intentarlo.") from None
    await session.refresh(reserva)
    return await _reserva_out(session, reserva)


async def editar_reserva(session: AsyncSession, reserva_id: int, cambios: dict) -> dict:
    reserva = await session.get(Reserva, reserva_id)
    if reserva is None:
        raise HTTPException(status_code=404, detail="No se encontró la reserva.")
    if reserva.estado != "confirmada":
        raise HTTPException(status_code=409, detail="No se puede editar una reserva cancelada.")
    entrada = cambios.get("entrada", reserva.fecha)
    salida = cambios.get("salida", reserva.salida)
    habitacion_id = cambios.get("habitacion_id", reserva.habitacion_id)
    if habitacion_id is None:
        raise HTTPException(status_code=422, detail="La reserva debe tener una habitación asignada.")
    await _validar_habitacion_y_disponibilidad(
        session,
        reserva.hotel_id,
        habitacion_id,
        entrada,
        salida,
        excluir_reserva_id=reserva.id,
    )
    reserva.habitacion_id = habitacion_id
    reserva.fecha = entrada
    reserva.salida = salida
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="No se pudo guardar el cambio; vuelve a intentarlo.") from None
    await session.refresh(reserva)
    return await _reserva_out(session, reserva)


async def cancelar_reserva(session: AsyncSession, reserva_id: int, motivo: str, persona: str) -> dict:
    reserva = await session.get(Reserva, reserva_id)
    if reserva is None:
        raise HTTPException(status_code=404, detail="No se encontró la reserva.")
    if reserva.estado != "confirmada":
        raise HTTPException(status_code=409, detail="La reserva ya está cancelada.")
    reserva.estado = "cancelada"
    reserva.motivo_cancelacion = motivo.strip()
    reserva.cancelada_por = persona
    reserva.cancelada_en = datetime.now(timezone.utc).isoformat()
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="No se pudo guardar la cancelación; vuelve a intentarlo.") from None
    await session.refresh(reserva)
    return await _reserva_out(session, reserva)


async def cargar_datos_de_ejemplo(session: AsyncSession) -> dict:
    from app import datos_de_ejemplo

    filas = await datos_de_ejemplo.cargar_si_vacia(session, incluir_usuarios_de_prueba=False)
    if not filas:
        raise HTTPException(status_code=409, detail="La aplicación ya contiene datos.")
    await session.commit()
    return {"cargados": True}
