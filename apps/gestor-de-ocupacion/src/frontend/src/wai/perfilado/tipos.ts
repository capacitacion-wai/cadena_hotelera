/**
 * Profiling module — types shared by its screens and the app's adapter.
 *
 * INVARIANT FILE — installed by `wai-app-builder/assets/modulos.py`; do not edit it.
 */
import type { ReactNode } from 'react'

export type Accion = 'ver' | 'crear' | 'editar' | 'borrar' | 'aprobar' | 'exportar'

/** Level 3: which rows an allowed action reaches. */
export type Alcance = 'propio' | 'equipo' | 'todo'

/** `que` is the capability's sentence in the Definition; `nombre`, when the Definition gives one, its short name. */
export interface Capacidad { id: string; que: string; nombre?: string; acciones?: Accion[] }

export interface Rol {
  id: string
  nombre: string
  quien?: string
  fijo: boolean
  administra: boolean
  alcance?: string
  permisos: Record<string, Accion[]>
  /** Levels 1-2: {capability: scope} (informative). Level 3: {capability: {action: scope}} (applied). */
  alcances?: Record<string, string | Record<string, Alcance>>
  /** Level 4: which side of the organisation wall, and whether it administers ITS organisation. */
  ambito?: 'interno' | 'organizacion'
  administra_organizacion?: boolean
  /** A custom role (setting `roles_personalizables`): `aplicacion`, with who created / last changed it
   *  and (internal administrators) who holds it. The Definition's roles have no `origen`. */
  origen?: 'aplicacion' | null
  creado_por?: string | null
  creado_en?: string | null
  cambiado_por?: string | null
  cambiado_en?: string | null
  personas?: { email: string; nombre: string | null }[]
}

/** What a custom role can be built from: {side: {capability: {action: [scopes, narrowest first]}}}. */
export type Piezas = Partial<Record<'interno' | 'organizacion', Record<string, Partial<Record<Accion, Alcance[]>>>>>

/** A custom role as the administrator composed it. */
export interface RolPropio {
  nombre: string
  quien: string
  ambito: 'interno' | 'organizacion'
  permisos: Record<string, Accion[]>
  alcances: Record<string, Partial<Record<Accion, Alcance>>>
}

export interface Matriz {
  nivel: number
  ajustes: { roles_personalizables?: boolean; alta?: string; nombre_organizacion?: string | null }
  acciones: Accion[]
  capacidades: Capacidad[]
  roles: Rol[]
  /** The capabilities some role limits to «their team» (empty = teams change nothing). */
  por_equipo?: string[]
  /** Custom roles are on: the administrator can create roles in the app. */
  personalizables?: boolean
}

export interface Usuario {
  id: number
  email: string
  nombre: string | null
  roles: string[]
  activo: boolean
  origen: string | null
  es_dueno: boolean
  equipo?: string | null
  /** Level 4: the person's organisation (null = internal) and an internal person's portfolio (null = all). */
  organizacion?: string | null
  cartera?: string[] | null
}

/** `DELETE /usuarios/{id}`: who was deleted; `cuenta_externa_abierta` = the platform could not close
 *  their external account now (the app refuses them anyway). */
export interface UsuarioBorrado { borrado: boolean; id: number; email: string; nombre: string | null; cuenta_externa_abierta: boolean }

export interface PaginaDeUsuarios { items: Usuario[]; total: number; limite: number; desplazamiento: number }

export interface Yo {
  email: string
  nombre: string | null
  roles: string[]
  nombres_de_roles: Record<string, string>
  administra: boolean
  es_dueno: boolean
  nivel: number
  alta: string
  permisos: Record<string, Accion[]>
  capacidades: Capacidad[]
  equipo?: string | null
  nombre_de_equipo?: string | null
  alcances?: Record<string, Record<string, Alcance>>
  /** Some permission says «their team»: only then «Work groups» exist on screen (menu, page, column). */
  usa_equipos?: boolean
  organizacion?: string | null
  nombre_de_organizacion?: string | null
  es_de_organizacion?: boolean
  administra_organizacion?: boolean
  cartera?: string[] | null
  externa?: boolean
  /** What the app calls its organisations («Proveedores»), from the setting `nombre_organizacion`. */
  nombre_de_las_organizaciones?: string | null
}

export interface Equipo { id: string; nombre: string; activo: boolean; miembros: number }

export interface Organizacion { id: string; nombre: string; activo: boolean; miembros: number }

export interface SinOrganizacion { tabla: string; existe: boolean; columnas: boolean; sin_organizacion: number; total: number }

export interface SinDueno { tabla: string; existe: boolean; columnas: boolean; sin_dueno: number; total: number }

export interface MiembroDelProyecto { email: string; nombre: string; dado_de_alta: boolean }

export interface Miembros { disponible: boolean; miembros: MiembroDelProyecto[] }

export interface AltaDesdeProyecto { dados_de_alta: string[]; ya_estaban: string[]; no_son_del_proyecto: string[] }

/** A column of the adapter's table. */
export interface Columna<T> {
  clave: string
  titulo: string
  celda: (fila: T) => ReactNode
  alinear?: 'left' | 'right' | 'center'
}

/** Texts of the module per language: `{ es: { clave: 'texto' }, en: {...} }` (flat keys). */
export type TextosDelPerfilado = Record<string, Record<string, string>>

/** An API error with the module's stable code (`X-WAI-Codigo`). */
export class ErrorDeApi extends Error {
  estado: number
  codigo: string
  constructor(estado: number, codigo: string, detalle: string) {
    super(detalle || `HTTP ${estado}`)
    this.estado = estado
    this.codigo = codigo
  }
}
