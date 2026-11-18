/**
 * Profiling module — «What each one can do» (administrators; an organisation's administrator sees only
 * the organisation roles, the API filters them). ONE TABLE: a row per capability (by its short name; the whole
 * sentence on hover) and a column per role (its name and its one line, without repeating the name), and in each
 * cell one mark — everything, some actions (which ones on hover, each with its reach), or nothing — and below, the
 * data reach in words only when it is not everything (only their own, only their work group). The last row says who
 * manages users. Everything is compared at a glance and nothing is cut (01/11/2026: the cards repeated each role's
 * name in its description, cut every capability with «…» and listed «Cannot» as long as «Can»). IT FITS THE PAGE
 * (02/11/2026: sized by its content, the first column took the whole sentence and the roles went off to a giant
 * horizontal scroll): a fixed first column with the task's SHORT name (`def_tarea_<id>_corto`, the whole sentence on
 * hover), narrow role columns with the role's line on hover, and only with many roles a scroll that keeps the first
 * column in place.
 * Never shown to the person: capability ids, the words scope / capability / level. The Definition's roles are
 * read-only here; with the setting `roles_personalizables` an internal administrator also «Create role» / «Edit» /
 * «Delete» the app's own roles (`EditorDeRol.tsx`), marked «Created in the app».
 *
 * INVARIANT FILE — installed by `wai-app-builder/assets/modulos.py`; do not edit it.
 */
import { useCallback, useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { Box, Chip, Stack, Tooltip, Typography } from '@mui/material'
import { alpha, useTheme } from '@mui/material/styles'
import { textoDe } from './accesible'
import { Boton, Pagina } from './adaptador'
import { api, useMiAcceso } from './api'
import { DialogoBorrarRol, EditorDeRol } from './EditorDeRol'
import { Cargando, ErrorDelModulo } from './Estado'
import { accionesDe } from './resumen'
import { DE_MAS_A_MENOS, alcanceVisible } from './TarjetaDeRol'
import { cortoDeTarea, descripcionDeRol, nombreDeRol, queDeTarea, useTextos } from './textos'
import type { Traducir } from './textos'
import type { Capacidad, Matriz, Rol } from './tipos'

type Marca = 'todo' | 'parte' | 'nada'

/** The role's one line WITHOUT its own name in front («Técnicos de subvenciones: ven…» → «Ven…»). */
function lineaDelRol(t: Traducir, rol: Rol): string {
  const nombre = nombreDeRol(t, rol.id, rol.nombre, rol.fijo).trim()
  let d = (descripcionDeRol(t, rol) || '').trim()
  if (nombre && d.toLocaleLowerCase().startsWith(nombre.toLocaleLowerCase())) {
    d = d.slice(nombre.length).replace(/^[\s:·—-]+/, '')
    d = d.charAt(0).toLocaleUpperCase() + d.slice(1)
  }
  return d
}

/** The mark of a cell: a filled dot (everything), a half one (some actions) or an empty one (nothing). */
function Punto({ marca }: { marca: Marca }) {
  const th = useTheme()
  const color = marca === 'todo' ? th.palette.success.main : marca === 'parte' ? th.palette.info.main : th.palette.text.disabled
  return (
    <Box component="span" aria-hidden sx={{
      display: 'inline-block', width: 10, height: 10, borderRadius: '50%', flexShrink: 0, border: `2px solid ${color}`, boxSizing: 'border-box',
      background: marca === 'todo' ? color : marca === 'parte' ? `linear-gradient(90deg, ${color} 50%, transparent 50%)` : 'transparent',
    }} />
  )
}

/** One cell: the mark and one word (All, the actions, —); below, the reach when not everything; on hover, each action. */
function Celda({ rol, capacidad, matriz, t }: { rol: Rol; capacidad: string; matriz: Matriz; t: Traducir }) {
  const acciones = accionesDe(rol, capacidad, matriz.acciones)
  if (acciones.length === 0) {
    return <Stack direction="row" spacing={0.75} alignItems="center"><Punto marca="nada" /><Typography variant="body2" color="text.disabled">—</Typography></Stack>
  }
  const usadas = new Set(matriz.roles.flatMap((r) => r.permisos[capacidad] || []))
  const posibles = usadas.size ? matriz.acciones.filter((a) => usadas.has(a)) : matriz.acciones
  const marca: Marca = posibles.every((a) => acciones.includes(a)) ? 'todo' : 'parte'
  const grupos = DE_MAS_A_MENOS
    .map((al) => ({ al, acciones: acciones.filter((a) => alcanceVisible(matriz, rol, capacidad, a) === al) }))
    .filter((g) => g.acciones.length > 0)
  const principal = marca === 'todo' ? t('roles_todo') : acciones.map((a) => t(`accion_${a}`)).join(', ')
  const secundaria = grupos.length === 1
    ? (grupos[0].al === 'todo' ? '' : t(`celda_alcance_${grupos[0].al}`))
    : grupos.map((g) => `${g.acciones.map((a) => t(`accion_${a}`)).join(', ')}: ${t(`celda_alcance_${g.al}`)}`).join(' · ')
  const detalle = (
    <Stack spacing={0.25}>
      {acciones.map((a) => <span key={a}>{`${t(`accion_${a}`)} — ${t(`detalle_alcance_${alcanceVisible(matriz, rol, capacidad, a)}`)}`}</span>)}
    </Stack>
  )
  return (
    <Tooltip title={detalle} enterTouchDelay={0} arrow>
      <Box sx={{ cursor: 'help' }}>
        <Stack direction="row" spacing={0.75} alignItems="center">
          <Punto marca={marca} />
          <Typography variant="body2" sx={{ fontSize: 13, lineHeight: 1.35 }}>{principal}</Typography>
        </Stack>
        {secundaria ? <Typography variant="caption" color="text.secondary" sx={{ display: 'block', pl: 2.25, lineHeight: 1.3 }}>{secundaria}</Typography> : null}
      </Box>
    </Tooltip>
  )
}

/** The table: capabilities × roles, the roles' headers and the «manages users» row. */
function TablaDePermisos({ matriz, capacidades, t, cabeceraExtra }: {
  matriz: Matriz; capacidades: Capacidad[]; t: Traducir; cabeceraExtra: (r: Rol) => ReactNode
}) {
  const th = useTheme()
  const n = matriz.roles.length
  // Fits the page: a bounded first column and narrow role columns; a scroll only from many roles on (and then the
  // first column stays in place). Never sized by the content: a long sentence pushed every role off-screen.
  const columnas = `minmax(190px, 240px) repeat(${n}, minmax(104px, 1fr))`
  const fila = { display: 'grid', gridTemplateColumns: columnas, columnGap: 1.5, px: 1.5, py: 1, alignItems: 'center' } as const
  const borde = `1px solid ${th.palette.divider}`
  // The fixed first column paints its row's stripe over the paper (a plain white box showed on every striped row).
  const fija = (fondo?: string) => ({ position: 'sticky', left: 0, zIndex: 1, bgcolor: 'background.paper', pr: 1,
    ...(fondo ? { backgroundImage: `linear-gradient(${fondo}, ${fondo})` } : {}) }) as const
  return (
    <Box sx={{ overflowX: 'auto', border: borde, borderRadius: 2, bgcolor: 'background.paper' }}>
      <Box sx={{ minWidth: `${200 + n * 116}px` }}>
        <Box sx={{ ...fila, alignItems: 'end', py: 1.25, borderBottom: borde, bgcolor: alpha(th.palette.primary.main, 0.04) }}>
          <Typography variant="caption" color="text.secondary" sx={{ ...fija(), bgcolor: 'transparent', fontWeight: 700, letterSpacing: 0.4 }}>{t('detalle_que')}</Typography>
          {matriz.roles.map((r) => (
            <Box key={r.id} sx={{ minWidth: 0 }}>
              <Tooltip title={lineaDelRol(t, r)} enterTouchDelay={0} arrow>
                <Typography variant="subtitle2" fontWeight={700} sx={{ lineHeight: 1.25, cursor: lineaDelRol(t, r) ? 'help' : undefined }}>
                  {nombreDeRol(t, r.id, r.nombre, r.fijo)}
                </Typography>
              </Tooltip>
              {r.origen === 'aplicacion' ? <Chip size="small" variant="outlined" sx={{ mt: 0.5 }} label={t('rol_creado_en_la_app')} /> : null}
              {cabeceraExtra(r)}
            </Box>
          ))}
        </Box>
        {capacidades.map((c, i) => {
          const corto = cortoDeTarea(t, c)
          const entera = queDeTarea(t, c)
          const fondo = i % 2 ? alpha(th.palette.text.primary, 0.015) : undefined
          return (
            <Box key={c.id} sx={{ ...fila, borderBottom: borde, bgcolor: fondo }}>
              <Tooltip title={entera && entera.trim() !== corto ? entera : ''} enterTouchDelay={0} placement="right">
                <Typography variant="body2" fontWeight={600} sx={{ ...fija(fondo), lineHeight: 1.35, cursor: entera ? 'help' : undefined }}>{corto}</Typography>
              </Tooltip>
              {matriz.roles.map((r) => <Celda key={r.id} rol={r} capacidad={c.id} matriz={matriz} t={t} />)}
            </Box>
          )
        })}
        <Box sx={{ ...fila }}>
          <Typography variant="body2" fontWeight={600} sx={fija()}>{t('roles_gestiona')}</Typography>
          {matriz.roles.map((r) => (
            <Stack key={r.id} direction="row" spacing={0.75} alignItems="center">
              <Punto marca={r.administra ? 'todo' : r.administra_organizacion ? 'parte' : 'nada'} />
              <Typography variant="body2" color={r.administra || r.administra_organizacion ? 'text.primary' : 'text.disabled'}>
                {r.administra ? t('si') : r.administra_organizacion ? t('roles_si_su_empresa') : '—'}
              </Typography>
            </Stack>
          ))}
        </Box>
      </Box>
    </Box>
  )
}

/** What the marks mean, under the table. */
function Leyenda({ t }: { t: Traducir }) {
  return (
    <Stack direction="row" spacing={2.5} useFlexGap flexWrap="wrap" alignItems="center">
      {(['todo', 'parte', 'nada'] as Marca[]).map((m) => (
        <Stack key={m} direction="row" spacing={0.75} alignItems="center">
          <Punto marca={m} /><Typography variant="caption" color="text.secondary">{t(`leyenda_${m}`)}</Typography>
        </Stack>
      ))}
      <Typography variant="caption" color="text.secondary">{t('leyenda_pasar')}</Typography>
    </Stack>
  )
}

/** «Changed by X on <date>» in the person's locale. */
function fecha(iso?: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' })
}

export default function PaginaRoles() {
  const t = useTextos()
  const { yo } = useMiAcceso()
  const [matriz, setMatriz] = useState<Matriz | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [editando, setEditando] = useState<Rol | 'nuevo' | null>(null)
  const [borrando, setBorrando] = useState<Rol | null>(null)
  const cargar = useCallback(() => {
    setError(null)
    api.roles().then(setMatriz, setError)
  }, [])
  useEffect(() => { cargar() }, [cargar])
  // Without capabilities in the Definition: one line, «everything in the app».
  const capacidades: Capacidad[] = matriz?.capacidades.length ? matriz.capacidades : [{ id: '*', que: t('tarjeta_todo') }]
  const crea = !!(yo?.administra && matriz?.personalizables)

  // Under the name of an app's own role: who changed it last and, for whoever may, edit / delete.
  const cabeceraExtra = (r: Rol) => (r.origen !== 'aplicacion' ? null : (
    <Stack spacing={0.5} sx={{ mt: 0.5 }}>
      {r.cambiado_por ? (
        <Typography variant="caption" color="text.secondary">{t('rol_cambiado_por', { por: r.cambiado_por, fecha: fecha(r.cambiado_en) })}</Typography>
      ) : null}
      {crea ? (
        <Stack direction="row" spacing={1}>
          <Boton size="small" onClick={() => setEditando(r)}>{t('rol_editar')}</Boton>
          <Boton size="small" color="warning" sx={textoDe('warning')} onClick={() => setBorrando(r)}>{t('rol_borrar')}</Boton>
        </Stack>
      ) : null}
    </Stack>
  ))

  return (
    <Pagina titulo={t('roles_titulo')} subtitulo={t('roles_subtitulo')}
      acciones={crea ? <Boton variant="contained" onClick={() => setEditando('nuevo')}>{t('rol_crear')}</Boton> : undefined}>
      {error ? <ErrorDelModulo error={error} reintentar={cargar} /> : null}
      {!matriz && !error ? <Cargando /> : null}
      {matriz ? (
        <Stack spacing={1.5}>
          <TablaDePermisos matriz={matriz} capacidades={capacidades} t={t} cabeceraExtra={cabeceraExtra} />
          <Leyenda t={t} />
        </Stack>
      ) : null}
      {matriz && crea ? (
        <>
          <EditorDeRol abierto={!!editando} rol={editando === 'nuevo' ? null : editando} matriz={matriz}
            cerrar={() => setEditando(null)} hecho={() => { setEditando(null); cargar() }} />
          <DialogoBorrarRol rol={borrando} matriz={matriz} cerrar={() => setBorrando(null)}
            hecho={() => { setBorrando(null); cargar() }} />
        </>
      ) : null}
    </Pagina>
  )
}
