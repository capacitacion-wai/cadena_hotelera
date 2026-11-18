/**
 * Profiling module — «My access»: my roles and what I can do (every active user); at level 3 also my
 * work group (only when some permission says «their team») and how far each action reaches (my own, my team's, everything); at level 4 my organisation
 * (or, internal, the organisations I reach) and whether I administer my organisation.
 *
 * INVARIANT FILE — installed by `wai-app-builder/assets/modulos.py`; do not edit it. It looks like
 * the app through the adapter (`Pagina`) and the theme.
 */
import { Alert, Chip, List, ListItem, ListItemText, Paper, Stack, Typography } from '@mui/material'
import { Pagina } from './adaptador'
import { useMiAcceso } from './api'
import { Cargando, ErrorDelModulo } from './Estado'
import { nombreDeOrganizaciones, nombreDeRol, queDeTarea, useTextos } from './textos'

export default function PaginaMiAcceso() {
  const t = useTextos()
  const { yo, error, cargando, recargar } = useMiAcceso()
  const todas = yo?.permisos['*'] || []
  // Only the actions the task HAS (`acciones`, from the Definition's roles): the administrator's `*` listed all six
  // under every task, «View a request» with «Create · Approve» (02/11/2026). All of them = «Everything».
  const filas = (yo?.capacidades || [])
    .map((c) => {
      const tiene = new Set([...(yo?.permisos[c.id] || []), ...todas])
      const posibles = c.acciones?.length ? c.acciones : Array.from(tiene)
      const acciones = posibles.filter((a) => tiene.has(a))
      return { ...c, acciones, todas: acciones.length > 1 && acciones.length === posibles.length }
    })
    .filter((c) => c.acciones.length > 0)
  return (
    <Pagina titulo={t('mi_acceso_titulo')} subtitulo={t('mi_acceso_subtitulo')}>
      {cargando && !yo ? <Cargando /> : null}
      {error ? <ErrorDelModulo error={error} reintentar={recargar} /> : null}
      {yo ? (
        <Stack spacing={2}>
          {yo.es_dueno ? <Alert severity="info">{t('mi_acceso_dueno')}</Alert> : null}
          <Paper variant="outlined" sx={{ p: 2 }}>
            <Typography variant="subtitle2" gutterBottom>{t('mi_acceso_roles')}</Typography>
            <Stack direction="row" spacing={1} useFlexGap flexWrap="wrap">
              {yo.roles.map((r) => (
                <Chip key={r} label={nombreDeRol(t, r, yo.nombres_de_roles[r], r === 'admin' || r === 'usuario')} />
              ))}
            </Stack>
            {yo.administra ? (
              <Typography variant="body2" color="text.secondary" sx={{ mt: 1.5 }}>{t('mi_acceso_administra')}</Typography>
            ) : null}
            {yo.nivel >= 4 ? (
              <Typography variant="body2" color="text.secondary" sx={{ mt: 1.5 }}>
                {yo.es_de_organizacion
                  ? t('mi_acceso_organizacion', { organizacion: yo.nombre_de_organizacion || yo.organizacion || '—' })
                  : yo.cartera
                    ? t('mi_acceso_cartera', { n: yo.cartera.length, organizaciones: nombreDeOrganizaciones(t, yo.nombre_de_las_organizaciones) })
                    : t('mi_acceso_todas_las_organizaciones', { organizaciones: nombreDeOrganizaciones(t, yo.nombre_de_las_organizaciones) })}
              </Typography>
            ) : null}
            {yo.administra_organizacion ? (
              <Typography variant="body2" color="text.secondary" sx={{ mt: 1.5 }}>{t('mi_acceso_administra_organizacion')}</Typography>
            ) : null}
            {yo.nivel >= 3 && yo.usa_equipos ? (
              <Typography variant="body2" color="text.secondary" sx={{ mt: 1.5 }}>
                {yo.equipo ? t('mi_acceso_equipo', { equipo: yo.nombre_de_equipo || yo.equipo }) : t('mi_acceso_sin_equipo')}
              </Typography>
            ) : null}
          </Paper>
          <Paper variant="outlined" sx={{ p: 2 }}>
            <Typography variant="subtitle2">{t('mi_acceso_puede')}</Typography>
            {filas.length === 0 ? (
              <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>{t('mi_acceso_nada')}</Typography>
            ) : (
              <List dense disablePadding>
                {filas.map((c) => (
                  <ListItem key={c.id} disableGutters>
                    <ListItemText
                      primary={queDeTarea(t, c) || t('sin_nombre')}
                      secondary={c.todas && !(yo.nivel >= 3 && c.acciones.some((a) => {
                        const al = yo.alcances?.[c.id]?.[a] || yo.alcances?.['*']?.[a]
                        return al && al !== 'todo'
                      })) ? t('roles_todo') : c.acciones.map((a) => {
                        const alcance = yo.nivel >= 3 ? yo.alcances?.[c.id]?.[a] || yo.alcances?.['*']?.[a] : undefined
                        return alcance && alcance !== 'todo' ? `${t(`accion_${a}`)} (${t(`alcance_${alcance}`)})` : t(`accion_${a}`)
                      }).join(' · ')}
                    />
                  </ListItem>
                ))}
              </List>
            )}
          </Paper>
        </Stack>
      ) : null}
    </Pagina>
  )
}
