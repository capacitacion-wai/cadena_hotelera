from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict

T = TypeVar("T")


class Pagina(BaseModel, Generic[T]):
    model_config = ConfigDict(from_attributes=True)

    items: list[T]
    total: int
    limit: int
    offset: int


class HotelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    nombre: str
    tarifa_noche_eur: float
    distancia_centro_valencia_km: float
    habitaciones_totales: int
    fuera_servicio: int
    vendidas: int
    disponibles: int
    libres: int
    ocupacion_porcentaje: float
    motivo_fuera_servicio: str | None = None


class OcupacionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    hoteles: int
    vendidas: int
    fuera_servicio: int
    sobreventa: int


class HabitacionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    numero: str
    estado: str
    detalle: str | None = None


class ReservaOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    huesped: str
    habitacion: str
    entrada: str
    salida: str
    estado: str
