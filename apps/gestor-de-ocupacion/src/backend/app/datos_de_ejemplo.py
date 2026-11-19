from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession


FILAS = {
    "base": {
        "hoteles": [
            {"id": 1, "nombre": "Gran Hotel Centro", "tarifa_noche_eur": 180, "distancia_centro_valencia_km": 0.5, "habitaciones_totales": 30, "reservas_de_esta_noche": 30},
            {"id": 2, "nombre": "Hotel Playa Malvarrosa", "tarifa_noche_eur": 210, "distancia_centro_valencia_km": 4.5, "habitaciones_totales": 24, "reservas_de_esta_noche": 23},
            {"id": 3, "nombre": "Hotel Aeropuerto", "tarifa_noche_eur": 120, "distancia_centro_valencia_km": 12, "habitaciones_totales": 40, "reservas_de_esta_noche": 24},
            {"id": 4, "nombre": "Hotel Sierra Calderona", "tarifa_noche_eur": 140, "distancia_centro_valencia_km": 45, "habitaciones_totales": 20, "reservas_de_esta_noche": 10},
            {"id": 5, "nombre": "Hotel Congresos", "tarifa_noche_eur": 95, "distancia_centro_valencia_km": 18, "habitaciones_totales": 35, "reservas_de_esta_noche": 14},
        ],
        "habitaciones_fuera_servicio": [
            {"hotel_id": 1, "numero": "201", "detalle": "Fuga de agua"},
            {"hotel_id": 1, "numero": "202", "detalle": "Fuga de agua"},
            {"hotel_id": 1, "numero": "203", "detalle": "Fuga de agua"},
        ],
        "huespedes_sin_habitacion": [
            {"hotel_id": 1, "huesped": "Amparo Vidal", "reserva_id": 10001},
            {"hotel_id": 1, "huesped": "Jordi Roca", "reserva_id": 10002},
            {"hotel_id": 1, "huesped": "Lucía Pérez", "reserva_id": 10003},
        ],
        "propuestas_pendientes": [
            {"huesped": "Amparo Vidal", "destino": "Hotel Congresos", "motivo": "Habitación fuera de servicio por una fuga de agua."},
        ],
        "reubicacion_aprobada": {
            "huesped": "Sergio Martí",
            "origen": "Gran Hotel Centro",
            "destino": "Hotel Congresos",
            "motivo": "Avería en la habitación asignada.",
        },
    },
    "vacio": {},
}


async def _base(session: AsyncSession, *, incluir_usuarios_de_prueba: bool = True) -> int:
    from app.modules.ocupacion.models import Habitacion, Hotel, Reserva
    from app.modules.reubicaciones.models import HistorialReubicacion, PropuestaReubicacion
    from app.wai.perfilado import restaurar_usuarios_de_prueba

    creados = await restaurar_usuarios_de_prueba(session) if incluir_usuarios_de_prueba else 0
    hoy = datetime.now(ZoneInfo("Europe/Madrid")).date()
    habitaciones_fuera = {
        (fila["hotel_id"], fila["numero"]): fila["detalle"]
        for fila in FILAS["base"]["habitaciones_fuera_servicio"]
    }
    hoteles: dict[str, Hotel] = {}
    for fila in FILAS["base"]["hoteles"]:
        hotel = Hotel(
            nombre=fila["nombre"],
            tarifa_noche_eur=fila["tarifa_noche_eur"],
            distancia_centro_valencia_km=fila["distancia_centro_valencia_km"],
            habitaciones_totales=fila["habitaciones_totales"],
        )
        session.add(hotel)
        hoteles[fila["nombre"]] = hotel
    await session.flush()

    habitaciones_por_hotel: dict[int, list[Habitacion]] = {}
    for fila in FILAS["base"]["hoteles"]:
        hotel = hoteles[fila["nombre"]]
        cuartos = []
        primer_numero = hotel.id * 100 + 101
        for offset in range(hotel.habitaciones_totales):
            numero = str(primer_numero + offset)
            detalle = habitaciones_fuera.get((fila["id"], numero))
            habitacion = Habitacion(
                hotel_id=hotel.id,
                numero=numero,
                estado="fuera_servicio" if detalle else "libre",
                detalle=detalle,
            )
            session.add(habitacion)
            cuartos.append(habitacion)
        habitaciones_por_hotel[hotel.id] = cuartos
    await session.flush()

    reservas_por_huesped: dict[str, Reserva] = {}
    huespedes_libres = {fila["id"]: [] for fila in FILAS["base"]["hoteles"]}
    reservas_dataset = {
        fila["huesped"]: fila["reserva_id"]
        for fila in FILAS["base"]["huespedes_sin_habitacion"]
        if "reserva_id" in fila
    }
    for fila in FILAS["base"]["huespedes_sin_habitacion"]:
        huespedes_libres.setdefault(fila["hotel_id"], []).append(fila["huesped"])

    for fila in FILAS["base"]["hoteles"]:
        hotel = hoteles[fila["nombre"]]
        reservas_noche = fila["reservas_de_esta_noche"]
        huespedes_sin_habitacion = huespedes_libres.get(fila["id"], [])
        for index in range(reservas_noche):
            if index < len(huespedes_sin_habitacion):
                huesped = huespedes_sin_habitacion[index]
                habitacion_id = None
            else:
                disponibles = [
                    room for room in habitaciones_por_hotel[hotel.id]
                    if room.estado == "libre"
                ]
                if not disponibles:
                    continue
                room = disponibles[0]
                room.estado = "ocupada"
                habitacion_id = room.id
                huesped = f"Huésped {hotel.id}-{index + 1:02d}"
            reserva = Reserva(
                id=reservas_dataset.get(huesped),
                hotel_id=hotel.id,
                habitacion_id=habitacion_id,
                huesped=huesped,
                estado="confirmada",
                fecha=hoy,
                salida=hoy + timedelta(days=1),
            )
            session.add(reserva)
            reservas_por_huesped[huesped] = reserva
            creados += 1
    await session.flush()
    creados += len(hoteles) + sum(len(rows) for rows in habitaciones_por_hotel.values())

    origen = hoteles[FILAS["base"]["hoteles"][0]["nombre"]]
    for index, fila in enumerate(FILAS["base"]["propuestas_pendientes"]):
        reserva = reservas_por_huesped[fila["huesped"]]
        destino = hoteles[fila["destino"]]
        propuesta = PropuestaReubicacion(
            reserva_id=reserva.id,
            hotel_origen_id=origen.id,
            hotel_destino_id=destino.id,
            motivo=fila["motivo"],
            estado="pendiente",
            propuesto_por="recepcion@hoteles.example",
            propuesto_en=datetime.now(timezone.utc) - timedelta(minutes=15 - index),
        )
        session.add(propuesta)
        await session.flush()
        session.add(HistorialReubicacion(
            propuesta_id=propuesta.id,
            accion="propuesta_creada",
            persona="recepcion@hoteles.example",
            fecha_hora=propuesta.propuesto_en,
        ))
        creados += 2

    movimiento = FILAS["base"]["reubicacion_aprobada"]
    destino = hoteles[movimiento["destino"]]
    cuarto_historico = next(room for room in habitaciones_por_hotel[destino.id] if room.estado == "libre")
    reserva_historica = Reserva(
        hotel_id=destino.id,
        habitacion_id=cuarto_historico.id,
        huesped=movimiento["huesped"],
        estado="confirmada",
        fecha=hoy - timedelta(days=1),
        salida=hoy,
    )
    session.add(reserva_historica)
    await session.flush()
    ahora = datetime.now(timezone.utc) - timedelta(days=1)
    propuesta_historica = PropuestaReubicacion(
        reserva_id=reserva_historica.id,
        hotel_origen_id=origen.id,
        hotel_destino_id=destino.id,
        motivo=movimiento["motivo"],
        estado="aprobada",
        propuesto_por="recepcion@hoteles.example",
        propuesto_en=ahora,
        aprobado_por="direccion@hoteles.example",
        aprobado_en=ahora + timedelta(minutes=24),
    )
    session.add(propuesta_historica)
    await session.flush()
    session.add_all([
        HistorialReubicacion(
            propuesta_id=propuesta_historica.id,
            accion="propuesta_creada",
            persona=propuesta_historica.propuesto_por,
            fecha_hora=propuesta_historica.propuesto_en,
        ),
        HistorialReubicacion(
            propuesta_id=propuesta_historica.id,
            accion="reubicacion_aprobada_y_ejecutada",
            persona=propuesta_historica.aprobado_por,
            fecha_hora=propuesta_historica.aprobado_en,
        ),
    ])
    return creados + 4


async def _vacio(session: AsyncSession) -> int:
    from app.wai.perfilado import restaurar_usuarios_de_prueba

    return await restaurar_usuarios_de_prueba(session)


CONJUNTOS = {
    "base": ("Cinco hoteles, sobreventa y alternativas para reubicar", _base),
    "vacio": ("Sin reservas para consultar el estado vacío", _vacio),
}


async def cargar_si_vacia(session: AsyncSession, *, incluir_usuarios_de_prueba: bool = True) -> int:
    from app.modules.ocupacion.models import Hotel

    total = await session.scalar(select(func.count()).select_from(Hotel)) or 0
    if total:
        return 0
    return await _base(session, incluir_usuarios_de_prueba=incluir_usuarios_de_prueba)
