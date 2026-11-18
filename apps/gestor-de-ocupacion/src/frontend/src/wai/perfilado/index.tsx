/**
 * Profiling module — what the app wires: its screens' routes and its menu entries.
 *
 * INVARIANT FILE — installed by `wai-app-builder/assets/modulos.py`; do not edit it.
 *
 *   // App.tsx, inside the protected shell (ProtectedRoute + the app's Layout):
 *   import { rutasDelPerfilado, useEntradasDelPerfilado } from './wai/perfilado'
 *   {rutasDelPerfilado().map((r) => <Route key={r.path} path={r.path} element={r.element} />)}
 *
 *   // where the app builds the Layout's navItems (the design decides the group and the order):
 *   const perfilado = useEntradasDelPerfilado()
 *   <Layout navItems={[...navItems, ...perfilado]} navGroups={[..., { label: t('…'), keys: perfilado.map((e) => e.key) }]} … />
 *
 * «Users» and «What each one can do» only appear to administrators; «My access» to everybody; «Work
 * groups» (level 3) to administrators, and only when some permission says «their team». Level 4: «Organisations» (named as the app calls them) to the internal
 * administrators, and «Users» + «Roles» also to an organisation's administrator (their organisation only).
 *
 *   // Level 4 with external accounts: the PUBLIC sign-in page, OUTSIDE the protected shell (next to /login),
 *   // and a link to it from the app's login page («Are you from an outside organisation? Sign in with a code»):
 *   {rutasPublicasDelPerfilado().map((r) => <Route key={r.path} path={r.path} element={r.element} />)}
 */
import type { ReactElement, ReactNode } from 'react'
import { Box, SvgIcon } from '@mui/material'
import { TACTIL } from './accesible'
import { useMiAcceso } from './api'
import PaginaEntrar from './PaginaEntrar'
import PaginaEquipos from './PaginaEquipos'
import PaginaMiAcceso from './PaginaMiAcceso'
import PaginaOrganizaciones from './PaginaOrganizaciones'
import PaginaRoles from './PaginaRoles'
import PaginaUsuarios from './PaginaUsuarios'
import { nombreDeOrganizaciones, useTextos } from './textos'

export { refrescarMiAcceso, useMiAcceso } from './api'
export { textoDeUnCodigo } from './textos'
export type { Yo } from './tipos'

export const RUTAS_DEL_PERFILADO = {
  usuarios: '/perfilado/usuarios',
  roles: '/perfilado/roles',
  miAcceso: '/perfilado/mi-acceso',
  equipos: '/perfilado/equipos',
  organizaciones: '/perfilado/organizaciones',
  entrar: '/perfilado/entrar',
} as const

/** Every screen of the module inside its touch rule (`accesible.ts`): the app's header actions included. */
const pantalla = (p: ReactElement) => <Box sx={TACTIL}>{p}</Box>

export function rutasDelPerfilado(): { path: string; element: ReactElement }[] {
  return [
    { path: RUTAS_DEL_PERFILADO.usuarios, element: pantalla(<PaginaUsuarios />) },
    { path: RUTAS_DEL_PERFILADO.roles, element: pantalla(<PaginaRoles />) },
    { path: RUTAS_DEL_PERFILADO.miAcceso, element: pantalla(<PaginaMiAcceso />) },
    { path: RUTAS_DEL_PERFILADO.equipos, element: pantalla(<PaginaEquipos />) },
    { path: RUTAS_DEL_PERFILADO.organizaciones, element: pantalla(<PaginaOrganizaciones />) },
  ]
}

/** The PUBLIC pages (no session yet): the sign-in of the external accounts. Outside ProtectedRoute. */
export function rutasPublicasDelPerfilado(): { path: string; element: ReactElement }[] {
  return [{ path: RUTAS_DEL_PERFILADO.entrar, element: pantalla(<PaginaEntrar />) }]
}

const icono = (d: string) => <SvgIcon fontSize="small"><path d={d} /></SvgIcon>
const ICONOS: Record<string, ReactNode> = {
  usuarios: icono('M16 11c1.66 0 2.99-1.34 2.99-3S17.66 5 16 5s-3 1.34-3 3 1.34 3 3 3m-8 0c1.66 0 2.99-1.34 2.99-3S9.66 5 8 5 5 6.34 5 8s1.34 3 3 3m0 2c-2.33 0-7 1.17-7 3.5V19h14v-2.5c0-2.33-4.67-3.5-7-3.5m8 0c-.29 0-.62.02-.97.05 1.16.84 1.97 1.97 1.97 3.45V19h6v-2.5c0-2.33-4.67-3.5-7-3.5'),
  roles: icono('M12 1 3 5v6c0 5.55 3.84 10.74 9 12 5.16-1.26 9-6.45 9-12V5zm0 10.99h7c-.53 4.12-3.28 7.79-7 8.94V12H5V6.3l7-3.11z'),
  miAcceso: icono('M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4m0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4'),
  organizaciones: icono('M12 7V3H2v18h20V7zM6 19H4v-2h2zm0-4H4v-2h2zm0-4H4V9h2zm0-4H4V5h2zm4 12H8v-2h2zm0-4H8v-2h2zm0-4H8V9h2zm0-4H8V5h2zm10 12h-8v-2h2v-2h-2v-2h2v-2h-2V9h8zm-2-8h-2v2h2zm0 4h-2v2h2z'),
  equipos: icono('M12 12.75c1.63 0 3.07.39 4.24.9 1.08.48 1.76 1.56 1.76 2.73V18H6v-1.61c0-1.18.68-2.26 1.76-2.73 1.17-.52 2.61-.91 4.24-.91M4 13c1.1 0 2-.9 2-2s-.9-2-2-2-2 .9-2 2 .9 2 2 2m1.13 1.1c-.37-.06-.74-.1-1.13-.1-.99 0-1.93.21-2.78.58C.48 14.9 0 15.62 0 16.43V18h4.5v-1.61c0-.83.23-1.61.63-2.29M20 13c1.1 0 2-.9 2-2s-.9-2-2-2-2 .9-2 2 .9 2 2 2m4 3.43c0-.81-.48-1.53-1.22-1.85-.85-.37-1.79-.58-2.78-.58-.39 0-.76.04-1.13.1.4.68.63 1.46.63 2.29V18H24zM12 6c1.66 0 3 1.34 3 3s-1.34 3-3 3-3-1.34-3-3 1.34-3 3-3'),
}

/** The module's menu entries, in the shape of the app's `LayoutNavItem` ({key, path, label, icon}). */
export function useEntradasDelPerfilado(): { key: string; path: string; label: string; icon: ReactNode }[] {
  const t = useTextos()
  const { yo } = useMiAcceso()
  const entradas: { key: string; path: string; label: string; icon: ReactNode }[] = [{ key: 'perfilado-mi-acceso', path: RUTAS_DEL_PERFILADO.miAcceso, label: t('menu_mi_acceso'), icon: ICONOS.miAcceso }]
  if (yo?.administra) {
    entradas.unshift(
      { key: 'perfilado-usuarios', path: RUTAS_DEL_PERFILADO.usuarios, label: t('menu_usuarios'), icon: ICONOS.usuarios },
      ...(yo.nivel >= 4 ? [{ key: 'perfilado-organizaciones', path: RUTAS_DEL_PERFILADO.organizaciones,
        label: nombreDeOrganizaciones(t, yo.nombre_de_las_organizaciones), icon: ICONOS.organizaciones }] : []),
      // «Work groups» only exist when some permission says «their team»: otherwise they change nothing.
      ...(yo.nivel >= 3 && yo.usa_equipos ? [{ key: 'perfilado-equipos', path: RUTAS_DEL_PERFILADO.equipos, label: t('menu_equipos'), icon: ICONOS.equipos }] : []),
      { key: 'perfilado-roles', path: RUTAS_DEL_PERFILADO.roles, label: t('menu_roles'), icon: ICONOS.roles },
    )
  } else if (yo?.administra_organizacion) {
    // Delegated administration: the people of THEIR organisation (the API scopes it, not this menu).
    entradas.unshift(
      { key: 'perfilado-usuarios', path: RUTAS_DEL_PERFILADO.usuarios, label: t('menu_usuarios'), icon: ICONOS.usuarios },
      { key: 'perfilado-roles', path: RUTAS_DEL_PERFILADO.roles, label: t('menu_roles'), icon: ICONOS.roles },
    )
  }
  return entradas
}
