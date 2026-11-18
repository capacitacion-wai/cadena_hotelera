from sqlalchemy import Column, DateTime, ForeignKey, Integer, String

from app.app_database import AppBase


class PropuestaReubicacion(AppBase):
    __tablename__ = "propuestas_reubicacion"

    id = Column(Integer, primary_key=True, autoincrement=True)
    reserva_id = Column(Integer, ForeignKey("reservas.id"), nullable=False, index=True)
    hotel_origen_id = Column(Integer, ForeignKey("hoteles.id"), nullable=False)
    hotel_destino_id = Column(Integer, ForeignKey("hoteles.id"), nullable=False)
    motivo = Column(String(500), nullable=False)
    estado = Column(String(30), nullable=False, default="pendiente", index=True)
    propuesto_por = Column(String(255), nullable=False)
    propuesto_en = Column(DateTime(timezone=True), nullable=False)
    aprobado_por = Column(String(255), nullable=True)
    aprobado_en = Column(DateTime(timezone=True), nullable=True)


class HistorialReubicacion(AppBase):
    __tablename__ = "historial_reubicacion"

    id = Column(Integer, primary_key=True, autoincrement=True)
    propuesta_id = Column(Integer, ForeignKey("propuestas_reubicacion.id"), nullable=False, index=True)
    accion = Column(String(50), nullable=False)
    persona = Column(String(255), nullable=False)
    fecha_hora = Column(DateTime(timezone=True), nullable=False)
