"""What goes in and out of the profiling API (explicit models, never the database row).

INVARIANT FILE — installed by `wai-app-builder/assets/modulos.py`; do not edit it.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class UsuarioOut(BaseModel):
    id: int
    email: str
    nombre: Optional[str] = None
    roles: List[str]
    activo: bool
    origen: Optional[str] = None
    es_dueno: bool = False
    equipo: Optional[str] = None
    organizacion: Optional[str] = None
    cartera: Optional[List[str]] = None
    creado_en: Optional[str] = None
    actualizado_en: Optional[str] = None


class UsuarioBorradoOut(BaseModel):
    """`DELETE /usuarios/{id}`: who was deleted (their record is gone; their business rows stay)."""
    borrado: bool = True
    id: int
    email: str
    nombre: Optional[str] = None
    #: An external account whose platform account could NOT be closed now (the app refuses it anyway).
    cuenta_externa_abierta: bool = False


class PaginaDeUsuarios(BaseModel):
    items: List[UsuarioOut]
    total: int
    limite: int
    desplazamiento: int


class AltaIn(BaseModel):
    """Level 4: `organizacion` = a person of that organisation (an organisation's administrator always
    adds to their own, whatever is sent)."""
    email: str
    roles: List[str] = Field(min_length=1)
    nombre: Optional[str] = None
    organizacion: Optional[str] = None


class AltaDesdeProyectoIn(BaseModel):
    emails: List[str] = Field(min_length=1, max_length=200)
    roles: List[str] = Field(min_length=1)


class AltaDesdeProyectoOut(BaseModel):
    dados_de_alta: List[str]
    ya_estaban: List[str]
    no_son_del_proyecto: List[str]


class CambioIn(BaseModel):
    """Only the fields SENT change; `equipo: null` takes the person out of their team (level 3);
    `organizacion: null` makes them internal and `cartera: null` gives an internal person every
    organisation, a list only those (level 4; internal administrators only)."""
    roles: Optional[List[str]] = None
    activo: Optional[bool] = None
    nombre: Optional[str] = None
    equipo: Optional[str] = None
    organizacion: Optional[str] = None
    cartera: Optional[List[str]] = None


class MiembroDelProyecto(BaseModel):
    email: str
    nombre: str = ""
    dado_de_alta: bool


class MiembrosOut(BaseModel):
    disponible: bool
    miembros: List[MiembroDelProyecto]


class YoOut(BaseModel):
    email: str
    nombre: Optional[str] = None
    roles: List[str]
    nombres_de_roles: Dict[str, str]
    administra: bool
    es_dueno: bool
    nivel: int
    alta: str
    permisos: Dict[str, List[str]]
    capacidades: List[Dict[str, Any]]
    equipo: Optional[str] = None
    nombre_de_equipo: Optional[str] = None
    alcances: Dict[str, Dict[str, str]] = Field(default_factory=dict)
    #: Some permission says «their team» (level 3+): without it teams change nothing and are not shown.
    usa_equipos: bool = False
    # level 4
    organizacion: Optional[str] = None
    nombre_de_organizacion: Optional[str] = None
    es_de_organizacion: bool = False
    administra_organizacion: bool = False
    cartera: Optional[List[str]] = None
    externa: bool = False
    nombre_de_las_organizaciones: Optional[str] = None


class MatrizOut(BaseModel):
    nivel: int
    ajustes: Dict[str, Any]
    acciones: List[str]
    capacidades: List[Dict[str, Any]]
    roles: List[Dict[str, Any]]
    #: The capabilities limited to «their team» by some role (`permisos.capacidades_por_equipo`).
    por_equipo: List[str] = Field(default_factory=list)
    #: Custom roles are on (setting `roles_personalizables`): the administrator can create roles.
    personalizables: bool = False


class RolPropioIn(BaseModel):
    """A custom role: what the administrator chose (validated against the Definition's pieces)."""
    nombre: str
    quien: Optional[str] = None
    ambito: Optional[str] = None
    permisos: Dict[str, List[str]]
    alcances: Dict[str, Dict[str, str]] = Field(default_factory=dict)


class EquipoOut(BaseModel):
    id: str
    nombre: str
    activo: bool
    miembros: int = 0
    creado_en: Optional[str] = None


class EquipoIn(BaseModel):
    nombre: str


class CambioDeEquipoIn(BaseModel):
    nombre: Optional[str] = None
    activo: Optional[bool] = None


class SinDuenoOut(BaseModel):
    tabla: str
    existe: bool
    columnas: bool
    sin_dueno: int
    total: int


class AsignarSinDuenoIn(BaseModel):
    dueno: str
    equipo: Optional[str] = None


class OrganizacionOut(BaseModel):
    id: str
    nombre: str
    activo: bool
    miembros: int = 0
    creado_en: Optional[str] = None


class OrganizacionIn(BaseModel):
    nombre: str


class CambioDeOrganizacionIn(BaseModel):
    nombre: Optional[str] = None
    activo: Optional[bool] = None


class SinOrganizacionOut(BaseModel):
    tabla: str
    existe: bool
    columnas: bool
    sin_organizacion: int
    total: int


class AsignarSinOrganizacionIn(BaseModel):
    organizacion: str


class PedirCodigoIn(BaseModel):
    email: str


class EntrarIn(BaseModel):
    email: str
    codigo: str = Field(min_length=1, max_length=20)


class SesionOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: Optional[int] = None
