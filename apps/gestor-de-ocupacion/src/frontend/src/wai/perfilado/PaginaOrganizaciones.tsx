/**
 * Profiling module — «Organisations» (internal administrators, level 4): the outside organisations
 * of the app (suppliers, customers… — the app names them with the setting `nombre_organizacion`),
 * how many active people each has, «Invite person» straight into one (its people get only the roles
 * for outside people), create / rename / archive (one with people cannot be archived; an
 * archived one is kept because the records still point at it). Below, the rows still WITHOUT an
 * organisation (created before level 4): only internal people who reach every organisation see them
 * until an administrator decides whose they are. The people of each organisation and the portfolio
 * of each internal person are set in «Users».
 *
 * INVARIANT FILE — installed by `wai-app-builder/assets/modulos.py`; do not edit it. It looks like
 * the app through the adapter (`Pagina`, `Tabla`, `Boton`) and the theme.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert, Chip, Dialog, DialogActions, DialogContent, DialogTitle, FormControlLabel, MenuItem, Stack, Switch, TextField,
} from '@mui/material'
import { TACTIL, textoDe } from './accesible'
import { Boton, Pagina, Tabla } from './adaptador'
import { api, useMiAcceso } from './api'
import { DialogoAlta } from './Dialogos'
import { Cargando, ErrorDelModulo } from './Estado'
import { mensajeDeError, nombreDeOrganizaciones, useTextos } from './textos'
import type { Columna, Matriz, Organizacion, SinOrganizacion } from './tipos'

function DialogoNombre({ abierto, titulo, inicial, cerrar, guardar }: {
  abierto: boolean; titulo: string; inicial: string; cerrar: () => void; guardar: (nombre: string) => Promise<unknown>
}) {
  const t = useTextos()
  const [nombre, setNombre] = useState(inicial)
  const [error, setError] = useState('')
  useEffect(() => { if (abierto) { setNombre(inicial); setError('') } }, [abierto, inicial])
  return (
    <Dialog open={abierto} onClose={cerrar} fullWidth maxWidth="xs" sx={TACTIL}>
      <DialogTitle>{titulo}</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ pt: 1 }}>
          {error ? <Alert severity="warning">{error}</Alert> : null}
          <TextField label={t('organizaciones_campo_nombre')} value={nombre} onChange={(e) => setNombre(e.target.value)} autoFocus />
        </Stack>
      </DialogContent>
      <DialogActions>
        <Boton onClick={cerrar}>{t('cancelar')}</Boton>
        <Boton variant="contained" disabled={!nombre.trim()}
          onClick={() => guardar(nombre.trim()).catch((e) => setError(mensajeDeError(t, e)))}>{t('guardar')}</Boton>
      </DialogActions>
    </Dialog>
  )
}

/** Gives the rows of one table without an organisation to an organisation — chosen by NAME. */
function DialogoAsignar({ tabla, organizaciones, cerrar, hecho }: {
  tabla: SinOrganizacion | null; organizaciones: Organizacion[]; cerrar: () => void; hecho: () => void
}) {
  const t = useTextos()
  const [elegida, setElegida] = useState('')
  const [error, setError] = useState<unknown>(null)
  useEffect(() => { if (tabla) { setElegida(''); setError(null) } }, [tabla])
  return (
    <Dialog open={!!tabla} onClose={cerrar} fullWidth maxWidth="xs" sx={TACTIL}>
      <DialogTitle>{t('sin_organizacion_asignar_titulo', { tabla: tabla?.tabla || '', n: tabla?.sin_organizacion || 0 })}</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ pt: 1 }}>
          <Alert severity="info">{t('sin_organizacion_asignar_aviso')}</Alert>
          {error ? <ErrorDelModulo error={error} /> : null}
          <TextField select label={t('col_empresa')} value={elegida} onChange={(e) => setElegida(e.target.value)}>
            {organizaciones.map((o) => <MenuItem key={o.id} value={o.id}>{o.nombre}</MenuItem>)}
          </TextField>
        </Stack>
      </DialogContent>
      <DialogActions>
        <Boton onClick={cerrar}>{t('cancelar')}</Boton>
        <Boton variant="contained" disabled={!elegida || !tabla}
          onClick={() => tabla && api.asignarSinOrganizacion(tabla.tabla, elegida).then(hecho, setError)}>
          {t('sin_organizacion_asignar')}
        </Boton>
      </DialogActions>
    </Dialog>
  )
}

export default function PaginaOrganizaciones() {
  const t = useTextos()
  const { yo } = useMiAcceso()
  const [organizaciones, setOrganizaciones] = useState<Organizacion[] | null>(null)
  const [sinOrganizacion, setSinOrganizacion] = useState<SinOrganizacion[]>([])
  const [archivadas, setArchivadas] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [aviso, setAviso] = useState('')
  const [creando, setCreando] = useState(false)
  const [renombrando, setRenombrando] = useState<Organizacion | null>(null)
  const [asignando, setAsignando] = useState<SinOrganizacion | null>(null)
  const [invitandoA, setInvitandoA] = useState<Organizacion | null>(null)
  const [matriz, setMatriz] = useState<Matriz | null>(null)
  useEffect(() => { api.roles().then(setMatriz, () => setMatriz(null)) }, [])
  // The roles for outside people (an organisation's person may only hold those).
  const rolesDeFuera = useMemo(() => (matriz?.roles || []).filter((r) => r.ambito === 'organizacion'), [matriz])
  const externas = yo?.alta === 'externas'
  const titulo = nombreDeOrganizaciones(t, yo?.nombre_de_las_organizaciones)

  const cargar = useCallback(() => {
    setError(null)
    api.organizaciones(archivadas).then(setOrganizaciones, setError)
    api.sinOrganizacion().then(setSinOrganizacion, () => setSinOrganizacion([]))
  }, [archivadas])
  useEffect(() => { cargar() }, [cargar])

  const activar = (o: Organizacion, activo: boolean) =>
    api.cambiarOrganizacion(o.id, { activo }).then(cargar, (e) => setAviso(mensajeDeError(t, e)))

  const columnas: Columna<Organizacion>[] = [
    { clave: 'nombre', titulo: t('col_nombre'), celda: (o) => o.nombre },
    { clave: 'miembros', titulo: t('organizaciones_col_miembros'), alinear: 'right', celda: (o) => o.miembros },
    { clave: 'estado', titulo: t('col_estado'), celda: (o) => (
      <Chip size="small" variant="outlined" color={o.activo ? 'success' : 'default'} sx={o.activo ? textoDe('success') : undefined} label={t(o.activo ? 'estado_activo' : 'organizaciones_archivada')} />
    ) },
    { clave: 'acciones', titulo: t('col_acciones'), alinear: 'right', celda: (o) => (
      <Stack direction="row" spacing={1} justifyContent="flex-end">
        {o.activo && rolesDeFuera.length ? (
          <Boton size="small" onClick={() => setInvitandoA(o)}>{t(externas ? 'organizaciones_invitar' : 'organizaciones_anadir_persona')}</Boton>
        ) : null}
        <Boton size="small" onClick={() => setRenombrando(o)}>{t('organizaciones_renombrar')}</Boton>
        <Boton size="small" color={o.activo ? 'warning' : 'primary'} sx={o.activo ? textoDe('warning') : undefined} onClick={() => activar(o, !o.activo)}>
          {t(o.activo ? 'organizaciones_archivar' : 'accion_reactivar')}
        </Boton>
      </Stack>
    ) },
  ]
  const pendientes = sinOrganizacion.filter((x) => x.existe && (x.sin_organizacion > 0 || !x.columnas))

  return (
    <Pagina titulo={titulo} subtitulo={t('organizaciones_subtitulo')}
      acciones={<Boton variant="contained" onClick={() => setCreando(true)}>{t('organizaciones_crear')}</Boton>}>
      <Stack spacing={2}>
        {aviso ? <Alert severity="info" onClose={() => setAviso('')}>{aviso}</Alert> : null}
        {error ? <ErrorDelModulo error={error} reintentar={cargar} /> : null}
        {pendientes.map((x) => (
          <Alert key={x.tabla} severity="warning"
            action={x.columnas ? <Boton color="inherit" size="small" onClick={() => setAsignando(x)}>{t('sin_organizacion_asignar')}</Boton> : undefined}>
            {x.columnas
              ? t('sin_organizacion_aviso', { tabla: x.tabla, n: x.sin_organizacion, total: x.total })
              : t('sin_organizacion_sin_columna', { tabla: x.tabla })}
          </Alert>
        ))}
        <FormControlLabel control={<Switch size="small" checked={archivadas} onChange={(_, v) => setArchivadas(v)} />}
          label={t('organizaciones_ver_archivadas')} />
        {!organizaciones && !error ? <Cargando /> : null}
        {organizaciones ? <Tabla columnas={columnas} filas={organizaciones} clave={(o) => o.id} vacio={t('organizaciones_vacio')} /> : null}
      </Stack>
      <DialogoNombre abierto={creando} titulo={t('organizaciones_crear')} inicial="" cerrar={() => setCreando(false)}
        guardar={(nombre) => api.crearOrganizacion(nombre).then(() => { setCreando(false); cargar() })} />
      <DialogoNombre abierto={!!renombrando} titulo={t('organizaciones_renombrar')} inicial={renombrando?.nombre || ''}
        cerrar={() => setRenombrando(null)}
        guardar={(nombre) => (renombrando ? api.cambiarOrganizacion(renombrando.id, { nombre }).then(() => { setRenombrando(null); cargar() }) : Promise.resolve())} />
      <DialogoAlta abierto={!!invitandoA} roles={rolesDeFuera} cerrar={() => setInvitandoA(null)}
        titulo={invitandoA ? t(externas ? 'organizaciones_invitar_a' : 'organizaciones_anadir_a', { organizacion: invitandoA.nombre }) : undefined}
        aviso={externas ? t('organizaciones_aviso_externas') : yo?.alta === 'correo' ? t('usuarios_aviso_correo') : undefined}
        conOrganizaciones organizaciones={organizaciones || []} organizacionFija={invitandoA?.id}
        hecho={() => { setInvitandoA(null); cargar() }} />
      <DialogoAsignar tabla={asignando} organizaciones={(organizaciones || []).filter((o) => o.activo)} cerrar={() => setAsignando(null)}
        hecho={() => { setAsignando(null); cargar() }} />
    </Pagina>
  )
}
