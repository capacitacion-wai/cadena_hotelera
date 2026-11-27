from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.ocupacion.models import Habitacion, Hotel, Reserva
from app.modules.ocupacion.service import _esta_esta_noche, resumen_hotel
from app.modules.reubicaciones.models import HistorialReubicacion, PropuestaReubicacion


async def _propuesta_salida(session: AsyncSession, propuesta: PropuestaReubicacion) -> dict:
    reserva = await session.get(Reserva, propuesta.reserva_id)
    origen = await session.get(Hotel, propuesta.hotel_origen_id)
    destino = await session.get(Hotel, propuesta.hotel_destino_id)
    if reserva is None or origen is None or destino is None:
        raise HTTPException(status_code=409, detail="La propuesta ya no tiene todos sus datos asociados.")
    return {
        "id": propuesta.id,
        "huesped": reserva.huesped,
        "origen": origen.nombre,
        "destino": destino.nombre,
        "tarifa_origen": origen.tarifa_noche_eur,
        "tarifa_destino": destino.tarifa_noche_eur,
        "diferencia_tarifa": round(destino.tarifa_noche_eur - origen.tarifa_noche_eur, 2),
        "distancia_destino_km": destino.distancia_centro_valencia_km,
        "motivo": propuesta.motivo,
        "estado": propuesta.estado,
        "propuesto_por": propuesta.propuesto_por,
        "propuesto_en": propuesta.propuesto_en,
        "aprobado_por": propuesta.aprobado_por,
        "aprobado_en": propuesta.aprobado_en,
    }


async def listar_reubicaciones(
    session: AsyncSession,
    limit: int,
    offset: int,
    historial_limit: int,
    historial_offset: int,
) -> dict:
    hoteles = (await session.execute(select(Hotel).order_by(Hotel.id))).scalars().all()
    resumentes = [await resumen_hotel(session, hotel) for hotel in hoteles]
    hoteles_por_id = {fila["id"]: fila for fila in resumentes}

    pendientes_filtro = PropuestaReubicacion.estado == "pendiente"
    total_pendientes = await session.scalar(
        select(func.count()).select_from(PropuestaReubicacion).where(pendientes_filtro)
    ) or 0
    pendientes = (await session.execute(
        select(PropuestaReubicacion).where(pendientes_filtro)
        .order_by(PropuestaReubicacion.propuesto_en.desc(), PropuestaReubicacion.id.desc())
        .limit(limit).offset(offset)
    )).scalars().all()

    aprobadas_filtro = PropuestaReubicacion.estado == "aprobada"
    total_historial = await session.scalar(
        select(func.count()).select_from(PropuestaReubicacion).where(aprobadas_filtro)
    ) or 0
    aprobadas = (await session.execute(
        select(PropuestaReubicacion).where(aprobadas_filtro)
        .order_by(PropuestaReubicacion.aprobado_en.desc(), PropuestaReubicacion.id.desc())
        .limit(historial_limit).offset(historial_offset)
    )).scalars().all()

    pendientes_ids = (await session.execute(
        select(PropuestaReubicacion.reserva_id).where(pendientes_filtro)
    )).scalars().all()
    sin_propuesta = (await session.execute(
        select(Reserva).where(
            Reserva.habitacion_id.is_(None),
            *_esta_esta_noche(),
        ).order_by(Reserva.hotel_id, Reserva.id)
    )).scalars().all()
    posibles = [{
        "reserva_id": reserva.id,
        "huesped": reserva.huesped,
        "origen_id": reserva.hotel_id,
        "origen": hoteles_por_id[reserva.hotel_id]["nombre"],
    } for reserva in sin_propuesta
        if reserva.id not in pendientes_ids
        and reserva.hotel_id in hoteles_por_id
        and hoteles_por_id[reserva.hotel_id]["vendidas"] > hoteles_por_id[reserva.hotel_id]["disponibles"]]

    return {
        "propuestas": {
            "items": [await _propuesta_salida(session, proposal) for proposal in pendientes],
            "total": total_pendientes,
            "limit": limit,
            "offset": offset,
        },
        "historial": {
            "items": [await _propuesta_salida(session, proposal) for proposal in aprobadas],
            "total": total_historial,
            "limit": historial_limit,
            "offset": historial_offset,
        },
        "huespedes_por_reubicar": {
            "items": posibles[:100],
            "total": len(posibles),
            "limit": 100,
            "offset": 0,
        },
        "destinos": {
            "items": resumentes[:100],
            "total": len(resumentes),
            "limit": 100,
            "offset": 0,
        },
    }


async def proponer_reubicacion(
    session: AsyncSession,
    reserva_id: int,
    hotel_destino_id: int,
    motivo: str,
    persona: str,
) -> PropuestaReubicacion:
    hoy = datetime.now(ZoneInfo("Europe/Madrid")).date()
    reserva = await session.get(Reserva, reserva_id)
    destino = await session.get(Hotel, hotel_destino_id)
    if reserva is None or destino is None:
        raise HTTPException(status_code=404, detail="No se encontró el huésped o el hotel de destino.")
    if reserva.habitacion_id is not None or not (reserva.fecha <= hoy < reserva.salida) or reserva.estado != "confirmada":
        raise HTTPException(status_code=409, detail="Este huésped no está pendiente de reubicación.")
    if reserva.hotel_id == hotel_destino_id:
        raise HTTPException(status_code=422, detail="El destino debe ser otro hotel de la cadena.")
    activa = await session.scalar(
        select(PropuestaReubicacion.id).where(
            PropuestaReubicacion.reserva_id == reserva_id,
            PropuestaReubicacion.estado == "pendiente",
        ).limit(1)
    )
    if activa is not None:
        raise HTTPException(status_code=409, detail="Ya hay una propuesta pendiente para este huésped.")
    resumen = await resumen_hotel(session, destino)
    if resumen["libres"] <= 0:
        raise HTTPException(status_code=409, detail="El hotel elegido ya no tiene habitaciones libres.")

    proposal = PropuestaReubicacion(
        reserva_id=reserva.id,
        hotel_origen_id=reserva.hotel_id,
        hotel_destino_id=destino.id,
        motivo=motivo.strip(),
        estado="pendiente",
        propuesto_por=persona,
        propuesto_en=datetime.now(timezone.utc),
    )
    session.add(proposal)
    try:
        await session.flush()
        session.add(HistorialReubicacion(
            propuesta_id=proposal.id,
            accion="propuesta_creada",
            persona=persona,
            fecha_hora=datetime.now(timezone.utc),
        ))
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="No se pudo guardar la propuesta; vuelve a intentarlo.")
    await session.refresh(proposal)
    return proposal


async def aprobar_reubicacion(
    session: AsyncSession,
    propuesta_id: int,
    persona: str,
    roles: list[str],
) -> PropuestaReubicacion:
    if "direccion" not in roles:
        raise HTTPException(status_code=403, detail="Solo Dirección puede aprobar una reubicación.")
    proposal = await session.get(PropuestaReubicacion, propuesta_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="No se encontró la propuesta.")
    if proposal.estado != "pendiente":
        raise HTTPException(status_code=409, detail="La propuesta ya no está pendiente.")
    reservation = await session.get(Reserva, proposal.reserva_id)
    if reservation is None or reservation.hotel_id != proposal.hotel_origen_id or reservation.habitacion_id is not None:
        raise HTTPException(status_code=409, detail="La reserva ya no está pendiente de traslado.")
    if not (reservation.fecha <= datetime.now(ZoneInfo("Europe/Madrid")).date() < reservation.salida) or reservation.estado != "confirmada":
        raise HTTPException(status_code=409, detail="La estancia ya no está activa esta noche.")

    ocupada_esta_noche = select(Reserva.id).where(
        Reserva.habitacion_id == Habitacion.id,
        *_esta_esta_noche(),
    ).exists()
    room = (await session.execute(
        select(Habitacion).where(
            Habitacion.hotel_id == proposal.hotel_destino_id,
            Habitacion.estado != "fuera_servicio",
            ~ocupada_esta_noche,
        ).order_by(Habitacion.numero).limit(1)
    )).scalars().first()
    if room is None:
        raise HTTPException(status_code=409, detail="El hotel de destino ya no tiene habitaciones libres.")

    now = datetime.now(timezone.utc)
    reservation.hotel_id = proposal.hotel_destino_id
    reservation.habitacion_id = room.id
    proposal.estado = "aprobada"
    proposal.aprobado_por = persona
    proposal.aprobado_en = now
    session.add(HistorialReubicacion(
        propuesta_id=proposal.id,
        accion="reubicacion_aprobada_y_ejecutada",
        persona=persona,
        fecha_hora=now,
    ))
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="No se pudo guardar la aprobación; vuelve a intentarlo.")
    await session.refresh(proposal)
    return proposal
