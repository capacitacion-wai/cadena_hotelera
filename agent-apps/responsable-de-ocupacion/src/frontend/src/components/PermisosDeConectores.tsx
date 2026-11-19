/**
 * Operations → Conectores: the EMPLOYEE's connector permissions (14.8.0).
 *
 * Contract: docs/permisos-de-conectores.md. The employee inherits its agent's permissions
 * or personalises them with the SAME shape the agent uses —a default authority and, per
 * connector, a mode (inherit · write · with approval · read-only · custom with per-tool
 * rules, labelled with the platform panel's words)— and can NEVER have more than the agent.
 *
 * THIS SCREEN CALCULATES NOTHING. It paints the platform's table (what the employee
 * applies), and while editing it paints the table of the platform's ensayo (dry-run) for
 * the draft. Saving goes through the platform's validation; a rejection comes back with a
 * code (`EMPLEADO_ABRE_MAS`…) that is translated here. The only thing the screen does on
 * its own is grey out the options above the agent's ceiling —which the table gives— so
 * nobody has to be told off for trying.
 */
import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert, Box, Button, Chip, CircularProgress, Collapse, MenuItem, Paper, Select, Stack,
  Table, TableBody, TableCell, TableHead, TableRow, TextField, ToggleButton,
  ToggleButtonGroup, Tooltip, Typography,
} from '@mui/material'
import {
  mcpConnectorsApi,
  type ConnectorMode,
  type EmployeeAuthority,
  type EmployeePolicy,
  type PermissionConnector,
  type PermissionError,
  type PermissionToolRow,
  type PermissionsTable,
  type ToolPermission,
  type WritePolicy,
} from '../services/mcpConnectorsApi'
import { apiFetch } from '../services/apiClient'

const AUTHORITIES: WritePolicy[] = ['write', 'write_with_approval', 'read_only']
const MODES: ConnectorMode[] = ['inherit', 'write', 'write_with_approval', 'read_only', 'custom']
const PERMS: ToolPermission[] = ['allow', 'ask', 'deny']
// Only to grey out what is above the agent's ceiling (the table gives the ceiling).
const AUTH_RANK: Record<WritePolicy, number> = { write: 0, write_with_approval: 1, read_only: 2 }
const PERM_RANK: Record<ToolPermission, number> = { allow: 0, ask: 1, deny: 2 }
const VACIA: EmployeePolicy = { authority: 'inherit', connectors: {} }

const PERM_COLOR: Record<ToolPermission, 'success' | 'info' | 'default'> = {
  allow: 'success', ask: 'info', deny: 'default',
}

function codigoDe(err: unknown): { code: string; message: string } {
  const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  if (detail && typeof detail === 'object' && 'code' in (detail as object)) {
    const d = detail as { code: string; message?: string }
    return { code: d.code, message: d.message || '' }
  }
  return { code: '', message: typeof detail === 'string' ? detail : String((err as Error)?.message || '') }
}

function esPersonalizada(p: EmployeePolicy | undefined): boolean {
  return !!p && (p.authority !== 'inherit' || Object.keys(p.connectors || {}).length > 0)
}

function entradaDe(p: EmployeePolicy, c: PermissionConnector) {
  const claves = [c.name, c.runtime_name].map((x) => (x || '').toLowerCase())
  const k = Object.keys(p.connectors).find((x) => claves.includes(x.toLowerCase()))
  return k ? { clave: k, entrada: p.connectors[k] } : { clave: c.name, entrada: undefined }
}

export default function PermisosDeConectores() {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const me = useQuery({
    queryKey: ['users-me'],
    queryFn: () => apiFetch('/api/agent/users/me').then((r) => (r.ok ? r.json() : { is_admin: false })),
    staleTime: 60_000,
  })
  const isAdmin = me.data?.is_admin === true
  const q = useQuery({
    queryKey: ['employee-permissions'],
    queryFn: () => mcpConnectorsApi.getPermissions(),
    staleTime: 15_000,
  })

  // The draft (null = not editing) and its debounced copy, which is what goes to the ensayo.
  const [draft, setDraft] = useState<EmployeePolicy | null>(null)
  const [abierta, setAbierta] = useState(false)
  const [ensayado, setEnsayado] = useState<EmployeePolicy | null>(null)
  useEffect(() => {
    const id = window.setTimeout(() => setEnsayado(draft), 350)
    return () => window.clearTimeout(id)
  }, [draft])
  const dry = useQuery({
    queryKey: ['employee-permissions-dry-run', JSON.stringify(ensayado)],
    queryFn: () => mcpConnectorsApi.dryRunPermissions(ensayado as EmployeePolicy),
    enabled: ensayado !== null,
    staleTime: Infinity,
  })
  const [saveError, setSaveError] = useState<{ code: string; message: string } | null>(null)
  const [saved, setSaved] = useState(false)
  const save = useMutation({
    mutationFn: (p: EmployeePolicy) => mcpConnectorsApi.savePermissions(p),
    onSuccess: () => {
      setDraft(null)
      setEnsayado(null)
      setSaveError(null)
      setSaved(true)
      window.setTimeout(() => setSaved(false), 4000)
      qc.invalidateQueries({ queryKey: ['employee-permissions'] })
      qc.invalidateQueries({ queryKey: ['mcp-connectors'] })
      qc.invalidateQueries({ queryKey: ['capabilities'] })
    },
    onError: (e) => setSaveError(codigoDe(e)),
  })

  const data = q.data
  const policy: EmployeePolicy = draft ?? data?.policy ?? VACIA
  const editing = draft !== null
  const table: PermissionsTable | null | undefined =
    editing ? (dry.data?.table ?? data?.table) : data?.table
  const errors: PermissionError[] = editing && dry.data && dry.data.table ? dry.data.errors : []
  const personalizada = abierta || esPersonalizada(policy)

  const textoError = (code: string, message: string) =>
    t(`permisos.errores.${code}`, { defaultValue: message || code })

  const cambiar = (fn: (p: EmployeePolicy) => EmployeePolicy) => {
    setSaveError(null)
    setDraft(fn(JSON.parse(JSON.stringify(policy)) as EmployeePolicy))
  }

  if (q.isLoading) {
    return <Box sx={{ display: 'flex', justifyContent: 'center', py: 2 }}><CircularProgress size={22} /></Box>
  }
  if (q.isError) {
    return <Alert severity="error">{textoError(codigoDe(q.error).code || 'PLATAFORMA_NO_DISPONIBLE', '')}</Alert>
  }
  if (!table) {
    return <Alert severity="warning">{t('permisos.sinTabla')}</Alert>
  }

  const agente = table.agent_authority
  const noProbado = t('permisos.noProbado')
  // A disabled option says WHY right there: a disabled menu item fires no events, so a
  // tooltip on it would never show.
  const opcion = (valor: string, etiqueta: string, bloqueada: boolean) => (
    <MenuItem key={valor} value={valor} disabled={bloqueada}>
      {etiqueta}
      {bloqueada && <Typography component="span" variant="caption" sx={{ ml: 1 }}>· {noProbado}</Typography>}
    </MenuItem>
  )
  const erroresGenerales = errors.filter((e) => !e.connector)

  return (
    <Paper variant="outlined" sx={{ p: 1.5 }}>
      <Typography variant="body2" sx={{ fontWeight: 700, mb: 0.5 }}>{t('permisos.titulo')}</Typography>
      <Typography variant="caption" color="text.secondary" display="block" sx={{ mb: 1 }}>
        {t('permisos.intro')}
      </Typography>
      {data && !data.loaded && <Alert severity="warning" sx={{ mb: 1 }}>{t('permisos.tablaVieja')}</Alert>}
      {data?.converted && !editing && <Alert severity="info" sx={{ mb: 1 }}>{t('permisos.convertida')}</Alert>}
      {!isAdmin && <Typography variant="caption" color="text.secondary" display="block" sx={{ mb: 1 }}>{t('permisos.soloAdmin')}</Typography>}

      <Stack direction="row" spacing={1.5} alignItems="center" sx={{ flexWrap: 'wrap', mb: 1 }} useFlexGap>
        <ToggleButtonGroup size="small" exclusive value={personalizada ? 'custom' : 'inherit'} disabled={!isAdmin}
          onChange={(_e, v: string | null) => {
            if (!v) return
            if (v === 'inherit') { setAbierta(false); cambiar(() => ({ ...VACIA, connectors: {} })) }
            else setAbierta(true)
          }}>
          <ToggleButton value="inherit" sx={{ textTransform: 'none', px: 1.25 }}>{t('permisos.hereda')}</ToggleButton>
          <ToggleButton value="custom" sx={{ textTransform: 'none', px: 1.25 }}>{t('permisos.personalizar')}</ToggleButton>
        </ToggleButtonGroup>
        <Typography variant="caption" color="text.secondary">
          {personalizada ? t('permisos.personalizarAyuda') : t('permisos.heredaAyuda')}
        </Typography>
      </Stack>

      {personalizada && (
        <Stack direction="row" spacing={1.5} alignItems="center" sx={{ flexWrap: 'wrap', mb: 1.5 }} useFlexGap>
          <Typography variant="caption" sx={{ minWidth: 96, fontWeight: 600 }}>{t('permisos.potestad')}</Typography>
          <Select size="small" value={policy.authority} disabled={!isAdmin} sx={{ minWidth: 220 }}
            onChange={(e) => cambiar((p) => ({ ...p, authority: e.target.value as EmployeeAuthority }))}>
            {opcion('inherit', t('permisos.potestades.inherit'), false)}
            {AUTHORITIES.map((a) => opcion(a, t(`permisos.potestades.${a}`), AUTH_RANK[a] < AUTH_RANK[agente]))}
          </Select>
          <Typography variant="caption" color="text.secondary">
            {t('permisos.delAgente', { valor: t(`permisos.potestades.${agente}`) })}
          </Typography>
        </Stack>
      )}

      {erroresGenerales.map((e, i) => (
        <Alert key={i} severity="error" sx={{ mb: 1 }}>{textoError(e.code, e.message)}</Alert>
      ))}

      {table.connectors.length === 0 ? (
        <Alert severity="info">{t('permisos.vacio')}</Alert>
      ) : (
        <Stack spacing={1}>
          {table.connectors.map((c) => (
            <ConectorFila key={c.name} c={c} policy={policy} personalizada={personalizada}
              editable={isAdmin} errores={errors.filter((e) =>
                [c.name, c.runtime_name].map((x) => (x || '').toLowerCase())
                  .includes((e.connector || '').toLowerCase()))}
              textoError={textoError} opcion={opcion} onChange={cambiar} />
          ))}
        </Stack>
      )}

      {saveError && (
        <Alert severity="error" sx={{ mt: 1 }} onClose={() => setSaveError(null)}>
          {textoError(saveError.code, saveError.message)}
        </Alert>
      )}
      {saved && <Alert severity="success" sx={{ mt: 1 }}>{t('permisos.guardado')}</Alert>}
      {editing && (
        <Stack direction="row" spacing={1} alignItems="center" sx={{ mt: 1.5 }}>
          <Button size="small" variant="contained" disabled={save.isPending || !isAdmin}
            onClick={() => save.mutate(policy)}>{t('permisos.guardar')}</Button>
          <Button size="small" disabled={save.isPending}
            onClick={() => { setDraft(null); setEnsayado(null); setSaveError(null); setAbierta(false) }}>
            {t('permisos.descartar')}
          </Button>
          {(dry.isFetching || draft !== ensayado) && (
            <Typography variant="caption" color="text.secondary">{t('permisos.ensayando')}</Typography>
          )}
        </Stack>
      )}
    </Paper>
  )
}

function ConectorFila({ c, policy, personalizada, editable, errores, textoError, opcion, onChange }: {
  c: PermissionConnector
  policy: EmployeePolicy
  personalizada: boolean
  editable: boolean
  errores: PermissionError[]
  textoError: (code: string, message: string) => string
  opcion: (valor: string, etiqueta: string, bloqueada: boolean) => JSX.Element
  onChange: (fn: (p: EmployeePolicy) => EmployeePolicy) => void
}) {
  const { t } = useTranslation()
  const { clave, entrada } = entradaDe(policy, c)
  const modo: ConnectorMode = entrada?.mode ?? 'inherit'
  const [verHerramientas, setVerHerramientas] = useState(false)
  const mostrar = modo === 'custom' || verHerramientas
  const modoEnAgente = c.mode === 'custom'
    ? t('permisos.modos.custom')
    : t(`permisos.potestades.${c.authority}`)

  const ponerModo = (m: ConnectorMode) => onChange((p) => {
    const conectores = { ...p.connectors }
    delete conectores[clave]
    if (m === 'custom') conectores[clave] = { mode: 'custom', tools: entrada?.tools ?? {}, reasons: entrada?.reasons ?? {} }
    else if (m !== 'inherit') conectores[clave] = { mode: m }
    return { ...p, connectors: conectores }
  })
  const ponerRegla = (tool: string, valor: ToolPermission | '') => onChange((p) => {
    const e = p.connectors[clave] ?? { mode: 'custom' as ConnectorMode }
    const tools = { ...(e.tools ?? {}) }
    const reasons = { ...(e.reasons ?? {}) }
    if (valor) tools[tool] = valor
    else { delete tools[tool]; delete reasons[tool] }
    return { ...p, connectors: { ...p.connectors, [clave]: { mode: 'custom', tools, reasons } } }
  })
  const ponerMotivo = (tool: string, texto: string) => onChange((p) => {
    const e = p.connectors[clave] ?? { mode: 'custom' as ConnectorMode }
    return { ...p, connectors: { ...p.connectors, [clave]: { ...e, reasons: { ...(e.reasons ?? {}), [tool]: texto } } } }
  })

  return (
    <Box sx={{ p: 1.25, bgcolor: 'action.hover', borderRadius: 1 }}>
      <Stack direction="row" spacing={1.5} alignItems="center" sx={{ flexWrap: 'wrap' }} useFlexGap>
        <Typography variant="body2" sx={{ fontWeight: 600, minWidth: 140, flex: 1 }}>{c.display_name || c.name}</Typography>
        <Typography variant="caption" color="text.secondary">
          {t('permisos.delAgente', { valor: modoEnAgente })}
        </Typography>
        {personalizada && (
          <Select size="small" value={modo} disabled={!editable} sx={{ minWidth: 200 }}
            onChange={(e) => ponerModo(e.target.value as ConnectorMode)}>
            {MODES.map((m) => opcion(m, t(`permisos.modos.${m}`),
              (AUTHORITIES as string[]).includes(m) && AUTH_RANK[m as WritePolicy] < AUTH_RANK[c.authority]))}
          </Select>
        )}
        {modo !== 'custom' && (
          <Button size="small" sx={{ textTransform: 'none' }} onClick={() => setVerHerramientas((v) => !v)}>
            {verHerramientas ? t('permisos.ocultar') : t('permisos.verHerramientas', { count: c.tools.length })}
          </Button>
        )}
      </Stack>
      {errores.map((e, i) => (
        <Alert key={i} severity="error" sx={{ mt: 1 }}>
          {e.tool ? `${e.tool}: ` : ''}{textoError(e.code, e.message)}
        </Alert>
      ))}
      <Collapse in={mostrar} unmountOnExit>
        <TablaDeHerramientas c={c} editable={editable && modo === 'custom'}
          reglas={entrada?.tools ?? {}} motivos={entrada?.reasons ?? {}}
          onRegla={ponerRegla} onMotivo={ponerMotivo} />
      </Collapse>
    </Box>
  )
}

function TablaDeHerramientas({ c, editable, reglas, motivos, onRegla, onMotivo }: {
  c: PermissionConnector
  editable: boolean
  reglas: Record<string, ToolPermission>
  motivos: Record<string, string>
  onRegla: (tool: string, valor: ToolPermission | '') => void
  onMotivo: (tool: string, texto: string) => void
}) {
  const { t } = useTranslation()
  const noProbado = t('permisos.noProbado')
  const filas = useMemo(() => [...c.tools].sort((a, b) =>
    Number(a.io_kind === 'read') - Number(b.io_kind === 'read') || a.name.localeCompare(b.name)), [c.tools])
  if (filas.length === 0) {
    return <Typography variant="caption" color="text.secondary" display="block" sx={{ mt: 1 }}>{t('permisos.sinHerramientas')}</Typography>
  }
  const tipo = (r: PermissionToolRow) => r.unclassified
    ? <Tooltip title={t('permisos.io.unclassified')}><Chip size="small" color="warning" variant="outlined" label={t('permisos.io.sinClasificar')} /></Tooltip>
    : <Chip size="small" variant="outlined" label={t(`permisos.io.${r.io_kind === 'read' ? 'read' : 'write'}`)} />
  return (
    <Box sx={{ mt: 1, overflowX: 'auto' }}>
      <Table size="small">
        <TableHead>
          <TableRow>
            <TableCell>{t('permisos.columnas.herramienta')}</TableCell>
            <TableCell>{t('permisos.columnas.tipo')}</TableCell>
            <TableCell>{t('permisos.columnas.agente')}</TableCell>
            {editable && <TableCell>{t('permisos.columnas.regla')}</TableCell>}
            <TableCell>{t('permisos.columnas.efectivo')}</TableCell>
          </TableRow>
        </TableHead>
        <TableBody>
          {filas.map((r) => {
            const regla = reglas[r.name] ?? ''
            return (
              <TableRow key={r.name}>
                <TableCell sx={{ fontFamily: 'monospace', fontSize: '0.8rem' }}>
                  {r.name}
                  {r.reason && <Typography variant="caption" color="text.secondary" display="block">{r.reason}</Typography>}
                </TableCell>
                <TableCell>{tipo(r)}</TableCell>
                <TableCell>
                  <Chip size="small" variant="outlined" color={PERM_COLOR[r.agent_permission]}
                    label={t(`permisos.efectivo.${r.agent_permission}`)} />
                  {r.opens_more_than_agent && (
                    <Typography variant="caption" color="warning.main" display="block">{t('permisos.abreMas')}</Typography>
                  )}
                </TableCell>
                {editable && (
                  <TableCell sx={{ minWidth: 220 }}>
                    <Select size="small" value={regla} displayEmpty sx={{ minWidth: 180 }}
                      onChange={(e) => onRegla(r.name, e.target.value as ToolPermission | '')}>
                      <MenuItem value="">{t('permisos.sinRegla')}</MenuItem>
                      {PERMS.map((p) => {
                        const bloqueada = PERM_RANK[p] < PERM_RANK[r.agent_permission]
                        return (
                          <MenuItem key={p} value={p} disabled={bloqueada}>
                            {t(`permisos.reglas.${p}`)}
                            {bloqueada && <Typography component="span" variant="caption" sx={{ ml: 1 }}>· {noProbado}</Typography>}
                          </MenuItem>
                        )
                      })}
                    </Select>
                    {regla && (
                      <TextField size="small" fullWidth sx={{ mt: 0.5 }} value={motivos[r.name] ?? ''}
                        placeholder={t('permisos.motivoEj')} label={t('permisos.motivo')}
                        onChange={(e) => onMotivo(r.name, e.target.value)} />
                    )}
                  </TableCell>
                )}
                <TableCell>
                  <Chip size="small" color={PERM_COLOR[r.permission]} label={t(`permisos.efectivo.${r.permission}`)} />
                  <Typography variant="caption" color="text.secondary" display="block">
                    {t(`permisos.origen.${r.source}`)}
                  </Typography>
                </TableCell>
              </TableRow>
            )
          })}
        </TableBody>
      </Table>
    </Box>
  )
}
