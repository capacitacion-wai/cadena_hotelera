/**
 * Profiling module — its API calls (through the app's client, via the adapter).
 *
 * INVARIANT FILE — installed by `wai-app-builder/assets/modulos.py`; do not edit it.
 */
import { useCallback, useEffect, useState } from 'react'
import { llamarApi } from './adaptador'
import { ErrorDeApi } from './tipos'
import type {
  AltaDesdeProyecto, Equipo, Matriz, Miembros, Organizacion, PaginaDeUsuarios, Piezas, Rol, RolPropio, SinDueno, SinOrganizacion,
  Usuario, UsuarioBorrado, Yo,
} from './tipos'

const BASE = '/api/perfilado'

export const api = {
  yo: () => llamarApi<Yo>('GET', `${BASE}/yo`),
  roles: () => llamarApi<Matriz>('GET', `${BASE}/roles`),
  // Custom roles (setting `roles_personalizables`): internal administrators only.
  piezasDeLosRoles: () => llamarApi<Piezas>('GET', `${BASE}/roles/propios/piezas`),
  crearRol: (rol: RolPropio) => llamarApi<Rol>('POST', `${BASE}/roles/propios`, rol),
  cambiarRol: (id: string, rol: RolPropio) => llamarApi<Rol>('PATCH', `${BASE}/roles/propios/${encodeURIComponent(id)}`, rol),
  borrarRol: (id: string, pasarA?: string) => llamarApi<{ borrado: boolean; pasados: number }>('DELETE',
    `${BASE}/roles/propios/${encodeURIComponent(id)}${pasarA ? `?${new URLSearchParams({ pasar_a: pasarA })}` : ''}`),
  usuarios: (limit: number, offset: number, q: string) =>
    llamarApi<PaginaDeUsuarios>('GET', `${BASE}/usuarios?${new URLSearchParams({ limit: String(limit), offset: String(offset), q })}`),
  proyecto: () => llamarApi<Miembros>('GET', `${BASE}/proyecto`),
  alta: (email: string, roles: string[], nombre?: string, organizacion?: string | null) =>
    llamarApi<Usuario>('POST', `${BASE}/usuarios`, { email, roles, nombre: nombre || null, organizacion: organizacion || null }),
  altaDesdeProyecto: (emails: string[], roles: string[]) =>
    llamarApi<AltaDesdeProyecto>('POST', `${BASE}/usuarios/desde-proyecto`, { emails, roles }),
  /** Every person (all pages of the list, 200 at a time): the teams screen groups them by team. */
  todasLasPersonas: async (): Promise<Usuario[]> => {
    const todas: Usuario[] = []
    for (let desde = 0; desde < 5000; desde += 200) {
      const p = await llamarApi<PaginaDeUsuarios>('GET', `${BASE}/usuarios?${new URLSearchParams({ limit: '200', offset: String(desde) })}`)
      todas.push(...p.items)
      if (todas.length >= p.total || p.items.length === 0) break
    }
    return todas
  },
  cambiar: (id: number, cambio: { roles?: string[]; activo?: boolean; equipo?: string | null; organizacion?: string | null; cartera?: string[] | null }) =>
    llamarApi<Usuario>('PATCH', `${BASE}/usuarios/${id}`, cambio),
  /** DELETES the person (deactivating is `cambiar(id, { activo: false })`): what they created stays. */
  borrar: (id: number) => llamarApi<UsuarioBorrado>('DELETE', `${BASE}/usuarios/${id}`),
  // Level 3: teams and the rows still without an owner.
  equipos: (archivados = false) => llamarApi<Equipo[]>('GET', `${BASE}/equipos?archivados=${archivados}`),
  crearEquipo: (nombre: string) => llamarApi<Equipo>('POST', `${BASE}/equipos`, { nombre }),
  cambiarEquipo: (id: string, cambio: { nombre?: string; activo?: boolean }) =>
    llamarApi<Equipo>('PATCH', `${BASE}/equipos/${encodeURIComponent(id)}`, cambio),
  sinDueno: () => llamarApi<SinDueno[]>('GET', `${BASE}/sin-dueno`),
  asignarSinDueno: (tabla: string, dueno: string, equipo: string | null) =>
    llamarApi<SinDueno>('POST', `${BASE}/sin-dueno/${encodeURIComponent(tabla)}`, { dueno, equipo }),
  // Level 4: organisations and the rows still without one.
  organizaciones: (archivadas = false) => llamarApi<Organizacion[]>('GET', `${BASE}/organizaciones?archivadas=${archivadas}`),
  crearOrganizacion: (nombre: string) => llamarApi<Organizacion>('POST', `${BASE}/organizaciones`, { nombre }),
  cambiarOrganizacion: (id: string, cambio: { nombre?: string; activo?: boolean }) =>
    llamarApi<Organizacion>('PATCH', `${BASE}/organizaciones/${encodeURIComponent(id)}`, cambio),
  sinOrganizacion: () => llamarApi<SinOrganizacion[]>('GET', `${BASE}/sin-organizacion`),
  asignarSinOrganizacion: (tabla: string, organizacion: string) =>
    llamarApi<SinOrganizacion>('POST', `${BASE}/sin-organizacion/${encodeURIComponent(tabla)}`, { organizacion }),
}

/** The sign-in of the EXTERNAL accounts (public: there is no session yet, so it does not go through
 *  the app's client, which would send a 401 to /login). */
async function publica<T>(ruta: string, cuerpo: unknown): Promise<T> {
  const res = await fetch(ruta, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(cuerpo) })
  if (!res.ok) {
    let detalle = ''
    try {
      const j = await res.json()
      detalle = typeof j?.detail === 'string' ? j.detail : ''
    } catch {
      detalle = ''
    }
    throw new ErrorDeApi(res.status, res.headers.get('X-WAI-Codigo') || '', detalle)
  }
  return (await res.json()) as T
}

export const entradaExterna = {
  pedirCodigo: (email: string) => publica<{ enviado: boolean }>(`${BASE}/externas/codigo`, { email }),
  entrar: (email: string, codigo: string) =>
    publica<{ access_token: string; token_type: string }>(`${BASE}/externas/entrar`, { email, codigo }),
}

let miAcceso: Promise<Yo> | null = null

/** Forgets «my access» (after changing someone's roles: it may be me). */
export function refrescarMiAcceso(): void {
  miAcceso = null
}

/** «My access», asked once per page load and shared by the menu and the screens. */
export function useMiAcceso(): { yo: Yo | null; error: unknown; cargando: boolean; recargar: () => void } {
  const [estado, setEstado] = useState<{ yo: Yo | null; error: unknown; cargando: boolean }>({ yo: null, error: null, cargando: true })
  const cargar = useCallback(() => {
    if (!miAcceso) miAcceso = api.yo()
    const pedido = miAcceso
    setEstado((e) => ({ ...e, cargando: true }))
    pedido.then(
      (yo) => setEstado({ yo, error: null, cargando: false }),
      (error) => {
        if (miAcceso === pedido) miAcceso = null
        setEstado({ yo: null, error, cargando: false })
      },
    )
  }, [])
  useEffect(() => { cargar() }, [cargar])
  return { ...estado, recargar: () => { refrescarMiAcceso(); cargar() } }
}
