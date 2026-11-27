from datetime import date
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    habitacion_id: int | None
    habitacion: str
    entrada: str
    salida: str
    estado: str
    motivo_cancelacion: str | None = None
    cancelada_por: str | None = None
    cancelada_en: str | None = None


class ReservaCrearIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    huesped: str = Field(min_length=1, max_length=160)
    habitacion_id: int = Field(gt=0)
    entrada: date
    salida: date

    @field_validator("huesped")
    @classmethod
    def huesped_no_vacio(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Indica el nombre del huésped.")
        return value

    @model_validator(mode="after")
    def salida_posterior(self):
        if self.salida <= self.entrada:
            raise ValueError("La salida debe ser posterior a la entrada.")
        return self


class ReservaEditarIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    habitacion_id: int | None = Field(default=None, gt=0)
    entrada: date | None = None
    salida: date | None = None

    @model_validator(mode="after")
    def cambios_validos(self):
        if not self.model_fields_set:
            raise ValueError("Indica las fechas o la habitación que quieres cambiar.")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("Los campos enviados no pueden estar vacíos.")
        if self.entrada is not None and self.salida is not None and self.salida <= self.entrada:
            raise ValueError("La salida debe ser posterior a la entrada.")
        return self


class ReservaCancelarIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    motivo: str = Field(min_length=1, max_length=500)

    @field_validator("motivo")
    @classmethod
    def motivo_no_vacio(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Indica el motivo de cancelación.")
        return value
