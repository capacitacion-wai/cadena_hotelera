/**
 * Profiling module — «Work groups» (administrators, level 3; the data calls them teams). A work group
 * groups INTERNAL people so that a permission that says «their team» reaches the records of everybody
 * in the group. The menu entry exists ONLY when some permission says so (`yo.usa_equipos`): otherwise
 * groups change nothing anybody sees (organisations are what separate outside companies). The page
 * says in one sentence where a group counts («only sees its group's in: …»), lists each group WITH its
 * people, creates a group choosing its first members and edits name and members in one dialog, and
 * archives (one with members cannot be archived; an archived one is kept because the records still
 * point at it). A person is in one group at most: adding them here takes them out of theirs (the same
 * `PATCH /usuarios/{id}` as «Edit» in Users). The rows still without an owner are handled in Users.
 *
 * INVARIANT FILE — installed by `wai-app-builder/assets/modulos.py`; do not edit it. It looks like
 * the app through the adapter (`Pagina`, `Tabla`, `Boton`) and the theme.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert, Checkbox, Chip, Dialog, DialogActions, DialogContent, DialogTitle, FormControlLabel, List, ListItem,
  ListItemText, Stack, Switch, TextField, Typography,
} from '@mui/material'
import { TACTIL, textoDe } from './accesible'
import { Boton, Pagina, Tabla } from './adaptador'
import { api, refrescarMiAcceso } from './api'
import { Cargando, ErrorDelModulo } from './Estado'
import { capacidadesPorEquipo } from './resumen'
import { cortoDeTarea, mensajeDeError, useTextos } from './textos'
import type { Columna, Equipo, Matriz, Usuario } from './tipos'

const nombreDe = (u: Usuario) => u.nombre || u.email

/** The people to tick, with a search box; someone in ANOTHER team says so (ticking them moves them). */
function SelectorDePersonas({ personas, equipos, equipo, elegidos, cambiar }: {
  personas: Usuario[]; equipos: Equipo[]; equipo: string | null; elegidos: number[]; cambiar: (ids: number[]) => void
}) {
  const t = useTextos()
  const [q, setQ] = useState('')
  const visibles = personas.filter((u) => !q.trim() || `${u.nombre || ''} ${u.email}`.toLowerCase().includes(q.trim().toLowerCase()))
  const nombreDeEquipo = (id: string) => equipos.find((x) => x.id === id)?.nombre || ''
  return (
    <Stack spacing={1}>
      <Typography variant="subtitle2">{t('equipos_personas')}</Typography>
      <Typography variant="body2" color="text.secondary">{t('equipos_personas_ayuda')}</Typography>
      {personas.length > 8 ? <TextField size="small" label={t('usuarios_buscar')} value={q} onChange={(e) => setQ(e.target.value)} /> : null}
      {personas.length === 0 ? <Typography variant="body2" color="text.secondary">{t('equipos_sin_personas')}</Typography> : null}
      <List dense disablePadding sx={{ maxHeight: 320, overflowY: 'auto' }}>
        {visibles.map((u) => {
          const marcado = elegidos.includes(u.id)
          const enOtro = u.equipo && u.equipo !== equipo ? nombreDeEquipo(u.equipo) : ''
          return (
            <ListItem key={u.id} disableGutters>
              <Checkbox edge="start" checked={marcado}
                onChange={(e) => cambiar(e.target.checked ? [...elegidos, u.id] : elegidos.filter((x) => x !== u.id))} />
              <ListItemText primary={nombreDe(u)}
                secondary={enOtro ? t(marcado ? 'equipos_pasara_de' : 'equipos_ahora_en', { equipo: enOtro }) : (u.nombre ? u.email : undefined)} />
            </ListItem>
          )
        })}
      </List>
    </Stack>
  )
}

/** Create a team (name + first members) or edit one (name + members), in one dialog. */
function DialogoEquipo({ abierto, equipo, personas, equipos, cerrar, hecho }: {
  abierto: boolean; equipo: Equipo | null; personas: Usuario[]; equipos: Equipo[]; cerrar: () => void
  /** Done (with a warning when it stopped halfway: the screen reloads to show what did change). */
  hecho: (aviso?: string) => void
}) {
  const t = useTextos()
  const [nombre, setNombre] = useState('')
  const [elegidos, setElegidos] = useState<number[]>([])
  const [error, setError] = useState('')
  const [guardando, setGuardando] = useState(false)
  const actuales = useMemo(() => (equipo ? personas.filter((u) => u.equipo === equipo.id).map((u) => u.id) : []), [equipo, personas])
  useEffect(() => {
    if (abierto) { setNombre(equipo?.nombre || ''); setElegidos(actuales); setError(''); setGuardando(false) }
  }, [abierto, equipo, actuales])
  const guardar = async () => {
    setGuardando(true); setError('')
    let algo = false
    try {
      let id = equipo?.id || ''
      if (!equipo) id = (await api.crearEquipo(nombre.trim())).id
      else if (nombre.trim() !== equipo.nombre) await api.cambiarEquipo(equipo.id, { nombre: nombre.trim() })
      algo = !equipo || nombre.trim() !== equipo.nombre
      // One request per person whose team changes (the same change as «Edit access» in Users).
      for (const uid of elegidos.filter((x) => !actuales.includes(x))) { await api.cambiar(uid, { equipo: id }); algo = true }
      for (const uid of actuales.filter((x) => !elegidos.includes(x))) { await api.cambiar(uid, { equipo: null }); algo = true }
      refrescarMiAcceso()
      hecho()
    } catch (e) {
      // Something was already saved (the team exists): reload so a retry does not create it twice.
      if (algo) { refrescarMiAcceso(); hecho(mensajeDeError(t, e)); return }
      setError(mensajeDeError(t, e))
      setGuardando(false)
    }
  }
  return (
    <Dialog open={abierto} onClose={cerrar} fullWidth maxWidth="sm" sx={TACTIL}>
      <DialogTitle>{equipo ? t('equipos_editar_titulo', { equipo: equipo.nombre }) : t('equipos_crear')}</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ pt: 1 }}>
          {error ? <Alert severity="warning">{error}</Alert> : null}
          <TextField label={t('equipos_campo_nombre')} value={nombre} onChange={(e) => setNombre(e.target.value)} autoFocus={!equipo} />
          <SelectorDePersonas personas={personas} equipos={equipos} equipo={equipo?.id || null} elegidos={elegidos} cambiar={setElegidos} />
        </Stack>
      </DialogContent>
      <DialogActions>
        <Boton onClick={cerrar}>{t('cancelar')}</Boton>
        <Boton variant="contained" disabled={!nombre.trim() || guardando} onClick={guardar}>{t('guardar')}</Boton>
      </DialogActions>
    </Dialog>
  )
}

export default function PaginaEquipos() {
  const t = useTextos()
  const [equipos, setEquipos] = useState<Equipo[] | null>(null)
  const [personas, setPersonas] = useState<Usuario[]>([])
  const [matriz, setMatriz] = useState<Matriz | null>(null)
  const [archivados, setArchivados] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [aviso, setAviso] = useState('')
  const [creando, setCreando] = useState(false)
  const [editando, setEditando] = useState<Equipo | null>(null)

  const cargar = useCallback(() => {
    setError(null)
    api.equipos(archivados).then(setEquipos, setError)
    api.todasLasPersonas().then(setPersonas, setError)
  }, [archivados])
  useEffect(() => { cargar() }, [cargar])
  useEffect(() => { api.roles().then(setMatriz, () => setMatriz(null)) }, [])

  const nivel = matriz?.nivel || 0
  const usos = capacidadesPorEquipo(matriz)
  // Teams are for the company's people: at level 4 the people of an organisation are not offered.
  const candidatas = useMemo(() => personas.filter((u) => u.activo && !(nivel >= 4 && u.organizacion)), [personas, nivel])
  const miembrosDe = (q: Equipo) => personas.filter((u) => u.activo && u.equipo === q.id)
  const activar = (q: Equipo, activo: boolean) =>
    api.cambiarEquipo(q.id, { activo }).then(cargar, (e) => setAviso(mensajeDeError(t, e)))

  const columnas: Columna<Equipo>[] = [
    { clave: 'nombre', titulo: t('col_nombre'), celda: (q) => q.nombre },
    { clave: 'miembros', titulo: t('equipos_col_miembros'), celda: (q) => {
      const m = miembrosDe(q)
      if (m.length === 0) return <Typography variant="body2" color="text.secondary">{t('equipos_nadie')}</Typography>
      return (
        <Stack direction="row" spacing={0.5} useFlexGap flexWrap="wrap">
          {m.slice(0, 5).map((u) => <Chip key={u.id} size="small" label={nombreDe(u)} />)}
          {m.length > 5 ? <Chip size="small" variant="outlined" label={t('equipos_y_mas', { n: m.length - 5 })} /> : null}
        </Stack>
      )
    } },
    { clave: 'estado', titulo: t('col_estado'), celda: (q) => (
      <Chip size="small" variant="outlined" color={q.activo ? 'success' : 'default'} sx={q.activo ? textoDe('success') : undefined} label={t(q.activo ? 'estado_activo' : 'equipos_archivado')} />
    ) },
    { clave: 'acciones', titulo: t('col_acciones'), alinear: 'right', celda: (q) => (
      <Stack direction="row" spacing={1} justifyContent="flex-end">
        {q.activo ? <Boton size="small" onClick={() => setEditando(q)}>{t('equipos_editar')}</Boton> : null}
        <Boton size="small" color={q.activo ? 'warning' : 'primary'} sx={q.activo ? textoDe('warning') : undefined} onClick={() => activar(q, !q.activo)}>
          {t(q.activo ? 'equipos_archivar' : 'accion_reactivar')}
        </Boton>
      </Stack>
    ) },
  ]
  const activos = (equipos || []).filter((q) => q.activo)

  return (
    <Pagina titulo={t('equipos_titulo')} subtitulo={t('equipos_subtitulo')}
      acciones={<Boton variant="contained" onClick={() => setCreando(true)}>{t('equipos_crear')}</Boton>}>
      <Stack spacing={2}>
        {matriz && usos.length === 0 ? <Alert severity="warning">{t('equipos_sin_efecto')}</Alert> : null}
        {usos.length > 0 ? (
          <Alert severity="info">{t('equipos_para_que', { lista: usos.map((c) => cortoDeTarea(t, c)).join(', ') })}</Alert>
        ) : null}
        {aviso ? <Alert severity="info" onClose={() => setAviso('')}>{aviso}</Alert> : null}
        {error ? <ErrorDelModulo error={error} reintentar={cargar} /> : null}
        <FormControlLabel control={<Switch size="small" checked={archivados} onChange={(_, v) => setArchivados(v)} />}
          label={t('equipos_ver_archivados')} />
        {!equipos && !error ? <Cargando /> : null}
        {equipos ? <Tabla columnas={columnas} filas={equipos} clave={(q) => q.id} vacio={t('equipos_vacio')} /> : null}
      </Stack>
      <DialogoEquipo abierto={creando || !!editando} equipo={editando} personas={candidatas} equipos={activos}
        cerrar={() => { setCreando(false); setEditando(null) }}
        hecho={(a) => { setCreando(false); setEditando(null); if (a) setAviso(a); cargar() }} />
    </Pagina>
  )
}
