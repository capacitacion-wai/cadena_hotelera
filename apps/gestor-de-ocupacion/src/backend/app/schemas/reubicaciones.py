from datetime import datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.ocupacion import HotelOut, Pagina

T = TypeVar("T")


class PropuestaIn(BaseModel):
    reserva_id: int
    hotel_destino_id: int
    motivo: str = Field(min_length=1, max_length=500)

    @field_validator("motivo")
    @classmethod
    def motivo_no_vacio(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Escribe el motivo de la propuesta.")
        return value


class PropuestaOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    huesped: str
    origen: str
    destino: str
    tarifa_origen: float
    tarifa_destino: float
    diferencia_tarifa: float
    distancia_destino_km: float
    motivo: str
    estado: str
    propuesto_por: str
    propuesto_en: datetime
    aprobado_por: str | None = None
    aprobado_en: datetime | None = None


class HuespedPorReubicarOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    reserva_id: int
    huesped: str
    origen_id: int
    origen: str


class DatosReubicacionesOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    propuestas: Pagina[PropuestaOut]
    historial: Pagina[PropuestaOut]
    huespedes_por_reubicar: Pagina[HuespedPorReubicarOut]
    destinos: Pagina[HotelOut]


class PropuestaCreadaOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    estado: str


class AprobacionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    estado: str
