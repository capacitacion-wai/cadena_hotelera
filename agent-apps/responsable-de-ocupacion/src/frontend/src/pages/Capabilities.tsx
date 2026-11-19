/**
 * Administration → "Capabilities" (v7.65.0). The rigorous, real-config picture of
 * what the employee can do — by business capability, with a MODE lens
 * (Autonomous / Chat / Channels) and 4 states (acts alone / with approval / read
 * only / cannot). Facts come from the deterministic backend resolver
 * (`/api/agent/capabilities`, which reuses the SAME gate the runtime applies).
 * Progressive disclosure keeps it usable: posture + summary → cards → detail
 * → change guide (safe quick-actions + deep-links). The agent only NARRATES
 * (Explícamelo); it never decides the facts nor applies changes.
 */
import { useMemo, useState } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import {
  Accordion, AccordionDetails, AccordionSummary, Alert, Box, Button, Chip,
  CircularProgress, Collapse, Divider, FormControl, InputLabel, Link, MenuItem,
  Paper, Select, Stack, Switch, TextField, ToggleButton, ToggleButtonGroup,
  Tooltip, Typography,
} from '@mui/material'
import ExpandMoreIcon from '@mui/icons-material/ExpandMore'
import ShieldOutlinedIcon from '@mui/icons-material/ShieldOutlined'
import AutoFixHighIcon from '@mui/icons-material/AutoFixHigh'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeSanitize from 'rehype-sanitize'
import PageHeader from '../components/PageHeader'
import { apiFetch } from '../services/apiClient'
import { TriggersMasterSwitch, ConcurrencyControl, TimeLimitsControl, SelfSchedulingControl } from '../components/InitiativeControls'
import '../styles/markdown.css'

type Mode = 'autonomous' | 'chat' | 'channels'

const pick = (v: any, lang: string): string =>
  (v && typeof v === 'object') ? (v[lang] || v.es || v.en || '') : String(v ?? '')

// v7.77.0 — ONE vocabulary, everywhere. The badge, the summary strip and the
// filter ALL derive from the SAME per-mode state (`by_mode[mode].state`), so what
// you SEE and what you FILTER BY can never disagree (the old filter read the raw
// `verdict`, which is mode-agnostic → it said "con aprobación" while the row said
// "Con tu aprobación" and filtered the wrong rows). 4 buckets: can/approval/deny/na.
const BUCKETS = ['can', 'approval', 'deny', 'na'] as const
type Bucket = typeof BUCKETS[number]

// The bucket a capability falls in FOR THE CURRENT MODE — no `t`, pure.
function capBucket(cap: any, mode: Mode): Bucket {
  const st = cap?.by_mode?.[mode]?.state
  if (st === 'can') return 'can'
  if (st === 'approval' || st === 'ask') return 'approval'
  if (st === 'deny') return 'deny'
  return 'na'
}

// The ONE label + colour per bucket, mode-aware. Used by badge + summary + filter.
function bucketLabel(bucket: Bucket, mode: Mode, t: any): string {
  if (bucket === 'can') return mode === 'autonomous' ? t('capabilities.state.actsAlone', 'Actúa solo')
    : mode === 'channels' ? t('capabilities.state.responds', 'Responde') : t('capabilities.state.can', 'Puede')
  if (bucket === 'approval') return mode === 'chat' ? t('capabilities.state.asksYou', 'Te pregunta')
    : t('capabilities.state.withApproval', 'Con tu aprobación')
  if (bucket === 'deny') return t('capabilities.state.cannot', 'No puede')
  return t('capabilities.state.unavailable', 'Sin confirmar')
}
const BUCKET_COLOR: Record<Bucket, 'success' | 'warning' | 'default'> = {
  can: 'success', approval: 'warning', deny: 'default', na: 'default',
}

// Full badge for a capability row: same label/colour as the filter + an honest tip.
function badge(byMode: any, mode: Mode, t: any) {
  const bucket = capBucket({ by_mode: byMode }, mode)
  const reason = byMode?.[mode]?.reason
  let tip = ''
  if (bucket === 'can') tip = t('capabilities.state.canTip', 'Puede hacerlo sin pedirte permiso.')
  else if (bucket === 'approval') tip = (reason === 'posture_observe' || reason === 'posture_ack')
    ? t('capabilities.state.postureTip', 'El permiso lo permitiría, pero por su nivel de autonomía te lo propone. Sube la autonomía para que actúe solo.')
    : t('capabilities.state.gateTip', 'Requiere tu aprobación por el permiso o la política del conector.')
  else if (bucket === 'deny') tip = t('capabilities.state.cannotTip', 'Bloqueado por un permiso o por estar en solo-lectura.')
  else tip = t('capabilities.state.naTip', 'No se puede confirmar ahora mismo: o no hay un canal disponible, o el conector aún no está conectado/autorizado (mira el aviso del bloque). En cuanto se conecte se recalcula.')
  return { key: bucket, color: BUCKET_COLOR[bucket], label: bucketLabel(bucket, mode, t), tip }
}

export default function Capabilities() {
  const { t, i18n } = useTranslation()
  const lang = (i18n.language || 'es').startsWith('en') ? 'en' : 'es'
  const qc = useQueryClient()
  const [mode, setMode] = useState<Mode>('autonomous')
  const [filter, setFilter] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [ask, setAsk] = useState<{ open: boolean; answer: string | null }>({ open: false, answer: null })
  const [err, setErr] = useState<string | null>(null)

  const snapQ = useQuery({
    queryKey: ['capabilities'],
    queryFn: () => apiFetch('/api/agent/capabilities').then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json() }),
    staleTime: 15_000,
  })
  const snap: any = snapQ.data

  const call = async (path: string, method: string, body?: any) => {
    const r = await apiFetch(path, { method, headers: body ? { 'Content-Type': 'application/json' } : undefined, body: body ? JSON.stringify(body) : undefined })
    if (!r.ok) { const b = await r.json().catch(() => null); throw new Error(b?.detail || `HTTP ${r.status}`) }
    return r.json()
  }
  const mut = useMutation({
    mutationFn: (v: { path: string; method: string; body?: any }) => call(v.path, v.method, v.body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['capabilities'] })
      // autonomy changes shift the triggers default → refresh the moved dials.
      qc.invalidateQueries({ queryKey: ['agent-settings'] })
      qc.invalidateQueries({ queryKey: ['agent-health'] })
    },
    onError: (e: any) => setErr(String(e?.message ?? e)),
  })
  const askMut = useMutation({
    mutationFn: (question: string) => call('/api/agent/capabilities/ask', 'POST', { question }),
    onSuccess: (d: any) => setAsk({ open: true, answer: d?.answer || '' }),
    onError: (e: any) => setErr(String(e?.message ?? e)),
  })

  const q = query.trim().toLowerCase()
  const matches = (cap: any) => {
    if (!q) return true
    const hay = `${pick(cap.label, 'es')} ${pick(cap.label, 'en')} ${pick(cap.desc, 'es')} ${(cap.tools || []).join(' ')}`.toLowerCase()
    return q.split(/\s+/).every((tok) => hay.includes(tok))
  }
  // Mode-aware: filter by the SAME bucket the row shows in this mode.
  const passesFilter = (cap: any) => !filter || capBucket(cap, mode) === filter

  // Summary counts recomputed CLIENT-SIDE per mode from by_mode state (not the
  // backend's mode-agnostic `snap.summary`), so the chips match the badges + the
  // filter, and the numbers change as you switch the mode lens.
  const summary = useMemo(() => {
    const c: Record<Bucket, number> = { can: 0, approval: 0, deny: 0, na: 0 }
    for (const g of (snap?.groups || [])) for (const cap of (g.capabilities || [])) c[capBucket(cap, mode)]++
    return c
  }, [snap, mode])
  const SUMMARY_KEYS: { k: Bucket; label: string; color: any }[] = BUCKETS.map((b) => ({
    k: b, label: bucketLabel(b, mode, t), color: BUCKET_COLOR[b],
  }))

  const autonomy = snap?.autonomy
  const overBudget = snap?.guardrails?.budget?.over

  const setAutonomy = (level: string) => {
    if (!level || level === autonomy?.level) return
    setErr(null)
    mut.mutate({ path: '/api/agent/settings', method: 'PATCH', body: { autonomy_level: level } })
  }

  return (
    <Box sx={{ minWidth: 0 }}>
      <PageHeader
        title={t('capabilities.title', 'Comportamiento del empleado')}
        subtitle={t('capabilities.subtitle', 'Cómo actúa: cuánta iniciativa tiene, qué puede hacer y con qué permisos — y cómo ajustarlo.')}
      />

      {err && <Alert severity="error" sx={{ mb: 2 }} onClose={() => setErr(null)}>{err}</Alert>}
      {snapQ.isLoading && <Box sx={{ display: 'flex', justifyContent: 'center', py: 6 }}><CircularProgress /></Box>}
      {snapQ.isError && <Alert severity="error">{t('capabilities.loadError', 'No se pudo cargar la foto de capacidades.')}</Alert>}

      {snap && (
        <Stack spacing={2}>
          {/* ── Layer 0: postura + resumen + banners ───────────────── */}
          {overBudget && (
            <Alert severity="warning">{t('capabilities.pausedBudget', 'Ahora mismo el empleado está EN PAUSA por presupuesto: no ejecutará trabajo autónomo hasta que se libere el límite.')}</Alert>
          )}
          {/* v7.79.0 — master switch FIRST: it decides IF the employee works on its
              own at all; the autonomy level below decides HOW BOLDLY it acts. */}
          <TriggersMasterSwitch />
          <Paper variant="outlined" sx={{ p: 2 }}>
            <Stack direction={{ xs: 'column', md: 'row' }} spacing={2} alignItems={{ md: 'center' }}>
              <Box sx={{ flex: 1, minWidth: 0 }}>
                <Stack direction="row" spacing={1} alignItems="center">
                  <ShieldOutlinedIcon color="primary" />
                  <Typography variant="subtitle1" sx={{ fontWeight: 700 }}>
                    {t('capabilities.postureTitle', 'Postura actual')}: {pick(autonomy?.label, lang)}
                  </Typography>
                </Stack>
                <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
                  {pick(autonomy?.description, lang)}
                </Typography>
              </Box>
              <FormControl size="small" sx={{ minWidth: 240 }}>
                <InputLabel>{t('capabilities.changePosture', 'Nivel de autonomía')}</InputLabel>
                <Select label={t('capabilities.changePosture', 'Nivel de autonomía')} value={autonomy?.level || ''}
                  disabled={mut.isPending} onChange={(e) => setAutonomy(String(e.target.value))}>
                  {(autonomy?.levels || []).map((l: any) => (
                    <MenuItem key={l.key} value={l.key}>{pick(l.label, lang)}</MenuItem>
                  ))}
                </Select>
              </FormControl>
            </Stack>
            {/* v7.77.0 — "what it does at each level": the 3 levels, current highlighted,
                so the operator understands what changing the dial implies. */}
            <Box sx={{ mt: 2 }}>
              <Typography variant="caption" sx={{ fontWeight: 700, display: 'block', mb: 0.75 }}>
                {t('capabilities.levelsTitle', 'Qué hace en cada nivel')}
              </Typography>
              <Stack spacing={0.75}>
                {(autonomy?.levels || []).map((l: any) => {
                  const isCur = l.key === autonomy?.level
                  return (
                    <Box key={l.key} sx={{
                      display: 'flex', gap: 1, alignItems: 'flex-start', p: 1, borderRadius: 1,
                      border: '1px solid', borderColor: isCur ? 'primary.main' : 'divider',
                      bgcolor: isCur ? 'action.selected' : 'transparent',
                    }}>
                      <Chip size="small" color={isCur ? 'primary' : 'default'} variant={isCur ? 'filled' : 'outlined'}
                        label={pick(l.label, lang)} sx={{ flexShrink: 0, fontWeight: 600 }} />
                      <Typography variant="caption" color="text.secondary" sx={{ mt: 0.25 }}>
                        {pick(l.description, lang)}
                      </Typography>
                    </Box>
                  )
                })}
              </Stack>
              <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1 }}>
                {t('capabilities.levelVsSwitch', 'El interruptor de arriba decide SI trabaja por su cuenta; este nivel decide CUÁNTA iniciativa tiene. En pausa no hará trabajo autónomo aunque el nivel sea alto (el chat y los canales siguen respondiendo).')}
              </Typography>
            </Box>
          </Paper>

          {/* v7.78.0 — INITIATIVE dials centralised here (moved from Operaciones):
              concurrency and self-scheduling (the master switch is up top). */}
          <Box>
            <ConcurrencyControl />
            <TimeLimitsControl />
            <SelfSchedulingControl />
          </Box>

          <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap alignItems="center">
            {SUMMARY_KEYS.map((s) => (
              <Chip key={s.k} color={s.color} variant={filter === s.k ? 'filled' : 'outlined'}
                onClick={() => setFilter(filter === s.k ? null : s.k)}
                label={`${s.label}: ${summary[s.k] ?? 0}`} sx={{ cursor: 'pointer' }} />
            ))}
            {filter && <Button size="small" onClick={() => setFilter(null)}>{t('capabilities.clearFilter', 'Quitar filtro')}</Button>}
          </Stack>

          {/* ── Mode lens + search + ask ────────────────────────────── */}
          <Stack direction={{ xs: 'column', md: 'row' }} spacing={1.5} alignItems={{ md: 'center' }}>
            <ToggleButtonGroup size="small" exclusive value={mode} onChange={(_, v) => v && setMode(v)}>
              <ToggleButton value="autonomous">{t('capabilities.mode.autonomous', 'Autónomo')}</ToggleButton>
              <ToggleButton value="chat">{t('capabilities.mode.chat', 'Chat')}</ToggleButton>
              <ToggleButton value="channels">{t('capabilities.mode.channels', 'Canales')}</ToggleButton>
            </ToggleButtonGroup>
            <TextField size="small" sx={{ flex: 1, minWidth: 220 }} value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={t('capabilities.searchPh', '¿Puede el empleado…? (p. ej. "enviar email", "borrar en kommo")')} />
            <Button size="small" variant="outlined" startIcon={<AutoFixHighIcon />}
              disabled={askMut.isPending || !query.trim()} onClick={() => askMut.mutate(query)}>
              {t('capabilities.explain', 'Explícamelo')}
            </Button>
          </Stack>
          <Typography variant="caption" color="text.secondary">
            {t('capabilities.modeHint', 'Lente por modo: el mismo permiso se ve distinto según cómo trabaje. En "Autónomo" tiene en cuenta su postura; en "Chat" tú estás presente para decidir en el momento.')}
          </Typography>

          <Collapse in={ask.open} unmountOnExit>
            <Alert severity="info" icon={<AutoFixHighIcon />} onClose={() => setAsk({ open: false, answer: null })}>
              {askMut.isPending ? t('common.loading', 'Cargando…') : (
                <Box className="markdown-body" sx={{ fontSize: '0.9rem', wordBreak: 'break-word' }}>
                  <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeSanitize]}>{ask.answer || ''}</ReactMarkdown>
                </Box>
              )}
            </Alert>
          </Collapse>

          {/* ── Layers 1-3: groups ──────────────────────────────────── */}
          {(snap.groups || []).map((g: any) => (
            <GroupCard key={g.key} g={g} mode={mode} lang={lang} t={t}
              matches={matches} passesFilter={passesFilter} />
          ))}

          {/* v7.77.0 — governance NOTE (not cards): the internal mechanisms that
              used to clutter the list, explained plainly. */}
          <Typography variant="caption" color="text.secondary" sx={{ px: 0.5 }}>
            {t('capabilities.govNote', 'Además, para trabajar con seguridad el empleado te pide aprobación antes de lo arriesgado, silencia señales que ya gestionaste para no repetirse, y guarda datos en su propio almacén interno. No son permisos que concedas: se controlan con la autonomía y los permisos de arriba.')}
          </Typography>

          {/* Automations + guardrails */}
          <AutomationsPanel a={snap.automations} lang={lang} t={t}
            onToggle={(id: number, enabled: boolean) => mut.mutate({ path: `/api/agent/automations/${id}`, method: 'PATCH', body: { enabled } })}
            busy={mut.isPending} />
          <GuardrailsPanel g={snap.guardrails} ch={snap.channels} t={t} />
          <Typography variant="caption" color="text.disabled">
            {t('capabilities.generatedAt', 'Foto tomada')}: {snap.generated_at ? new Date(snap.generated_at).toLocaleString() : '—'}
          </Typography>
        </Stack>
      )}
    </Box>
  )
}

function GroupCard({ g, mode, lang, t, matches, passesFilter }: any) {
  const caps = (g.capabilities || []).filter((c: any) => matches(c) && passesFilter(c))
  if (!caps.length) return null
  const isConnector = g.kind === 'connector'
  // v7.73.0 — catalog readability: 'enumerated' (real tool list) · 'dynamic'
  // (Composio router: catalog on demand, connected, NOT a failure) · 'unavailable'
  // (raw + empty: a genuine, usually transient, read failure). Older snapshots
  // without `catalog` fall back to the enumerated boolean.
  const catalog: 'enumerated' | 'dynamic' | 'unavailable' =
    g.catalog || (g.enumerated ? 'enumerated' : 'unavailable')
  return (
    <Accordion defaultExpanded disableGutters>
      <AccordionSummary expandIcon={<ExpandMoreIcon />}>
        <Stack direction="row" spacing={1} alignItems="center" sx={{ flex: 1, minWidth: 0 }}>
          <Typography sx={{ fontWeight: 700 }}>{pick(g.label, lang)}</Typography>
          {isConnector && (
            <Chip size="small" variant="outlined"
              color={catalog === 'unavailable' ? 'warning' : catalog === 'dynamic' ? 'info' : 'default'}
              label={catalog === 'enumerated' ? t('capabilities.connectorOk', 'conectado')
                : catalog === 'dynamic' ? t('capabilities.connectorDynamic', 'conectado · modo dinámico')
                : t('capabilities.connectorNotReady', 'sin conectar / no verificado')} />
          )}
        </Stack>
      </AccordionSummary>
      <AccordionDetails>
        <Stack spacing={1}>
          {isConnector && catalog === 'dynamic' && (
            <Alert severity="info" sx={{ py: 0.5 }}>
              {t('capabilities.connectorDynamicMsg', '{{name}} está conectado y operativo en modo dinámico: usa su catálogo de acciones bajo demanda, por lo que aquí no se muestra una lista fija. Los permisos de lectura y escritura de abajo se aplican igual.', { name: pick(g.label, lang) })}
            </Alert>
          )}
          {isConnector && catalog === 'unavailable' && (
            <Alert severity="warning" sx={{ py: 0.5 }}>
              {t('capabilities.connectorNotReadyMsg', '{{name}} no está conectado o autorizado ahora mismo (o no se ha podido leer su catálogo de acciones). Hasta que se conecte, no se puede confirmar qué puede leer o hacer. Conéctalo/autorízalo en Operaciones → Conectores.', { name: pick(g.label, lang) })}
            </Alert>
          )}
          {caps.map((c: any) => <CapabilityRow key={c.key} c={c} mode={mode} lang={lang} t={t} />)}
          {isConnector && (
            <Box sx={{ mt: 1, p: 1.25, bgcolor: 'action.hover', borderRadius: 1 }}>
              <Typography variant="caption" color="text.secondary" sx={{ fontWeight: 700, display: 'block', mb: 0.75 }}>
                {t('capabilities.howToChange', 'Cómo darle más o menos margen')}
              </Typography>
              {/* 14.8.0 — the permissions are edited in ONE place (Operations → Conectores),
                  against the platform's table: here they are only read. */}
              <Stack direction="row" spacing={1.5} alignItems="center" flexWrap="wrap" useFlexGap>
                <Chip size="small" variant="outlined"
                  label={`${t('capabilities.writeIn', 'Escritura en {{name}}', { name: pick(g.label, lang) })}: ${
                    g.write_policy === 'read_only' ? t('capabilities.polReadOnly', 'Solo lectura')
                    : g.write_policy === 'write_with_approval' ? t('capabilities.polApproval', 'Con tu aprobación')
                    : t('capabilities.polWrite', 'Libre (crea/modifica/envía)')}`} />
                <Link component={RouterLink} to="/operations" variant="body2">
                  {t('capabilities.openConnectors', 'Permisos por acción y conexión →')}
                </Link>
              </Stack>
            </Box>
          )}
        </Stack>
      </AccordionDetails>
    </Accordion>
  )
}

function CapabilityRow({ c, mode, lang, t }: any) {
  const [open, setOpen] = useState(false)
  const b = badge(c.by_mode, mode, t)
  return (
    <Box sx={{ border: '1px solid', borderColor: 'divider', borderRadius: 1.5, p: 1.25 }}>
      <Stack direction="row" spacing={1} alignItems="center">
        <Box sx={{ flex: 1, minWidth: 0 }}>
          <Typography variant="body2" sx={{ fontWeight: 600 }}>{pick(c.label, lang)}</Typography>
          <Typography variant="caption" color="text.secondary">{pick(c.desc, lang)}</Typography>
        </Box>
        <Tooltip title={b.tip}><Chip size="small" color={b.color} label={b.label} /></Tooltip>
        <Link component="button" type="button" variant="caption" underline="hover" onClick={() => setOpen((v) => !v)}>
          {t('capabilities.detail', 'detalle')}
        </Link>
      </Stack>
      <Collapse in={open} unmountOnExit>
        <Divider sx={{ my: 1 }} />
        <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap sx={{ mb: 0.5 }}>
          {(['autonomous', 'chat', 'channels'] as Mode[]).map((mm) => {
            const bb = badge(c.by_mode, mm, t)
            return <Chip key={mm} size="small" variant="outlined" color={bb.color}
              label={`${t(`capabilities.mode.${mm}`, mm)}: ${bb.label}`} />
          })}
        </Stack>
        {c.tools?.length > 0 && (
          <Typography variant="caption" color="text.disabled">
            {t('capabilities.toolsLabel', 'Herramientas')}: {c.tools.join(', ')}
          </Typography>
        )}
        {c.name && (
          <Typography variant="caption" color="text.disabled" sx={{ display: 'block' }}>
            {c.name} · {c.write ? t('capabilities.write', 'escritura') : t('capabilities.read', 'lectura')} · {c.verdict}
          </Typography>
        )}
      </Collapse>
    </Box>
  )
}

function AutomationsPanel({ a, lang, t, onToggle, busy }: any) {
  if (!a) return null
  return (
    <Accordion disableGutters>
      <AccordionSummary expandIcon={<ExpandMoreIcon />}>
        <Typography sx={{ fontWeight: 700 }}>
          {t('capabilities.autoTitle', 'Trabajos autónomos')} · {t('capabilities.autoSummary', '{{enabled}} activos, {{alone}} actúan solos, {{appr}} con aprobación', { enabled: a.enabled, alone: a.act_alone, appr: a.requires_approval })}
        </Typography>
      </AccordionSummary>
      <AccordionDetails>
        <Stack spacing={0.75}>
          {(a.preview || []).map((it: any) => (
            <Stack key={it.id} direction="row" spacing={1} alignItems="center">
              <Switch size="small" checked={!!it.enabled} disabled={busy}
                onChange={(e) => onToggle(it.id, e.target.checked)} />
              <Typography variant="body2" sx={{ flex: 1, minWidth: 0 }}>{it.title}</Typography>
              {/* v7.79.0 — DISABLED wins: an inactive automation reads "Inactiva"
                  even if it would require approval when active (it isn't running). */}
              <Chip size="small" variant="outlined"
                color={!it.enabled ? 'default' : (it.requires_approval ? 'warning' : 'success')}
                label={!it.enabled ? t('capabilities.autoOff', 'Inactiva')
                  : (it.requires_approval ? t('capabilities.autoNeedsAppr', 'Con tu aprobación')
                    : t('capabilities.autoAlone', 'Actúa solo'))} />
            </Stack>
          ))}
          <Link component={RouterLink} to="/trabajo-autonomo" variant="body2" sx={{ mt: 0.5 }}>
            {t('capabilities.openAutomations', 'Gestionar trabajos autónomos →')}
          </Link>
        </Stack>
      </AccordionDetails>
    </Accordion>
  )
}

function GuardrailsPanel({ g, ch, t }: any) {
  if (!g) return null
  return (
    <Accordion disableGutters>
      <AccordionSummary expandIcon={<ExpandMoreIcon />}>
        <Typography sx={{ fontWeight: 700 }}>{t('capabilities.guardrails', 'Límites y salvaguardas')}</Typography>
      </AccordionSummary>
      <AccordionDetails>
        <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
          <Chip variant="outlined" color={g.budget?.over ? 'error' : 'default'}
            label={g.budget?.over ? t('capabilities.budgetOver', 'Presupuesto agotado (en pausa)') : t('capabilities.budgetOk', 'Presupuesto disponible')} />
          {g.limits && <Chip variant="outlined" label={t('capabilities.limits', 'Concurrencia: {{c}} (autónomos {{a}})', { c: g.limits.max_concurrent, a: g.limits.max_autonomous })} />}
          <Chip variant="outlined" label={t('capabilities.pending', 'Aprobaciones pendientes: {{n}}', { n: g.pending_approvals ?? 0 })} />
          <Chip variant="outlined" label={t('capabilities.mutes', 'Silencios activos: {{n}}', { n: g.suppressions ?? 0 })} />
          <Chip variant="outlined" color={ch?.can_communicate ? 'default' : 'warning'}
            label={ch?.can_communicate ? t('capabilities.channelsOn', 'Canales: {{k}}', { k: (ch.active || []).join(', ') }) : t('capabilities.channelsOff', 'Sin canal de comunicación')} />
        </Stack>
      </AccordionDetails>
    </Accordion>
  )
}
