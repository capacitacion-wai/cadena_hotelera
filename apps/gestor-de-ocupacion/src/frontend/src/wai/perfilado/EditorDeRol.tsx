/**
 * Profiling module — the custom roles' dialogs (setting `roles_personalizables`, internal administrators):
 * «Create role» / «Edit» and «Delete». The editor speaks plainly: name, one line «who it is for»,
 * (level 4) for the company's people or for an outside company's, and per capability (its short name)
 * the actions it may get — ONLY those the API's `piezas` offers, i.e. what the Definition already gives
 * some role of that side — with «sees: only their own / their group's / everything» when there is a
 * choice. Before saving it shows the card the role will have («Can… / Cannot…»). Deleting a role somebody
 * holds asks where to move them (a role of the same side), in the same step.
 *
 * INVARIANT FILE — installed by `wai-app-builder/assets/modulos.py`; do not edit it.
 */
import { useEffect, useMemo, useState } from 'react'
import {
  Alert, Box, Checkbox, Dialog, DialogActions, DialogContent, DialogTitle, FormControlLabel, MenuItem, Radio, RadioGroup,
  Stack, TextField, Typography,
} from '@mui/material'
import { rellenoDe, TACTIL } from './accesible'
import { Boton } from './adaptador'
import { api } from './api'
import { Cargando, ErrorDelModulo } from './Estado'
import TarjetaDeRol from './TarjetaDeRol'
import { cortoDeTarea, mensajeDeError, nombreDeOrganizaciones, nombreDeRol, useTextos } from './textos'
import type { Accion, Alcance, Capacidad, Matriz, Piezas, Rol, RolPropio } from './tipos'

type Lado = 'interno' | 'organizacion'

/** The scopes offered for a capability: those every ticked action allows (narrowest first). */
function opcionesComunes(porAccion: Partial<Record<Accion, Alcance[]>>, acciones: Accion[]): Alcance[] {
  const listas = acciones.map((a) => porAccion[a] || [])
  return listas.length ? listas[0].filter((s) => listas.every((l) => l.includes(s))) : []
}

export function EditorDeRol({ abierto, rol, matriz, cerrar, hecho }: {
  abierto: boolean; rol: Rol | null; matriz: Matriz; cerrar: () => void; hecho: () => void
}) {
  const t = useTextos()
  const [piezas, setPiezas] = useState<Piezas | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [aviso, setAviso] = useState('')
  const [nombre, setNombre] = useState('')
  const [quien, setQuien] = useState('')
  const [lado, setLado] = useState<Lado>('interno')
  const [elegidas, setElegidas] = useState<Record<string, Accion[]>>({})
  const [ve, setVe] = useState<Record<string, Alcance>>({})
  useEffect(() => {
    if (!abierto) return
    setError(null); setAviso('')
    setNombre(rol?.nombre || ''); setQuien(rol?.quien || ''); setLado(rol?.ambito === 'organizacion' ? 'organizacion' : 'interno')
    setElegidas(Object.fromEntries(Object.entries(rol?.permisos || {}).map(([c, a]) => [c, [...a]])))
    // One «sees» per capability in this editor: the narrowest the role has there (what it promises).
    setVe(Object.fromEntries(Object.entries(rol?.alcances || {}).flatMap(([c, porAccion]) => {
      if (!porAccion || typeof porAccion !== 'object') return []
      const valores = Object.values(porAccion) as Alcance[]
      const orden: Alcance[] = ['propio', 'equipo', 'todo']
      return valores.length ? [[c, orden.find((s) => valores.includes(s)) as Alcance]] : []
    })))
    api.piezasDeLosRoles().then(setPiezas, setError)
  }, [abierto, rol])

  const delLado = piezas?.[lado] || {}
  const capacidades: Capacidad[] = matriz.capacidades.filter((c) => delLado[c.id])
  const etiquetaOrg = nombreDeOrganizaciones(t, matriz.ajustes?.nombre_organizacion)
  const enUso = (rol?.personas || []).length > 0

  const propuesta: RolPropio = useMemo(() => {
    const permisos: Record<string, Accion[]> = {}
    const alcances: Record<string, Partial<Record<Accion, Alcance>>> = {}
    for (const [c, acciones] of Object.entries(elegidas)) {
      const validas = acciones.filter((a) => delLado[c]?.[a])
      if (!validas.length) continue
      permisos[c] = validas
      const opciones = opcionesComunes(delLado[c] || {}, validas)
      const elegido = ve[c] && opciones.includes(ve[c]) ? ve[c] : opciones[opciones.length - 1]
      if (elegido) alcances[c] = Object.fromEntries(validas.map((a) => [a, elegido]))
    }
    return { nombre: nombre.trim(), quien: quien.trim(), ambito: lado, permisos, alcances }
  }, [elegidas, ve, delLado, nombre, quien, lado])

  const vista: Rol = { id: rol?.id || '__nuevo__', nombre: propuesta.nombre || t('editor_sin_nombre'), quien: propuesta.quien,
    fijo: false, administra: false, permisos: propuesta.permisos, alcances: propuesta.alcances, ambito: lado }
  const marcar = (c: string, a: Accion, si: boolean) => {
    const actuales = elegidas[c] || []
    setElegidas({ ...elegidas, [c]: si ? [...actuales, a] : actuales.filter((x) => x !== a) })
  }
  const guardar = () => (rol ? api.cambiarRol(rol.id, propuesta) : api.crearRol(propuesta))
    .then(hecho, (e) => setAviso(mensajeDeError(t, e)))

  return (
    <Dialog open={abierto} onClose={cerrar} fullWidth maxWidth="md" sx={TACTIL}>
      <DialogTitle>{rol ? t('editor_titulo_editar', { rol: nombreDeRol(t, rol.id, rol.nombre, rol.fijo) }) : t('editor_titulo_crear')}</DialogTitle>
      <DialogContent>
        {error ? <ErrorDelModulo error={error} /> : null}
        {!piezas && !error ? <Cargando /> : null}
        {piezas ? (
          <Box sx={{ display: 'grid', gap: 3, gridTemplateColumns: { xs: '1fr', md: '3fr 2fr' }, pt: 1 }}>
            <Stack spacing={2}>
              {aviso ? <Alert severity="warning">{aviso}</Alert> : null}
              <TextField label={t('editor_nombre')} value={nombre} onChange={(e) => setNombre(e.target.value)} autoFocus={!rol} />
              <TextField label={t('editor_quien')} helperText={t('editor_quien_ayuda')} value={quien} onChange={(e) => setQuien(e.target.value)} />
              {matriz.nivel >= 4 ? (
                <Stack spacing={0.5}>
                  <Typography variant="subtitle2">{t('editor_para_quien')}</Typography>
                  <RadioGroup value={lado} onChange={(_, v) => { setLado(v as Lado); setElegidas({}); setVe({}) }}>
                    <FormControlLabel value="interno" disabled={enUso} control={<Radio size="small" />} label={t('editor_para_internos')} />
                    <FormControlLabel value="organizacion" disabled={enUso} control={<Radio size="small" />}
                      label={t('editor_para_organizacion', { organizaciones: etiquetaOrg })} />
                  </RadioGroup>
                  {enUso ? <Typography variant="caption" color="text.secondary">{t('editor_lado_fijo')}</Typography> : null}
                  {lado === 'organizacion' ? <Typography variant="caption" color="text.secondary">{t('editor_muro')}</Typography> : null}
                </Stack>
              ) : null}
              <Typography variant="subtitle2">{t('editor_que_puede')}</Typography>
              <Typography variant="body2" color="text.secondary">{t('editor_que_puede_ayuda')}</Typography>
              {capacidades.length === 0 ? <Typography variant="body2" color="text.secondary">{t('editor_nada_que_elegir')}</Typography> : null}
              {capacidades.map((c) => {
                const porAccion = delLado[c.id] || {}
                const acciones = matriz.acciones.filter((a) => porAccion[a])
                const marcadas = (elegidas[c.id] || []).filter((a) => porAccion[a])
                const opciones = opcionesComunes(porAccion, marcadas)
                return (
                  <Box key={c.id}>
                    <Typography variant="body2" fontWeight={600}>{cortoDeTarea(t, c)}</Typography>
                    <Stack direction="row" useFlexGap flexWrap="wrap" alignItems="center" columnGap={1}>
                      {acciones.map((a) => (
                        <FormControlLabel key={a} label={t(`accion_${a}`)}
                          control={<Checkbox size="small" checked={marcadas.includes(a)} onChange={(e) => marcar(c.id, a, e.target.checked)} />} />
                      ))}
                      {marcadas.length && opciones.length > 1 ? (
                        <TextField select size="small" label={t('editor_ve')} sx={{ minWidth: 180 }}
                          value={ve[c.id] && opciones.includes(ve[c.id]) ? ve[c.id] : opciones[opciones.length - 1]}
                          onChange={(e) => setVe({ ...ve, [c.id]: e.target.value as Alcance })}>
                          {opciones.map((s) => <MenuItem key={s} value={s}>{t(`editor_ve_${s}`)}</MenuItem>)}
                        </TextField>
                      ) : null}
                    </Stack>
                  </Box>
                )
              })}
            </Stack>
            <Stack spacing={1}>
              <Typography variant="subtitle2">{t('editor_vista')}</Typography>
              <TarjetaDeRol rol={vista} matriz={matriz} capacidades={matriz.capacidades} t={t} />
            </Stack>
          </Box>
        ) : null}
      </DialogContent>
      <DialogActions>
        <Boton onClick={cerrar}>{t('cancelar')}</Boton>
        <Boton variant="contained" disabled={!piezas || !propuesta.nombre || Object.keys(propuesta.permisos).length === 0}
          onClick={guardar}>{t('guardar')}</Boton>
      </DialogActions>
    </Dialog>
  )
}

/** Delete a custom role; if somebody holds it, they move to the role chosen here (same side). */
export function DialogoBorrarRol({ rol, matriz, cerrar, hecho }: {
  rol: Rol | null; matriz: Matriz; cerrar: () => void; hecho: () => void
}) {
  const t = useTextos()
  const [destino, setDestino] = useState('')
  const [aviso, setAviso] = useState('')
  useEffect(() => { setDestino(''); setAviso('') }, [rol])
  const personas = rol?.personas || []
  const lado = (r: Rol) => (matriz.nivel >= 4 && r.ambito === 'organizacion' ? 'organizacion' : 'interno')
  const posibles = rol ? matriz.roles.filter((r) => r.id !== rol.id && lado(r) === lado(rol)) : []
  return (
    <Dialog open={!!rol} onClose={cerrar} fullWidth maxWidth="xs" sx={TACTIL}>
      <DialogTitle>{t('borrar_rol_titulo', { rol: rol?.nombre || '' })}</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ pt: 1 }}>
          {aviso ? <Alert severity="warning">{aviso}</Alert> : null}
          {personas.length ? (
            <>
              <Typography variant="body2">
                {t('borrar_rol_en_uso', { n: personas.length, personas: personas.slice(0, 5).map((p) => p.nombre || p.email).join(', ') })}
              </Typography>
              <TextField select label={t('borrar_rol_pasar_a')} value={destino} onChange={(e) => setDestino(e.target.value)}>
                {posibles.map((r) => <MenuItem key={r.id} value={r.id}>{nombreDeRol(t, r.id, r.nombre, r.fijo)}</MenuItem>)}
              </TextField>
            </>
          ) : <Typography variant="body2">{t('borrar_rol_sin_uso')}</Typography>}
        </Stack>
      </DialogContent>
      <DialogActions>
        <Boton onClick={cerrar}>{t('cancelar')}</Boton>
        <Boton variant="contained" color="warning" sx={rellenoDe('warning')} disabled={!rol || (personas.length > 0 && !destino)}
          onClick={() => rol && api.borrarRol(rol.id, destino || undefined).then(hecho, (e) => setAviso(mensajeDeError(t, e)))}>
          {t('borrar_rol_boton')}
        </Boton>
      </DialogActions>
    </Dialog>
  )
}
