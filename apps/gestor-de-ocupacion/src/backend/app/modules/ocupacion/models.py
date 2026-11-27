from sqlalchemy import Column, Date, Float, ForeignKey, Integer, String

from app.app_database import AppBase


class Hotel(AppBase):
    __tablename__ = "hoteles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    nombre = Column(String(160), nullable=False, index=True)
    tarifa_noche_eur = Column(Float, nullable=False)
    distancia_centro_valencia_km = Column(Float, nullable=False)
    habitaciones_totales = Column(Integer, nullable=False)


class Habitacion(AppBase):
    __tablename__ = "habitaciones"

    id = Column(Integer, primary_key=True, autoincrement=True)
    hotel_id = Column(Integer, ForeignKey("hoteles.id"), nullable=False, index=True)
    numero = Column(String(20), nullable=False)
    estado = Column(String(30), nullable=False, default="libre", index=True)
    detalle = Column(String(300), nullable=True)


class Reserva(AppBase):
    __tablename__ = "reservas"

    id = Column(Integer, primary_key=True, autoincrement=True)
    hotel_id = Column(Integer, ForeignKey("hoteles.id"), nullable=False, index=True)
    habitacion_id = Column(Integer, ForeignKey("habitaciones.id"), nullable=True, index=True)
    huesped = Column(String(160), nullable=False)
    estado = Column(String(30), nullable=False, default="confirmada", index=True)
    fecha = Column(Date, nullable=False, index=True)
    salida = Column(Date, nullable=False, index=True)
    motivo_cancelacion = Column(String(500), nullable=True)
    cancelada_por = Column(String(255), nullable=True)
    cancelada_en = Column(String(40), nullable=True)
