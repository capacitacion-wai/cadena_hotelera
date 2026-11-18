/**
 * Profiling module — «Users» (administrators): who can use the app, in plain columns — Name · Company
 * (level 4: the organisation, or «internal» in the app's words) · What they can do (each role with its
 * one line) · Status — plus «Work group» ONLY when some permission says «their team» (`por_equipo`)
 * and «Companies they see» ONLY when some internal person on the page has a portfolio. One action,
 * «Edit», opens a single dialog with plain questions; «Deactivate» / «Reactivate» stays apart, and
 * «Delete» (destructive, after a confirmation) removes the person — disabled, with the reason, for
 * the owner of the app and for yourself. Add
 * from the project or by email (level 4 with external accounts: invite). Level 3: the rows still
 * without an owner are announced here, with who gets them. Paginated on the server. An organisation's
 * administrator (level 4) sees and manages ONLY the people of their organisation, with organisation
 * roles — the API enforces it; this screen only hides what they cannot do.
 *
 * INVARIANT FILE — installed by `wai-app-builder/assets/modulos.py`; do not edit it.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Alert, Box, Chip, Stack, TextField, Tooltip, Typography } from '@mui/material'
import { textoDe } from './accesible'
import { Boton, Pagina, Tabla } from './adaptador'
import { api, refrescarMiAcceso, useMiAcceso } from './api'
import { DialogoAcceso, DialogoAlta, DialogoBorrar, DialogoProyecto, DialogoSinDueno, rolesDelAmbito } from './Dialogos'
import type { CambioDeAcceso } from './Dialogos'
import { Cargando, ErrorDelModulo } from './Estado'
import { usaEquipo } from './resumen'
import { descripcionDeRol, mensajeDeError, nombreDeOrganizaciones, nombreDeRol, useTextos } from './textos'
import type { Columna, Equipo, Matriz, Organizacion, PaginaDeUsuarios, SinDueno, Usuario } from './tipos'

const POR_PAGINA = 20

export default function PaginaUsuarios() {
  const t = useTextos()
  const { yo } = useMiAcceso()
  const [pagina, setPagina] = useState<PaginaDeUsuarios | null>(null)
  const [matriz, setMatriz] = useState<Matriz | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [aviso, setAviso] = useState('')
  const [q, setQ] = useState('')
  const [desde, setDesde] = useState(0)
  const [dialogo, setDialogo] = useState<'' | 'correo' | 'proyecto'>('')
  const [editando, setEditando] = useState<Usuario | null>(null)
  const [equipos, setEquipos] = useState<Equipo[]>([])
  const [organizaciones, setOrganizaciones] = useState<Organizacion[]>([])
  const [sinDueno, setSinDueno] = useState<SinDueno[]>([])
  const [asignando, setAsignando] = useState<SinDueno | null>(null)
  const [borrando, setBorrando] = useState<Usuario | null>(null)

  const esAdmin = !!yo?.administra
  const nivel = matriz?.nivel || 0
  const cargar = useCallback(() => {
    setError(null)
    api.usuarios(POR_PAGINA, desde, q).then(setPagina, setError)
  }, [desde, q])
  useEffect(() => { cargar() }, [cargar])
  useEffect(() => { api.roles().then(setMatriz, setError) }, [])
  // An organisation's administrator manages their organisation's people only: no groups, no
  // organisations, no portfolios, no project (those are the internal administrators').
  const conEquipos = nivel >= 3 && esAdmin && usaEquipo(matriz)
  const conOrganizaciones = nivel >= 4
  const externas = conOrganizaciones && yo?.alta === 'externas'
  const etiquetaOrg = nombreDeOrganizaciones(t, yo?.nombre_de_las_organizaciones)
  useEffect(() => { if (conEquipos) api.equipos().then(setEquipos, setError) }, [conEquipos])
  useEffect(() => { if (conOrganizaciones) api.organizaciones().then(setOrganizaciones, setError) }, [conOrganizaciones])
  const cargarSinDueno = useCallback(() => { api.sinDueno().then(setSinDueno, () => setSinDueno([])) }, [])
  useEffect(() => { if (nivel >= 3 && esAdmin) cargarSinDueno() }, [nivel, esAdmin, cargarSinDueno])

  const roles = useMemo(() => matriz?.roles || [], [matriz])
  const rolesInternos = useMemo(() => rolesDelAmbito(roles, null, conOrganizaciones), [roles, conOrganizaciones])
  const cambiar = (u: Usuario, cambio: CambioDeAcceso & { activo?: boolean }) =>
    api.cambiar(u.id, cambio).then(() => { refrescarMiAcceso(); setEditando(null); cargar() })
  // The owner and yourself cannot be deleted (the API refuses it too): the button says why.
  const noSeBorra = (u: Usuario) => (u.es_dueno ? t('borrar_usuario_no_dueno') : yo && u.email === yo.email ? t('borrar_usuario_no_tu') : '')
  const nombreDeEquipo = (id?: string | null) => (id ? equipos.find((x) => x.id === id)?.nombre || '' : '')
  const nombreDeOrg = (id?: string | null) => (id ? organizaciones.find((o) => o.id === id)?.nombre || '' : '')
  const conCartera = conOrganizaciones && esAdmin && (pagina?.items || []).some((u) => !u.organizacion && u.cartera != null)

  const columnas: Columna<Usuario>[] = [
    { clave: 'nombre', titulo: t('col_nombre'), celda: (u) => (
      <Box>
        <Stack direction="row" spacing={1} alignItems="center">
          <Typography variant="body2" fontWeight={600}>{u.nombre || u.email}</Typography>
          {u.es_dueno ? <Chip size="small" color="primary" variant="outlined" label={t('dueno')} /> : null}
        </Stack>
        {u.nombre ? <Typography variant="caption" color="text.secondary">{u.email}</Typography> : null}
      </Box>
    ) },
    ...(conOrganizaciones ? [{ clave: 'empresa', titulo: t('col_empresa'), celda: (u: Usuario) => (
      u.organizacion
        ? <Typography variant="body2">{nombreDeOrg(u.organizacion) || etiquetaOrg}</Typography>
        : <Typography variant="body2" color="text.secondary">{t('empresa_interna')}</Typography>
    ) }] : []),
    { clave: 'puede', titulo: t('col_que_puede'), celda: (u) => (
      <Stack spacing={0.5}>
        {u.roles.map((id) => {
          const r = roles.find((x) => x.id === id)
          return (
            <Box key={id}>
              <Typography variant="body2">{nombreDeRol(t, id, r?.nombre, r?.fijo)}</Typography>
              {descripcionDeRol(t, r) ? <Typography variant="caption" color="text.secondary">{descripcionDeRol(t, r)}</Typography> : null}
            </Box>
          )
        })}
      </Stack>
    ) },
    ...(conCartera ? [{ clave: 'cartera', titulo: t('col_cartera', { organizaciones: etiquetaOrg }), celda: (u: Usuario) => (
      u.organizacion ? null : (
        <Typography variant="body2" color={u.cartera == null ? 'text.secondary' : undefined}>
          {u.cartera == null ? t('cartera_sin_limite') : u.cartera.map(nombreDeOrg).filter(Boolean).join(', ') || t('cartera_ninguna')}
        </Typography>
      )
    ) }] : []),
    ...(conEquipos ? [{ clave: 'grupo', titulo: t('col_grupo'), celda: (u: Usuario) => (
      u.organizacion && conOrganizaciones ? null : (
        <Typography variant="body2" color={u.equipo ? undefined : 'text.secondary'}>{nombreDeEquipo(u.equipo) || t('sin_grupo')}</Typography>
      )
    ) }] : []),
    { clave: 'estado', titulo: t('col_estado'), celda: (u) => (
      <Chip size="small" variant="outlined" color={u.activo ? 'success' : 'default'} sx={u.activo ? textoDe('success') : undefined} label={t(u.activo ? 'estado_activo' : 'estado_inactivo')} />
    ) },
    { clave: 'acciones', titulo: '', alinear: 'right', celda: (u) => (
      <Stack direction="row" spacing={1} justifyContent="flex-end">
        <Boton size="small" onClick={() => setEditando(u)}>{t('accion_editar')}</Boton>
        <Boton size="small" color={u.activo ? 'warning' : 'primary'} sx={u.activo ? textoDe('warning') : undefined}
          onClick={() => cambiar(u, { activo: !u.activo }).catch((e) => setAviso(mensajeDeError(t, e)))}>
          {t(u.activo ? 'accion_desactivar' : 'accion_reactivar')}
        </Boton>
        {noSeBorra(u) ? (
          <Tooltip title={noSeBorra(u)}>
            <span><Boton size="small" color="error" disabled>{t('accion_borrar_usuario')}</Boton></span>
          </Tooltip>
        ) : <Boton size="small" color="error" sx={textoDe('error')} onClick={() => setBorrando(u)}>{t('accion_borrar_usuario')}</Boton>}
      </Stack>
    ) },
  ]
  const hasta = pagina ? Math.min(desde + POR_PAGINA, pagina.total) : 0
  const pendientes = sinDueno.filter((x) => x.existe && (x.sin_dueno > 0 || !x.columnas))

  return (
    <Pagina
      titulo={t('usuarios_titulo')}
      subtitulo={t('usuarios_subtitulo')}
      acciones={(
        <Stack direction="row" spacing={1}>
          {esAdmin && yo?.alta === 'proyecto' ? <Boton variant="contained" onClick={() => setDialogo('proyecto')}>{t('usuarios_anadir_desde_proyecto')}</Boton> : null}
          <Boton variant={esAdmin && yo?.alta === 'proyecto' ? 'outlined' : 'contained'} onClick={() => setDialogo('correo')}>
            {t(externas ? 'usuarios_invitar' : 'usuarios_anadir')}
          </Boton>
        </Stack>
      )}
    >
      <Stack spacing={2}>
        {aviso ? <Alert severity="info" onClose={() => setAviso('')}>{aviso}</Alert> : null}
        {error ? <ErrorDelModulo error={error} reintentar={cargar} /> : null}
        {pendientes.map((x) => (
          <Alert key={x.tabla} severity="warning"
            action={x.columnas ? <Boton color="inherit" size="small" onClick={() => setAsignando(x)}>{t('sin_dueno_asignar')}</Boton> : undefined}>
            {x.columnas ? t('sin_dueno_aviso', { tabla: x.tabla, n: x.sin_dueno, total: x.total }) : t('sin_dueno_sin_columnas', { tabla: x.tabla })}
          </Alert>
        ))}
        <TextField size="small" label={t('usuarios_buscar')} value={q} onChange={(e) => { setDesde(0); setQ(e.target.value) }} />
        {!pagina && !error ? <Cargando /> : null}
        {pagina ? <Tabla columnas={columnas} filas={pagina.items} clave={(u) => u.id} vacio={t('usuarios_vacio')} /> : null}
        {pagina && pagina.total > POR_PAGINA ? (
          <Stack direction="row" spacing={1} alignItems="center" justifyContent="flex-end">
            <Typography variant="body2" color="text.secondary">{t('pagina', { desde: desde + 1, hasta, total: pagina.total })}</Typography>
            <Boton size="small" disabled={desde === 0} onClick={() => setDesde(Math.max(0, desde - POR_PAGINA))}>{t('anterior')}</Boton>
            <Boton size="small" disabled={hasta >= pagina.total} onClick={() => setDesde(desde + POR_PAGINA)}>{t('siguiente')}</Boton>
          </Stack>
        ) : null}
      </Stack>
      <DialogoAlta abierto={dialogo === 'correo'} roles={roles} cerrar={() => setDialogo('')}
        aviso={externas ? t('usuarios_aviso_externas') : yo?.alta === 'correo' ? t('usuarios_aviso_correo') : undefined}
        titulo={externas ? t('usuarios_invitar') : undefined}
        conOrganizaciones={conOrganizaciones} organizaciones={organizaciones} etiquetaOrganizacion={etiquetaOrg}
        organizacionFija={!esAdmin && yo?.administra_organizacion ? yo.organizacion : undefined}
        hecho={() => { setDialogo(''); cargar() }} />
      <DialogoProyecto abierto={dialogo === 'proyecto'} roles={rolesInternos} cerrar={() => setDialogo('')}
        hecho={(resumen) => { setDialogo(''); setAviso(resumen); cargar() }} />
      <DialogoAcceso usuario={editando} roles={roles} equipos={equipos} organizaciones={organizaciones} esAdmin={esAdmin}
        conEquipos={conEquipos} conOrganizaciones={conOrganizaciones}
        etiquetaOrganizacion={etiquetaOrg} cerrar={() => setEditando(null)}
        guardar={(c) => (editando ? cambiar(editando, c) : Promise.resolve())} />
      <DialogoBorrar usuario={borrando} conDuenos={nivel >= 3} cerrar={() => setBorrando(null)}
        hecho={(resumen) => { setBorrando(null); setAviso(resumen); cargar(); if (nivel >= 3 && esAdmin) cargarSinDueno() }} />
      <DialogoSinDueno tabla={asignando} equipos={equipos} conEquipos={conEquipos} cerrar={() => setAsignando(null)}
        hecho={() => { setAsignando(null); cargarSinDueno() }} />
    </Pagina>
  )
}
