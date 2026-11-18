/**
 * Profiling module — the dialogs of the Users screen, in plain questions: add by email, add from the
 * project, «Edit» (ONE dialog for everything that decides a person's access: «What can they do?» —
 * the roles, each with its one line —, «Which company are they from?» at level 4, «Which companies do
 * they see?» for an internal person at level 4, and «Which work group are they in?» only when some
 * permission says «their team»; saved with one request), the owner of the rows created before
 * owners existed (level 3) and «Delete» (a confirmation in plain words). Roles, groups and
 * organisations are chosen by NAME (never a raw id).
 *
 * INVARIANT FILE — installed by `wai-app-builder/assets/modulos.py`; do not edit it.
 */
import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import {
  Alert, Checkbox, Dialog, DialogActions, DialogContent, DialogTitle, FormControlLabel, FormGroup,
  List, ListItem, ListItemText, MenuItem, Radio, RadioGroup, Stack, TextField, Typography,
} from '@mui/material'
import { rellenoDe, TACTIL } from './accesible'
import { Boton } from './adaptador'
import { api } from './api'
import { Cargando, ErrorDelModulo } from './Estado'
import { descripcionDeRol, mensajeDeError, nombreDeRol, useTextos } from './textos'
import type { Equipo, Miembros, Organizacion, Rol, SinDueno, Usuario } from './tipos'

/** Level 4: the roles a person can hold on THEIR side of the wall (an organisation's person only
 *  organisation roles, an internal one only internal roles). Below level 4, all of them. */
export function rolesDelAmbito(roles: Rol[], organizacion: string | null | undefined, conOrganizaciones: boolean): Rol[] {
  if (!conOrganizaciones) return roles
  const ambito = organizacion ? 'organizacion' : 'interno'
  return roles.filter((r) => (r.ambito || 'interno') === ambito)
}

export function SelectorDeRoles({ roles, elegidos, cambiar, sinTitulo = false }: {
  roles: Rol[]; elegidos: string[]; cambiar: (r: string[]) => void; sinTitulo?: boolean
}) {
  const t = useTextos()
  return (
    <FormGroup>
      {sinTitulo ? null : <Typography variant="subtitle2" sx={{ mt: 1 }}>{t('campo_roles')}</Typography>}
      {roles.map((r) => (
        <FormControlLabel
          key={r.id}
          sx={{ alignItems: 'flex-start', mb: 0.5 }}
          label={(
            <Stack sx={{ pt: 1 }}>
              <Typography variant="body2">{nombreDeRol(t, r.id, r.nombre, r.fijo)}</Typography>
              {descripcionDeRol(t, r) ? <Typography variant="caption" color="text.secondary">{descripcionDeRol(t, r)}</Typography> : null}
            </Stack>
          )}
          control={<Checkbox
            checked={elegidos.includes(r.id)}
            onChange={(e) => cambiar(e.target.checked ? [...elegidos, r.id] : elegidos.filter((x) => x !== r.id))}
          />}
        />
      ))}
    </FormGroup>
  )
}

function porDefecto(roles: Rol[]): string[] {
  const comun = roles.find((r) => !r.administra)
  return comun ? [comun.id] : roles.slice(0, 1).map((r) => r.id)
}

export function DialogoAlta({ abierto, roles, aviso, cerrar, hecho, titulo, organizaciones, organizacionFija, conOrganizaciones = false, etiquetaOrganizacion }: {
  abierto: boolean; roles: Rol[]; aviso?: string; cerrar: () => void; hecho: () => void
  /** Level 4: the organisations to choose from (internal administrators) or the one fixed (an
   *  organisation's administrator adds only to theirs). */
  titulo?: string; organizaciones?: Organizacion[]; organizacionFija?: string | null; conOrganizaciones?: boolean
  etiquetaOrganizacion?: string
}) {
  const t = useTextos()
  const [email, setEmail] = useState('')
  const [nombre, setNombre] = useState('')
  const [organizacion, setOrganizacion] = useState('')
  const [elegidos, setElegidos] = useState<string[]>([])
  const [error, setError] = useState('')
  const org = organizacionFija || organizacion || null
  const posibles = rolesDelAmbito(roles, org, conOrganizaciones)
  useEffect(() => {
    if (abierto) { setEmail(''); setNombre(''); setError(''); setOrganizacion('') }
  }, [abierto])
  useEffect(() => { if (abierto) setElegidos(porDefecto(rolesDelAmbito(roles, organizacionFija || organizacion || null, conOrganizaciones))) },
    [abierto, roles, organizacion, organizacionFija, conOrganizaciones])
  const guardar = () => api.alta(email.trim(), elegidos, nombre.trim(), org).then(hecho, (e) => setError(mensajeDeError(t, e)))
  return (
    <Dialog open={abierto} onClose={cerrar} fullWidth maxWidth="xs" sx={TACTIL}>
      <DialogTitle>{titulo || t('usuarios_anadir')}</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ pt: 1 }}>
          {aviso ? <Alert severity="info">{aviso}</Alert> : null}
          {error ? <Alert severity="warning">{error}</Alert> : null}
          <TextField label={t('campo_correo')} type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoFocus />
          <TextField label={t('campo_nombre')} value={nombre} onChange={(e) => setNombre(e.target.value)} />
          {conOrganizaciones && !organizacionFija ? (
            <TextField select label={t('editar_empresa')} value={organizacion} onChange={(e) => setOrganizacion(e.target.value)}>
              <MenuItem value="">{t('empresa_interna')}</MenuItem>
              {(organizaciones || []).map((o) => <MenuItem key={o.id} value={o.id}>{o.nombre}</MenuItem>)}
            </TextField>
          ) : null}
          <SelectorDeRoles roles={posibles} elegidos={elegidos} cambiar={setElegidos} />
        </Stack>
      </DialogContent>
      <DialogActions>
        <Boton onClick={cerrar}>{t('cancelar')}</Boton>
        <Boton variant="contained" disabled={!email.trim() || elegidos.length === 0} onClick={guardar}>{t('anadir')}</Boton>
      </DialogActions>
    </Dialog>
  )
}

export function DialogoProyecto({ abierto, roles, cerrar, hecho }: {
  abierto: boolean; roles: Rol[]; cerrar: () => void; hecho: (resumen: string) => void
}) {
  const t = useTextos()
  const [miembros, setMiembros] = useState<Miembros | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [marcados, setMarcados] = useState<string[]>([])
  const [elegidos, setElegidos] = useState<string[]>([])
  useEffect(() => {
    if (!abierto) return
    setMiembros(null); setError(null); setMarcados([]); setElegidos(porDefecto(roles))
    api.proyecto().then(setMiembros, setError)
  }, [abierto, roles])
  const pendientes = (miembros?.miembros || []).filter((m) => !m.dado_de_alta)
  const guardar = () => api.altaDesdeProyecto(marcados, elegidos).then(
    (r) => hecho(t('proyecto_resultado', { altas: r.dados_de_alta.length, ya: r.ya_estaban.length, fuera: r.no_son_del_proyecto.length })),
    setError,
  )
  return (
    <Dialog open={abierto} onClose={cerrar} fullWidth maxWidth="sm" sx={TACTIL}>
      <DialogTitle>{t('proyecto_titulo')}</DialogTitle>
      <DialogContent>
        {error ? <ErrorDelModulo error={error} /> : null}
        {!miembros && !error ? <Cargando /> : null}
        {miembros && !miembros.disponible ? <Alert severity="warning">{t('proyecto_no_disponible')}</Alert> : null}
        {miembros?.disponible ? (
          <Stack spacing={1}>
            {pendientes.length === 0 ? <Typography variant="body2" color="text.secondary">{t('proyecto_vacio')}</Typography> : null}
            <List dense>
              {miembros.miembros.map((m) => (
                <ListItem key={m.email} disableGutters secondaryAction={m.dado_de_alta ? <Typography variant="caption" color="text.secondary">{t('proyecto_ya')}</Typography> : null}>
                  <Checkbox
                    edge="start"
                    disabled={m.dado_de_alta}
                    checked={m.dado_de_alta || marcados.includes(m.email)}
                    onChange={(e) => setMarcados(e.target.checked ? [...marcados, m.email] : marcados.filter((x) => x !== m.email))}
                  />
                  <ListItemText primary={m.nombre || m.email} secondary={m.nombre ? m.email : undefined} />
                </ListItem>
              ))}
            </List>
            <SelectorDeRoles roles={roles} elegidos={elegidos} cambiar={setElegidos} />
          </Stack>
        ) : null}
      </DialogContent>
      <DialogActions>
        <Boton onClick={cerrar}>{t('cancelar')}</Boton>
        <Boton variant="contained" disabled={marcados.length === 0 || elegidos.length === 0} onClick={guardar}>{t('anadir')}</Boton>
      </DialogActions>
    </Dialog>
  )
}

/** One question of «Edit»: the question and, when it helps, one line saying what it decides. */
function Pregunta({ titulo, ayuda, children }: { titulo: string; ayuda?: string; children: ReactNode }) {
  return (
    <Stack spacing={0.5}>
      <Typography variant="subtitle2">{titulo}</Typography>
      {ayuda ? <Typography variant="body2" color="text.secondary">{ayuda}</Typography> : null}
      {children}
    </Stack>
  )
}

export type CambioDeAcceso = { roles?: string[]; organizacion?: string | null; cartera?: string[] | null; equipo?: string | null }

const mismos = (a: string[] | null | undefined, b: string[] | null | undefined) =>
  (a == null) === (b == null) && [...(a || [])].sort().join('|') === [...(b || [])].sort().join('|')

/** «Edit» one person. What it asks: an organisation's administrator only the roles (the API refuses
 *  the rest); an internal administrator also, at level 4, the company (never for the owner) and which
 *  companies an internal person sees, and the work group of an internal person only when groups are
 *  used. Only what changed is sent, in ONE request (roles travel with a new company: the API checks
 *  them together; moving someone to an outside company takes them out of their group). */
export function DialogoAcceso({ usuario, roles, equipos, organizaciones, esAdmin, conEquipos, conOrganizaciones,
  etiquetaOrganizacion, cerrar, guardar }: {
  usuario: Usuario | null; roles: Rol[]; equipos: Equipo[]; organizaciones: Organizacion[]; esAdmin: boolean
  /** Work groups are used (some permission says «their team»): only then the question is asked. */
  conEquipos: boolean; conOrganizaciones: boolean; etiquetaOrganizacion: string
  cerrar: () => void; guardar: (cambio: CambioDeAcceso) => Promise<void>
}) {
  const t = useTextos()
  const [org, setOrg] = useState('')
  const [elegidos, setElegidos] = useState<string[]>([])
  const [sinLimite, setSinLimite] = useState(true)
  const [marcadas, setMarcadas] = useState<string[]>([])
  const [equipo, setEquipo] = useState('')
  const [error, setError] = useState('')
  useEffect(() => {
    if (!usuario) return
    setOrg(usuario.organizacion || ''); setElegidos(usuario.roles); setSinLimite(usuario.cartera == null)
    setMarcadas(usuario.cartera || []); setEquipo(usuario.equipo || ''); setError('')
  }, [usuario])
  const verEmpresa = conOrganizaciones && esAdmin && !usuario?.es_dueno
  const verCartera = conOrganizaciones && esAdmin && !org && organizaciones.length > 0
  const verEquipo = conEquipos && esAdmin && !(conOrganizaciones && org)
  const posibles = rolesDelAmbito(roles, org || null, conOrganizaciones)
  const cambiarEmpresa = (nueva: string) => {
    setOrg(nueva)
    const delLado = rolesDelAmbito(roles, nueva || null, conOrganizaciones)
    const validos = elegidos.filter((r) => delLado.some((x) => x.id === r))
    setElegidos(validos.length ? validos : porDefecto(delLado))
  }
  const enviar = () => {
    if (!usuario) return
    const cambio: CambioDeAcceso = {}
    if (!mismos(elegidos, usuario.roles)) cambio.roles = elegidos
    if (verEmpresa && (org || null) !== (usuario.organizacion || null)) {
      cambio.organizacion = org || null
      cambio.roles = elegidos
      if (org && usuario.equipo) cambio.equipo = null
    }
    if (verCartera) {
      const nueva = sinLimite ? null : marcadas
      if (!mismos(nueva, usuario.organizacion ? null : usuario.cartera)) cambio.cartera = nueva
    }
    if (verEquipo && (equipo || null) !== (usuario.equipo || null)) cambio.equipo = equipo || null
    if (Object.keys(cambio).length === 0) { cerrar(); return }
    guardar(cambio).catch((e) => setError(mensajeDeError(t, e)))
  }
  return (
    <Dialog open={!!usuario} onClose={cerrar} fullWidth maxWidth="sm" sx={TACTIL}>
      <DialogTitle>{usuario?.nombre || usuario?.email || ''}</DialogTitle>
      <DialogContent>
        <Stack spacing={2.5} sx={{ pt: 1 }}>
          {error ? <Alert severity="warning">{error}</Alert> : null}
          {verEmpresa ? (
            <Pregunta titulo={t('editar_empresa')} ayuda={t('editar_empresa_ayuda')}>
              <TextField select size="small" value={org} onChange={(e) => cambiarEmpresa(e.target.value)}>
                <MenuItem value="">{t('empresa_interna')}</MenuItem>
                {organizaciones.map((o) => <MenuItem key={o.id} value={o.id}>{o.nombre}</MenuItem>)}
              </TextField>
            </Pregunta>
          ) : null}
          <Pregunta titulo={t('editar_que_puede')}>
            <SelectorDeRoles roles={posibles} elegidos={elegidos} cambiar={setElegidos} sinTitulo />
          </Pregunta>
          {verCartera ? (
            <Pregunta titulo={t('editar_cartera', { organizaciones: etiquetaOrganizacion })}>
              <RadioGroup value={sinLimite ? 'todas' : 'solo'} onChange={(_, v) => setSinLimite(v === 'todas')}>
                <FormControlLabel value="todas" control={<Radio size="small" />} label={t('editar_cartera_todas')} />
                <FormControlLabel value="solo" control={<Radio size="small" />} label={t('editar_cartera_lista')} />
              </RadioGroup>
              {!sinLimite ? (
                <FormGroup sx={{ pl: 3 }}>
                  {organizaciones.map((o) => (
                    <FormControlLabel key={o.id} label={o.nombre} control={<Checkbox checked={marcadas.includes(o.id)}
                      onChange={(e) => setMarcadas(e.target.checked ? [...marcadas, o.id] : marcadas.filter((x) => x !== o.id))} />} />
                  ))}
                </FormGroup>
              ) : null}
            </Pregunta>
          ) : null}
          {verEquipo ? (
            <Pregunta titulo={t('editar_grupo')} ayuda={t('editar_grupo_ayuda')}>
              <TextField select size="small" value={equipo} onChange={(e) => setEquipo(e.target.value)}>
                <MenuItem value="">{t('sin_grupo')}</MenuItem>
                {equipos.map((q) => <MenuItem key={q.id} value={q.id}>{q.nombre}</MenuItem>)}
              </TextField>
            </Pregunta>
          ) : null}
        </Stack>
      </DialogContent>
      <DialogActions>
        <Boton onClick={cerrar}>{t('cancelar')}</Boton>
        <Boton variant="contained" disabled={elegidos.length === 0} onClick={enviar}>{t('guardar')}</Boton>
      </DialogActions>
    </Dialog>
  )
}

/** Level 3: gives the rows of one table without an owner to a person (and their work group, when
 *  groups are used) — chosen by NAME. Only the rows WITHOUT an owner change. */
export function DialogoSinDueno({ tabla, equipos, conEquipos, cerrar, hecho }: {
  tabla: SinDueno | null; equipos: Equipo[]; conEquipos: boolean; cerrar: () => void; hecho: () => void
}) {
  const t = useTextos()
  const [personas, setPersonas] = useState<Usuario[]>([])
  const [dueno, setDueno] = useState('')
  const [equipo, setEquipo] = useState('')
  const [error, setError] = useState<unknown>(null)
  useEffect(() => {
    if (!tabla) return
    setDueno(''); setEquipo(''); setError(null)
    api.usuarios(200, 0, '').then((p) => setPersonas(p.items.filter((u) => u.activo)), setError)
  }, [tabla])
  const elegir = (email: string) => {
    setDueno(email)
    setEquipo(personas.find((u) => u.email === email)?.equipo || '')
  }
  return (
    <Dialog open={!!tabla} onClose={cerrar} fullWidth maxWidth="xs" sx={TACTIL}>
      <DialogTitle>{t('sin_dueno_asignar_titulo', { tabla: tabla?.tabla || '', n: tabla?.sin_dueno || 0 })}</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ pt: 1 }}>
          <Alert severity="info">{t('sin_dueno_asignar_aviso')}</Alert>
          {error ? <ErrorDelModulo error={error} /> : null}
          <TextField select label={t('sin_dueno_campo_dueno')} value={dueno} onChange={(e) => elegir(e.target.value)}>
            {personas.map((u) => <MenuItem key={u.email} value={u.email}>{u.nombre ? `${u.nombre} · ${u.email}` : u.email}</MenuItem>)}
          </TextField>
          {conEquipos ? (
            <TextField select label={t('col_grupo')} value={equipo} onChange={(e) => setEquipo(e.target.value)}>
              <MenuItem value="">{t('sin_grupo')}</MenuItem>
              {equipos.map((q) => <MenuItem key={q.id} value={q.id}>{q.nombre}</MenuItem>)}
            </TextField>
          ) : null}
        </Stack>
      </DialogContent>
      <DialogActions>
        <Boton onClick={cerrar}>{t('cancelar')}</Boton>
        <Boton variant="contained" disabled={!dueno || !tabla}
          onClick={() => tabla && api.asignarSinDueno(tabla.tabla, dueno, equipo || null).then(hecho, setError)}>
          {t('sin_dueno_asignar')}
        </Boton>
      </DialogActions>
    </Dialog>
  )
}

/** «Borrar»: one or two plain sentences and a destructive button. The person stops getting in and
 *  loses their permissions; what they created stays (level 3+: it becomes «without an owner»). */
export function DialogoBorrar({ usuario, conDuenos, cerrar, hecho }: {
  usuario: Usuario | null; conDuenos: boolean; cerrar: () => void; hecho: (aviso: string) => void
}) {
  const t = useTextos()
  const [error, setError] = useState('')
  const [enviando, setEnviando] = useState(false)
  useEffect(() => { if (usuario) { setError(''); setEnviando(false) } }, [usuario])
  const nombre = usuario?.nombre || usuario?.email || ''
  const borrar = () => {
    if (!usuario) return
    setEnviando(true)
    api.borrar(usuario.id).then(
      (r) => hecho(t(r.cuenta_externa_abierta ? 'borrar_usuario_hecho_cuenta_abierta' : 'borrar_usuario_hecho', { nombre })),
      (e) => { setEnviando(false); setError(mensajeDeError(t, e)) },
    )
  }
  return (
    <Dialog open={!!usuario} onClose={cerrar} fullWidth maxWidth="xs" sx={TACTIL}>
      <DialogTitle>{t('borrar_usuario_titulo', { nombre })}</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ pt: 1 }}>
          <Typography variant="body2">
            {t(conDuenos ? 'borrar_usuario_aviso' : 'borrar_usuario_aviso_sin_duenos', { nombre })}
          </Typography>
          {error ? <Alert severity="error">{error}</Alert> : null}
        </Stack>
      </DialogContent>
      <DialogActions>
        <Boton onClick={cerrar}>{t('cancelar')}</Boton>
        <Boton variant="contained" color="error" sx={rellenoDe('error')} disabled={!usuario || enviando} onClick={borrar}>
          {t('borrar_usuario_boton')}
        </Boton>
      </DialogActions>
    </Dialog>
  )
}
