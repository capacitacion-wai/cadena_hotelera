/**
 * Profiling module — how the screens READ the matrix: which actions a role has on a capability, the
 * scope each one reaches (for DISPLAY: the same rule as `permisos._alcance_del_rol` in the backend,
 * which is the one applied), which capabilities are limited to «their team» (said by the API) and a
 * capability's short name. Pure functions, no screen.
 *
 * INVARIANT FILE — installed by `wai-app-builder/assets/modulos.py`; do not edit it.
 */
import type { Accion, Alcance, Capacidad, Matriz, Rol } from './tipos'

const ALCANCES: Alcance[] = ['propio', 'equipo', 'todo']
const esAlcance = (a: unknown): a is Alcance => typeof a === 'string' && (ALCANCES as string[]).includes(a)

/** The actions the role allows on the capability (its row plus the `*` row), in the matrix's order. */
export function accionesDe(rol: Rol, capacidad: string, orden: Accion[]): Accion[] {
  const tiene = new Set([...(rol.permisos[capacidad] || []), ...(rol.permisos['*'] || [])])
  return orden.filter((a) => tiene.has(a))
}

/** The scope of ONE allowed action of ONE role. Below level 3 everything is `todo`; an action allowed
 *  through `*` is `todo`; else the action's scope, the capability's, the role's default, `todo`. */
export function alcanceDe(rol: Rol, capacidad: string, accion: Accion, nivel: number): Alcance {
  if (nivel < 3) return 'todo'
  if ((rol.permisos['*'] || []).includes(accion) || !(rol.permisos[capacidad] || []).includes(accion)) return 'todo'
  let a: unknown = rol.alcances?.[capacidad]
  if (a && typeof a === 'object') a = (a as Record<string, unknown>)[accion]
  if (!esAlcance(a)) a = rol.alcance
  return esAlcance(a) ? a : 'todo'
}

/** The capabilities some role limits to «their team» — said by the API (`por_equipo`, the backend's
 *  `permisos.capacidades_por_equipo`), never recomputed here. Empty = teams change nothing. */
export function capacidadesPorEquipo(matriz: Matriz | null): Capacidad[] {
  const ids = matriz?.por_equipo || []
  return (matriz?.capacidades || []).filter((c) => ids.includes(c.id))
}

export function usaEquipo(matriz: Matriz | null): boolean {
  return (matriz?.por_equipo || []).length > 0
}

/** A capability's short name: the Definition's `nombre` if it has one, else the beginning of its
 *  sentence (up to «:», a dash, a bracket or a full stop; at most seven words). Its id is never shown:
 *  without a sentence it is `sinNombre`. */
export function nombreCorto(c: Capacidad, sinNombre: string): string {
  if (c.nombre && c.nombre.trim()) return c.nombre.trim()
  const frase = (c.que || '').trim()
  if (!frase) return sinNombre
  const corte = frase.split(/\s*(?::|\s[—–-]\s|\(|\.\s)/)[0].trim() || frase
  const palabras = corte.split(/\s+/)
  // Only a LONG sentence (over 14 words) is cut: at 7, «Consultar el catálogo y las ventas de…» did not say of what.
  return palabras.length > 14 ? `${palabras.slice(0, 14).join(' ')}…` : corte
}
