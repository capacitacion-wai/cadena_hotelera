/**
 * Profiling module — ONE role in plain words (the «What each one can do» screen and the preview of the
 * custom-role editor): its name, its one line, «Can» (each capability by its short name; which actions
 * when it has only some of those the capability has in the app; the data reach in words when it is not
 * everything) and «Cannot». An organisation role says once that it only sees its company's data.
 *
 * INVARIANT FILE — installed by `wai-app-builder/assets/modulos.py`; do not edit it.
 */
import type { ReactNode } from 'react'
import { Box, List, ListItem, ListItemText, Paper, Stack, Typography } from '@mui/material'
import { accionesDe, alcanceDe } from './resumen'
import { cortoDeTarea, descripcionDeRol, nombreDeRol } from './textos'
import type { Traducir } from './textos'
import type { Accion, Capacidad, Matriz, Rol } from './tipos'

/** What is said about the data an action reaches: an organisation role's «everything» is its company's. */
export type AlcanceVisible = 'todo' | 'organizacion' | 'equipo' | 'propio'
export const DE_MAS_A_MENOS: AlcanceVisible[] = ['todo', 'organizacion', 'equipo', 'propio']

const deOrganizacion = (m: Matriz, r: Rol) => m.nivel >= 4 && r.ambito === 'organizacion'
export const alcanceVisible = (m: Matriz, r: Rol, c: string, a: Accion): AlcanceVisible => {
  const x = alcanceDe(r, c, a, m.nivel)
  return x === 'todo' && deOrganizacion(m, r) ? 'organizacion' : x
}

/** «a, b and c» in the app's language. */
function enumerar(t: Traducir, partes: string[]): string {
  return partes.length <= 1 ? partes.join('') : `${partes.slice(0, -1).join(', ')} ${t('y')} ${partes[partes.length - 1]}`
}

/** The actions that MEAN something for a capability in this app: those some role has on it by name
 *  (not through «everything»). A role with all of them just «can» use it. */
function accionesDeLaCapacidad(m: Matriz, c: string): Accion[] {
  const usadas = new Set(m.roles.flatMap((r) => r.permisos[c] || []))
  return usadas.size ? m.acciones.filter((a) => usadas.has(a)) : m.acciones
}

export default function TarjetaDeRol({ rol, matriz, capacidades, t, marca, pie }: {
  rol: Rol; matriz: Matriz; capacidades: Capacidad[]; t: Traducir
  /** Beside the name (the «created in the app» badge). */
  marca?: ReactNode
  /** Under the lists (who changed it last, its actions). */
  pie?: ReactNode
}) {
  const todo = matriz.acciones.every((a) => (rol.permisos['*'] || []).includes(a))
  const puede: { texto: string; matiz: string }[] = []
  const noPuede: string[] = []
  if (todo) puede.push({ texto: t('tarjeta_todo'), matiz: '' })
  else {
    for (const c of capacidades) {
      const tiene = accionesDe(rol, c.id, matriz.acciones)
      const nombre = cortoDeTarea(t, c)
      if (tiene.length === 0) { noPuede.push(nombre); continue }
      const posibles = accionesDeLaCapacidad(matriz, c.id)
      const parcial = posibles.some((a) => !tiene.includes(a))
      const texto = parcial ? t('tarjeta_solo_acciones', { capacidad: nombre, acciones: enumerar(t, tiene.map((a) => t(`accion_${a}`).toLocaleLowerCase())) }) : nombre
      // The reach, in words, only when it is not everything (an organisation's own is said once, above).
      const grupos = DE_MAS_A_MENOS.map((al) => ({ al, acciones: tiene.filter((a) => alcanceVisible(matriz, rol, c.id, a) === al) }))
        .filter((g) => g.acciones.length && g.al !== 'todo' && g.al !== 'organizacion')
      const matiz = grupos.length === 0 ? ''
        : grupos.length === 1 && grupos[0].acciones.length === tiene.length ? t(`tarjeta_alcance_${grupos[0].al}`)
          : grupos.map((g) => `${enumerar(t, g.acciones.map((a) => t(`accion_${a}`).toLocaleLowerCase()))}: ${t(`tarjeta_alcance_${g.al}`).toLocaleLowerCase()}`).join(' · ')
      puede.push({ texto, matiz })
    }
  }
  if (rol.administra) puede.push({ texto: t('tarjeta_gestiona'), matiz: '' })
  else if (rol.administra_organizacion) puede.push({ texto: t('tarjeta_gestiona_organizacion'), matiz: '' })
  return (
    <Paper variant="outlined" sx={{ p: 2, height: '100%', boxSizing: 'border-box' }}>
      <Stack direction="row" spacing={1} alignItems="center" useFlexGap flexWrap="wrap">
        <Typography variant="subtitle1" fontWeight={600}>{nombreDeRol(t, rol.id, rol.nombre, rol.fijo)}</Typography>
        {marca}
      </Stack>
      {descripcionDeRol(t, rol) ? <Typography variant="body2" color="text.secondary">{descripcionDeRol(t, rol)}</Typography> : null}
      {deOrganizacion(matriz, rol) ? <Typography variant="body2" sx={{ mt: 1 }}>{t('tarjeta_solo_su_empresa')}</Typography> : null}
      <Typography variant="subtitle2" sx={{ mt: 1.5 }}>{t('tarjeta_puede')}</Typography>
      {puede.length === 0 ? <Typography variant="body2" color="text.secondary">{t('tarjeta_nada')}</Typography> : (
        <List dense disablePadding>
          {puede.map((p, i) => (
            <ListItem key={i} disableGutters sx={{ py: 0 }}>
              <ListItemText primary={`• ${p.texto}`} secondary={p.matiz || undefined} />
            </ListItem>
          ))}
        </List>
      )}
      {noPuede.length ? (
        <>
          <Typography variant="subtitle2" sx={{ mt: 1 }}>{t('tarjeta_no_puede')}</Typography>
          <List dense disablePadding>
            {noPuede.map((n) => (
              <ListItem key={n} disableGutters sx={{ py: 0 }}>
                <ListItemText primary={`• ${n}`} primaryTypographyProps={{ color: 'text.secondary' }} />
              </ListItem>
            ))}
          </List>
        </>
      ) : null}
      {pie ? <Box sx={{ mt: 1.5 }}>{pie}</Box> : null}
    </Paper>
  )
}

