import { useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { healthNoticeText } from '../components/healthNotices'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link as RouterLink, useNavigate } from 'react-router-dom'
import {
  Alert, AlertTitle, alpha, Autocomplete, Badge, Box, Button, Card, CardContent, Chip, CircularProgress,
  Dialog, DialogActions,
  DialogContent, DialogTitle, Divider, Grid, IconButton, InputAdornment, Link, List,
  FormControlLabel,
  ListItem, ListItemButton, ListItemIcon, ListItemText, Menu, MenuItem, Paper, Skeleton, Stack, Switch, Tab, Tabs,
  Table, TableBody, TableCell, TableContainer, TableHead, TablePagination, TableRow, TextField,
  ToggleButton, ToggleButtonGroup, Tooltip, Typography,
} from '@mui/material'
import {
  Bolt as TriggerIcon, PlayCircleOutline as ActionIcon,
  Memory as CapabilityIcon, MonitorHeart as StatusIcon,
  Login as ReadIcon, Logout as WriteIcon, OpenInNew as LinkIcon,
  Schedule as ScheduleIcon, Edit as EditIcon, History as HistoryIcon,
  CheckCircle as OkIcon, ErrorOutline as ErrorOutlineIcon,
  Pending as PendingIcon, PlayArrow as RunningIcon,
  PauseCircleOutline as PausedIcon, HourglassEmpty as WaitingIcon,
  AssignmentOutlined as EncargoIcon,
  Speed as SpeedIcon, Hub as ConnectorIcon, VpnKey as AuthIcon,
  SwapHoriz as SwapAccountIcon,
  Groups as SessionsIcon, PersonOutline as UserIcon,
  ForumOutlined as ChannelSessionIcon, RestartAlt as ResetIcon,
  FolderOpen as WorkspaceIcon, Folder as FolderIcon,
  InsertDriveFileOutlined as FileIcon, UploadFile as UploadIcon,
  Download as DownloadIcon, DeleteOutline as DeleteIcon,
  CreateNewFolder as NewFolderIcon, ArrowUpward as UpIcon,
  Refresh as RefreshIcon, Visibility as PreviewIcon,
  FolderZip as ZipIcon, DriveFileMove as MoveIcon, Home as HomeIcon,
  Psychology as MemoryTabIcon, PushPin as PinIcon,
  AutoAwesome as LearningTabIcon,
  ContentCopy as CopyIcon,
  PushPinOutlined as PinOutlineIcon, Add as AddIcon,
  Search as SearchIcon, LightbulbOutlined as FactIcon,
  TuneOutlined as PreferenceIcon, AccountTreeOutlined as ProcedureIcon,
  HistoryToggleOff as EpisodeIcon, Storage as DatabaseTabIcon,
  RuleOutlined as AutomationIcon, OpenInFull as ExpandFieldIcon,
  FormatBold as BoldIcon, FormatItalic as ItalicIcon, Title as HeadingIcon,
  FormatListBulleted as BulletListIcon, FormatListNumbered as NumberListIcon,
  Link as MdLinkIcon, Code as CodeIcon,
  Fullscreen as FullscreenIcon, FullscreenExit as FullscreenExitIcon,
  InfoOutlined as InfoIcon,
  MoreVert as MoreVertIcon, ArrowDropDown as ArrowDropDownIcon,
  ViewAgenda as CardsViewIcon, TableRows as TableViewIcon,
} from '@mui/icons-material'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkBreaks from 'remark-breaks'
import '../styles/markdown.css'
import PageHeader from '../components/PageHeader'
import UserPicker from '../components/UserPicker'
import StatusChip from '../components/StatusChip'
import CronBuilder, { parseCron, describeCron } from '../components/CronBuilder'
import WorkspaceViewer, { StorageUsageCard, fmtBytes } from '../components/WorkspaceViewer'
import MemoryViewer from '../components/MemoryViewer'
import LearningViewer from '../components/LearningViewer'
import DateRangeFilter, { DEFAULT_RANGE, rangeParams, type DateRange } from '../components/DateRangeFilter'
import BusinessParameters, { useKnowledgeRequests } from '../components/BusinessParameters'
import KnowledgeDocuments from '../components/KnowledgeDocuments'
import useConfirm, { usePrompt } from '../components/ConfirmDialog'
import { apiFetch } from '../services/apiClient'
import { useManifest } from '../hooks/useManifest'
import { formatDateTime, effectiveTimeZone } from '../utils/datetime'
import { channelsApi, type Channel } from '../services/channelsApi'
import {
  mcpConnectorsApi,
  type McpConnector,
  type FormaDeConexion,
} from '../services/mcpConnectorsApi'
import PermisosDeConectores from '../components/PermisosDeConectores'
import HistorialDeConexiones from '../components/HistorialDeConexiones'
import { extractApiError } from '../utils/apiError'

export default function Operations() {
  const { t } = useTranslation()
  // U5: persist active tab in the URL hash so a refresh / shared
  // link keeps the operator on the same surface. Hash slugs:
  // #estado / #disparadores / #acciones (also accept the i18n keys
  // status / triggers / actions for English deep-links).
  const HASH_BY_TAB = ['estado', 'tareas', 'conocimiento', 'conectores', 'sesiones', 'workspace', 'basedatos', 'memoria', 'aprendizaje']
  const TAB_BY_HASH: Record<string, number> = {
    estado: 0, status: 0,
    // "Tareas automáticas" merges the old Disparadores/Acciones/Automatizaciones
    // tabs; keep their hashes as aliases so old deep-links still land here.
    tareas: 1, tasks: 1,
    disparadores: 1, triggers: 1,
    acciones: 1, actions: 1,
    automatizaciones: 1, automations: 1,
    conocimiento: 2, knowledge: 2,
    conectores: 3, connectors: 3,
    sesiones: 4, sessions: 4,
    workspace: 5,
    basedatos: 6, database: 6,
    memoria: 7, memory: 7,
    aprendizaje: 8, learning: 8,
  }
  const initialHash = typeof window !== 'undefined'
    ? window.location.hash.replace('#', '')
    : ''
  const [tab, setTab] = useState<number>(TAB_BY_HASH[initialHash] ?? 0)
  // v7.140.0 — a "Aprendido" chip in another surface deep-links here with
  // ?learning=<id>; land on the tab and highlight that learning. Read once so a
  // later manual tab change doesn't keep dragging the operator back.
  const focusLearningId = useMemo(() => {
    if (typeof window === 'undefined') return null
    const raw = new URLSearchParams(window.location.search).get('learning')
    const n = raw ? Number(raw) : NaN
    return Number.isFinite(n) ? n : null
  }, [])
  useEffect(() => {
    if (focusLearningId != null) setTab(8)
  }, [focusLearningId])
  // R-1 (P3): mutate the URL inside an effect — never during render.
  // Calling history.replaceState in the function body fired on every
  // re-render and overwrote the hash even when the operator had
  // navigated to a different anchor.
  useEffect(() => {
    if (typeof window === 'undefined') return
    const target = `#${HASH_BY_TAB[tab]}`
    if (window.location.hash !== target) {
      window.history.replaceState(null, '', target)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab])
  // Listen for back/forward navigation so the active tab follows
  // the URL the user just landed on.
  useEffect(() => {
    if (typeof window === 'undefined') return
    const onHash = () => {
      const next = TAB_BY_HASH[window.location.hash.replace('#', '')]
      if (next !== undefined && next !== tab) setTab(next)
    }
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab])
  const { data, isLoading, error } = useQuery({
    queryKey: ['agent-manifest'],
    queryFn:  () => apiFetch('/api/agent/manifest').then(async (r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      return r.json()
    }),
    // R3: pause polling when the tab is hidden — react-query's
    // refetchIntervalInBackground defaults to false, but we make it
    // explicit so the page doesn't burn ingress + DB cycles for an
    // operator with the tab in a background window. Resumes on
    // visibility change automatically.
    refetchInterval: 30_000,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
    retry: false,
  })

  // Connectors (channels) state is queried at page level so the tab
  // label can badge the number of connectors that need attention
  // (pending OAuth consent, expired credential, polling errors) —
  // the operator shouldn't have to open the tab to discover that a
  // Composio connector lost its grant. 404 (older backends without
  // the channels feature) degrades to "no connectors".
  const channelsQuery = useQuery({
    queryKey: ['ops-channels'],
    queryFn: async () => {
      const [caps, list] = await Promise.all([
        channelsApi.capabilities().catch(() => null),
        channelsApi.list().catch(() => [] as Channel[]),
      ])
      return { caps, list }
    },
    refetchInterval: 60_000,
    refetchIntervalInBackground: false,
    retry: false,
  })
  const connectors = channelsQuery.data?.list ?? []
  const connectorsNeedingAuth = connectors.filter(
    (c) => c.status === 'pending' || c.status === 'unauthorized' || c.status === 'error',
  ).length
  // Business parameters the employee is waiting on → badge on the Conocimiento tab.
  const knowledgePending = useKnowledgeRequests().pending
  // v7.140.0 — rules the employee is ASKING to be allowed to apply. It badges
  // the Aprendizaje tab because a proposal nobody sees is a proposal that never
  // gets answered — and until it is answered the employee keeps hitting the same
  // false positive. 404 on older backends degrades to "nothing pending".
  const learningPending = useQuery({
    queryKey: ['self-proposals-badge'],
    queryFn: async () => {
      const r = await apiFetch('/api/agent/self-memory/proposals')
      if (!r.ok) return { total: 0 }
      return r.json() as Promise<{ total: number }>
    },
    refetchInterval: 60_000,
    refetchIntervalInBackground: false,
    retry: false,
  }).data?.total ?? 0

  if (isLoading) {
    // A blank screen reads as "broken" — show the page chrome and a
    // spinner while the manifest query resolves.
    return (
      <Box>
        <PageHeader title={t('operations.title')} subtitle={t('operations.subtitle')} />
        <Box sx={{ display: 'flex', justifyContent: 'center', mt: 6 }}>
          <CircularProgress />
        </Box>
      </Box>
    )
  }
  if (error) {
    return (
      <Box>
        <PageHeader title={t('operations.title')} subtitle={t('operations.subtitle')} />
        <Alert severity="error">{String((error as any)?.message ?? error)}</Alert>
      </Box>
    )
  }
  if (!data || !data.agent) return null

  const actions = Array.isArray(data.actions) ? data.actions : []
  const stats = data.stats ?? {}

  return (
    <Box>
      <PageHeader
        title={t('operations.title')}
        subtitle={t('operations.subtitle')}
      />

      {/* Operational blockers — always visible: what is stopping the employee
          right now (stuck approval, channel down, connector down, triggers off). */}
      <HealthBanner
        connectorsNeedingAuth={connectorsNeedingAuth}
        onGoConnectors={() => setTab(3)}
      />

      {/* Capabilities header — always visible: who is this agent? */}
      <CapabilitiesCard agent={data.agent} />

      {/* AI-employee identity now lives on its own "Identidad" admin page
          (src/pages/Identity.tsx) — removed from Operations. */}

      {/* Sections in tabs to keep the page scannable instead of a giant scroll */}
      <Paper variant="outlined" sx={{ borderRadius: 2, overflow: 'hidden' }}>
        <Tabs
          value={tab}
          onChange={(_, v) => setTab(v)}
          variant="scrollable"
          scrollButtons="auto"
          sx={{
            px: 2,
            // grey[50] is almost white in BOTH modes → in dark mode the tabs header
            // looked light. Made theme-aware: a subtle dark tint in dark mode.
            backgroundColor: (theme) => theme.palette.mode === 'dark'
              ? theme.palette.background.default
              : theme.palette.grey[50],
            borderBottom: '1px solid',
            borderColor: 'divider',
            '& .MuiTab-root': { textTransform: 'none', fontWeight: 600, minHeight: 56 },
          }}
        >
          <Tab icon={<StatusIcon fontSize="small" />} iconPosition="start" label={t('operations.status.title')} />
          {/* Config-of-the-employee first (Tareas automáticas, Conocimiento),
              then the monitoring/connection tabs. */}
          <Tab icon={<AutomationIcon fontSize="small" />} iconPosition="start" label={t('operations.auto.title', 'Tareas automáticas')} />
          <Tab icon={<FactIcon fontSize="small" />} iconPosition="start" label={
            // Inline count chip (not an overlapping Badge) — the scrollable Tabs
            // container clips a badge that floats above/right of the label.
            <Stack direction="row" spacing={0.75} alignItems="center">
              <span>{t('operations.knowledge.title', 'Conocimiento')}</span>
              {knowledgePending > 0 && (
                <Chip size="small" color="warning" label={knowledgePending}
                  sx={{ height: 18, '& .MuiChip-label': { px: 0.6, fontSize: 11, fontWeight: 700 } }} />
              )}
            </Stack>
          } />
          <Tab
            icon={<ConnectorIcon fontSize="small" />}
            iconPosition="start"
            label={
              <Badge
                color="warning"
                badgeContent={connectorsNeedingAuth}
                invisible={connectorsNeedingAuth === 0}
                sx={{ '& .MuiBadge-badge': { right: -10 } }}
              >
                {t('operations.connectors.title')}
              </Badge>
            }
          />
          <Tab icon={<SessionsIcon fontSize="small" />} iconPosition="start" label={t('operations.sessions.title')} />
          <Tab icon={<WorkspaceIcon fontSize="small" />} iconPosition="start" label={t('operations.workspace.title')} />
          <Tab icon={<DatabaseTabIcon fontSize="small" />} iconPosition="start" label={t('operations.database.title')} />
          <Tab icon={<MemoryTabIcon fontSize="small" />} iconPosition="start" label={t('operations.memory.title')} />
          {/* v7.140.0 — Learning (`Aprendizaje`): what the employee has learned from the ENVIRONMENT
              (unlike Memoria, which is per person) + the traceability of the
              rules that were born from it. Badge = it is asking you for permission. */}
          <Tab icon={<LearningTabIcon fontSize="small" />} iconPosition="start" label={
            <Stack direction="row" spacing={0.75} alignItems="center">
              <span>{t('operations.learning.title', 'Aprendizaje')}</span>
              {learningPending > 0 && (
                <Chip size="small" color="primary" label={learningPending}
                  sx={{ height: 18, '& .MuiChip-label': { px: 0.6, fontSize: 11, fontWeight: 700 } }} />
              )}
            </Stack>
          } />
        </Tabs>
        <Box sx={{ p: { xs: 2, md: 3 } }}>
          {tab === 0 && <StatusBlock stats={stats} />}
          {tab === 1 && <AutomaticWorkSection actions={actions} />}
          {tab === 2 && <KnowledgeSection />}
          {tab === 3 && (
            <ConnectorsBlock
              caps={channelsQuery.data?.caps ?? null}
              connectors={connectors}
              needingAuth={connectorsNeedingAuth}
              loading={channelsQuery.isLoading}
            />
          )}
          {tab === 4 && <SessionsBlock />}
          {tab === 5 && <WorkspaceViewer />}
          {tab === 6 && <DatabaseBlock />}
          {tab === 7 && <MemoryViewer />}
          {tab === 8 && <LearningViewer focusLearningId={focusLearningId} />}
        </Box>
      </Paper>
    </Box>
  )
}

// R-OPS-1: pick the best per-locale value from a ``business_description``
// dict ({"es": "...", "en": "..."}). Falls back to the other locale if
// the active one is missing so the operator never sees an empty card
// just because they switched language. Returns null when every locale
// is empty / the field is absent.
// R-TZ-1 (v7.1): every backend timestamp is now a TZ-aware ISO
// string ending in ``+00:00``. ``new Date(iso)`` converts that to
// the browser-local instant, and ``toLocaleString`` with
// ``timeZoneName`` shows the user *their* TZ — so an operator in
// Madrid sees "13 may 2026, 17:49 CEST" while a teammate in NYC
// sees "13 may 2026, 11:49 EDT" for the SAME event.
//
// R-TZ-1.1 (v7.2 fix): we used to set ``dateStyle: 'medium'`` +
// ``timeStyle: 'medium'`` AND ``timeZoneName: 'short'``. Per the
// ECMA-402 spec those options are mutually exclusive — combining
// them throws ``TypeError: Invalid option : option`` in V8
// (Chrome/Edge). We now use **explicit fields** (year / month /
// day / hour / minute / second / timeZoneName) which produce the
// same visual result and play nicely with ``timeZoneName``.
//
// The single-argument call returns null on falsy input so callers
// can do ``fmtDateTime(x) ?? t('operations.triggers.lastRunMissing')``
// without short-circuiting boilerplate.
function fmtDateTime(
  iso: string | null | undefined,
  locale: string,
  opts?: Intl.DateTimeFormatOptions,
): string | null {
  if (!iso) return null
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return null
  // The legacy style API (``dateStyle`` / ``timeStyle``) cannot be
  // combined with component options like ``timeZoneName`` (V8 spec
  // ban), so it can't go through the shared formatter. We still honour
  // the GLOBAL display-timezone toggle via ``effectiveTimeZone()``.
  const callerUsesStyles = !!(opts && (opts.dateStyle || opts.timeStyle))
  if (callerUsesStyles) {
    try {
      return d.toLocaleString(locale, { timeZone: effectiveTimeZone(), ...opts })
    } catch {
      return d.toLocaleString(locale)
    }
  }
  // Standard shape → shared helper, so Operations stays in lockstep
  // with every other screen (business/local toggle + tz abbreviation).
  return formatDateTime(iso, opts)
}

// R-OPS-4 (v7.1): a daily cron written as ``interval_sec: 86400``
// reads as "cada 86400s" on the chip, which made the operator
// misread the cadence as "every 15 minutes" (it was a tooltip
// confusion in production support). We collapse small multiples
// of common units (s / m / h / d) so the chip always shows the
// shortest human form the value supports: 900 → 15min, 3600 → 1h,
// 86400 → 24h. We deliberately stop at hours rather than days
// because operators reason about "horas hasta el próximo tick";
// "1 d" forces a mental conversion. For values that don't divide
// cleanly we fall back to the raw seconds so the chip is still

type AutomationRow = any

// The ONE current-state of a standing order, distinct from its on/off config and
// from the last execution's outcome. Backend-derived (`runtime_state`), rendered
// as a single unmissable badge so the operator reads "what is happening now?"
// at a glance: running / needs approval / waiting / disabled.
const RUNTIME_STATE_META: Record<string, {
  color: 'info' | 'warning' | 'default' | 'success'; icon: JSX.Element; def: string
}> = {
  running: { color: 'info', icon: <RunningIcon fontSize="small" />, def: 'Ejecutándose ahora' },
  pending_approval: { color: 'warning', icon: <PendingIcon fontSize="small" />, def: 'Requiere aprobación' },
  idle: { color: 'success', icon: <WaitingIcon fontSize="small" />, def: 'En espera' },
  disabled: { color: 'default', icon: <PausedIcon fontSize="small" />, def: 'Desactivada' },
}

function RuntimeStateChip({ state }: { state?: string }) {
  const { t } = useTranslation()
  const meta = RUNTIME_STATE_META[state || 'idle'] ?? RUNTIME_STATE_META.idle
  return (
    <Chip
      size="small"
      color={meta.color}
      variant={meta.color === 'default' ? 'outlined' : 'filled'}
      icon={meta.icon}
      label={t(`operations.automations.runtime.${state || 'idle'}`, meta.def)}
      sx={{ fontWeight: 700 }}
    />
  )
}

// A tiny labelled block ("CONFIGURACIÓN", "ÚLTIMA EJECUCIÓN") so each automation
// card is read as three separate things instead of one chip soup.
function CardZone({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <Box sx={{ mt: 1 }}>
      <Typography variant="overline" color="text.secondary" sx={{ lineHeight: 1.4, letterSpacing: 0.4 }}>
        {label}
      </Typography>
      <Box sx={{ mt: 0.25 }}>{children}</Box>
    </Box>
  )
}

// "Última ejecución completa" — the last time this standing order actually ran
// and how it went, with a one-click deep-link to that execution. Kept apart from
// runtime badge (what's happening NOW) so the operator can tell "it already
// ran and went fine" from "it is running now".
function LastRunLine({ a }: { a: AutomationRow }) {
  const { t } = useTranslation()
  const lr = a.last_run
  const runTone = (s?: string): 'success' | 'error' | 'info' | 'default' => {
    const v = (s || '').toUpperCase()
    if (v === 'COMPLETED') return 'success'
    if (v === 'FAILED' || v === 'ERROR') return 'error'
    if (v === 'RUNNING') return 'info'
    return 'default'
  }
  // v7.137.0 — the last time IN ONE SENTENCE: the WHEN fired → did the ONLY IF pass? → what did it do?
  // Combines last_eval (from the ledger, exists even if the condition cut off without a run) + last_run.
  const ev = a.last_eval
  const runLink = lr ? (
    <Link component={RouterLink} to={`/runs/${lr.id}`} variant="caption">
      {t('operations.automations.viewLastRun', 'Ver ejecución →')}
    </Link>
  ) : null
  if (lr && String(lr.status || '').toUpperCase() === 'RUNNING') {
    return (
      <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
        <Chip size="small" color="info" label={t('operations.automations.runningNow', 'Ejecutándose ahora…')} />
        {runLink}
      </Stack>
    )
  }
  if (a.last_fire_status === 'pending_approval') {
    return (
      <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
        <Typography variant="body2" color="warning.main">
          {t('operations.automations.firedPending', 'Saltó · esperando tu aprobación.')}
        </Typography>
        <Link component={RouterLink} to="/approvals" variant="caption">
          {t('operations.automations.goApprovals', 'Ir a aprobaciones →')}
        </Link>
      </Stack>
    )
  }
  if (ev) {
    const when = ev.at ? formatDateTime(ev.at) : '—'
    if (ev.error) {
      return (
        <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
          <Typography variant="body2" color="warning.main">
            {t('operations.automations.firedError', { when, defaultValue: 'Saltó {{when}} · no se pudo comprobar el «solo si»' })}
          </Typography>
          <Tooltip title={ev.error}><Typography variant="caption" color="error.main" sx={{ maxWidth: 260 }} noWrap>{ev.error}</Typography></Tooltip>
        </Stack>
      )
    }
    if (!ev.met) {
      return (
        <Typography variant="body2" color="text.secondary">
          {t('operations.automations.firedNotMet', { when, defaultValue: 'Saltó {{when}} · el «solo si» no se cumplió → no actuó' })}
        </Typography>
      )
    }
    // v7.138.0 — the "only if" was met but the firing was BLOCKED (fired=false):
    // anti-repetition/cooldown or silenced. Do NOT say "acted" nor link to a run that does not
    // exist (that was the inconsistency: the row said "acted" and the ledger "0 fired").
    if (!ev.fired) {
      return (
        <Typography variant="body2" color="text.secondary">
          {ev.silenced
            ? t('operations.automations.firedMetSilenced', { when, defaultValue: 'Saltó {{when}} · el «solo si» se cumplió, pero no actuó (silenciado)' })
            : t('operations.automations.firedMetNoAct', { when, defaultValue: 'Saltó {{when}} · el «solo si» se cumplió, pero no actuó (anti-repetición/cooldown o silenciado)' })}
        </Typography>
      )
    }
    return (
      <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
        <Typography variant="body2" color="success.main">
          {t('operations.automations.firedMet', { when, defaultValue: 'Saltó {{when}} · el «solo si» se cumplió → actuó' })}
        </Typography>
        {lr && <Chip size="small" color={runTone(lr.status)}
          label={t(`operations.automations.runOutcome.${(lr.status || '').toUpperCase()}`, { defaultValue: String(lr.status || '—') })} />}
        {lr?.error && <Tooltip title={lr.error}><Typography variant="caption" color="error.main" sx={{ maxWidth: 240 }} noWrap>{lr.error}</Typography></Tooltip>}
        {runLink}
      </Stack>
    )
  }
  // No eval trace (legacy / always without ledger) → falls back to the last run.
  if (lr) {
    const when = lr.finished_at || lr.started_at
    return (
      <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
        <Typography variant="body2">{when ? formatDateTime(when) : '—'}</Typography>
        <Chip size="small" color={runTone(lr.status)}
          label={t(`operations.automations.runOutcome.${(lr.status || '').toUpperCase()}`, { defaultValue: String(lr.status || '—') })} />
        {lr.error && <Tooltip title={lr.error}><Typography variant="caption" color="error.main" sx={{ maxWidth: 260 }} noWrap>{lr.error}</Typography></Tooltip>}
        {runLink}
      </Stack>
    )
  }
  if (a.last_fire_status === 'rejected') {
    return (
      <Typography variant="body2" color="text.secondary">
        {t('operations.automations.lastFireRejected', 'Su último disparo se rechazó.')}
      </Typography>
    )
  }
  return (
    <Typography variant="body2" color="text.secondary">
      {t('operations.automations.neverRan', 'Aún no ha saltado.')}
    </Typography>
  )
}

// "Próximo disparo" per standing order: when it will fire NEXT, derived from its
// signal (never from memory). A schedule shows the concrete next fire-time in the
// UI's timezone (REUSING the same `nextCronRun` the editor's live sentence uses);
// data/event/manual signals describe their cadence in words. A disabled/paused
// order says so loudly instead of a fake time. Shown in BOTH the cards and the
// table so the operator reads "when it will act" at a glance in either view.
function NextFireLine({ a, catTz, compact = false }: { a: AutomationRow; catTz?: string; compact?: boolean }) {
  const { t } = useTranslation()
  // Executions off → never a next-fire time (the "if they are disabled, say so
  // here" rule). Visually distinct: paused icon + muted italic.
  if (a.runtime_state === 'disabled' || !a.enabled) {
    return (
      <Stack direction="row" spacing={0.5} alignItems="center" useFlexGap sx={{ flexWrap: 'wrap' }}>
        <PausedIcon fontSize="small" sx={{ color: 'text.disabled' }} />
        <Typography variant="caption" sx={{ fontWeight: 600, fontStyle: 'italic', color: 'text.secondary' }}>
          {t('operations.automations.next.disabled', 'Desactivada — no se ejecutará')}
        </Typography>
      </Stack>
    )
  }
  const st = a.signal_type
  let icon = <WaitingIcon fontSize="small" color="action" />
  let text: string
  let isDate = false
  if (st === 'schedule') {
    const when = nextCronRun(a.signal_spec?.cron || '', a.signal_spec?.tz || catTz || undefined)
    icon = <ScheduleIcon fontSize="small" color="action" />
    if (when) { text = formatDateTime(when); isDate = true }
    else text = t('operations.automations.next.scheduleUnknown', 'Según su horario')
  } else if (st === 'data_condition') {
    const sec = Number(a.check_interval_sec) || 0
    const min = sec > 0 ? Math.max(1, Math.round(sec / 60)) : 0
    text = min > 0
      ? t('operations.automations.next.interval', { min, defaultValue: 'En la próxima comprobación (cada {{min}} min)' })
      : t('operations.automations.next.intervalNoN', 'En la próxima comprobación')
  } else if (st === 'business_event' || st === 'channel_message') {
    text = t('operations.automations.next.event', 'Cuando ocurra el evento')
  } else if (st === 'manual') {
    icon = <RunningIcon fontSize="small" color="action" />
    text = t('operations.automations.next.manual', 'Solo manual')
  } else {
    text = '—'
  }
  // Prefix "Próximo disparo:" only for a concrete date and only outside the table
  // (the table column header already carries that label; the word phrases are
  // self-describing).
  const label = (!compact && isDate)
    ? t('operations.automations.next.label', { when: text, defaultValue: 'Próximo disparo: {{when}}' })
    : text
  return (
    <Stack direction="row" spacing={0.5} alignItems="center" useFlexGap sx={{ flexWrap: 'wrap' }}>
      {icon}
      <Typography variant="caption" color="text.secondary">{label}</Typography>
    </Stack>
  )
}

// The THREE ways an operator can run a standing order by hand, unmistakably
// labelled, folded into one tidy dropdown so the card/row stays uncluttered:
//   · Probar                    — dry-run the "solo si" (no action, 0 cost)
//   · Ejecutar comprobando…     — act ONLY if the "solo si" holds
//   · Forzar ejecución          — act ignoring the "solo si" (with confirm)
// Reused verbatim in the cards view and inside each table row's actions menu.
function RunModesMenu({ canTest, onProbar, onChecked, onForce, buttonSx }: {
  canTest: boolean
  onProbar: () => void
  onChecked: () => void
  onForce: () => void
  buttonSx?: any
}) {
  const { t } = useTranslation()
  const [anchor, setAnchor] = useState<null | HTMLElement>(null)
  const close = () => setAnchor(null)
  return (
    <>
      <Button
        size="small" variant="outlined"
        startIcon={<RunningIcon fontSize="small" />} endIcon={<ArrowDropDownIcon fontSize="small" />}
        onClick={(e) => setAnchor(e.currentTarget)}
        sx={{ textTransform: 'none', ...buttonSx }}
      >
        {t('operations.automations.runMenu', 'Ejecutar…')}
      </Button>
      <Menu anchorEl={anchor} open={!!anchor} onClose={close} slotProps={{ paper: { sx: { maxWidth: 360 } } }}>
        {canTest && (
          <MenuItem onClick={() => { close(); onProbar() }} sx={{ whiteSpace: 'normal', alignItems: 'flex-start' }}>
            <ListItemIcon sx={{ mt: 0.5 }}><PreviewIcon fontSize="small" /></ListItemIcon>
            <ListItemText
              primary={t('operations.automations.test', 'Probar')}
              secondary={t('operations.automations.testHelp', 'Comprueba el «solo si» sin actuar. Sin coste.')}
            />
          </MenuItem>
        )}
        <MenuItem onClick={() => { close(); onChecked() }} sx={{ whiteSpace: 'normal', alignItems: 'flex-start' }}>
          <ListItemIcon sx={{ mt: 0.5 }}><OkIcon fontSize="small" color="success" /></ListItemIcon>
          <ListItemText
            primary={t('operations.automations.runChecked', 'Ejecutar comprobando el «solo si»')}
            secondary={t('operations.automations.runCheckedHelp', 'Ejecuta la reacción solo si la condición se cumple ahora.')}
          />
        </MenuItem>
        <MenuItem onClick={() => { close(); onForce() }} sx={{ whiteSpace: 'normal', alignItems: 'flex-start' }}>
          <ListItemIcon sx={{ mt: 0.5 }}><TriggerIcon fontSize="small" color="warning" /></ListItemIcon>
          <ListItemText
            primary={t('operations.automations.forceRunBtn', 'Forzar ejecución')}
            secondary={t('operations.automations.forceHelp', 'Ejecuta ignorando el «solo si»: actúa aunque la condición no se cumpla.')}
          />
        </MenuItem>
      </Menu>
    </>
  )
}

// v7.152.0 — does this automation come from the DEPLOYMENT? It was born from a template of the
// descriptor and nobody has derived it yet. Its content is read-only: the
// backend rejects the change, so the screen has to say so BEFORE the
// user types, not after. An evolution (`derived_from_id`) is already theirs and is editable.
function isFromDeployment(a: AutomationRow): boolean {
  return Boolean(a?.template_key) && !a?.derived_from_id
}

// One row of the automations TABLE view. Holds its own actions-menu anchor so the
// per-row menu (edit · the 3 ways to run · enable/disable · history
// · eliminar) opens independently. Members (read-only) only get "Historial".
function AutomationTableRow({ a, isAdmin, canTest, catTz, stats, onEdit, onEvolve, onHistory, onToggle, onDelete, onProbar, onChecked, onForce }: {
  a: AutomationRow
  isAdmin: boolean
  canTest: boolean
  catTz?: string
  /** Range-scoped governance numbers from the batch ``/automations/stats`` call.
   *  Undefined = not loaded / not published by this employee's version, which
   *  renders as "—" rather than a misleading zero. */
  stats?: { fired?: number; cost_usd?: number; runs?: number; produced?: number; open?: number }
  onEdit: () => void
  onEvolve: () => void
  onHistory: () => void
  onToggle: (enabled: boolean) => void
  onDelete: () => void
  onProbar: () => void
  onChecked: () => void
  onForce: () => void
}) {
  const { t } = useTranslation()
  const [menuEl, setMenuEl] = useState<null | HTMLElement>(null)
  const close = () => setMenuEl(null)
  const plain = stripHtml(a.description).replace(/\s+/g, ' ').trim()
  return (
    <TableRow hover sx={{ opacity: a.runtime_state === 'disabled' ? 0.7 : 1 }}>
      <TableCell sx={{ maxWidth: 240 }}>
        <Typography variant="body2" sx={{ fontWeight: 600 }} noWrap>{a.title || `#${a.id}`}</Typography>
        <Stack direction="row" spacing={0.5} sx={{ mt: 0.25, flexWrap: 'wrap', gap: 0.5 }}>
          {a.area_tag && (
            <Chip size="small" variant="outlined" label={a.area_tag}
              sx={{ height: 18, '& .MuiChip-label': { px: 0.6, fontSize: 10 } }} />
          )}
          {isFromDeployment(a) && (
            <Tooltip title={t('operations.automations.fromDeploymentHelp',
              'Viene del despliegue: su contenido no se edita y se actualiza al actualizar el empleado. Para cambiarla, créala como evolución.') as string}>
              <Chip size="small" color="info" variant="outlined"
                label={t('operations.automations.fromDeployment', 'Del despliegue')}
                sx={{ height: 18, '& .MuiChip-label': { px: 0.6, fontSize: 10 } }} />
            </Tooltip>
          )}
          {a.derived_from_id && (
            <Chip size="small" variant="outlined"
              label={t('operations.automations.derived', 'Evolución')}
              sx={{ height: 18, '& .MuiChip-label': { px: 0.6, fontSize: 10 } }} />
          )}
        </Stack>
      </TableCell>
      <TableCell sx={{ maxWidth: 320 }}>
        {plain ? (
          <Tooltip title={plain}>
            <Typography variant="caption" color="text.secondary"
              sx={{ display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden' }}>
              {plain}
            </Typography>
          </Tooltip>
        ) : <Typography variant="caption" color="text.disabled">—</Typography>}
      </TableCell>
      <TableCell><RuntimeStateChip state={a.runtime_state} /></TableCell>
      <TableCell><NextFireLine a={a} catTz={catTz} compact /></TableCell>
      <TableCell align="right">
        <Typography variant="body2">{stats ? (stats.fired ?? 0) : '—'}</Typography>
      </TableCell>
      <TableCell align="right" sx={{ whiteSpace: 'nowrap' }}>
        <Typography variant="body2">
          {stats ? `${(stats.cost_usd ?? 0).toFixed(3)} $` : '—'}
        </Typography>
      </TableCell>
      <TableCell align="right">
        <Typography variant="body2">
          {stats && stats.produced != null
            ? `${stats.open ?? 0} / ${stats.produced}`
            : '—'}
        </Typography>
      </TableCell>
      <TableCell align="right" sx={{ whiteSpace: 'nowrap' }}>
        <IconButton size="small" onClick={(e) => setMenuEl(e.currentTarget)}
          aria-label={t('operations.automations.rowActions', 'Acciones')}>
          <MoreVertIcon fontSize="small" />
        </IconButton>
        <Menu anchorEl={menuEl} open={!!menuEl} onClose={close}>
          {isAdmin && !isFromDeployment(a) && (
            <MenuItem onClick={() => { close(); onEdit() }}>
              <ListItemIcon><EditIcon fontSize="small" /></ListItemIcon>
              <ListItemText primary={t('operations.automations.rowEdit', 'Editar')} />
            </MenuItem>
          )}
          {/* From the deployment: its content is read-only (the backend rejects the
              save), so «Editar» is NOT offered here — it would be lying to the
              user and they would discover the seal on pressing Save. We offer the
              real path: create an editable evolution. */}
          {isAdmin && isFromDeployment(a) && (
            <MenuItem onClick={() => { close(); onEvolve() }}>
              <ListItemIcon><CopyIcon fontSize="small" /></ListItemIcon>
              <ListItemText
                primary={t('operations.automations.rowEvolve', 'Crear una evolución')}
                secondary={t('operations.automations.rowEvolveHelp',
                  'Copia editable; la original se queda intacta')} />
            </MenuItem>
          )}
          {/* Second, right under «Editar» (Edit): it is the most-used entry (what it has
              done, what it has left, why it does not always notify) and it was buried
              at the end among the execution actions. */}
          <MenuItem onClick={() => { close(); onHistory() }}>
            <ListItemIcon><HistoryIcon fontSize="small" /></ListItemIcon>
            <ListItemText primary={t('operations.automations.history.open', 'Actividad y gobierno')} />
          </MenuItem>
          {isAdmin && canTest && (
            <MenuItem onClick={() => { close(); onProbar() }}>
              <ListItemIcon><PreviewIcon fontSize="small" /></ListItemIcon>
              <ListItemText primary={t('operations.automations.test', 'Probar')} />
            </MenuItem>
          )}
          {isAdmin && (
            <MenuItem onClick={() => { close(); onChecked() }}>
              <ListItemIcon><OkIcon fontSize="small" color="success" /></ListItemIcon>
              <ListItemText primary={t('operations.automations.runChecked', 'Ejecutar comprobando el «solo si»')} />
            </MenuItem>
          )}
          {isAdmin && (
            <MenuItem onClick={() => { close(); onForce() }}>
              <ListItemIcon><TriggerIcon fontSize="small" color="warning" /></ListItemIcon>
              <ListItemText primary={t('operations.automations.forceRunBtn', 'Forzar ejecución')} />
            </MenuItem>
          )}
          {isAdmin && (
            <MenuItem onClick={() => { close(); onToggle(!a.enabled) }}>
              <ListItemIcon>{a.enabled ? <PausedIcon fontSize="small" /> : <RunningIcon fontSize="small" />}</ListItemIcon>
              <ListItemText primary={a.enabled
                ? t('operations.automations.switchOff', 'Desactivar')
                : t('operations.automations.switchOn', 'Activar')} />
            </MenuItem>
          )}
          {isAdmin && <Divider />}
          {isAdmin && (
            <MenuItem onClick={() => { close(); onDelete() }} sx={{ color: 'error.main' }}>
              <ListItemIcon><DeleteIcon fontSize="small" color="error" /></ListItemIcon>
              <ListItemText primary={t('operations.automations.rowDelete', 'Eliminar')} />
            </MenuItem>
          )}
        </Menu>
      </TableCell>
    </TableRow>
  )
}

// IANA timezone list for the per-automation schedule picker. Uses the runtime
// catalogue when available (modern browsers / Node 18+), else a small fallback.
const TZ_OPTIONS: string[] = (() => {
  try {
    const v = (Intl as any).supportedValuesOf?.('timeZone')
    if (Array.isArray(v) && v.length) return v
  } catch { /* not supported — fall through */ }
  return ['UTC', 'Europe/Madrid', 'Europe/London', 'America/New_York', 'America/Mexico_City', 'America/Bogota', 'America/Argentina/Buenos_Aires', 'America/Santiago', 'America/Sao_Paulo']
})()

export function AutomationsSection() {
  const { t, i18n } = useTranslation()
  const lang = i18n.language?.startsWith('en') ? 'en' : 'es'
  const loc = (v: any) => (v && typeof v === 'object' ? (v[lang] || v.es || v.en || '') : (v || ''))
  // Short, human chip labels — never render the raw enum (data_condition /
  // draft_and_approve / armed) to the boss. The full description is a.sentence.
  const sigChip = (s: string) => t(`operations.automations.chipSignal.${s}`, s)
  const reaChip = (s: string) => t(`operations.automations.chipReaction.${s}`, s)
  const { confirm, dialog: confirmDialog } = useConfirm()
  const qc = useQueryClient()
  // This section is reused on /trabajo-autonomo (employee nav — MEMBERS see it),
  // but standing orders are a boss activity: every write endpoint is require_admin.
  // So members get a READ-ONLY view (no create/edit/toggle/test/run) and a hint —
  // otherwise a member's click would 403 silently. Fail-closed on unknown role.
  const me = useQuery({
    queryKey: ['users-me'],
    queryFn: () => apiFetch('/api/agent/users/me').then((r) => (r.ok ? r.json() : { is_admin: false })),
    staleTime: 60_000,
  })
  const isAdmin = me.data?.is_admin === true
  const list = useQuery({
    queryKey: ['automations'],
    queryFn: () => apiFetch('/api/agent/automations').then(async (r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json()
    }),
    refetchInterval: 30_000,
  })
  const catalog = useQuery({
    queryKey: ['automations-catalog'],
    queryFn: () => apiFetch('/api/agent/automations/catalog').then(async (r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json()
    }),
    staleTime: 5 * 60_000,
  })
  const items: AutomationRow[] = list.data?.items ?? []
  const cat = catalog.data
  const [editing, setEditing] = useState<AutomationRow | null>(null)
  const [history, setHistory] = useState<AutomationRow | null>(null)
  const [testResult, setTestResult] = useState<Record<number, any>>({})
  // v7.137.0 — «Ejecutar ahora» (Run now) was misleading (it forced, ignoring the "only if"). Now
  // «Probar» (Test) = dry-run (checks without acting) and «Forzar ejecución» (Force run) = acts skipping the
  // "only if", with explicit confirmation.
  const [forcing, setForcing] = useState<AutomationRow | null>(null)
  const [expandRecent, setExpandRecent] = useState<number | null>(null)  // v7.137.0 — last 5 inline
  const [actionErr, setActionErr] = useState<string | null>(null)
  // Manual-run feedback with a severity (success = ran, info = not applicable,
  // warning = forced) so "not applicable" doesn't read as a green success.
  const [runMsg, setRunMsg] = useState<{ severity: 'success' | 'info' | 'warning'; text: string } | null>(null)
  const flashRun = (severity: 'success' | 'info' | 'warning', text: string) => {
    setRunMsg({ severity, text }); setTimeout(() => setRunMsg(null), 9000)
  }
  // Listing controls: free-text search (title + plain description + area) and a
  // cards⇄table view toggle, with table pagination state.
  // v7.140.0 — TABLE is the default and the choice PERSISTS. Cards are nice for
  // three standing orders; with twenty they force scrolling to compare, which is
  // exactly what the operator does here (which ones fire, which cost, which are
  // stuck). The preference used to be lost on every reload.
  const [search, setSearch] = useState('')
  const [view, setView] = useState<'cards' | 'table'>(() => {
    try {
      const saved = localStorage.getItem('wai.automations.view')
      return saved === 'cards' || saved === 'table' ? saved : 'table'
    } catch { return 'table' }
  })
  useEffect(() => {
    try { localStorage.setItem('wai.automations.view', view) } catch { /* private mode */ }
  }, [view])
  // Governance numbers for the table, in ONE batch call. Deliberately NOT part of
  // /api/agent/automations (polled every 30s, drives the switches): these are
  // aggregates, so they get their own key with the range in it, a high staleTime
  // and no polling — they change when the operator changes the range, not every
  // half minute in every open browser.
  const [statsRange, setStatsRange] = useState<DateRange>(DEFAULT_RANGE)
  const batchStats = useQuery({
    queryKey: ['automations-stats', statsRange],
    queryFn: async () => {
      const r = await apiFetch(`/api/agent/automations/stats?${rangeParams(statsRange)}`)
      if (!r.ok) return { items: {} }
      return r.json() as Promise<{ items: Record<string, any> }>
    },
    enabled: view === 'table',
    staleTime: 120_000,
    refetchInterval: false,
    retry: false,
  })
  const [page, setPage] = useState(0)
  const [rowsPerPage, setRowsPerPage] = useState(10)
  // Reset to the first page whenever the result set or view changes so a stale
  // page index never lands the operator on an empty page.
  useEffect(() => { setPage(0) }, [search, view, rowsPerPage])

  const invalidate = () => qc.invalidateQueries({ queryKey: ['automations'] })
  // Wrap the imperative actions so a failed PATCH/DELETE/test surfaces to the
  // operator instead of silently reverting on the next poll (the switch would
  // otherwise flip back with no explanation).
  const run = async (fn: () => Promise<void>) => {
    try { setActionErr(null); await fn() }
    catch (e: any) { setActionErr(String(e?.message ?? e)) }
  }
  const toggle = (a: AutomationRow, enabled: boolean) => run(async () => {
    const r = await apiFetch(`/api/agent/automations/${a.id}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    })
    if (!r.ok) throw new Error(`HTTP ${r.status}`)
    invalidate()
  })
  // v7.152.0 — evolving what came from the deployment: the backend copies the row,
  // points it at its origin and leaves it DISABLED. The editor opens on the copy so that
  // the change the user wanted to make is made there, not on the original.
  const evolve = (a: AutomationRow) => run(async () => {
    const r = await apiFetch(`/api/agent/automations/${a.id}/evolve`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    })
    if (!r.ok) throw new Error(`HTTP ${r.status}`)
    const nueva = await r.json()
    invalidate()
    setEditing({ ...nueva })
  })
  const del = (a: AutomationRow) => confirm(
    { message: t('operations.automations.deleteConfirm', '¿Eliminar esta automatización?'), danger: true },
    () => void run(async () => {
      const r = await apiFetch(`/api/agent/automations/${a.id}`, { method: 'DELETE' })
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      invalidate()
    }),
  )
  const test = (a: AutomationRow) => run(async () => {
    const r = await apiFetch(`/api/agent/automations/${a.id}/test`, { method: 'POST' })
    if (!r.ok) throw new Error(`HTTP ${r.status}`)
    const data = await r.json()
    setTestResult((p) => ({ ...p, [a.id]: data }))
  })
  // Manual run — CHECKING the "solo si" (?check=true): the reaction runs ONLY if
  // the condition holds right now. The response reports what happened so we can
  // tell "executed" from "not applicable" (met === false) without a trip to history.
  const runChecked = (a: AutomationRow) => run(async () => {
    const r = await apiFetch(`/api/agent/automations/${a.id}/run?check=true`, { method: 'POST' })
    const b = await r.json().catch(() => null)
    if (!r.ok) throw new Error(b?.detail || `HTTP ${r.status}`)
    const name = b?.title || a.title || `#${a.id}`
    // Prefer the human-facing `reason`; fall back to a compact of `detail`.
    const why = b?.reason || (b?.detail ? compactJson(b.detail, 160) : '')
    if (b?.fired) {
      flashRun('success', t('operations.automations.runFired', { name, defaultValue: '«{{name}}»: ejecutado — el «solo si» se cumplía. Sigue el resultado en el historial.' }))
    } else if (b?.met === false) {
      const reason = why ? ` ${why}` : ''
      flashRun('info', t('operations.automations.runNotMet', { name, reason, defaultValue: '«{{name}}»: no procedía — el «solo si» no se cumple ahora, así que no actuó.{{reason}}' }))
    } else {
      const reason = why ? ` ${why}` : ''
      flashRun('info', t('operations.automations.runNoOp', { name, reason, defaultValue: '«{{name}}»: no se ejecutó (no procedía).{{reason}} Revisa el historial para el detalle.' }))
    }
    invalidate()
  })
  // Manual run — FORCING (?check=false): fire the reaction NOW ignoring the
  // condition. Confirmed via the "Forzar ejecución" dialog; works for a
  // paused/manual automation (an explicit operator command).
  const runForce = (a: AutomationRow) => run(async () => {
    const r = await apiFetch(`/api/agent/automations/${a.id}/run?check=false`, { method: 'POST' })
    const b = await r.json().catch(() => null)
    if (!r.ok) throw new Error(b?.detail || `HTTP ${r.status}`)
    const name = b?.title || a.title || `#${a.id}`
    flashRun('warning', t('operations.automations.runForced', { name, defaultValue: '«{{name}}»: ejecutado (forzado, ignorando el «solo si»). Sigue el resultado en el historial.' }))
    invalidate()
  })
  // Which rows have a "Probar" (dry-run of the "solo si") to offer.
  const canTestRow = (a: AutomationRow): boolean =>
    a.signal_type === 'data_condition'
    || ['checks', 'watch', 'data', 'sql', 'agent'].includes(a.condition_spec?.mode)
  // template_key set already used → mark templates as "Ya añadida" and block dups.
  const usedTemplateKeys = new Set<string>(items.map((a) => a.template_key).filter(Boolean))
  // v7.139.1 — template picker filter: default «pendientes» (pending: what is still to add),
  // + «todas» (all, with the already-added ones marked) and «añadidas» (added). With counters.
  const [tplFilter, setTplFilter] = useState<'pending' | 'all' | 'added'>('pending')
  const allTpls = (Array.isArray(cat?.templates) ? (cat!.templates as any[]) : [])
  const tplAddedCount = allTpls.filter((t) => usedTemplateKeys.has(t.key)).length
  const tplPendingCount = allTpls.length - tplAddedCount
  const shownTpls = allTpls.filter((tpl) => {
    const used = usedTemplateKeys.has(tpl.key)
    return tplFilter === 'all' ? true : tplFilter === 'added' ? used : !used
  })
  // Client-side filter shared by BOTH views (title + plain-text description + area).
  const q = search.trim().toLowerCase()
  const filtered = q
    ? items.filter((a) => [a.title, stripHtml(a.description), a.area_tag]
        .filter(Boolean).join(' ').toLowerCase().includes(q))
    : items
  const pagedItems = view === 'table'
    ? filtered.slice(page * rowsPerPage, page * rowsPerPage + rowsPerPage)
    : filtered

  return (
    <Stack spacing={2}>
      {/* The page (pages/AutonomousWork.tsx) provides the "Trabajo autónomo"
          title via PageHeader, so this section only shows the action + hint. */}
      {isAdmin && (
        <Stack direction="row" alignItems="center" spacing={1}>
          <Box sx={{ flex: 1 }} />
          <Button size="small" variant="contained" startIcon={<AddIcon />}
            onClick={() => setEditing({ signal_type: 'data_condition', reaction_type: 'notify', requires_approval: true, signal_spec: {}, reaction_spec: {} })}
            sx={{ textTransform: 'none' }}>
            {t('operations.auto.autonomousAdd', 'Añadir trabajo autónomo')}
          </Button>
        </Stack>
      )}
      <Typography variant="body2" color="text.secondary">
        {isAdmin
          ? t('operations.auto.autonomousHint', 'Encargos permanentes que TÚ le das (además de sus rutinas): «Cuando… → entonces…». Nacen desactivados; también puedes crearlos o cambiarlos hablándole en el chat.')
          : t('operations.auto.autonomousHintMember', 'Estas son las órdenes permanentes que tu administrador ha configurado para el empleado. Puedes consultarlas; solo un administrador las crea o cambia.')}
      </Typography>
      {actionErr && <Alert severity="error" onClose={() => setActionErr(null)}>{actionErr}</Alert>}
      {runMsg && <Alert severity={runMsg.severity} onClose={() => setRunMsg(null)}>{runMsg.text}</Alert>}
      {isAdmin && allTpls.length > 0 && (
        <Stack spacing={1} sx={{ maxWidth: 560 }}>
          <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
            <Typography variant="body2" sx={{ fontWeight: 600 }}>
              {t('operations.automations.templates', 'Empezar desde una plantilla')}
            </Typography>
            {/* Counter of already-added vs pending (v7.139.1). */}
            <Chip size="small" color="success" variant="outlined"
              label={t('operations.automations.tplAddedCount', {
                added: tplAddedCount, total: allTpls.length,
                defaultValue: '{{added}}/{{total}} ya añadidas' })} />
            {/* Filter: pending (default) · all · already added. */}
            <ToggleButtonGroup size="small" exclusive value={tplFilter}
              onChange={(_e, v) => { if (v) setTplFilter(v) }}>
              <ToggleButton value="pending">
                {t('operations.automations.tplFilterPending', 'Pendientes')} ({tplPendingCount})
              </ToggleButton>
              <ToggleButton value="all">
                {t('operations.automations.tplFilterAll', 'Todas')} ({allTpls.length})
              </ToggleButton>
              <ToggleButton value="added">
                {t('operations.automations.tplFilterAdded', 'Ya añadidas')} ({tplAddedCount})
              </ToggleButton>
            </ToggleButtonGroup>
          </Stack>
          <TextField
            select
            size="small"
            value=""
            helperText={t('operations.automations.templatesHelp', 'Rellena el formulario con una plantilla pensada para tu negocio; luego ajústala a tu gusto. Las «ya añadidas» no se pueden duplicar.')}
            sx={{ maxWidth: 460 }}
            InputLabelProps={{ shrink: true }}
            SelectProps={{ displayEmpty: true, renderValue: () => t('operations.automations.templatesPick', 'Elegir plantilla…') as string }}
            onChange={(e) => {
              const tpl = allTpls.find((x) => x.key === e.target.value)
              if (!tpl || usedTemplateKeys.has(tpl.key)) return
              setEditing({
                ...tpl.body,
                // Carry the template key so create sends it back → this template
                // then shows "Ya añadida" and can't be added twice.
                template_key: tpl.key,
                signal_spec: tpl.body?.signal_spec ?? {},
                reaction_spec: tpl.body?.reaction_spec ?? {},
              })
            }}
          >
            {shownTpls.length === 0 && (
              <MenuItem value="" disabled>
                {tplFilter === 'pending'
                  ? t('operations.automations.tplAllAdded', 'Todas las plantillas ya están añadidas 🎉')
                  : t('operations.automations.tplNoneInFilter', 'No hay plantillas en este filtro.')}
              </MenuItem>
            )}
            {shownTpls.map((tpl) => {
              const used = usedTemplateKeys.has(tpl.key)
              return (
                <MenuItem key={tpl.key} value={tpl.key} disabled={used} sx={{ display: 'block', whiteSpace: 'normal', maxWidth: 460 }}>
                  <Stack direction="row" spacing={1} alignItems="center">
                    <Typography variant="body2" sx={{ fontWeight: 600, flex: 1 }}>{loc(tpl.label)}</Typography>
                    {used && (
                      <Chip size="small" color="success" variant="outlined" icon={<OkIcon fontSize="small" />}
                        label={t('operations.automations.templateAdded', 'Ya añadida')} />
                    )}
                  </Stack>
                  {loc(tpl.description) && (
                    <Typography variant="caption" color="text.secondary" sx={{ display: 'block' }}>
                      {loc(tpl.description)}
                    </Typography>
                  )}
                </MenuItem>
              )
            })}
          </TextField>
        </Stack>
      )}
      {list.isLoading ? (
        <Box sx={{ display: 'flex', justifyContent: 'center', py: 3 }}><CircularProgress size={24} /></Box>
      ) : list.isError ? (
        <Alert severity="error">{t('operations.loadError', 'No se pudo cargar. Reintenta en unos segundos.')}</Alert>
      ) : items.length === 0 ? (
        <Alert severity="info">{t('operations.automations.empty', 'Aún no hay automatizaciones. Añade una o pídesela al empleado por el chat.')}</Alert>
      ) : (
        <>
          {/* Search box + view selector — they filter and affect BOTH views. */}
          <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} alignItems={{ sm: 'center' }} sx={{ mb: 1 }}>
            <TextField
              size="small"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder={t('operations.automations.searchPh', 'Buscar por nombre, descripción o área…')}
              sx={{ flex: 1, maxWidth: 460 }}
              InputProps={{
                startAdornment: (
                  <InputAdornment position="start"><SearchIcon fontSize="small" color="action" /></InputAdornment>
                ),
                endAdornment: search ? (
                  <InputAdornment position="end">
                    <IconButton size="small" edge="end" aria-label={t('common.clear', 'Limpiar')} onClick={() => setSearch('')}>
                      <DeleteIcon fontSize="small" />
                    </IconButton>
                  </InputAdornment>
                ) : undefined,
              }}
            />
            <Box sx={{ flex: 1 }} />
            {view === 'table' && (
              <DateRangeFilter value={statsRange} onChange={setStatsRange} dense />
            )}
            <ToggleButtonGroup
              size="small" exclusive value={view}
              onChange={(_, v) => { if (v) setView(v) }}
              aria-label={t('operations.automations.viewMode', 'Vista')}
            >
              <ToggleButton value="cards" aria-label={t('operations.automations.viewCards', 'Tarjetas')}>
                <Tooltip title={t('operations.automations.viewCards', 'Tarjetas')}><CardsViewIcon fontSize="small" /></Tooltip>
              </ToggleButton>
              <ToggleButton value="table" aria-label={t('operations.automations.viewTable', 'Tabla')}>
                <Tooltip title={t('operations.automations.viewTable', 'Tabla')}><TableViewIcon fontSize="small" /></Tooltip>
              </ToggleButton>
            </ToggleButtonGroup>
          </Stack>
          {filtered.length === 0 ? (
            <Alert severity="info">{t('operations.automations.searchEmpty', 'Ninguna automatización coincide con la búsqueda.')}</Alert>
          ) : view === 'table' ? (
            <Paper variant="outlined" sx={{ borderRadius: 2, overflow: 'hidden' }}>
              <TableContainer>
                <Table size="small">
                  <TableHead>
                    <TableRow>
                      <TableCell>{t('operations.automations.colName', 'Nombre')}</TableCell>
                      <TableCell>{t('operations.automations.colDescription', 'Descripción')}</TableCell>
                      <TableCell>{t('operations.automations.colState', 'Estado')}</TableCell>
                      <TableCell>{t('operations.automations.colNextRun', 'Próximo disparo')}</TableCell>
                      {/* v7.140.0 — the three figures the operator compares across
                          standing orders. The full funnel stays in the
                          detail so the table does not become a dashboard. */}
                      <TableCell align="right">
                        <Tooltip title={t('operations.automations.colFiresHint', 'Veces que ha actuado en el rango elegido')}>
                          <span>{t('operations.automations.colFires', 'Disparos')}</span>
                        </Tooltip>
                      </TableCell>
                      <TableCell align="right">{t('operations.automations.colCost', 'Coste')}</TableCell>
                      <TableCell align="right">
                        <Tooltip title={t('operations.automations.colOpenHint', 'Cosas que ha generado y siguen abiertas')}>
                          <span>{t('operations.automations.colOpen', 'Abiertas')}</span>
                        </Tooltip>
                      </TableCell>
                      <TableCell align="right">{t('operations.automations.colActions', 'Acciones')}</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {pagedItems.map((a) => (
                      <AutomationTableRow
                        key={a.id}
                        a={a}
                        isAdmin={isAdmin}
                        canTest={canTestRow(a)}
                        catTz={cat?.business_tz}
                        stats={batchStats.data?.items?.[String(a.id)]}
                        onEdit={() => setEditing({ ...a })}
                        onEvolve={() => void evolve(a)}
                        onHistory={() => setHistory(a)}
                        onToggle={(enabled) => void toggle(a, enabled)}
                        onDelete={() => void del(a)}
                        onProbar={() => void test(a)}
                        onChecked={() => void runChecked(a)}
                        onForce={() => setForcing(a)}
                      />
                    ))}
                  </TableBody>
                </Table>
              </TableContainer>
              <TablePagination
                component="div"
                count={filtered.length}
                page={page}
                onPageChange={(_, p) => setPage(p)}
                rowsPerPage={rowsPerPage}
                onRowsPerPageChange={(e) => setRowsPerPage(parseInt(e.target.value, 10))}
                rowsPerPageOptions={[10, 25, 50]}
                labelRowsPerPage={t('operations.automations.rowsPerPage', 'Filas por página')}
              />
            </Paper>
          ) : (
          <Stack spacing={1}>
            {filtered.map((a) => (
            <Card key={a.id} variant="outlined"
              sx={{ opacity: a.runtime_state === 'disabled' ? 0.72 : 1 }}>
              <CardContent sx={{ py: 1.5, '&:last-child': { pb: 1.5 } }}>
                {/* Header: identity (left) + current state + controls (right). */}
                <Stack direction="row" spacing={1} alignItems="flex-start">
                  <Box sx={{ flex: 1, minWidth: 0 }}>
                    <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>{a.title}</Typography>
                    <Typography variant="caption" color="text.secondary" sx={{ display: 'block' }}>{a.sentence}</Typography>
                    {htmlHasText(a.description) && (
                      <SanitizedHtml html={a.description} sx={{ mt: 0.5, fontSize: 13, color: 'text.secondary' }} />
                    )}
                  </Box>
                  {/* CURRENT STATE — the one thing the operator scans for. */}
                  <RuntimeStateChip state={a.runtime_state} />
                  <Stack direction="row" spacing={0.25} alignItems="center">
                    {isAdmin && (
                      <Tooltip title={a.enabled
                        ? t('operations.automations.switchOff', 'Desactivar')
                        : t('operations.automations.switchOn', 'Activar')}>
                        <Switch size="small" checked={!!a.enabled} onChange={(e) => void toggle(a, e.target.checked)} />
                      </Tooltip>
                    )}
                    <Tooltip title={t('operations.automations.history.open', 'Actividad y gobierno')}>
                      <IconButton size="small" onClick={() => setHistory(a)}><HistoryIcon fontSize="small" /></IconButton>
                    </Tooltip>
                    {isAdmin && (
                      <>
                        <IconButton size="small" onClick={() => setEditing({ ...a })}><EditIcon fontSize="small" /></IconButton>
                        <IconButton size="small" color="error" onClick={() => void del(a)}><DeleteIcon fontSize="small" /></IconButton>
                      </>
                    )}
                  </Stack>
                </Stack>

                {/* ZONE 1 — CONFIGURATION: when · what it does · approval · active. */}
                <CardZone label={t('operations.automations.zoneConfig', 'Configuración')}>
                  <Stack direction="row" spacing={0.5} sx={{ flexWrap: 'wrap' }} useFlexGap>
                    <Chip size="small" variant="outlined" icon={<TriggerIcon fontSize="small" />}
                      label={t('operations.automations.cfgWhen', { value: sigChip(a.signal_type), defaultValue: 'Cuándo: {{value}}' })} />
                    <Chip size="small" variant="outlined" icon={<ActionIcon fontSize="small" />}
                      label={t('operations.automations.cfgDoes', { value: reaChip(a.reaction_type), defaultValue: 'Hace: {{value}}' })} />
                    <Chip size="small" variant="outlined" color={a.requires_approval ? 'warning' : 'default'}
                      label={a.requires_approval
                        ? t('operations.automations.cfgApprovalYes', 'Con aprobación')
                        : t('operations.automations.cfgApprovalNo', 'Sin aprobación')} />
                    {!isAdmin && (
                      <Chip size="small" variant="outlined" color={a.enabled ? 'success' : 'default'}
                        label={a.enabled
                          ? t('operations.automations.stateOn', 'activa')
                          : t('operations.automations.stateOff', 'inactiva')} />
                    )}
                    {a.state?.last_error && (
                      <Tooltip title={a.state.last_error}>
                        <Chip size="small" color="error" variant="outlined" label={t('operations.automations.condError', 'error de condición')} />
                      </Tooltip>
                    )}
                  </Stack>
                  {/* Next firing — when it will act (or "disabled — will not run"). */}
                  <Box sx={{ mt: 0.75 }}>
                    <NextFireLine a={a} catTz={cat?.business_tz} />
                  </Box>
                  {isAdmin && (
                    <Stack direction="row" spacing={1} alignItems="center" sx={{ mt: 0.75 }}>
                      {/* The 3 ways to run by hand, in a tidy menu. */}
                      <RunModesMenu
                        canTest={canTestRow(a)}
                        onProbar={() => void test(a)}
                        onChecked={() => void runChecked(a)}
                        onForce={() => setForcing(a)}
                      />
                    </Stack>
                  )}
                  {testResult[a.id] && (
                    <Typography variant="caption" color={testResult[a.id].would_fire ? 'success.main' : testResult[a.id].async ? 'info.main' : 'text.secondary'} sx={{ display: 'block', mt: 0.5 }}>
                      {(testResult[a.id].evaluable === false || testResult[a.id].async)
                        ? testResult[a.id].reason
                        : (testResult[a.id].would_fire
                            ? t('operations.automations.wouldFire', { value: testResult[a.id].value, defaultValue: 'Dispararía ahora (valor: {{value}})' })
                            : t('operations.automations.wontFire', { value: testResult[a.id].value, defaultValue: 'No dispara ahora (valor: {{value}})' }))}
                    </Typography>
                  )}
                </CardZone>

                {/* ZONE 2 — LAST RUN + last 5 inline + «ver todas» (see all, modal). */}
                <CardZone label={t('operations.automations.zoneLastRun', 'Última ejecución')}>
                  <LastRunLine a={a} />
                  <Stack direction="row" spacing={1.5} sx={{ mt: 0.5 }}>
                    <Link component="button" type="button" variant="caption"
                      onClick={() => setExpandRecent((v) => (v === a.id ? null : a.id))}>
                      {expandRecent === a.id
                        ? t('operations.automations.hideRecent', 'Ocultar últimas')
                        : t('operations.automations.showRecent', 'Ver últimas 5')}
                    </Link>
                    <Link component="button" type="button" variant="caption" onClick={() => setHistory(a)}>
                      {t('operations.automations.viewAllHistory', 'Ver todas →')}
                    </Link>
                  </Stack>
                  {expandRecent === a.id && <RecentEvalsInline automationId={a.id} />}
                </CardZone>
              </CardContent>
            </Card>
            ))}
          </Stack>
          )}
        </>
      )}
      {editing && (
        <AutomationEditor
          key={editing.id ?? 'new'}
          initial={editing}
          catalog={cat}
          onClose={() => setEditing(null)}
          onSaved={() => { setEditing(null); invalidate() }}
        />
      )}
      {history && (
        <AutomationHistoryDialog automation={history} onClose={() => setHistory(null)} />
      )}
      {forcing && (
        <Dialog open onClose={() => setForcing(null)} maxWidth="xs" fullWidth>
          <DialogTitle>{t('operations.automations.forceTitle', 'Forzar ejecución')}</DialogTitle>
          <DialogContent>
            <Typography variant="body2">
              {t('operations.automations.forceBody', 'Ejecutará «{{name}}» AHORA saltándose el «solo si»: el empleado actuará aunque la condición no se cumpla. Para comprobar SIN actuar, usa «Probar».', { name: forcing.title || `#${forcing.id}` })}
            </Typography>
          </DialogContent>
          <DialogActions>
            <Button onClick={() => setForcing(null)}>{t('common.cancel', 'Cancelar')}</Button>
            <Button variant="contained" color="warning"
              onClick={() => { const a = forcing; setForcing(null); void runForce(a) }}>
              {t('operations.automations.forceConfirm', 'Forzar ahora')}
            </Button>
          </DialogActions>
        </Dialog>
      )}
      {confirmDialog}
    </Stack>
  )
}

/** Minimal, dependency-free HTML sanitizer for the automation `description`
 *  (an HTML string the operator writes and the API stores/serves). We are NOT
 *  trying to be a full sanitizer — the content is authored by the tenant's own
 *  admins — but we strip the obvious XSS vectors before it ever hits
 *  ``dangerouslySetInnerHTML``: <script>/<style>/<iframe> blocks, inline event
 *  handlers (on*=) and ``javascript:`` URLs. No new npm dependency. */
function sanitizeHtml(html: string): string {
  if (!html) return ''
  let out = String(html)
  // Drop the whole element (with its content) for the truly dangerous tags.
  out = out.replace(/<(script|style|iframe)\b[\s\S]*?<\/\1>/gi, '')
  // …and any stray/unclosed opening or closing tag of the same kind.
  out = out.replace(/<\/?(script|style|iframe)\b[^>]*>/gi, '')
  // Strip inline event handlers (onclick=, onerror=… — quoted or bare).
  out = out.replace(/\son[a-z]+\s*=\s*"[^"]*"/gi, '')
  out = out.replace(/\son[a-z]+\s*=\s*'[^']*'/gi, '')
  out = out.replace(/\son[a-z]+\s*=\s*[^\s>]+/gi, '')
  // Neutralise javascript: in href/src.
  out = out.replace(/((?:href|src)\s*=\s*)"javascript:[^"]*"/gi, '$1"#"')
  out = out.replace(/((?:href|src)\s*=\s*)'javascript:[^']*'/gi, "$1'#'")
  out = out.replace(/((?:href|src)\s*=\s*)javascript:[^\s>]+/gi, '$1"#"')
  return out
}

/** Render an (author-supplied) HTML string as sanitized, lightly-styled content.
 *  Used for the automation `description` both in the editor preview and inline in
 *  the row detail. */
function SanitizedHtml({ html, sx }: { html: string; sx?: any }) {
  return (
    <Box
      sx={{
        wordBreak: 'break-word',
        '& p': { m: 0, mb: 0.5 },
        '& p:last-child': { mb: 0 },
        '& ul, & ol': { my: 0.5, pl: 2.5 },
        '& li': { mb: 0.25 },
        '& a': { color: 'primary.main' },
        '& h1, & h2, & h3, & h4': { m: 0, mb: 0.5, fontSize: '1rem', fontWeight: 700 },
        '& code': { fontFamily: 'monospace', fontSize: '0.85em' },
        '& img': { maxWidth: '100%' },
        ...sx,
      }}
      dangerouslySetInnerHTML={{ __html: sanitizeHtml(html) }}
    />
  )
}

/** Strip tags → plain text, to decide whether an HTML value actually carries
 *  real content (the `description` field is mandatory: "<p><br></p>" is empty). */
function stripHtml(html: string): string {
  return String(html || '').replace(/<[^>]*>/g, ' ').replace(/&nbsp;/gi, ' ')
}
function htmlHasText(html: string): boolean {
  return stripHtml(html).replace(/\s+/g, ' ').trim().length > 0
}

/** Formatting toolbar for the markdown/HTML editor: each button wraps/prefixes the
 *  current textarea selection with the corresponding markdown/HTML syntax. */
function MdToolbar({ onApply }: { onApply: (kind: string) => void }) {
  const { t } = useTranslation()
  const btns: { k: string; icon: React.ReactNode; title: string }[] = [
    { k: 'bold', icon: <BoldIcon fontSize="small" />, title: t('operations.automations.mdBold', 'Negrita') },
    { k: 'italic', icon: <ItalicIcon fontSize="small" />, title: t('operations.automations.mdItalic', 'Cursiva') },
    { k: 'h2', icon: <HeadingIcon fontSize="small" />, title: t('operations.automations.mdHeading', 'Encabezado') },
    { k: 'ul', icon: <BulletListIcon fontSize="small" />, title: t('operations.automations.mdBullet', 'Lista') },
    { k: 'ol', icon: <NumberListIcon fontSize="small" />, title: t('operations.automations.mdNumbered', 'Lista numerada') },
    { k: 'link', icon: <MdLinkIcon fontSize="small" />, title: t('operations.automations.mdLink', 'Enlace') },
    { k: 'code', icon: <CodeIcon fontSize="small" />, title: t('operations.automations.mdCode', 'Código') },
  ]
  return (
    <Stack direction="row" spacing={0.25} sx={{ mb: 1, flexWrap: 'wrap' }} useFlexGap>
      {btns.map((b) => (
        <Tooltip key={b.k} title={b.title}>
          <IconButton size="small" onMouseDown={(e) => e.preventDefault()} onClick={() => onApply(b.k)}>{b.icon}</IconButton>
        </Tooltip>
      ))}
    </Stack>
  )
}

/** A multiline TextField with a "maximize" icon that opens the same value in a
 *  large modal — for long free-text fields (NL conditions, instructions, SQL)
 *  the inline 2–3 rows are too cramped to write/read comfortably.
 *
 *  When ``markdown`` is set: the inline field gets a "Vista previa" toggle that
 *  renders the value formatted right on the main screen, and the maximize modal
 *  adds a style toolbar (bold/italic/heading/list/link/code) + a live preview.
 *  Markdown renders with GFM + soft line-breaks (same as the chat).
 *
 *  When ``html`` is set: same editor + preview machinery, but the value is an
 *  HTML string and the preview renders it SANITIZED (see ``SanitizedHtml``); the
 *  toolbar inserts HTML tags instead of markdown. Used for the automation
 *  ``description``. ``error`` paints the field/preview border red (mandatory). */
function ExpandableTextField({ label, value, onChange, placeholder, helperText, minRows = 2, mono = false, markdown = false, html = false, error = false }: {
  label: string; value: string; onChange: (v: string) => void
  placeholder?: string; helperText?: string; minRows?: number; mono?: boolean
  markdown?: boolean; html?: boolean; error?: boolean
}) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const [preview, setPreview] = useState(false)
  // "rich" = has a formatted preview toggle (markdown OR html).
  const rich = markdown || html
  // Default to the formatted inline preview when the rich field already
  // has content (editing an existing standing order / a template just filled
  // it) — an empty field opens in edit mode so you can start typing.
  const [inlinePreview, setInlinePreview] = useState(() => rich && Boolean((value || '').trim()))
  const editorRef = useRef<HTMLTextAreaElement | null>(null)
  const monoSx = mono ? { fontFamily: 'monospace', fontSize: 13 } : undefined
  const mdHint = markdown
    ? t('operations.automations.markdownHint', 'Admite formato markdown (encabezados, listas, pasos) — como un skill.')
    : html
      ? t('operations.automations.htmlHint', 'Admite HTML básico (negrita, cursiva, listas, enlaces).')
      : undefined
  const helper = [helperText, mdHint].filter(Boolean).join(' ')

  // Wrap / prefix the current selection in the maximized editor with markdown
  // (or HTML tags when ``html`` is set).
  const applyMd = (kind: string) => {
    const el = editorRef.current
    if (!el) return
    const start = el.selectionStart ?? value.length
    const end = el.selectionEnd ?? value.length
    const sel = value.slice(start, end)
    const before = value.slice(0, start)
    const after = value.slice(end)
    const phText = t('operations.automations.mdPhText', 'texto')
    const phHeading = t('operations.automations.mdPhHeading', 'Título')
    const phItem = t('operations.automations.mdPhItem', 'elemento')
    let out = sel
    if (html) {
      if (kind === 'bold') out = `<b>${sel || phText}</b>`
      else if (kind === 'italic') out = `<i>${sel || phText}</i>`
      else if (kind === 'code') out = `<code>${sel || phText}</code>`
      else if (kind === 'link') out = `<a href="https://">${sel || phText}</a>`
      else if (kind === 'h2') out = `<h3>${sel || phHeading}</h3>`
      else if (kind === 'ul') out = `<ul>\n${(sel || phItem).split('\n').map((l) => `  <li>${l}</li>`).join('\n')}\n</ul>`
      else if (kind === 'ol') out = `<ol>\n${(sel || phItem).split('\n').map((l) => `  <li>${l}</li>`).join('\n')}\n</ol>`
    } else {
      if (kind === 'bold') out = `**${sel || phText}**`
      else if (kind === 'italic') out = `*${sel || phText}*`
      else if (kind === 'code') out = `\`${sel || phText}\``
      else if (kind === 'link') out = `[${sel || phText}](https://)`
      else if (kind === 'h2') out = (sel || phHeading).split('\n').map((l) => `## ${l}`).join('\n')
      else if (kind === 'ul') out = (sel || phItem).split('\n').map((l) => `- ${l}`).join('\n')
      else if (kind === 'ol') out = (sel || phItem).split('\n').map((l, i) => `${i + 1}. ${l}`).join('\n')
    }
    onChange(`${before}${out}${after}`)
    requestAnimationFrame(() => {
      try { el.focus(); const pos = before.length + out.length; el.setSelectionRange(pos, pos) } catch { /* noop */ }
    })
  }

  const renderPreview = (minHeight: number) => (
    <Box className={html ? undefined : 'markdown-content'} sx={{ minHeight, p: 1.5, border: '1px solid', borderColor: error ? 'error.main' : 'divider', borderRadius: 1 }}>
      {value.trim()
        ? (html
            ? <SanitizedHtml html={value} />
            : <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]}>{value}</ReactMarkdown>)
        : <Typography variant="body2" color="text.secondary">{placeholder || t('operations.automations.mdEmpty', 'Sin contenido todavía.')}</Typography>}
    </Box>
  )

  return (
    <>
      {rich && inlinePreview ? (
        <Box>
          <Stack direction="row" spacing={0.5} alignItems="center" sx={{ mb: 0.25 }}>
            <Typography variant="caption" color={error ? 'error' : 'text.secondary'} sx={{ flex: 1 }}>{label}</Typography>
            <Tooltip title={t('operations.automations.mdEdit', 'Editar')}>
              <IconButton size="small" onClick={() => setInlinePreview(false)}><EditIcon fontSize="inherit" /></IconButton>
            </Tooltip>
            <Tooltip title={t('common.expand', 'Ampliar')}>
              <IconButton size="small" onClick={() => { setPreview(true); setOpen(true) }}><ExpandFieldIcon fontSize="inherit" /></IconButton>
            </Tooltip>
          </Stack>
          <Box onClick={() => setInlinePreview(false)} sx={{ cursor: 'text' }}>{renderPreview(minRows * 26)}</Box>
          {helper && <Typography variant="caption" color={error ? 'error' : 'text.secondary'} sx={{ mt: 0.5, display: 'block' }}>{helper}</Typography>}
        </Box>
      ) : (
        <TextField
          size="small" fullWidth multiline minRows={minRows} error={error}
          label={label} placeholder={placeholder} helperText={helper || undefined}
          value={value} onChange={(e) => onChange(e.target.value)}
          InputProps={{
            sx: monoSx,
            endAdornment: (
              <InputAdornment position="end" sx={{ alignSelf: 'flex-start', mt: 0.75 }}>
                {rich && (
                  <Tooltip title={t('operations.automations.mdPreview', 'Vista previa')}>
                    <IconButton size="small" onClick={() => setInlinePreview(true)}>
                      <PreviewIcon fontSize="inherit" />
                    </IconButton>
                  </Tooltip>
                )}
                <Tooltip title={t('common.expand', 'Ampliar')}>
                  <IconButton size="small" edge="end" onClick={() => { setPreview(false); setOpen(true) }}>
                    <ExpandFieldIcon fontSize="inherit" />
                  </IconButton>
                </Tooltip>
              </InputAdornment>
            ),
          }}
        />
      )}
      <Dialog open={open} onClose={() => setOpen(false)} maxWidth="md" fullWidth>
        <DialogTitle sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
          <Box sx={{ flex: 1 }}>{label}</Box>
          {rich && (
            <ToggleButtonGroup size="small" exclusive value={preview ? 'preview' : 'edit'} onChange={(_, v) => v && setPreview(v === 'preview')}>
              <ToggleButton value="edit" sx={{ textTransform: 'none' }}>{t('operations.automations.mdEdit', 'Editar')}</ToggleButton>
              <ToggleButton value="preview" sx={{ textTransform: 'none' }}>{t('operations.automations.mdPreview', 'Vista previa')}</ToggleButton>
            </ToggleButtonGroup>
          )}
        </DialogTitle>
        <DialogContent>
          {rich && preview ? (
            <Box sx={{ mt: 1 }}>{renderPreview(200)}</Box>
          ) : (
            <Box sx={{ mt: 1 }}>
              {rich && <MdToolbar onApply={applyMd} />}
              <TextField
                autoFocus fullWidth multiline minRows={16}
                placeholder={placeholder} value={value}
                onChange={(e) => onChange(e.target.value)}
                inputRef={editorRef}
                InputProps={{ sx: monoSx }}
              />
            </Box>
          )}
          {helper && (
            <Typography variant="caption" color="text.secondary" sx={{ mt: 1, display: 'block' }}>
              {helper}
            </Typography>
          )}
        </DialogContent>
        <DialogActions>
          <Button variant="contained" onClick={() => setOpen(false)}>{t('common.done', 'Listo')}</Button>
        </DialogActions>
      </Dialog>
    </>
  )
}

/** Full firing trail of one automation: every time it fired and what the
 *  employee did about it, each entry deep-linking to the run (the existing
 *  forensic view: prompt, tools, outputs, cost) and the event. */
/** One section of the panel: title, the HELP always visible (not a tooltip) and its
 *  content in its own card.
 *
 *  The help is not decoration: the panel mixes five different questions
 *  ("how much has it worked", "what has it left", "when did it act", "why is it silent",
 *  "the fine trace") and without a sentence saying what is being looked at, the correct
 *  figures get misread — which is exactly what happened. A tooltip does not work:
 *  what needs explaining is exactly what nobody knows they should ask. */
function PanelSection({ title, help, action, children }: {
  title: string; help?: string; action?: React.ReactNode; children: React.ReactNode
}) {
  return (
    <Paper variant="outlined" sx={{ p: 1.75, borderRadius: 2 }}>
      <Stack direction="row" spacing={1} alignItems="flex-start" sx={{ mb: 1.25 }}>
        <Box sx={{ flex: 1, minWidth: 0 }}>
          <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>{title}</Typography>
          {help && (
            <Typography variant="caption" color="text.secondary"
              sx={{ display: 'block', mt: 0.25 }}>{help}</Typography>
          )}
        </Box>
        {action}
      </Stack>
      {children}
    </Paper>
  )
}

function AutomationHistoryDialog({ automation, onClose }: { automation: AutomationRow; onClose: () => void }) {
  const { t } = useTranslation()
  const [maximized, setMaximized] = useState(false)
  const q = useQuery({
    queryKey: ['automation-history', automation.id],
    queryFn: () => apiFetch(`/api/agent/automations/${automation.id}/history`).then(async (r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json()
    }),
    refetchInterval: 30_000,
  })
  const items: any[] = q.data?.items ?? []
  const runColor = (s?: string): 'success' | 'error' | 'info' | 'default' => {
    const v = (s || '').toUpperCase()
    if (v === 'COMPLETED') return 'success'
    if (v === 'FAILED' || v === 'ERROR') return 'error'
    if (v === 'RUNNING') return 'info'
    return 'default'
  }
  return (
    <Dialog open onClose={onClose} maxWidth="lg" fullWidth fullScreen={maximized}>
      <DialogTitle sx={{ display: 'flex', alignItems: 'flex-start', gap: 1 }}>
        <Box sx={{ flex: 1, minWidth: 0 }}>
          <Typography variant="h6" sx={{ fontWeight: 700, lineHeight: 1.3 }}>
            {automation.title}
          </Typography>
          <Typography variant="caption" color="text.secondary">
            {t('operations.automations.history.subtitle',
              'Trabajo autónomo: cuánto ha trabajado, qué ha dejado, cuándo actuó y por qué no siempre avisa.')}
          </Typography>
        </Box>
        <Tooltip title={maximized ? t('common.restoreSize', 'Restaurar tamaño') : t('common.maximize', 'Maximizar')}>
          <IconButton size="small" onClick={() => setMaximized((v) => !v)}
            aria-label={maximized ? t('common.restoreSize', 'Restaurar tamaño') : t('common.maximize', 'Maximizar')}>
            {maximized ? <FullscreenExitIcon /> : <FullscreenIcon />}
          </IconButton>
        </Tooltip>
      </DialogTitle>
      <DialogContent dividers>
        <Stack spacing={2}>
        {/* 1. The numbers and what it has left (governance). 2. When it acted. 3. The fine trace.
            That order tells a story: how much it works → what it produces → when → the detail. */}
        <AutomationGovernanceSection automationId={automation.id} onNavigate={onClose} />

        <PanelSection
          title={t('operations.automations.history.firesTitle', 'Cuándo ha actuado')}
          help={t('operations.automations.history.count', { count: automation.state?.fire_count ?? 0, defaultValue: 'Ha saltado {{count}} vez/veces en total. Pincha en cada una para ver todo lo que hizo.' })}
        >
        {q.isLoading ? (
          <Box sx={{ display: 'flex', justifyContent: 'center', py: 3 }}><CircularProgress size={24} /></Box>
        ) : q.isError ? (
          <Alert severity="error">{t('operations.loadError', 'No se pudo cargar. Reintenta en unos segundos.')}</Alert>
        ) : items.length === 0 ? (
          <Alert severity="info">{t('operations.automations.history.empty', 'Todavía no ha saltado ninguna vez.')}</Alert>
        ) : (
          <Stack spacing={1}>
            {items.map((h) => {
              const apSt = h.approval_status as string | undefined
              const apColor: 'warning' | 'success' | 'default' | 'error' =
                apSt === 'pending' ? 'warning' : apSt === 'rejected' ? 'error'
                  : (apSt === 'approved' || apSt === 'executed') ? 'success' : 'default'
              return (
              <Card key={h.id ?? h.event_id ?? `appr${h.approval_id}`} variant="outlined">
                <CardContent sx={{ py: 1, '&:last-child': { pb: 1 } }}>
                  <Stack direction="row" spacing={1} alignItems="center" justifyContent="space-between">
                    <Typography variant="body2">{h.fired_at ? formatDateTime(h.fired_at) : '—'}</Typography>
                    <Stack direction="row" spacing={0.75} alignItems="center" flexWrap="wrap" useFlexGap>
                      {h.requires_approval && (
                        <Chip size="small" variant="outlined" color="warning"
                          label={t('operations.automations.needsApproval', 'requiere aprobación')} />
                      )}
                      {apSt && (
                        <Chip size="small" color={apColor}
                          label={t(`operations.automations.history.approval.${apSt}`, { defaultValue: apSt })} />
                      )}
                      {h.run_status && <Chip size="small" color={runColor(h.run_status)} label={h.run_status} />}
                    </Stack>
                  </Stack>
                  {h.summary && (
                    <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5, whiteSpace: 'pre-line' }}>
                      {h.summary}
                    </Typography>
                  )}
                  <Stack direction="row" spacing={2} sx={{ mt: 0.5 }} flexWrap="wrap" useFlexGap>
                    {h.scheduled_item_id && (
                      <Link component={RouterLink} to={`/encargos/${h.scheduled_item_id}`} variant="caption"
                        onClick={onClose}
                        sx={{ display: 'inline-flex', alignItems: 'center', gap: 0.25, fontWeight: 600 }}>
                        <EncargoIcon fontSize="inherit" />
                        {t('operations.automations.history.viewEncargo', 'Ver encargo →')}
                      </Link>
                    )}
                    {h.run_id && (
                      <Link component={RouterLink} to={`/runs/${h.run_id}`} variant="caption">
                        {h.requires_approval
                          ? t('operations.automations.history.viewExecution', 'Ver ejecución →')
                          : t('operations.automations.history.viewRun', 'Ver qué hizo →')}
                      </Link>
                    )}
                    {h.approval_id && (
                      <Link component={RouterLink} to={`/approvals?id=${h.approval_id}`} variant="caption"
                        onClick={onClose}>
                        {t('operations.automations.history.viewApproval', 'Ver aprobación')}
                      </Link>
                    )}
                    {h.event_id && (
                      <Link component={RouterLink} to={`/events/${h.event_id}`} variant="caption">
                        {t('operations.automations.history.viewEvent', 'Ver evento')}
                      </Link>
                    )}
                  </Stack>
                </CardContent>
              </Card>
              )
            })}
          </Stack>
        )}
        </PanelSection>

        <AutomationEvalsSection automationId={automation.id} />
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>{t('common.close', 'Cerrar')}</Button>
      </DialogActions>
    </Dialog>
  )
}

/** «Gobierno» (Governance): the funnel, the cost, what it has generated and the memory it keeps
 *  so as not to repeat itself — all within the range the operator picks (v7.140.0).
 *
 *  It is the answer to "is this standing order earning its keep?", which cannot
 *  be answered with "it is active": you need to see how many times it checked,
 *  how many passed the "only if", how many actually fired, how many the
 *  anti-repetition held back and what it has cost. And, because forgetting the anti-repetition can
 *  trigger an avalanche of notifications, re-arms are admin-only and leave a trace. */
function AutomationGovernanceSection({ automationId, onNavigate }: {
  automationId: number; onNavigate?: () => void
}) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const [range, setRange] = useState<DateRange>(DEFAULT_RANGE)
  const [err, setErr] = useState<string | null>(null)
  const me = useQuery({
    queryKey: ['me-admin'],
    queryFn: () => apiFetch('/api/agent/users/me').then((r) => (r.ok ? r.json() : { is_admin: false })),
    staleTime: 300_000,
  })
  const isAdmin = !!me.data?.is_admin

  // Its own queryKey WITH the range, high staleTime and NO polling: these are
  // aggregates read on demand, not a live dashboard. Enriching the hot
  // /automations endpoint (refetch every 30 s, drives the switches) with them
  // would make every open browser recompute them constantly.
  const stats = useQuery({
    queryKey: ['automation-stats', automationId, range],
    queryFn: async () => {
      const r = await apiFetch(`/api/agent/automations/${automationId}/stats?${rangeParams(range)}`)
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      return r.json()
    },
    staleTime: 120_000,
    refetchInterval: false,
  })
  const memory = useQuery({
    queryKey: ['automation-memory', automationId],
    queryFn: async () => {
      const r = await apiFetch(`/api/agent/automations/${automationId}/memory?limit=50`)
      if (!r.ok) return { items: [], counts: {}, total: 0 }
      return r.json()
    },
    staleTime: 60_000,
    refetchInterval: false,
    retry: false,
  })

  const act = async (path: string, method: string) => {
    try {
      setErr(null)
      const r = await apiFetch(path, { method })
      if (!r.ok) {
        const b = await r.json().catch(() => null)
        throw new Error(b?.detail || `HTTP ${r.status}`)
      }
      qc.invalidateQueries({ queryKey: ['automation-memory', automationId] })
      qc.invalidateQueries({ queryKey: ['automation-evals', automationId] })
    } catch (e: any) { setErr(String(e?.message ?? e)) }
  }

  const f = stats.data?.funnel ?? {}
  const produced: Record<string, any> = stats.data?.produced?.items ?? {}
  const watchItems: any[] = memory.data?.items ?? []
  const counts: Record<string, number> = memory.data?.counts ?? {}

  // The funnel, in the order it is read: of everything it looked at, how much met, and of
  // that, what ended up as a notification — and the rest WITH A NAME. «Ya avisado» (already notified) is the piece that
  // was missing: without it there was a gap of hundreds of unexplained checks and
  // the operator concluded, rightly, that their employee was keeping things quiet.
  const FUNNEL: { key: string; label: string; hint: string }[] = [
    { key: 'checks', label: t('operations.autoGov.checks', 'Comprobaciones'), hint: t('operations.autoGov.checksHint', 'Veces que miró si tocaba actuar. No cuesta nada: no llama al modelo.') },
    { key: 'met', label: t('operations.autoGov.met', 'Cumplían la condición'), hint: t('operations.autoGov.metHint', 'De esas comprobaciones, en cuántas se cumplía el «solo si».') },
    { key: 'fired', label: t('operations.autoGov.fired', 'Te avisó'), hint: t('operations.autoGov.firedHint', 'Cuántas veces actuó de verdad (aquí es donde se genera el trabajo y el coste).') },
    { key: 'held', label: t('operations.autoGov.held', 'Ya te lo había dicho'), hint: t('operations.autoGov.heldHint', 'Cumplía la condición, pero no había nada NUEVO: ya te había avisado de eso. Es el anti-repetición trabajando, no un aviso perdido.') },
    { key: 'silenced', label: t('operations.autoGov.silenced', 'En pausa'), hint: t('operations.autoGov.silencedHint', 'Cumplía, pero estaba silenciada o en enfriamiento, así que no avisó.') },
    { key: 'errors', label: t('operations.autoGov.errors', 'No pudo comprobar'), hint: t('operations.autoGov.errorsHint', 'La comprobación falló (un timeout, una herramienta caída): no supo si tocaba actuar.') },
  ]

  return (
    <Stack spacing={2}>
    <PanelSection
      title={t('operations.autoGov.title', 'Cuánto ha trabajado y qué ha costado')}
      help={t('operations.autoGov.help', 'Se lee de izquierda a derecha: de todo lo que miró, en cuánto se cumplía la condición y de eso cuánto acabó en aviso. Lo que no acaba en aviso tiene nombre — ya avisado, en pausa o error — para que no queden huecos sin explicar.')}
      action={<DateRangeFilter value={range} onChange={setRange} dense />}
    >
      {err && <Alert severity="error" sx={{ mb: 1 }} onClose={() => setErr(null)}>{err}</Alert>}

      {stats.isLoading ? (
        <Box sx={{ display: 'flex', justifyContent: 'center', py: 2 }}><CircularProgress size={20} /></Box>
      ) : stats.isError ? (
        <Alert severity="info">
          {t('operations.autoGov.unavailable', 'Este empleado todavía no publica estadísticas de gobierno (necesita una versión más reciente).')}
        </Alert>
      ) : (<>
        <Grid container spacing={1}>
          {FUNNEL.map((it) => (
            <Grid item xs={6} sm={4} md={2} key={it.key}>
              <Tooltip title={it.hint}>
                <Paper variant="outlined" sx={{ p: 1, borderRadius: 1.5, textAlign: 'center', height: '100%' }}>
                  <Typography variant="h6" sx={{ fontWeight: 700, lineHeight: 1.2 }}>
                    {f[it.key] ?? 0}
                  </Typography>
                  <Typography variant="caption" color="text.secondary">{it.label}</Typography>
                </Paper>
              </Tooltip>
            </Grid>
          ))}
        </Grid>
        {/* The same information, in one sentence. It is what really gets read. */}
        {(f.checks ?? 0) > 0 && (
          <Typography variant="body2" sx={{ mt: 1.25 }}>
            {t('operations.autoGov.sentence', {
              defaultValue: 'De {{checks}} comprobaciones, en {{met}} se cumplía la condición y te avisó {{fired}} vez/veces. De las demás, {{held}} eran cosas de las que ya te había avisado.',
              checks: f.checks ?? 0, met: f.met ?? 0, fired: f.fired ?? 0, held: f.held ?? 0,
            })}
          </Typography>
        )}
        <Stack direction="row" spacing={0.75} flexWrap="wrap" useFlexGap sx={{ mt: 1 }}>
          <Chip size="small" variant="outlined"
            label={t('operations.autoGov.cost', 'Coste: {{usd}} USD', { usd: (f.cost_usd ?? 0).toFixed?.(4) ?? f.cost_usd ?? 0 })} />
          <Chip size="small" variant="outlined"
            label={t('operations.autoGov.tokens', 'Tokens: {{i}} / {{o}}', { i: f.tokens_in ?? 0, o: f.tokens_out ?? 0 })} />
          {f.fired > 0 && (f.runs ?? 0) < f.fired && (
            <Tooltip title={t('operations.autoGov.deterministicHint', 'Algunos disparos son deterministas: hacen su trabajo sin llamar al modelo, así que cuentan como disparo con coste 0.') as string}>
              <Chip size="small" variant="outlined" color="success"
                label={t('operations.autoGov.deterministic', '{{n}} disparos sin coste de modelo', { n: f.fired - (f.runs ?? 0) })} />
            </Tooltip>
          )}
          {f.rollup_from && (
            <Tooltip title={t('operations.autoGov.rollupHint', 'El detalle fila a fila se conserva 14 días; los totales anteriores vienen del resumen diario, que no se borra.') as string}>
              <Chip size="small" variant="outlined"
                label={t('operations.autoGov.rollup', 'totales resumidos desde {{day}}', { day: f.rollup_from })} />
            </Tooltip>
          )}
        </Stack>

      </>)}
    </PanelSection>

    {/* What it has left behind, with the name THIS employee gives each thing and
        a link that leads exactly to those rows (not to the whole list). */}
    <PanelSection
      title={t('operations.autoGov.produced', 'Lo que ha generado')}
      help={t('operations.autoGov.producedHelp', 'Lo que existe hoy porque esta orden actuó, desde el principio (esto no depende del rango de fechas de arriba). «Sin cerrar» es lo que todavía espera a una persona.')}
    >
      {Object.keys(produced).length === 0 ? (
        <Typography variant="body2" color="text.secondary">
          {t('operations.autoGov.producedEmpty', 'Todavía no ha generado nada trazable.')}
        </Typography>
      ) : (
        <Stack spacing={0.75}>
          {Object.entries(produced).map(([kind, v]: [string, any]) => {
            // The name and the route come from the employee's manifest (the backend
            // resolves them): a generic label like «Avisos» and an invented route like
            // /domain/alertas do not exist in its vocabulary and the link died.
            const label = v.label || t(`operations.autoGov.kind.${kind}`, kind)
            const base = v.path as string | undefined
            const link = (open: boolean) => base
              ? `${base}?created_by_automation=${automationId}${open ? '&only_open=1' : ''}`
              : null
            const all = link(false); const openOnes = link(true)
            return (
              <Stack key={kind} direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
                <Typography variant="body2" sx={{ fontWeight: 600, minWidth: 180 }}>{label}</Typography>
                {all
                  ? <Link component={RouterLink} to={all} variant="body2" onClick={onNavigate}>
                      {t('operations.autoGov.producedAll', 'ver los {{n}} que creó →', { n: v.total ?? 0 })}
                    </Link>
                  : <Typography variant="body2" color="text.secondary">
                      {t('operations.autoGov.producedCount', '{{n}} creados', { n: v.total ?? 0 })}
                    </Typography>}
                {(v.open ?? 0) > 0 && (openOnes
                  ? <Link component={RouterLink} to={openOnes} variant="body2" onClick={onNavigate}
                      sx={{ fontWeight: 700 }}>
                      {t('operations.autoGov.producedOpen', '{{n}} sin cerrar →', { n: v.open })}
                    </Link>
                  : <Chip size="small" color="warning" variant="outlined"
                      label={t('operations.autoGov.producedOpenPlain', '{{n}} sin cerrar', { n: v.open })} />)}
              </Stack>
            )
          })}
        </Stack>
      )}
    </PanelSection>

    {/* The anti-repetition memory: finally visible, and deletable. */}
    <PanelSection
      title={t('operations.autoGov.memory', 'Por qué no te avisa dos veces de lo mismo')}
      help={t('operations.autoGov.memoryHint', 'Apunta cada cosa de la que ya te avisó. Mientras siga apuntada no vuelve a avisarte de ella; en cuanto desaparece del mundo real, la da por resuelta y la olvida. Si borras un ítem a mano, volverá a avisarte de él en la próxima comprobación — que es justo lo que quieres cuando algo se quedó atascado.')}
    >
      {watchItems.length > 0 && (
        <Stack direction="row" spacing={0.75} flexWrap="wrap" useFlexGap sx={{ mb: 1 }}>
          {(['open', 'recurring', 'resolved'] as const).map((st) => (
            <Tooltip key={st} title={t(`operations.autoGov.watchStateHint.${st}`, {
              defaultValue: {
                open: 'Te avisó de esto y sigue ahí: no volverá a avisarte mientras siga así.',
                recurring: 'Ya había desaparecido y ha vuelto: por eso te avisó otra vez.',
                resolved: 'Dejó de aparecer, así que lo da por resuelto.',
              }[st],
            })}>
              <Chip size="small" variant="outlined"
                label={`${t(`operations.autoGov.watchState.${st}`, st)}: ${counts[st] ?? 0}`} />
            </Tooltip>
          ))}
        </Stack>
      )}
      {isAdmin && (
        <Stack direction="row" spacing={0.75} flexWrap="wrap" useFlexGap sx={{ mb: 1 }}>
          <Button size="small" sx={{ textTransform: 'none' }}
            onClick={() => void act(`/api/agent/automations/${automationId}/reset-cooldown`, 'POST')}>
            {t('operations.autoGov.rearm', 'Rearmar (quitar enfriamiento)')}
          </Button>
          <Button size="small" sx={{ textTransform: 'none' }}
            onClick={() => void act(`/api/agent/automations/${automationId}/silence`, 'DELETE')}>
            {t('operations.autoGov.unsilence', 'Quitar silencio temporal')}
          </Button>
          <Button size="small" color="error" sx={{ textTransform: 'none' }}
            disabled={!watchItems.length}
            onClick={() => void act(`/api/agent/automations/${automationId}/memory`, 'DELETE')}>
            {t('operations.autoGov.forgetAll', 'Olvidar todo')}
          </Button>
        </Stack>
      )}
      {watchItems.length === 0 ? (
        <Typography variant="body2" color="text.secondary">
          {t('operations.autoGov.memoryEmpty', 'Ahora mismo no tiene nada apuntado: la próxima novedad que encuentre te la contará.')}
        </Typography>
      ) : (
        <Stack spacing={0.5}>
          {watchItems.slice(0, 12).map((it) => (
            <Stack key={it.item_key} direction="row" spacing={1} alignItems="center"
              sx={{ border: 1, borderColor: 'divider', borderRadius: 1, px: 1, py: 0.5 }}>
              <Box sx={{ flex: 1, minWidth: 0 }}>
                <Typography variant="caption" sx={{ fontWeight: 600, wordBreak: 'break-all' }}>
                  {it.item_key}
                </Typography>
                <Typography variant="caption" color="text.secondary" display="block">
                  {t('operations.autoGov.watchSeen', 'visto {{n}}×', { n: it.times_seen })}
                  {it.last_seen_at ? ` · ${formatDateTime(it.last_seen_at)}` : ''}
                  {it.fired_at ? ` · ${t('operations.autoGov.watchFired', 'avisó')}` : ''}
                </Typography>
              </Box>
              <Chip size="small" variant="outlined"
                label={String(t(`operations.autoGov.watchState.${it.state}`, it.state))} />
              {isAdmin && (
                <Tooltip title={t('operations.autoGov.forgetOne', 'Olvidar este ítem') as string}>
                  <IconButton size="small" color="error"
                    onClick={() => void act(
                      `/api/agent/automations/${automationId}/memory?item_key=${encodeURIComponent(it.item_key)}`,
                      'DELETE')}>
                    <DeleteIcon fontSize="small" />
                  </IconButton>
                </Tooltip>
              )}
            </Stack>
          ))}
        </Stack>
      )}
    </PanelSection>
    </Stack>
  )
}

function fmtEvalDetail(d: any): string {
  if (!d || typeof d !== 'object') return ''
  const parts: string[] = []
  if (d.watch && typeof d.watch === 'object') {
    const w = d.watch
    const bits = [
      w.fresh ? `${w.fresh} sin avisar antes` : '',
      w.escalated ? `${w.escalated} han empeorado` : '',
      w.resolved ? `${w.resolved} ya resueltos` : '',
      w.ongoing ? `${w.ongoing} seguían igual` : '',
    ].filter(Boolean)
    if (bits.length) parts.push(bits.join(', '))
  }
  if (Array.isArray(d.checks)) {
    parts.push(d.checks.map((c: any) =>
      `${c.name || 'check'}=${JSON.stringify(c.value)}${c.met ? ' ✓' : ' ✗'}`).join('; '))
  }
  if (d.value !== undefined && d.value !== null) parts.push(`valor: ${d.value}`)
  return parts.join(' · ')
}

// ── "Probar la condición" (Test the condition, `checks` mode) — rich panel ────────
// Operators that COMPARE the extracted value against a threshold: with one of
// these an empty/null threshold means the check compares against nothing (mis-
// configured). The backend already rejects it on save, but the test panel
// explains it anyway if it shows up in an old rule.
const THRESHOLD_OPS = ['gt', 'lt', 'gte', 'lte', 'eq', 'contains', 'not_contains', 'no_activity']
function checkThresholdMissing(c: any): boolean {
  if (!c || !THRESHOLD_OPS.includes(c.op)) return false
  const th = c.threshold
  return th === '' || th === null || th === undefined
}

// One-line, size-capped JSON for showing arguments / samples without blowing up a card.
function compactJson(v: any, max = 300): string {
  if (v === undefined) return '—'
  let s: string
  try { s = typeof v === 'string' ? v : JSON.stringify(v) } catch { s = String(v) }
  if (s === undefined || s === null) s = String(v)
  return s.length > max ? `${s.slice(0, max)}…` : s
}

// Small monospace code chip for a value / JSON payload.
function Mono({ children }: { children: React.ReactNode }) {
  return (
    <Box component="code" sx={{
      fontFamily: 'monospace', fontSize: 12, display: 'block', whiteSpace: 'pre-wrap',
      wordBreak: 'break-word', bgcolor: (th) => alpha(th.palette.text.primary, 0.04),
      borderRadius: 0.5, px: 0.75, py: 0.25,
    }}>{children}</Box>
  )
}

// Label + value row inside a check card (avoids keyed fragments so no React import).
function DetailRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <Stack direction="row" spacing={1.25} sx={{ alignItems: 'baseline' }}>
      <Typography variant="caption" color="text.secondary" sx={{ fontWeight: 600, minWidth: 116, flexShrink: 0 }}>{label}</Typography>
      <Box sx={{ minWidth: 0, flex: 1 }}>{children}</Box>
    </Stack>
  )
}

/** Rich render of the `evaluate-condition` response for `checks` mode: the overall
 *  verdict + how any/all combined into it (with the short-circuit note), then one
 *  card per check showing the arguments sent, the shape/sample the tool returned,
 *  the extraction path, the extracted value, the comparator + threshold, a
 *  pass/fail chip and the human sentence. Errors and empty-threshold
 *  misconfigurations are surfaced explicitly. */
function ChecksResultPanel({ result }: { result: any }) {
  const { t } = useTranslation()
  const checks: any[] = Array.isArray(result?.checks) ? result.checks : []
  const shortCircuited = typeof result?.evaluated === 'number'
    && typeof result?.total_checks === 'number'
    && result.evaluated < result.total_checks
  return (
    <Stack spacing={1} sx={{ mt: 1 }}>
      {/* Overall verdict — how ANY/ALL combined the checks into the result. */}
      {result?.explain && (
        <Typography variant="body2" sx={{ fontWeight: 600 }}>{result.explain}</Typography>
      )}
      {shortCircuited && (
        <Typography variant="caption" color="text.secondary">
          {t('operations.automations.checksShortCircuit', {
            defaultValue: 'Se evaluaron {{n}} de {{total}} comprobaciones (se cortó en cuanto el resultado quedó decidido).',
            n: result.evaluated, total: result.total_checks,
          })}
        </Typography>
      )}
      {result?.error && <Alert severity="error" sx={{ py: 0.5 }}>{result.error}</Alert>}
      {checks.length === 0 && !result?.error && (
        <Typography variant="caption" color="text.secondary">
          {t('operations.automations.checksNone', 'No hay comprobaciones que mostrar.')}
        </Typography>
      )}
      {checks.map((c, i) => {
        const missing = checkThresholdMissing(c)
        const noThresholdOp = ['non_empty', 'empty', 'no_activity'].includes(c.op)
        return (
          <Card key={i} variant="outlined" sx={{
            borderColor: (th) => c.error
              ? alpha(th.palette.error.main, 0.5)
              : c.met ? alpha(th.palette.success.main, 0.5) : 'divider',
          }}>
            <CardContent sx={{ py: 1, '&:last-child': { pb: 1 } }}>
              <Stack direction="row" spacing={1} alignItems="center" justifyContent="space-between">
                <Typography variant="body2" sx={{ fontWeight: 700, minWidth: 0 }}>
                  {c.name || t('operations.automations.checkUnnamed', 'Comprobación sin nombre')}
                </Typography>
                <Chip size="small"
                  color={c.error ? 'error' : c.met ? 'success' : 'default'}
                  icon={c.error ? <ErrorOutlineIcon fontSize="small" /> : c.met ? <OkIcon fontSize="small" /> : undefined}
                  label={c.error
                    ? t('operations.automations.checkFail', 'Error')
                    : c.met ? t('operations.automations.checkPass', 'Se cumple') : t('operations.automations.checkNo', 'No se cumple')} />
              </Stack>
              <Stack direction="row" spacing={0.5} sx={{ mt: 0.5 }} flexWrap="wrap" useFlexGap>
                {c.connector && <Chip size="small" variant="outlined" icon={<ConnectorIcon fontSize="small" />} label={c.connector} />}
                {c.tool && <Chip size="small" variant="outlined" icon={<CodeIcon fontSize="small" />} label={c.tool} />}
              </Stack>
              {missing && (
                <Typography variant="caption" color="warning.main" sx={{ display: 'block', mt: 0.5 }}>
                  {t('operations.automations.checkThresholdEmpty', 'Umbral vacío: esta comprobación no compara nada (elige un valor o cambia el operador).')}
                </Typography>
              )}
              {c.error && (
                <Alert severity="error" sx={{ mt: 0.75, py: 0 }}>
                  {c.error}{c.explain ? ` — ${c.explain}` : ''}
                </Alert>
              )}
              <Stack spacing={0.5} sx={{ mt: 0.75 }}>
                <DetailRow label={t('operations.automations.checkArgs', 'Parámetros')}>
                  <Mono>{compactJson(c.arguments)}</Mono>
                </DetailRow>
                <DetailRow label={t('operations.automations.checkReturned', 'Devolvió')}>
                  {c.result_shape && (
                    <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 0.25 }}>{c.result_shape}</Typography>
                  )}
                  {c.result_sample !== undefined && <Mono>{compactJson(c.result_sample)}</Mono>}
                </DetailRow>
                <DetailRow label={t('operations.automations.checkExtract', 'Campo a mirar')}>
                  <Typography variant="body2" sx={{ fontFamily: 'monospace', fontSize: 12 }}>
                    {c.extract || t('operations.automations.checkExtractWhole', '(toda la respuesta)')}
                  </Typography>
                </DetailRow>
                <DetailRow label={t('operations.automations.checkExtracted', 'Valor')}>
                  <Mono>{compactJson(c.value)}</Mono>
                </DetailRow>
                <DetailRow label={t('operations.automations.checkCompare', 'Se cumple si')}>
                  <Typography variant="body2">
                    {t(`operations.automations.checkOp.${c.op}`, String(c.op || '—'))}
                    {!noThresholdOp && c.threshold !== undefined && c.threshold !== null && c.threshold !== ''
                      ? ` ${c.threshold}`
                      : ''}
                  </Typography>
                </DetailRow>
              </Stack>
              {c.explain && !c.error && (
                <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.75 }}>
                  {c.explain}
                </Typography>
              )}
            </CardContent>
          </Card>
        )
      })}
      {result?.pre && (
        <Alert severity={result.pre.met ? 'info' : 'warning'} icon={false} sx={{ py: 0.5 }}>
          <Typography variant="caption">
            {t('operations.automations.checkPre', {
              defaultValue: 'Guardián previo (pre): {{s}}',
              s: result.pre.met
                ? t('operations.automations.checkPreOk', '✓ se cumple')
                : t('operations.automations.checkPreNo', '✗ no se cumple'),
            })}
            {result.pre.explain ? ` — ${result.pre.explain}` : ''}
          </Typography>
        </Alert>
      )}
    </Stack>
  )
}

// v7.137.0 — last 5 checks inline in the row (lazy; «Ver todas» (see all) opens the modal).
function RecentEvalsInline({ automationId }: { automationId: number }) {
  const { t } = useTranslation()
  const q = useQuery({
    queryKey: ['automation-recent-evals', automationId],
    queryFn: () => apiFetch(`/api/agent/automations/${automationId}/evals?limit=5&only_notable=false`)
      .then(async (r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json() }),
    refetchInterval: 60_000,
  })
  const items: any[] = q.data?.items ?? []
  if (q.isLoading) return <CircularProgress size={16} sx={{ my: 1 }} />
  if (!items.length) {
    return (
      <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5 }}>
        {t('operations.automations.recentEmpty', 'Sin comprobaciones registradas todavía.')}
      </Typography>
    )
  }
  return (
    <Stack spacing={0.25} sx={{ mt: 0.5 }}>
      {items.map((e) => (
        <Stack key={e.id} direction="row" spacing={1} alignItems="center">
          <Typography variant="caption" sx={{ minWidth: 128 }}>{e.at ? formatDateTime(e.at) : '—'}</Typography>
          {e.error
            ? <Chip size="small" color="error" label={t('operations.automations.evals.errorChip', 'error')} />
            : e.fired
              ? <Chip size="small" color="success" variant="outlined" label={t('operations.automations.recentActed', 'actuó')} />
              : <Chip size="small" variant="outlined" label={t('operations.automations.recentNoAct', 'no procedía')} />}
          {typeof e.fresh === 'number' && e.fresh > 0 && <Chip size="small" color="info" label={`+${e.fresh}`} />}
        </Stack>
      ))}
    </Stack>
  )
}

// EVALUATIONS ledger: the trace of every time the "only if" was checked (even when it did NOT
// fire). Distinct from the FIRINGS above — gives the monitor's full traceability.
function AutomationEvalsSection({ automationId }: { automationId: number }) {
  const { t } = useTranslation()
  // v7.137.0 — by default ONLY relevant ones: there can be thousands ("not applicable" almost always).
  const [onlyNotable, setOnlyNotable] = useState(true)
  const q = useQuery({
    queryKey: ['automation-evals', automationId, onlyNotable],
    queryFn: () => apiFetch(
      `/api/agent/automations/${automationId}/evals?limit=150&only_notable=${onlyNotable}`,
    ).then(async (r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json() }),
    refetchInterval: 30_000,
  })
  const items: any[] = q.data?.items ?? []
  const s = q.data?.summary
  return (
    <PanelSection
      title={t('operations.automations.evals.title', 'Una a una: cada comprobación')}
      help={s
        ? t('operations.automations.evals.summary', {
            defaultValue: 'El detalle fino, para cuando quieras auditar una decisión concreta. Van {{total}} comprobaciones registradas (todas, sin filtro de fechas): te avisó {{fired}} vez/veces{{errors}}. Por defecto solo se muestran las relevantes, porque la mayoría son «no tocaba».',
            total: s.total, fired: s.fired,
            errors: s.errors ? t('operations.automations.evals.summaryErrors', { count: s.errors }) : '',
          })
        : t('operations.automations.evals.intro', 'El detalle fino: cada vez que salta el «cuándo», el empleado comprueba el «solo si» (coste 0). Por defecto solo las relevantes; la mayoría son «no tocaba».')}
      action={
        <Chip size="small" variant="outlined" clickable onClick={() => setOnlyNotable((v) => !v)}
          label={onlyNotable
            ? t('operations.automations.evals.onlyNotable', 'Solo relevantes')
            : t('operations.automations.evals.all', 'Todas (últimas 150)')} />
      }
    >
      {/* Legend. These figures used to be a bare «+1» and «✓1»: nobody can guess
          that one is «new things it had not seen» and the other «things that are no
          longer there, I consider them closed». */}
      <Stack direction="row" spacing={0.75} flexWrap="wrap" useFlexGap sx={{ mb: 1.25 }}>
        <Chip size="small" color="info" variant="outlined"
          label={t('operations.automations.evals.legendFresh', 'nuevo = no lo había visto antes, por eso avisó')} />
        <Chip size="small" color="success" variant="outlined"
          label={t('operations.automations.evals.legendResolved', 'resuelto = ha dejado de aparecer, lo da por cerrado')} />
      </Stack>
      {q.isLoading ? (
        <Box sx={{ display: 'flex', justifyContent: 'center', py: 2 }}><CircularProgress size={20} /></Box>
      ) : q.isError ? (
        <Alert severity="error">{t('operations.loadError', 'No se pudo cargar. Reintenta en unos segundos.')}</Alert>
      ) : items.length === 0 ? (
        <Alert severity="info">{onlyNotable
          ? t('operations.automations.evals.emptyNotable', 'Ninguna comprobación relevante todavía (ninguna disparó ni dio error). Pulsa «Todas» para ver las últimas.')
          : t('operations.automations.evals.empty', 'Todavía no se ha comprobado ninguna vez (o se podaron las trazas antiguas).')}</Alert>
      ) : (
        <Stack spacing={0.75}>
          {items.map((e) => (
            <Card key={e.id} variant="outlined">
              <CardContent sx={{ py: 0.5, '&:last-child': { pb: 0.5 } }}>
                <Stack direction="row" spacing={1} alignItems="center" justifyContent="space-between">
                  <Typography variant="caption">{e.at ? formatDateTime(e.at) : '—'}</Typography>
                  <Stack direction="row" spacing={0.5} flexWrap="wrap" useFlexGap>
                    {e.fired
                      ? <Chip size="small" color="warning" label={t('operations.automations.evals.fired', 'te avisó')} />
                      : e.met
                        ? <Tooltip title={t('operations.automations.evals.heldHint', 'La condición se cumplía, pero era algo de lo que ya te había avisado.') as string}>
                            <Chip size="small" variant="outlined" label={t('operations.automations.evals.held', 'nada nuevo')} />
                          </Tooltip>
                        : <Chip size="small" variant="outlined" label={t('operations.automations.evals.noFire', 'no tocaba')} />}
                    {e.error && <Chip size="small" color="error" label={t('operations.automations.evals.errorChip', 'no pudo comprobar')} />}
                    {e.silenced && <Chip size="small" variant="outlined" label={t('operations.automations.evals.silenced', 'en pausa')} />}
                    {typeof e.fresh === 'number' && e.fresh > 0 &&
                      <Chip size="small" color="info"
                        label={t('operations.automations.evals.freshChip', '{{n}} nuevo/s', { n: e.fresh })} />}
                    {typeof e.resolved === 'number' && e.resolved > 0 &&
                      <Chip size="small" color="success"
                        label={t('operations.automations.evals.resolvedChip', '{{n}} resuelto/s', { n: e.resolved })} />}
                  </Stack>
                </Stack>
                {(e.error || fmtEvalDetail(e.detail)) && (
                  <Typography variant="caption" color="text.secondary"
                    sx={{ display: 'block', mt: 0.25, whiteSpace: 'pre-line' }}>
                    {e.error || fmtEvalDetail(e.detail)}
                  </Typography>
                )}
              </CardContent>
            </Card>
          ))}
        </Stack>
      )}
    </PanelSection>
  )
}

// ── Automation editor helpers ────────────────────────────────────────
// Next-run preview for the schedule signal: a light 5-field cron evaluator
// (numbers, `*`, `*/n`, `a-b`, comma lists — the whole CronBuilder output
// surface). Day-loop + hour/minute set expansion keeps it fast and lets the
// preview be DST-correct in the automation's own timezone via Intl.
function cronFieldSet(expr: string, min: number, max: number): Set<number> | null | 'bad' {
  if (!expr || expr === '*') return null
  const out = new Set<number>()
  for (const part of expr.split(',')) {
    const m = part.match(/^(\*|\d+(?:-\d+)?)(?:\/(\d+))?$/)
    if (!m) return 'bad'
    const step = m[2] ? parseInt(m[2], 10) : 1
    let lo = min
    let hi = max
    if (m[1] !== '*') {
      const [a, b] = m[1].split('-').map(Number)
      if (b === undefined && !m[2]) { out.add(a); continue }
      lo = a; hi = b ?? max
    }
    for (let v = lo; v <= hi; v += step) out.add(v)
  }
  return out
}

function tzOffsetAt(date: Date, tz?: string): number {
  try {
    const dtf = new Intl.DateTimeFormat('en-US', {
      timeZone: tz || undefined, hour12: false,
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    })
    const p: Record<string, string> = {}
    for (const x of dtf.formatToParts(date)) p[x.type] = x.value
    const asUTC = Date.UTC(+p.year, +p.month - 1, +p.day, (+p.hour) % 24, +p.minute, +p.second)
    return asUTC - date.getTime()
  } catch { return 0 }
}

const WEEKDAY_IDX: Record<string, number> = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 }

function nextCronRun(cron: string, tz?: string): Date | null {
  const parts = (cron || '').trim().split(/\s+/)
  if (parts.length !== 5) return null
  const mi = cronFieldSet(parts[0], 0, 59)
  const hr = cronFieldSet(parts[1], 0, 23)
  const dom = cronFieldSet(parts[2], 1, 31)
  const mon = cronFieldSet(parts[3], 1, 12)
  const dow = cronFieldSet(parts[4], 0, 6)
  if ([mi, hr, dom, mon, dow].includes('bad')) return null
  const hours = hr === null ? Array.from({ length: 24 }, (_, i) => i) : [...(hr as Set<number>)].sort((a, b) => a - b)
  const mins = mi === null ? Array.from({ length: 60 }, (_, i) => i) : [...(mi as Set<number>)].sort((a, b) => a - b)
  if (hours.length * mins.length > 300) return null // "every minute"-ish: skip preview
  let dayFmt: Intl.DateTimeFormat
  try {
    dayFmt = new Intl.DateTimeFormat('en-US', {
      timeZone: tz || undefined, year: 'numeric', month: '2-digit', day: '2-digit', weekday: 'short',
    })
  } catch { return null }
  const now = Date.now()
  for (let d = 0; d < 62; d++) {
    const ref = new Date(now + d * 86_400_000)
    const p: Record<string, string> = {}
    for (const x of dayFmt.formatToParts(ref)) p[x.type] = x.value
    const y = +p.year; const mo = +p.month; const da = +p.day
    if (mon !== null && !(mon as Set<number>).has(mo)) continue
    if (dom !== null && !(dom as Set<number>).has(da)) continue
    if (dow !== null && !(dow as Set<number>).has(WEEKDAY_IDX[p.weekday] ?? -1)) continue
    for (const h of hours) {
      for (const mnt of mins) {
        const guess = Date.UTC(y, mo - 1, da, h, mnt)
        const instant = guess - tzOffsetAt(new Date(guess), tz)
        if (instant > now) return new Date(instant)
      }
    }
  }
  return null
}

// Friendly presets (seconds) for the "do not repeat before…" field. 0 = no pause.
const COOLDOWN_PRESETS = [0, 900, 3600, 21_600, 86_400]

// Numbered card shell for the automation editor: title row + optional inline
// help (the old monolithic manual, split so each explanation sits NEXT TO the
// fields it explains). Module-level ON PURPOSE: defined inside the editor it
// would be a new component type per render and React would remount the
// subtree, dropping input focus on every keystroke.
function EditorSection({ n, title, hint, help, action, children }: { n: string; title: string; hint?: string; help?: string; action?: any; children?: any }) {
  const { t } = useTranslation()
  return (
    <Paper variant="outlined" sx={{ p: 2, borderRadius: 2 }}>
      <Stack direction="row" spacing={1.25} alignItems="center" sx={{ mb: hint ? 0.25 : 1 }}>
        <Box sx={{
          width: 26, height: 26, borderRadius: '50%', flexShrink: 0, display: 'flex',
          alignItems: 'center', justifyContent: 'center', fontSize: 13, fontWeight: 700,
          bgcolor: (th) => alpha(th.palette.primary.main, 0.12), color: 'primary.main',
        }}>{n}</Box>
        <Typography variant="subtitle2" sx={{ fontWeight: 700, flex: 1 }}>{title}</Typography>
        {action}
      </Stack>
      {hint && (
        <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 1, ml: 4.75 }}>{hint}</Typography>
      )}
      {help && (
        <Box component="details" sx={{ ml: 4.75, mb: 1 }}>
          <Box component="summary" sx={{ cursor: 'pointer', typography: 'caption', color: 'info.main' }}>
            {t('operations.automations.help.moreExamples', 'Ver explicación y ejemplos')}
          </Box>
          <Typography variant="caption" color="text.secondary" sx={{ whiteSpace: 'pre-line', display: 'block', mt: 0.5 }}>
            {help}
          </Typography>
        </Box>
      )}
      <Box sx={{ ml: { sm: 4.75 } }}>{children}</Box>
    </Paper>
  )
}

function AutomationEditor({ initial, catalog, onClose, onSaved }: { initial: any; catalog: any; onClose: () => void; onSaved: () => void }) {
  const { t } = useTranslation()
  const [f, setF] = useState<any>(() => {
    let signal_type = initial.signal_type || 'schedule'
    let signal_spec = { ...(initial.signal_spec || {}) }
    let condition = (initial.condition_spec && initial.condition_spec.mode)
      ? { ...initial.condition_spec }
      : { mode: 'always', source: 'local' }
    // Legacy: a `data_condition` SIGNAL becomes a SCHEDULE trigger + a DATA
    // condition (the new "two gates" model).
    if (signal_type === 'data_condition') {
      condition = { mode: 'data', source: 'local', ...(initial.signal_spec || {}) }
      signal_type = 'schedule'
      signal_spec = { cron: signal_spec.cron || '0 */2 * * *' }
    }
    if (signal_type === 'schedule' && !signal_spec.cron) signal_spec.cron = '0 9 * * 1'
    // Legacy: `draft_and_approve` mixed WHAT-it-does with the approval gate.
    // Since 7.92.0 approval is ONLY the switch, so it loads as act+approval
    // (same behaviour) and the type is never written back.
    let reaction_type = initial.reaction_type || 'act'
    let requires_approval = initial.requires_approval ?? true
    if (reaction_type === 'draft_and_approve') {
      reaction_type = 'act'
      requires_approval = true
    }
    return {
      title: initial.title || '',
      // HTML description (mandatory): what this automation does, functionally.
      description: initial.description || '',
      // Which catalog template this order came from (null if hand-built) — carried
      // through so the templates dropdown can mark it "Ya añadida" and block dups.
      template_key: initial.template_key ?? null,
      signal_type,
      signal_spec,
      condition,
      reaction_type,
      // COST axis (7.127.0): 'deterministic' = a templated notify/create_task done
      // directly (0 tokens); 'agent' = an LLM run. Default 'agent' (back-compat).
      execution_mode: initial.execution_mode || 'agent',
      reaction_instructions: initial.reaction_instructions || '',
      reaction_spec: { ...(initial.reaction_spec || {}) },
      requires_approval,
      check_before_approval: initial.check_before_approval ?? true,
      cooldown_sec: initial.cooldown_sec ?? 0,
      enabled: !!initial.enabled,
    }
  })
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  // "Solo si…" (condition) card starts collapsed unless a condition exists —
  // most standing orders have none, and hiding it halves the form.
  const [condOpen, setCondOpen] = useState(() => {
    const m = (initial.condition_spec && initial.condition_spec.mode) || (initial.signal_type === 'data_condition' ? 'data' : 'always')
    return m !== 'always'
  })
  // "Y cómo te informa" — the COMMUNICATION axis, independent of the action.
  const [informMode, setInformMode] = useState<'activity' | 'channel'>(
    () => (initial.reaction_spec?.notify?.channel_id ? 'channel' : 'activity'),
  )
  // Condition dry-run ("Probar ahora"), only for already-saved rules.
  const [testBusy, setTestBusy] = useState(false)
  const [testResult, setTestResult] = useState<any>(null)
  // The editor holds a lot now — let the operator blow it up to full screen.
  const [maximized, setMaximized] = useState(false)
  // The `description` is mandatory: flag it red once touched / on a save attempt.
  const [descTouched, setDescTouched] = useState(false)
  const set = (k: string, v: any) => setF((p: any) => ({ ...p, [k]: v }))
  const setSig = (k: string, v: any) => setF((p: any) => ({ ...p, signal_spec: { ...p.signal_spec, [k]: v } }))
  const setRea = (k: string, v: any) => setF((p: any) => ({ ...p, reaction_spec: { ...p.reaction_spec, [k]: v } }))
  const setCond = (k: string, v: any) => setF((p: any) => ({ ...p, condition: { ...p.condition, [k]: v } }))
  // v7.135.0 — `watch` mode (monitoring with memory): its config lives nested in
  // condition.watch {connector,tool,arguments,items_at,key,signature,ignore_keys,
  // silence_until,resolve_when_absent,on_error}.
  const watchCfg: any = (f.condition.watch && typeof f.condition.watch === 'object') ? f.condition.watch : {}
  const setWatch = (k: string, v: any) => setF((p: any) => ({ ...p, condition: { ...p.condition, watch: { ...(p.condition.watch || {}), [k]: v } } }))
  const entities = catalog?.signals?.data_condition?.entities ?? []
  const events = catalog?.signals?.business_event?.events ?? []
  // ── Checks (cheap gate via MCP, 7.127.0): connector + tool name pickers
  //    reusing the existing endpoints; each signal is 1 MCP tool + one
  //    comparison (0 tokens). ──
  const { data: _connData } = useQuery({ queryKey: ['auto-checks-connectors'], queryFn: () => mcpConnectorsApi.list().catch(() => null) })
  const { data: _toolsData } = useQuery({ queryKey: ['auto-checks-tools'], queryFn: () => mcpConnectorsApi.listTools().catch(() => null) })
  const checkConnectors = (_connData?.connectors ?? []) as any[]
  const toolsForConnector = (name: string): any[] => ((_toolsData?.connectors ?? []).find((c: any) => c.name === name)?.tools ?? [])
  const checks: any[] = Array.isArray(f.condition.checks) ? f.condition.checks : []
  const setChecks = (arr: any[]) => setCond('checks', arr)
  const setCheck = (i: number, k: string, v: any) => setChecks(checks.map((c, j) => (j === i ? { ...c, [k]: v } : c)))
  const addCheck = () => setChecks([...checks, { name: '', connector: '', tool: '', arguments: {}, extract: '', op: 'gte', threshold: 1 }])
  const removeCheck = (i: number) => setChecks(checks.filter((_, j) => j !== i))
  const CHECK_OPS = catalog?.check_operators ?? ['gte', 'gt', 'lte', 'lt', 'eq', 'non_empty', 'empty', 'contains', 'not_contains']
  const channels = catalog?.signals?.channel_message?.channels ?? []
  const projectAvailable = !!catalog?.project_data_available
  const aggs = catalog?.signals?.data_condition?.aggregates ?? ['count', 'sum', 'avg', 'min', 'max']
  const ops = catalog?.signals?.data_condition?.operators ?? ['gt', 'lt', 'gte', 'lte', 'eq', 'no_activity']
  // The channel chosen for a notify reaction (types the recipient picker).
  const notifyChannel = channels.find((c: any) => String(c.id) === String(f.reaction_spec.channel_id))
  // v7.134.0 — context variables the "Qué avisar" editor can INSERT into the
  // message. From the catalog (tokens _render_templates actually substitutes);
  // a small fallback keeps the palette useful if an older backend omits it.
  const contextVars: { token: string; label?: string }[] = catalog?.context_variables ?? [
    { token: '{{tenant}}', label: 'Nombre del negocio' },
    { token: '{{now}}', label: 'Fecha y hora actual' },
  ]
  // Append a token to the notify message (chips are additive — the operator
  // fine-tunes placement in the textarea afterwards).
  const insertVar = (tok: string) => {
    const cur = String(f.reaction_spec.message || '')
    const sep = cur && !cur.endsWith(' ') && !cur.endsWith('\n') ? ' ' : ''
    setRea('message', cur + sep + tok)
  }
  // Localised labels for the raw enum values (never show data_condition / gt /
  // no_activity to the operator).
  const sigLabel = (s: string) => t(`operations.automations.signalType.${s}`, s)
  const reaLabel = (s: string) => t(`operations.automations.reactionType.${s}`, s)
  const aggLabel = (s: string) => t(`operations.automations.agg.${s}`, s)
  const opLabel = (s: string) => t(`operations.automations.op.${s}`, s)

  // ── "Tu orden, en una frase" — the live sentence composed from the form ──
  const buildSentence = (): string => {
    let when: string
    if (f.signal_type === 'schedule') {
      try { when = describeCron(parseCron(f.signal_spec.cron || ''), t) || t('operations.automations.sentence.whenSchedule', { defaultValue: 'Según el horario configurado' }) }
      catch { when = t('operations.automations.sentence.whenSchedule', { defaultValue: 'Según el horario configurado' }) }
    } else if (f.signal_type === 'channel_message') {
      const ch = channels.find((c: any) => String(c.id) === String(f.signal_spec.channel_id))
      when = ch
        ? t('operations.automations.sentence.whenChannel', { defaultValue: 'Cuando llegue un mensaje por «{{ch}}»', ch: ch.label })
        : t('operations.automations.sentence.whenAnyChannel', { defaultValue: 'Cuando llegue un mensaje por cualquier canal' })
    } else if (f.signal_type === 'business_event') {
      const ev = events.find((e: any) => e.type === f.signal_spec.event_type)
      when = t('operations.automations.sentence.whenEvent', { defaultValue: 'Cuando ocurra «{{ev}}»', ev: ev?.label || f.signal_spec.event_type || '…' })
    } else {
      when = t('operations.automations.sentence.whenManual', { defaultValue: 'Solo cuando tú la lances' })
    }
    let cond = ''
    const c = f.condition
    if (condOpen && c.mode === 'data') {
      const en = entities.find((x: any) => x.table === c.table)
      cond = t('operations.automations.sentence.condData', {
        defaultValue: 'si {{agg}} de {{entity}} {{op}} {{n}}',
        agg: aggLabel(c.aggregate || 'count'), entity: en?.label || c.table || '…',
        op: opLabel(c.operator || 'gt'), n: c.threshold ?? '…',
      })
    } else if (condOpen && c.mode === 'agent') {
      const txt = (c.instructions || '').trim()
      cond = txt
        ? t('operations.automations.sentence.condAgent', { defaultValue: 'si comprueba que «{{txt}}»', txt: txt.length > 60 ? `${txt.slice(0, 60)}…` : txt })
        : t('operations.automations.sentence.condAgentEmpty', { defaultValue: 'si se cumple lo que le pidas comprobar' })
    } else if (condOpen && c.mode === 'sql') {
      cond = t('operations.automations.sentence.condSql', { defaultValue: 'si la consulta SQL se cumple' })
    } else if (condOpen && c.mode === 'watch') {
      cond = t('operations.automations.sentence.condWatch', { defaultValue: 'si la vigilancia detecta algo nuevo o escalado' })
    }
    let then: string
    if (f.reaction_type === 'notify') {
      then = t('operations.automations.sentence.thenNotify', {
        defaultValue: 'te avisará por {{ch}}',
        ch: notifyChannel?.label || t('operations.automations.sentence.aChannel', { defaultValue: 'un canal' }),
      })
    } else if (f.reaction_type === 'create_task') {
      then = t('operations.automations.sentence.thenTask', { defaultValue: 'creará una tarea de seguimiento' })
    } else if (f.reaction_type === 'generate_deliverable') {
      then = t('operations.automations.sentence.thenDeliverable', { defaultValue: 'generará un {{fmt}}', fmt: String(f.reaction_spec.format || 'pdf').toUpperCase() })
    } else {
      const txt = (f.reaction_instructions || '').trim()
      then = txt
        ? t('operations.automations.sentence.thenAct', { defaultValue: 'hará: «{{txt}}»', txt: txt.length > 70 ? `${txt.slice(0, 70)}…` : txt })
        : t('operations.automations.sentence.thenActEmpty', { defaultValue: 'hará el trabajo que le indiques' })
    }
    let inform = ''
    const nspec = f.reaction_spec.notify
    if (f.reaction_type !== 'notify' && informMode === 'channel' && nspec?.channel_id) {
      inform = ' ' + t('operations.automations.sentence.andNotify', { defaultValue: 'y te avisará por {{ch}}', ch: nspec.channel_label || '…' })
    }
    const approval = f.requires_approval
      ? ' ' + t('operations.automations.sentence.withApproval', { defaultValue: '(con tu aprobación previa)' })
      : ''
    return [when, cond, then + inform + approval].filter(Boolean).join(' → ')
  }
  const sentence = buildSentence()
  const nextRun = f.signal_type === 'schedule'
    ? nextCronRun(f.signal_spec.cron || '', f.signal_spec.tz || catalog?.business_tz || undefined)
    : null

  // AD-HOC "Probar" (Test, 7.127.0): evaluates the condition AS IT IS in the editor, WITHOUT
  // saving it, and returns the result IN PARTS (mode-aware). This way each
  // kind of "what does it check?" is validated seeing exactly what it returns and, if it fails, where.
  const testNow = async () => {
    setTestBusy(true); setTestResult(null)
    try {
      const r = await apiFetch('/api/agent/automations/evaluate-condition', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ condition_spec: f.condition }),
      })
      const b = await r.json().catch(() => null)
      if (!r.ok) throw new Error(b?.detail || `HTTP ${r.status}`)
      setTestResult(b)
    } catch (e: any) { setTestResult({ evaluable: false, error: String(e?.message ?? e) }) } finally { setTestBusy(false) }
  }

  const save = async () => {
    // The `description` is mandatory (the backend 422s without it): give immediate
    // feedback rather than a round-trip.
    if (!htmlHasText(f.description)) {
      setDescTouched(true)
      setErr(t('operations.automations.form.descriptionRequired', 'La descripción es obligatoria: explica cuándo se dispara, qué comprueba y qué hace.'))
      return
    }
    setBusy(true); setErr(null)
    try {
      const isEdit = !!initial.id
      // The COMMUNICATION axis lives in reaction_spec.notify for every action
      // except `notify` itself (whose channel is the action, legacy top-level
      // shape). Strip it when the operator chose "solo Actividad".
      const reaction_spec = { ...f.reaction_spec }
      if (f.reaction_type === 'notify' || informMode !== 'channel' || !reaction_spec.notify?.channel_id) {
        delete reaction_spec.notify
      }
      const condition_spec = condOpen ? f.condition : { mode: 'always', source: 'local' }
      const body = {
        // Title is optional for the operator: empty = the live sentence.
        title: f.title.trim() || sentence.slice(0, 140),
        // Mandatory HTML description of what this automation does.
        description: f.description,
        // Sent so the backend can tie a created order back to its template.
        template_key: f.template_key ?? null,
        signal_type: f.signal_type,
        signal_spec: f.signal_spec,
        condition_spec,
        reaction_type: f.reaction_type,
        // Deterministic only makes sense for the templated outcomes; force 'agent'
        // for act/generate_deliverable so the backend never tries to shortcut them.
        execution_mode: (f.reaction_type === 'create_task' || f.reaction_type === 'notify')
          ? f.execution_mode : 'agent',
        reaction_spec,
        reaction_instructions: f.reaction_instructions,
        requires_approval: f.requires_approval,
        check_before_approval: f.check_before_approval,
        enabled: f.enabled,
        cooldown_sec: f.cooldown_sec,
      }
      const r = await apiFetch(isEdit ? `/api/agent/automations/${initial.id}` : '/api/agent/automations', {
        method: isEdit ? 'PATCH' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      })
      if (!r.ok) { const b = await r.json().catch(() => null); throw new Error(b?.detail || `HTTP ${r.status}`) }
      onSaved()
    } catch (e: any) { setErr(String(e?.message ?? e)) } finally { setBusy(false) }
  }

  const informChannel = channels.find((c: any) => String(c.id) === String(f.reaction_spec.notify?.channel_id))
  const cooldownIsPreset = COOLDOWN_PRESETS.includes(Number(f.cooldown_sec) || 0)
  const descMissing = !htmlHasText(f.description)
  // Drop a When / Only-if / What-it-does skeleton into the description so every
  // order is documented the same way. Fills an empty field; otherwise appends.
  const insertDescScaffold = () => {
    const sec = (title: string) => `<h4>${title}</h4><p></p>`
    const skeleton = sec(t('operations.automations.scaffold.when', 'Cuándo'))
      + sec(t('operations.automations.scaffold.onlyIf', 'Solo si'))
      + sec(t('operations.automations.scaffold.does', 'Qué hace'))
    const cur = String(f.description || '')
    set('description', htmlHasText(cur) ? `${cur}${skeleton}` : skeleton)
    if (!descTouched) setDescTouched(true)
  }

  return (
    <Dialog open onClose={onClose} maxWidth="lg" fullWidth fullScreen={maximized}>
      <DialogTitle sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
        <Box sx={{ flex: 1 }}>{initial.id ? t('operations.automations.editTitle', 'Editar automatización') : t('operations.automations.addTitle', 'Nueva automatización')}</Box>
        <Tooltip title={maximized ? t('common.restoreSize', 'Restaurar tamaño') : t('common.maximize', 'Maximizar')}>
          <IconButton size="small" aria-label={maximized ? t('common.restoreSize', 'Restaurar tamaño') : t('common.maximize', 'Maximizar')} onClick={() => setMaximized((v) => !v)}>
            {maximized ? <FullscreenExitIcon /> : <FullscreenIcon />}
          </IconButton>
        </Tooltip>
      </DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 0.5 }}>
          {err && <Alert severity="error" onClose={() => setErr(null)}>{err}</Alert>}

          {/* ── The live sentence: the full order in natural language ── */}
          <Paper variant="outlined" sx={{
            p: 1.5, borderRadius: 2, position: 'sticky', top: 0, zIndex: 2,
            bgcolor: (th) => th.palette.mode === 'dark' ? th.palette.background.paper : alpha(th.palette.primary.main, 0.05),
            borderColor: (th) => alpha(th.palette.primary.main, 0.35),
          }}>
            <Typography variant="caption" color="text.secondary" sx={{ display: 'block' }}>
              {t('operations.automations.sentence.label', 'Tu orden, en una frase')}
            </Typography>
            <Typography variant="body2" sx={{ fontWeight: 600 }}>{sentence}</Typography>
            {nextRun && (
              <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.25 }}>
                {t('operations.automations.sentence.nextRun', { defaultValue: 'Próximo disparo: {{when}}', when: formatDateTime(nextRun) })}
              </Typography>
            )}
          </Paper>

          {/* ── Description (HTML, MANDATORY): the first and most important thing —
              what this automation does, in plain words and functionally. ── */}
          <Paper variant="outlined" sx={{
            p: 2, borderRadius: 2,
            borderColor: (th) => (descTouched && descMissing) ? th.palette.error.main : alpha(th.palette.primary.main, 0.35),
          }}>
            <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.75 }}>
              <Typography variant="subtitle2" sx={{ fontWeight: 700, flex: 1 }} color={(descTouched && descMissing) ? 'error' : undefined}>
                {t('operations.automations.form.description', 'Descripción (obligatoria)')}
              </Typography>
              <Tooltip title={t('operations.automations.form.insertStructureTip', 'Inserta un esqueleto Cuándo / Solo si / Qué hace para que la descripción quede completa y homogénea.')}>
                <Button size="small" variant="text" startIcon={<AddIcon fontSize="small" />}
                  onClick={insertDescScaffold} sx={{ textTransform: 'none' }}>
                  {t('operations.automations.form.insertStructure', 'Insertar estructura')}
                </Button>
              </Tooltip>
              <Tooltip title={t('operations.automations.form.descriptionTip', 'Describe la automatización de forma FUNCIONAL: CUÁNDO se dispara (la señal), el «solo si» (qué comprueba) y QUÉ hace (la reacción). Así tú y el empleado la entendéis de un vistazo.')}>
                <InfoIcon fontSize="small" color="action" sx={{ cursor: 'help' }} />
              </Tooltip>
            </Stack>
            <ExpandableTextField
              html minRows={3}
              error={descTouched && descMissing}
              label={t('operations.automations.form.descriptionLabel', 'Qué hace esta automatización')}
              placeholder={t('operations.automations.form.descriptionPh', 'p. ej. <b>Cada mañana a las 9:00</b> revisa los pedidos; <i>solo si</i> hay pedidos sin atender, avisa al responsable por email.')}
              helperText={(descTouched && descMissing)
                ? t('operations.automations.form.descriptionRequired', 'La descripción es obligatoria: explica cuándo se dispara, qué comprueba y qué hace.')
                : t('operations.automations.form.descriptionHelp', 'Explica FUNCIONALMENTE: cuándo se dispara (la señal), el «solo si» (qué comprueba) y qué hace (la reacción).')}
              value={f.description || ''}
              onChange={(v) => { set('description', v); if (!descTouched) setDescTouched(true) }}
            />
          </Paper>

          {/* ── 1 · WHEN ──────────────────────────────────────────── */}
          <EditorSection
            n="1"
            title={t('operations.automations.whenTitle', 'Cuándo')}
            hint={t('operations.automations.whenHint', 'Qué tiene que pasar para que el empleado actúe.')}
            help={t('operations.automations.help.when', '')}
          >
            <Stack spacing={2}>
              {/* v7.134.0 — no `channel_message`: an incoming message is already handled by
                  the employee; automating "when a message arrives" was redundant. It is
                  offered ONLY if a legacy automation already uses it (so its editing does not
                  break); never for a new one. */}
              <TextField select size="small" fullWidth label={t('operations.automations.fSignal', 'Tipo de señal')} value={f.signal_type} onChange={(e) => set('signal_type', e.target.value)}>
                {[...(f.signal_type === 'channel_message' ? ['channel_message'] : []), 'schedule', 'business_event', 'manual'].map((s) => <MenuItem key={s} value={s}>{sigLabel(s)}</MenuItem>)}
              </TextField>
              {f.signal_type === 'channel_message' && (
                <TextField select size="small" fullWidth label={t('operations.automations.fChannel', 'Canal')} value={f.signal_spec.channel_id || ''} onChange={(e) => setSig('channel_id', e.target.value)}
                  helperText={t('operations.automations.channelMsgLegacy', 'Señal en desuso: un mensaje entrante ya lo atiende el empleado. Elige otro tipo de señal cuando puedas.')}>
                  {channels.map((c: any) => <MenuItem key={c.id} value={c.id}>{c.label}</MenuItem>)}
                </TextField>
              )}
              {f.signal_type === 'schedule' && (
                <Box>
                  <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 0.5 }}>
                    {t('operations.automations.fSchedule', 'Frecuencia')}
                  </Typography>
                  <CronBuilder value={f.signal_spec.cron || '0 9 * * 1'} onChange={(c) => setSig('cron', c)} />
                  <Autocomplete
                    size="small"
                    freeSolo
                    options={TZ_OPTIONS}
                    value={f.signal_spec.tz || catalog?.business_tz || ''}
                    onChange={(_, v) => setSig('tz', v || '')}
                    onInputChange={(_, v) => setSig('tz', v || '')}
                    sx={{ mt: 1.5 }}
                    renderInput={(params) => (
                      <TextField
                        {...params}
                        label={t('operations.automations.fTimezone', 'Zona horaria')}
                        helperText={t('operations.automations.fTimezoneHelp', 'La hora se interpreta en esta zona horaria. Por defecto, la del empleado ({{tz}}).', { tz: catalog?.business_tz || 'UTC' })}
                      />
                    )}
                  />
                  {/* Quick shortcuts so the operator doesn't have to scroll the
                      full IANA list for the two zones they actually want. */}
                  <Stack direction="row" spacing={1} sx={{ mt: 1 }} flexWrap="wrap" useFlexGap alignItems="center">
                    <Typography variant="caption" color="text.secondary">
                      {t('operations.automations.tzQuick', 'Atajos:')}
                    </Typography>
                    {(() => {
                      const browserTz = (() => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone } catch { return '' } })()
                      return browserTz ? (
                        <Chip size="small" variant="outlined" clickable onClick={() => setSig('tz', browserTz)}
                          label={t('operations.automations.tzBrowser', 'Navegador: {{tz}}', { tz: browserTz })} />
                      ) : null
                    })()}
                    {catalog?.business_tz && (
                      <Chip size="small" variant="outlined" clickable onClick={() => setSig('tz', catalog.business_tz)}
                        label={t('operations.automations.tzBusiness', 'Negocio: {{tz}}', { tz: catalog.business_tz })} />
                    )}
                  </Stack>
                </Box>
              )}
              {f.signal_type === 'business_event' && (
                <TextField select size="small" fullWidth label={t('operations.automations.fEvent', 'Evento')} value={f.signal_spec.event_type || ''} onChange={(e) => setSig('event_type', e.target.value)}>
                  {events.map((ev: any) => <MenuItem key={ev.type} value={ev.type}>{ev.label || ev.type}</MenuItem>)}
                </TextField>
              )}
            </Stack>
          </EditorSection>

          {/* ── 2 · ONLY IF… (optional condition, collapsed by default) ── */}
          <EditorSection
            n="2"
            title={t('operations.automations.condTitle2', 'Solo si… (opcional)')}
            hint={condOpen ? t('operations.automations.condHint', 'Tras la señal, el agente mira esto y solo actúa si se cumple.') : undefined}
            help={condOpen ? t('operations.automations.help.cond', 'El «solo si» filtra tras la señal: el empleado solo actúa si se cumple.\n\n• Comprobación (MCP): llama a una herramienta y compara un valor (p. ej. nº de errores ≥ 1). Sin coste.\n• Vigilancia con memoria: dispara solo por casos NUEVOS o que han ESCALADO; recuerda los que ya vio y no repite.\n• Un dato del negocio: recuento/umbral sobre tus tablas (p. ej. pedidos pendientes > 0).\n• Consulta SQL: una regla a medida, solo lectura.\n• Que lo juzgue el empleado (IA): lo más flexible, pero consume en cada comprobación.\n\nUsa «Probar la condición» para ver, parte por parte, qué devuelve cada comprobación y si se cumple.') : undefined}
            action={condOpen ? (
              <Button size="small" color="inherit" onClick={() => { setCondOpen(false); setCond('mode', 'always'); setTestResult(null) }}>
                {t('operations.automations.condRemove', 'Quitar condición')}
              </Button>
            ) : (
              <>
                <Chip size="small" variant="outlined" label={t('operations.automations.condAlwaysChip', 'Siempre — sin condición extra')} />
                <Button size="small" onClick={() => { setCondOpen(true); if ((f.condition.mode || 'always') === 'always') setCond('mode', 'checks') }}>
                  {t('operations.automations.condAdd', 'Añadir condición')}
                </Button>
              </>
            )}
          >
            {condOpen && (
            <Stack spacing={2}>
              <TextField select size="small" fullWidth label={t('operations.automations.fCondMode', '¿Qué comprueba?')} value={f.condition.mode === 'always' ? 'checks' : (f.condition.mode || 'checks')} onChange={(e) => { setCond('mode', e.target.value); setTestResult(null) }}>
                {[
                  ['checks', t('operations.automations.condMode.checks', 'Comprobación con una herramienta (MCP) — sin coste')],
                  ['watch', t('operations.automations.condMode.watch', 'Vigilancia con memoria — dispara solo lo nuevo o escalado (sin coste)')],
                  ['data', t('operations.automations.condMode.data', 'Un dato del negocio (recuento/umbral) — sin coste')],
                  ['sql', t('operations.automations.condMode.sql', 'Consulta SQL (avanzado) — sin coste')],
                  ['agent', t('operations.automations.condMode.agent', 'Que lo juzgue el empleado (IA) — consume en cada comprobación')],
                ].map(([m, label]) => <MenuItem key={m} value={m}>{label}</MenuItem>)}
              </TextField>
              {f.condition.mode === 'checks' && (
                <Stack spacing={1.5}>
                  <Stack direction="row" spacing={1}>
                    <TextField select size="small" sx={{ flex: 1 }} label={t('operations.automations.fCombine', '¿Cuándo se cumple?')} value={f.condition.combine || 'all'} onChange={(e) => setCond('combine', e.target.value)}>
                      <MenuItem value="all">{t('operations.automations.combineAll', 'TODAS las señales')}</MenuItem>
                      <MenuItem value="any">{t('operations.automations.combineAny', 'ALGUNA señal')}</MenuItem>
                    </TextField>
                    <TextField select size="small" sx={{ flex: 1 }} label={t('operations.automations.fOnError', 'Si no se puede comprobar')} value={f.condition.on_error || 'alert'} onChange={(e) => setCond('on_error', e.target.value)}>
                      <MenuItem value="alert">{t('operations.automations.onErrAlert', 'Avisar sin entrar')}</MenuItem>
                      <MenuItem value="skip">{t('operations.automations.onErrSkip', 'No hacer nada')}</MenuItem>
                      <MenuItem value="fire">{t('operations.automations.onErrFire', 'Entrar igualmente')}</MenuItem>
                    </TextField>
                  </Stack>
                  {checks.length === 0 && (
                    <Typography variant="caption" color="text.secondary">
                      {t('operations.automations.checksEmpty', 'Añade al menos una señal: el empleado la comprueba con una herramienta MCP (0 tokens) y solo entra si se cumple.')}
                    </Typography>
                  )}
                  {checks.map((chk, i) => (
                    <Paper key={i} variant="outlined" sx={{ p: 1.5, borderRadius: 2 }}>
                      <Stack spacing={1}>
                        <Stack direction="row" spacing={1} alignItems="center">
                          <TextField size="small" sx={{ flex: 1 }} label={t('operations.automations.fCheckName', 'Nombre de la señal')} placeholder={t('operations.automations.fCheckNamePh', 'p. ej. errores en logs')} value={chk.name || ''} onChange={(e) => setCheck(i, 'name', e.target.value)} />
                          <IconButton size="small" aria-label="remove" onClick={() => removeCheck(i)}><DeleteIcon fontSize="small" /></IconButton>
                        </Stack>
                        <Stack direction="row" spacing={1}>
                          <TextField select size="small" sx={{ flex: 1 }} label={t('operations.automations.fCheckConnector', 'Herramienta (MCP)')} value={chk.connector || ''} onChange={(e) => { setCheck(i, 'connector', e.target.value); setCheck(i, 'tool', '') }}>
                            {checkConnectors.map((c) => <MenuItem key={c.name} value={c.name}>{c.display_name || c.name}</MenuItem>)}
                            {checkConnectors.length === 0 && <MenuItem value="" disabled>{t('operations.automations.noConnectors', 'Sin MCPs conectados')}</MenuItem>}
                          </TextField>
                          <Autocomplete
                            size="small" sx={{ flex: 1 }} freeSolo
                            options={toolsForConnector(chk.connector).map((tl: any) => tl.name)}
                            value={chk.tool || ''}
                            onChange={(_, v) => setCheck(i, 'tool', v || '')}
                            onInputChange={(_, v) => setCheck(i, 'tool', v || '')}
                            renderInput={(params) => <TextField {...params} label={t('operations.automations.fCheckTool', 'Acción/consulta')} />}
                          />
                        </Stack>
                        <ExpandableTextField
                          mono minRows={2}
                          label={t('operations.automations.fCheckArgs', 'Parámetros (JSON)')}
                          placeholder={'{ "namespace": "{{tenant}}", "level": "error", "since_seconds": 900 }'}
                          value={typeof chk.arguments === 'string' ? chk.arguments : JSON.stringify(chk.arguments || {}, null, 0)}
                          onChange={(v) => { try { setCheck(i, 'arguments', JSON.parse(v || '{}')) } catch { setCheck(i, 'arguments', v) } }}
                        />
                        <Stack direction="row" spacing={1}>
                          <TextField size="small" sx={{ flex: 1 }} label={t('operations.automations.fCheckExtract', 'Campo a mirar (opcional)')} placeholder="count" value={chk.extract || ''} onChange={(e) => setCheck(i, 'extract', e.target.value)} />
                          <TextField select size="small" sx={{ flex: 1 }} label={t('operations.automations.fCheckOp', 'Se cumple si')} value={chk.op || 'gte'} onChange={(e) => setCheck(i, 'op', e.target.value)}>
                            {CHECK_OPS.map((o: string) => <MenuItem key={o} value={o}>{t(`operations.automations.checkOp.${o}`, o)}</MenuItem>)}
                          </TextField>
                          {!['non_empty', 'empty'].includes(chk.op || 'gte') && (
                            <TextField size="small" sx={{ flex: 1 }} label={t('operations.automations.fCheckValue', 'Valor')} value={chk.threshold ?? ''} onChange={(e) => setCheck(i, 'threshold', e.target.value)} />
                          )}
                        </Stack>
                      </Stack>
                    </Paper>
                  ))}
                  <Button size="small" startIcon={<AddIcon />} onClick={addCheck} sx={{ alignSelf: 'flex-start' }}>
                    {t('operations.automations.addCheck', 'Añadir señal')}
                  </Button>
                </Stack>
              )}
              {/* v7.135.0 — `watch` mode: monitoring with memory (per-item dedup),
                  0 tokens. Full editor: what it watches + how it identifies/escalates +
                  ruido a ignorar + ventana de silencio. */}
              {f.condition.mode === 'watch' && (
                <Stack spacing={1.5}>
                  <Typography variant="caption" color="text.secondary">
                    {t('operations.automations.watchIntro', 'Vigila una lista (incidencias, tickets, leads…) y dispara SOLO por casos NUEVOS o que han ESCALADO; recuerda los que ya vio y no repite. Ideal para monitores recurrentes.')}
                  </Typography>
                  <Stack direction="row" spacing={1}>
                    <TextField select size="small" sx={{ flex: 1 }} label={t('operations.automations.fCheckConnector', 'Herramienta (MCP)')} value={watchCfg.connector || ''} onChange={(e) => { setWatch('connector', e.target.value); setWatch('tool', '') }}>
                      {checkConnectors.map((c) => <MenuItem key={c.name} value={c.name}>{c.display_name || c.name}</MenuItem>)}
                      {checkConnectors.length === 0 && <MenuItem value="" disabled>{t('operations.automations.noConnectors', 'Sin MCPs conectados')}</MenuItem>}
                    </TextField>
                    <Autocomplete
                      size="small" sx={{ flex: 1 }} freeSolo
                      options={toolsForConnector(watchCfg.connector).map((tl: any) => tl.name)}
                      value={watchCfg.tool || ''}
                      onChange={(_, v) => setWatch('tool', v || '')}
                      onInputChange={(_, v) => setWatch('tool', v || '')}
                      renderInput={(params) => <TextField {...params} label={t('operations.automations.fWatchTool', 'Acción que devuelve la lista')} />}
                    />
                  </Stack>
                  <ExpandableTextField
                    mono minRows={2}
                    label={t('operations.automations.fCheckArgs', 'Parámetros (JSON)')}
                    placeholder={'{ "namespace": "{{tenant}}", "level": "error", "since_seconds": 1800 }'}
                    value={typeof watchCfg.arguments === 'string' ? watchCfg.arguments : JSON.stringify(watchCfg.arguments || {}, null, 0)}
                    onChange={(v) => { try { setWatch('arguments', JSON.parse(v || '{}')) } catch { setWatch('arguments', v) } }}
                  />
                  <Stack direction="row" spacing={1}>
                    <TextField size="small" sx={{ flex: 1 }} label={t('operations.automations.fWatchItemsAt', 'Ruta a la lista (opcional)')} placeholder="items" value={watchCfg.items_at || ''} onChange={(e) => setWatch('items_at', e.target.value)} helperText={t('operations.automations.fWatchItemsAtHelp', 'Dónde está la lista en la respuesta. Vacío = la respuesta ya es la lista.')} />
                    <TextField size="small" sx={{ flex: 1 }} label={t('operations.automations.fWatchKey', 'Campo que identifica cada caso')} placeholder="id" value={watchCfg.key || ''} onChange={(e) => setWatch('key', e.target.value)} helperText={t('operations.automations.fWatchKeyHelp', 'La clave para no repetir avisos del mismo caso.')} />
                  </Stack>
                  <TextField size="small" fullWidth label={t('operations.automations.fWatchSignature', 'Campo de escalado (opcional)')} placeholder="status" value={watchCfg.signature || ''} onChange={(e) => setWatch('signature', e.target.value)} helperText={t('operations.automations.fWatchSignatureHelp', 'Si este campo cambia en un caso ya visto, cuenta como ESCALADO y vuelve a disparar.')} />
                  <TextField size="small" fullWidth label={t('operations.automations.fWatchIgnore', 'Ignorar estos casos (ruido conocido)')} placeholder="deploy-*, mantenimiento" value={Array.isArray(watchCfg.ignore_keys) ? watchCfg.ignore_keys.join(', ') : (watchCfg.ignore_keys || '')} onChange={(e) => setWatch('ignore_keys', e.target.value.split(/[,\n]/).map((s: string) => s.trim()).filter(Boolean))} helperText={t('operations.automations.fWatchIgnoreHelp', 'Claves (o patrones con *) que NUNCA disparan. Separa con comas.')} />
                  <Stack direction="row" spacing={1} alignItems="flex-start">
                    <TextField size="small" sx={{ flex: 1 }} type="datetime-local" label={t('operations.automations.fWatchSilence', 'Silenciar hasta (opcional)')} InputLabelProps={{ shrink: true }} value={watchCfg.silence_until || ''} onChange={(e) => setWatch('silence_until', e.target.value)} helperText={t('operations.automations.fWatchSilenceHelp', 'Ventana de mantenimiento: no dispara hasta esa fecha/hora.')} />
                    <TextField select size="small" sx={{ flex: 1 }} label={t('operations.automations.fOnError', 'Si no se puede comprobar')} value={watchCfg.on_error || 'alert'} onChange={(e) => setWatch('on_error', e.target.value)}>
                      <MenuItem value="alert">{t('operations.automations.onErrAlert', 'Avisar sin entrar')}</MenuItem>
                      <MenuItem value="skip">{t('operations.automations.onErrSkip', 'No hacer nada')}</MenuItem>
                      <MenuItem value="fire">{t('operations.automations.onErrFire', 'Entrar igualmente')}</MenuItem>
                    </TextField>
                  </Stack>
                  <Stack direction="row" spacing={1} alignItems="center">
                    <Switch size="small" checked={watchCfg.resolve_when_absent !== false} onChange={(e) => setWatch('resolve_when_absent', e.target.checked)} />
                    <Typography variant="body2">{t('operations.automations.fWatchResolve', 'Marcar un caso como resuelto cuando desaparece de la lista')}</Typography>
                  </Stack>
                </Stack>
              )}
              {(f.condition.mode === 'data' || f.condition.mode === 'sql') && (
                <TextField select size="small" fullWidth label={t('operations.automations.fSource', 'Dónde mira')} value={f.condition.source || 'local'} onChange={(e) => setCond('source', e.target.value)}>
                  <MenuItem value="local">{t('operations.automations.sourceLocal', 'Datos del empleado')}</MenuItem>
                  <MenuItem value="project" disabled={!projectAvailable}>
                    {t('operations.automations.sourceProject', 'Datos del proyecto')}
                    {!projectAvailable ? ` — ${t('operations.automations.sourceUnavailable', 'no disponible')}` : ''}
                  </MenuItem>
                </TextField>
              )}
              {f.condition.mode === 'data' && (
                <>
                  <TextField select size="small" fullWidth label={t('operations.automations.fEntity', 'Entidad')} value={f.condition.table || ''} onChange={(e) => { setCond('table', e.target.value); setCond('column', '') }}>
                    {entities.map((en: any) => <MenuItem key={en.table} value={en.table}>{en.label}</MenuItem>)}
                  </TextField>
                  <Stack direction="row" spacing={1}>
                    <TextField select size="small" sx={{ flex: 1 }} label={t('operations.automations.fAgg', 'Cálculo')} value={f.condition.aggregate || 'count'} onChange={(e) => setCond('aggregate', e.target.value)}>
                      {aggs.map((x: string) => <MenuItem key={x} value={x}>{aggLabel(x)}</MenuItem>)}
                    </TextField>
                    {/* Column picker fed by the REAL schema (catalog.local_schema)
                        so the operator can't typo a column that doesn't exist. */}
                    <Autocomplete
                      size="small" sx={{ flex: 1 }} freeSolo
                      options={(f.condition.source || 'local') === 'local' ? ((catalog?.local_schema || {})[f.condition.table] || []) : []}
                      value={f.condition.column || ''}
                      onChange={(_, v) => setCond('column', v || '')}
                      onInputChange={(_, v) => setCond('column', v || '')}
                      renderInput={(params) => (
                        <TextField {...params} label={t('operations.automations.fColumn', 'Columna (opcional)')} />
                      )}
                    />
                  </Stack>
                  <Stack direction="row" spacing={1}>
                    <TextField select size="small" sx={{ flex: 1 }} label={t('operations.automations.fOperator', 'Condición')} value={f.condition.operator || 'gt'} onChange={(e) => setCond('operator', e.target.value)}>
                      {ops.map((x: string) => <MenuItem key={x} value={x}>{opLabel(x)}</MenuItem>)}
                    </TextField>
                    <TextField size="small" type="number" sx={{ flex: 1 }} label={t('operations.automations.fThreshold', 'Umbral')} value={f.condition.threshold ?? ''} onChange={(e) => setCond('threshold', Number(e.target.value))} />
                  </Stack>
                </>
              )}
              {f.condition.mode === 'agent' && (
                <ExpandableTextField
                  markdown
                  minRows={3}
                  label={t('operations.automations.fAgentCheck', '¿Qué debe comprobar el empleado? (en tus palabras)')}
                  placeholder={t('operations.automations.fAgentCheckPlaceholder', 'Ej.: comprueba en el CRM si hay oportunidades en negociación sin actividad en los últimos 7 días')}
                  helperText={t('operations.automations.fAgentCheckHelp', 'El empleado lo comprueba con sus herramientas (MCPs) al dispararse y solo actúa si se cumple. Es lo más flexible (puede mirar casi cualquier cosa), pero consume más recursos: lanza una ejecución en cada disparo para comprobarlo.')}
                  value={f.condition.instructions || ''}
                  onChange={(v) => setCond('instructions', v)}
                />
              )}
              {f.condition.mode === 'sql' && (
                <ExpandableTextField
                  minRows={3} mono
                  label={t('operations.automations.fSql', 'Consulta SQL (devuelve verdadero/falso)')}
                  placeholder="SELECT COUNT(*) > 0 FROM ... WHERE ..."
                  helperText={t('operations.automations.fSqlHelp', 'Solo lectura (SELECT). Si devuelve un valor verdadero/positivo, la condición se cumple. Permite reglas complejas que el agente puede escribir por ti.')}
                  value={f.condition.sql || ''}
                  onChange={(v) => setCond('sql', v)}
                />
              )}
              {/* ── "Probar" (Test) validator (7.127.0): ad-hoc, unsaved, IN PARTS ── */}
              {(f.condition.mode || 'always') !== 'always' && (
                <Box>
                  <Button size="small" variant="outlined" disabled={testBusy} onClick={() => void testNow()}>
                    {testBusy ? t('operations.automations.testBusy', 'Comprobando…') : t('operations.automations.testNow', 'Probar la condición ahora')}
                  </Button>
                  <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5 }}>
                    {t('operations.automations.testWhat', 'Evalúa la condición tal cual está aquí (sin guardar) y te enseña qué devuelve cada parte.')}
                  </Typography>
                  {testResult && (
                    <Box sx={{ mt: 1 }}>
                      {testResult.evaluable === false ? (
                        <Alert severity="info" onClose={() => setTestResult(null)}>
                          {testResult.error || testResult.note || t('operations.automations.testNotEvaluable', 'No se puede evaluar sin coste.')}
                        </Alert>
                      ) : (
                        <>
                          <Alert severity={testResult.met ? 'success' : 'warning'} onClose={() => setTestResult(null)}>
                            {testResult.gated_by === 'pre'
                              ? t('operations.automations.testPreBlocked', 'El guardián previo NO se cumple → no entraría.')
                              : testResult.met
                                ? t('operations.automations.testMet', 'Ahora mismo SÍ se cumple → el empleado entraría.')
                                : t('operations.automations.testNotMet', 'Ahora mismo NO se cumple → el empleado no entraría.')}
                            {testResult.mode === 'checks' && testResult.combine ? ` (${testResult.combine === 'any' ? t('operations.automations.combineAnyShort', 'basta ALGUNA') : t('operations.automations.combineAllShort', 'hacen falta TODAS')})` : ''}
                          </Alert>
                          {testResult.mode === 'checks' && (
                            <ChecksResultPanel result={testResult} />
                          )}
                          {(testResult.mode === 'data' || testResult.mode === 'sql') && (
                            <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5 }}>
                              {t('operations.automations.testValue', { defaultValue: 'Valor calculado: {{v}}', v: String(testResult.value ?? (testResult.detail?.value ?? '—')) })}
                              {testResult.detail?.error ? ` · ${testResult.detail.error}` : ''}
                            </Typography>
                          )}
                          {/* v7.136.0 — RICH dry-run of the watch: config warnings, shape of the
                              response, counts, per-item table and the why. */}
                          {testResult.mode === 'watch' && (
                            <Stack spacing={0.75} sx={{ mt: 1 }}>
                              {Array.isArray(testResult.warnings) && testResult.warnings.map((w: string, i: number) => (
                                <Alert key={i} severity="warning" sx={{ py: 0 }}>{w}</Alert>
                              ))}
                              {testResult.error && <Alert severity="error" sx={{ py: 0 }}>{testResult.error}</Alert>}
                              {testResult.response_shape && (
                                <Typography variant="caption" color="text.secondary" sx={{ display: 'block' }}>
                                  {t('operations.automations.watchShape', { defaultValue: 'La acción devolvió: {{s}}', s: testResult.response_shape })}
                                </Typography>
                              )}
                              {testResult.counts && (
                                <Stack direction="row" spacing={0.5} flexWrap="wrap" useFlexGap>
                                  <Chip size="small" color="success" variant="outlined" label={t('operations.automations.watchNew', { defaultValue: '{{n}} nuevos', n: testResult.counts.nuevos })} />
                                  <Chip size="small" color="warning" variant="outlined" label={t('operations.automations.watchEsc', { defaultValue: '{{n}} escalados', n: testResult.counts.escalados })} />
                                  <Chip size="small" variant="outlined" label={t('operations.automations.watchSeen', { defaultValue: '{{n}} ya vistos', n: testResult.counts.ya_vistos })} />
                                  <Chip size="small" variant="outlined" label={t('operations.automations.watchIgn', { defaultValue: '{{n}} ignorados', n: testResult.counts.ignorados })} />
                                </Stack>
                              )}
                              {testResult.why && (
                                <Typography variant="caption" color={testResult.would_fire ? 'success.main' : 'text.secondary'} sx={{ display: 'block' }}>
                                  {testResult.why}
                                </Typography>
                              )}
                              {Array.isArray(testResult.items) && testResult.items.length > 0 && (
                                <Box sx={{ maxHeight: 180, overflow: 'auto', borderRadius: 1, p: 0.5, bgcolor: (th) => alpha(th.palette.text.primary, 0.03) }}>
                                  {testResult.items.slice(0, 50).map((it: any, i: number) => (
                                    <Box key={i} sx={{ display: 'flex', alignItems: 'center', gap: 1, py: 0.25 }}>
                                      <Chip size="small" sx={{ minWidth: 78 }}
                                        color={it.class === 'nuevo' ? 'success' : it.class === 'escalado' ? 'warning' : 'default'}
                                        variant={(it.class === 'ya visto' || it.class === 'ignorado') ? 'outlined' : 'filled'}
                                        label={it.class} />
                                      <Typography variant="caption" sx={{ fontFamily: 'monospace' }}>
                                        {it.key || '—'}{it.signature ? ` · firma=${it.signature}` : ''}
                                      </Typography>
                                    </Box>
                                  ))}
                                </Box>
                              )}
                              {testResult.first_run && (
                                <Typography variant="caption" color="text.secondary">
                                  {t('operations.automations.watchFirstRun', 'Primer barrido (sin memoria previa): todo sale como «nuevo». Guarda y vuelve a probar para ver la comparación real.')}
                                </Typography>
                              )}
                            </Stack>
                          )}
                          {/* `checks` mode renders its own `pre` inside ChecksResultPanel. */}
                          {testResult.pre && testResult.mode !== 'checks' && (
                            <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5 }}>
                              {t('operations.automations.testPre', { defaultValue: 'Guardián previo (pre): {{s}}', s: testResult.pre.met ? '✓ se cumple' : '✗ no se cumple' })}
                            </Typography>
                          )}
                        </>
                      )}
                    </Box>
                  )}
                </Box>
              )}
            </Stack>
            )}
          </EditorSection>

          {/* ── 3 · WHAT IT DOES + HOW IT INFORMS YOU ──────────────── */}
          <EditorSection
            n="3"
            title={t('operations.automations.thenTitle2', 'Qué hace')}
            hint={t('operations.automations.thenHint', 'Qué quieres que haga el empleado cuando ocurra.')}
            help={t('operations.automations.help.then', '')}
          >
            <Stack spacing={2}>
              <TextField select size="small" fullWidth label={t('operations.automations.fReaction', 'Acción')} value={f.reaction_type} onChange={(e) => set('reaction_type', e.target.value)}>
                {['act', 'create_task', 'generate_deliverable', 'notify'].map((s) => <MenuItem key={s} value={s}>{reaLabel(s)}</MenuItem>)}
              </TextField>
              {/* COST axis (7.127.0): template reactions can be done
                  directly (0 tokens) or let the AI write them. */}
              {(f.reaction_type === 'create_task' || f.reaction_type === 'notify') && (
                <TextField select size="small" fullWidth
                  label={t('operations.automations.fExecMode', '¿Cómo se hace?')}
                  value={f.execution_mode || 'agent'}
                  onChange={(e) => set('execution_mode', e.target.value)}
                  helperText={t('operations.automations.execModeHelp', 'Con plantilla = directo, sin consumir recursos; con el empleado = lo redacta la IA (consume en cada disparo).')}
                >
                  <MenuItem value="deterministic">{t('operations.automations.execDeterministic', 'Con plantilla (sin coste)')}</MenuItem>
                  <MenuItem value="agent">{t('operations.automations.execAgent', 'Que lo redacte el empleado (consume)')}</MenuItem>
                </TextField>
              )}
              {f.reaction_type === 'notify' && (
                <>
                  <TextField
                    select size="small" fullWidth
                    label={t('operations.automations.fNotifyChannel', 'Avisar por (canal)')}
                    value={f.reaction_spec.channel_id || ''}
                    onChange={(e) => {
                      const ch = channels.find((c: any) => String(c.id) === String(e.target.value))
                      setF((p: any) => ({ ...p, reaction_spec: { ...p.reaction_spec, channel_id: e.target.value, channel_label: ch?.label, channel_kind: ch?.kind } }))
                    }}
                    helperText={channels.length === 0 ? t('operations.automations.noChannels', 'No hay canales configurados. Configura un canal de email o WhatsApp primero.') : undefined}
                  >
                    {channels.map((c: any) => <MenuItem key={c.id} value={c.id}>{c.label}</MenuItem>)}
                  </TextField>
                  <UserPicker
                    kind={notifyChannel?.kind === 'whatsapp' ? 'whatsapp' : 'email'}
                    label={notifyChannel?.kind === 'whatsapp'
                      ? t('operations.automations.fRecipientPhone', 'Destinatario (persona con teléfono)')
                      : t('operations.automations.fRecipientEmail', 'Destinatario (persona del directorio)')}
                    helperText={t('operations.automations.fRecipientHelp', 'Elige una persona del directorio. Vacío = destinatario por defecto del canal.')}
                    value={f.reaction_spec.recipient || null}
                    onChange={(v) => setRea('recipient', v ?? '')}
                  />
                  {/* v7.134.0 — «Qué avisar» (What to notify): the message. With a template it is
                      sent AS IS (0 cost); with the employee, it is completed with what was
                      detected. The chips insert context variables that get substituted. */}
                  <Box>
                    <ExpandableTextField
                      markdown minRows={2}
                      label={t('operations.automations.fNotifyMessage', 'Qué avisar')}
                      placeholder={t('operations.automations.fNotifyMessagePh', 'p. ej. Aviso de {{tenant}}: hay pedidos sin atender ({{now}}).')}
                      helperText={f.execution_mode === 'deterministic'
                        ? t('operations.automations.fNotifyMessageHelpDet', 'Se envía TAL CUAL, sin coste. Inserta variables con los botones de abajo.')
                        : t('operations.automations.fNotifyMessageHelpAgent', 'El empleado enviará este mensaje y lo completará con los datos concretos de lo detectado.')}
                      value={f.reaction_spec.message || ''}
                      onChange={(v) => setRea('message', v)}
                    />
                    <Stack direction="row" spacing={1} sx={{ mt: 1 }} flexWrap="wrap" useFlexGap alignItems="center">
                      <Typography variant="caption" color="text.secondary">
                        {t('operations.automations.varsInsert', 'Insertar variable:')}
                      </Typography>
                      {contextVars.map((v) => (
                        <Tooltip key={v.token} title={v.label || ''}>
                          <Chip size="small" variant="outlined" clickable onClick={() => insertVar(v.token)} label={v.token} />
                        </Tooltip>
                      ))}
                    </Stack>
                  </Box>
                </>
              )}
              {f.reaction_type === 'generate_deliverable' && (
                <TextField select size="small" fullWidth label={t('operations.automations.fFormat', 'Formato')} value={f.reaction_spec.format || 'pdf'} onChange={(e) => setRea('format', e.target.value)}>
                  {['pdf', 'xlsx', 'pptx'].map((x) => <MenuItem key={x} value={x}>{x.toUpperCase()}</MenuItem>)}
                </TextField>
              )}
              {/* v7.134.0 — create_task = an ASSIGNMENT to DELEGATE / POSTPONE / FOLLOW UP.
                  It is not "do it now" (that is «Actuar», act). It exposes who it is delegated
                  to and the priority, which were not editable in the form before. */}
              {f.reaction_type === 'create_task' && (
                <Box sx={{ borderRadius: 1, p: 1.5, bgcolor: (th) => alpha(th.palette.info.main, 0.06) }}>
                  <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 1 }}>
                    {t('operations.automations.taskWhat', 'Un encargo es para DELEGAR a una persona, APLAZAR o hacer SEGUIMIENTO. Si es algo que el empleado puede hacer ya, elige «Actuar» en su lugar.')}
                  </Typography>
                  <Stack spacing={1.5}>
                    <TextField select size="small" fullWidth
                      label={t('operations.automations.fAssignee', '¿Quién lo hace?')}
                      value={f.reaction_spec.assignee || 'agent'}
                      onChange={(e) => setRea('assignee', e.target.value)}>
                      <MenuItem value="agent">{t('operations.automations.assigneeAgent', 'El empleado (más tarde / seguimiento)')}</MenuItem>
                      <MenuItem value="user">{t('operations.automations.assigneeUser', 'Una persona (delegar)')}</MenuItem>
                    </TextField>
                    {f.reaction_spec.assignee === 'user' && (
                      <UserPicker kind="email"
                        label={t('operations.automations.fAssignedTo', 'Persona a la que se delega')}
                        helperText={t('operations.automations.fAssignedToHelp', 'Elige a quién se le encarga (del directorio).')}
                        value={f.reaction_spec.assigned_to || null}
                        onChange={(v) => setRea('assigned_to', v ?? '')} />
                    )}
                    <TextField select size="small" fullWidth
                      label={t('operations.automations.fPriority', 'Prioridad')}
                      value={f.reaction_spec.priority || 'medium'}
                      onChange={(e) => setRea('priority', e.target.value)}>
                      {['low', 'medium', 'high', 'urgent'].map((p) => (
                        <MenuItem key={p} value={p}>{t(`operations.automations.priority.${p}`, p)}</MenuItem>
                      ))}
                    </TextField>
                  </Stack>
                </Box>
              )}
              {/* The WHAT IT DOES in natural language. For `notify` it is NOT shown: its
                  content is the «Qué avisar» (What to notify) editor above (not two fields). */}
              {f.reaction_type !== 'notify' && (
                <ExpandableTextField markdown minRows={2}
                  label={f.reaction_type === 'create_task'
                    ? t('operations.automations.fTaskDetail', 'Detalle del encargo (en tus palabras)')
                    : t('operations.automations.fInstructions', '¿Qué quieres que haga? (en tus palabras)')}
                  value={f.reaction_instructions} onChange={(v) => set('reaction_instructions', v)} />
              )}

              {/* ── The COMMUNICATION axis, separate from the WHAT IT DOES ── */}
              {f.reaction_type !== 'notify' && (
                <Box sx={{ borderRadius: 1, p: 1.5, bgcolor: (th) => alpha(th.palette.info.main, 0.06) }}>
                  <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>
                    {t('operations.automations.informTitle', 'Y cómo te informa')}
                  </Typography>
                  <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 1 }}>
                    {t('operations.automations.informNote', 'Haga lo que haga, SIEMPRE queda registrado en «Actividad del agente». Además, puede avisarte del resultado por un canal.')}
                  </Typography>
                  <ToggleButtonGroup
                    exclusive size="small"
                    value={informMode}
                    onChange={(_, v) => { if (v) setInformMode(v) }}
                  >
                    <ToggleButton value="activity" sx={{ textTransform: 'none' }}>
                      {t('operations.automations.informActivity', 'Solo registrarlo')}
                    </ToggleButton>
                    <ToggleButton value="channel" sx={{ textTransform: 'none' }} disabled={channels.length === 0}>
                      {t('operations.automations.informChannel', 'Además, avisarme por un canal')}
                    </ToggleButton>
                  </ToggleButtonGroup>
                  {channels.length === 0 && (
                    <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5 }}>
                      {t('operations.automations.informNoChannels', 'Para recibir avisos configura primero un canal (email/WhatsApp) en «Canales».')}
                    </Typography>
                  )}
                  {informMode === 'channel' && channels.length > 0 && (
                    <Stack spacing={1.5} sx={{ mt: 1.5 }}>
                      <TextField
                        select size="small" fullWidth
                        label={t('operations.automations.fNotifyChannel', 'Avisar por (canal)')}
                        value={f.reaction_spec.notify?.channel_id || ''}
                        onChange={(e) => {
                          const ch = channels.find((c: any) => String(c.id) === String(e.target.value))
                          setRea('notify', { ...(f.reaction_spec.notify || {}), channel_id: e.target.value, channel_label: ch?.label, channel_kind: ch?.kind })
                        }}
                      >
                        {channels.map((c: any) => <MenuItem key={c.id} value={c.id}>{c.label}</MenuItem>)}
                      </TextField>
                      <UserPicker
                        kind={informChannel?.kind === 'whatsapp' ? 'whatsapp' : 'email'}
                        label={informChannel?.kind === 'whatsapp'
                          ? t('operations.automations.fRecipientPhone', 'Destinatario (persona con teléfono)')
                          : t('operations.automations.fRecipientEmail', 'Destinatario (persona del directorio)')}
                        helperText={t('operations.automations.fRecipientHelp', 'Elige una persona del directorio. Vacío = destinatario por defecto del canal.')}
                        value={f.reaction_spec.notify?.recipient || null}
                        onChange={(v) => setRea('notify', { ...(f.reaction_spec.notify || {}), recipient: v ?? '' })}
                      />
                    </Stack>
                  )}
                </Box>
              )}

              {/* Approval: ONE single concept, in one single place. */}
              <Stack direction="row" spacing={1} alignItems="center">
                <Switch size="small" checked={f.requires_approval} onChange={(e) => set('requires_approval', e.target.checked)} />
                <Typography variant="body2">{t('operations.automations.fApproval', 'Requiere tu aprobación')}</Typography>
                <Tooltip title={t('operations.automations.fApprovalHelp', 'Antes de ejecutar la acción, el empleado la deja pendiente en «Aprobaciones» y espera tu visto bueno.')}>
                  <Typography variant="caption" color="info.main" sx={{ cursor: 'help' }}>ⓘ</Typography>
                </Tooltip>
              </Stack>
              {/* v7.63.0 — check-before-approve, only relevant for a natural-language
                  condition that also requires approval. Default ON: verify FIRST,
                  then ask for approval only if it holds (with the evidence). */}
              {condOpen && f.condition.mode === 'agent' && f.requires_approval && (
                <Box sx={{ borderRadius: 1, p: 1.5, bgcolor: (th) => alpha(th.palette.success.main, 0.06) }}>
                  <Stack direction="row" spacing={1} alignItems="center">
                    <Switch size="small" checked={f.check_before_approval} onChange={(e) => set('check_before_approval', e.target.checked)} />
                    <Typography variant="body2" sx={{ fontWeight: 600 }}>
                      {t('operations.automations.fCheckBeforeApproval', 'Comprobar antes de pedir aprobación')}
                    </Typography>
                  </Stack>
                  <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5 }}>
                    {f.check_before_approval
                      ? t('operations.automations.fCheckBeforeApprovalOn', 'El empleado verifica la condición (solo lectura) ANTES de pedirte aprobación, y solo te la pide si se cumple —con la evidencia que encontró—. Gasta una comprobación extra por disparo, pero nunca apruebas a ciegas.')
                      : t('operations.automations.fCheckBeforeApprovalOff', 'Se te pide aprobación primero y el empleado comprueba la condición al ejecutar (más barato, pero apruebas antes de saber si se cumple).')}
                  </Typography>
                </Box>
              )}
            </Stack>
          </EditorSection>

          {/* ── Final settings ─────────────────────────────────────── */}
          <Stack direction="row" spacing={2} alignItems="flex-start" flexWrap="wrap" useFlexGap>
            <TextField
              size="small" sx={{ flex: 1, minWidth: 220 }}
              label={t('operations.automations.fTitleOptional', 'Nombre (opcional)')}
              placeholder={sentence.slice(0, 60)}
              helperText={t('operations.automations.fTitleHelp', 'Vacío = se usa la frase de arriba.')}
              value={f.title}
              onChange={(e) => set('title', e.target.value)}
            />
            <TextField
              select size="small" sx={{ width: 220 }}
              label={t('operations.automations.fCooldown2', 'No repetir antes de')}
              value={cooldownIsPreset ? String(f.cooldown_sec) : 'custom'}
              onChange={(e) => { if (e.target.value !== 'custom') set('cooldown_sec', Number(e.target.value)) }}
              helperText={t('operations.automations.fCooldownHelp', 'Pausa mínima entre disparos seguidos (anti-repetición).')}
            >
              <MenuItem value="0">{t('operations.automations.cooldown.none', 'Sin pausa')}</MenuItem>
              <MenuItem value="900">{t('operations.automations.cooldown.m15', '15 minutos')}</MenuItem>
              <MenuItem value="3600">{t('operations.automations.cooldown.h1', '1 hora')}</MenuItem>
              <MenuItem value="21600">{t('operations.automations.cooldown.h6', '6 horas')}</MenuItem>
              <MenuItem value="86400">{t('operations.automations.cooldown.h24', '24 horas')}</MenuItem>
              <MenuItem value="custom">{t('operations.automations.cooldown.custom', 'Personalizado…')}</MenuItem>
            </TextField>
            {!cooldownIsPreset && (
              <TextField size="small" type="number" sx={{ width: 160 }}
                label={t('operations.automations.fCooldownCustom', 'Segundos')}
                value={f.cooldown_sec}
                onChange={(e) => set('cooldown_sec', Number(e.target.value))} />
            )}
          </Stack>
          <Box component="details">
            <Box component="summary" sx={{ cursor: 'pointer', typography: 'caption', color: 'info.main' }}>
              {t('operations.automations.help.fieldsTitle', 'Los demás campos')}
            </Box>
            <Typography variant="caption" color="text.secondary" sx={{ whiteSpace: 'pre-line', display: 'block', mt: 0.5 }}>
              {t('operations.automations.help.fields', '')}
              {'\n'}
              {t('operations.automations.help.tip', '')}
            </Typography>
          </Box>
        </Stack>
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 2, gap: 1, flexWrap: 'wrap' }}>
        {descMissing && (
          <Typography variant="caption" color="warning.main" sx={{ width: '100%', textAlign: 'right' }}>
            {t('operations.automations.form.descriptionRequiredShort', 'Añade la descripción (obligatoria) para guardar.')}
          </Typography>
        )}
        <Stack direction="row" spacing={1} alignItems="center" sx={{ mr: 'auto' }}>
          <Switch size="small" checked={f.enabled} onChange={(e) => set('enabled', e.target.checked)} />
          <Typography variant="body2">{t('operations.automations.fActivateOnSave', 'Activar al guardar')}</Typography>
        </Stack>
        <Button onClick={onClose}>{t('common.cancel', 'Cancelar')}</Button>
        <Tooltip title={descMissing ? t('operations.automations.form.descriptionRequiredShort', 'Añade la descripción (obligatoria) para guardar.') : ''}>
          <span>
            <Button variant="contained" disabled={busy || descMissing} onClick={() => void save()}>{t('common.save', 'Guardar')}</Button>
          </span>
        </Tooltip>
      </DialogActions>
    </Dialog>
  )
}


function HealthBanner({ connectorsNeedingAuth, onGoConnectors }: {
  connectorsNeedingAuth: number
  onGoConnectors: () => void
}) {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const q = useQuery({
    queryKey: ['agent-health'],
    queryFn: () => apiFetch('/api/agent/health').then(async (r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      return r.json()
    }),
    staleTime: 30_000,
    refetchInterval: 60_000,
    retry: false,
  })
  const serverBlockers: any[] = q.data?.blockers ?? []
  // The connector-down blocker is composed here: the page already probes the
  // Composio connectors, so the health endpoint deliberately doesn't re-probe.
  const blockers = [...serverBlockers]
  if (connectorsNeedingAuth > 0) {
    blockers.push({ key: 'connectors_down', severity: 'warning', params: { count: connectorsNeedingAuth } })
  }
  if (!blockers.length) return null

  // The wording lives in components/healthNotices.ts — shared by this
  // screen and the top-bar bell. A single copy by design:
  // two identical sentences in two files end up being two different sentences.
  const text = (b: any) => healthNoticeText(b, t)

  const link = (b: any): { label: string; go: () => void } | null => {
    if (b.key === 'connectors_down') return { label: t('operations.health.fix', 'Revisar'), go: onGoConnectors }
    if (b.key === 'triggers_off' || b.key === 'no_active_automations') return { label: t('operations.health.fix', 'Revisar'), go: () => { window.location.hash = '#disparadores' } }
    if (b.key === 'channels_down') return { label: t('operations.health.fix', 'Revisar'), go: () => navigate('/channels') }
    return null
  }

  return (
    <Stack spacing={1} sx={{ mb: 2 }}>
      {blockers.map((b, i) => {
        const lk = link(b)
        return (
          <Alert
            key={`${b.key}-${i}`}
            severity={b.severity === 'error' ? 'error' : 'warning'}
            action={lk ? <Button color="inherit" size="small" onClick={lk.go}>{lk.label}</Button> : undefined}
          >
            {text(b)}
          </Alert>
        )
      })}
    </Stack>
  )
}


const ACTIVITY_KIND_ICON: Record<string, JSX.Element> = {
  alert: <ErrorOutlineIcon fontSize="small" color="warning" />,
  recommendation: <FactIcon fontSize="small" color="info" />,
  communication: <ChannelSessionIcon fontSize="small" color="action" />,
  scoring: <SpeedIcon fontSize="small" color="action" />,
  decision: <OkIcon fontSize="small" color="success" />,
  scheduled_item: <ScheduleIcon fontSize="small" color="action" />,
  approval: <AuthIcon fontSize="small" color="action" />,
  automation_fired: <TriggerIcon fontSize="small" color="primary" />,
}

// Exported so the user-facing "Actividad" page (pages/Activity.tsx) can reuse
// the exact same business-activity feed — it is a boss-facing accountability
// view, not administration, so it lives in the user nav group, not here.
// Which canonical output table each activity kind lives in — so a feed item can
// deep-link to its OWN element (the alert/recommendation/…) instead of only the
// run that produced it. `scheduled_item`/`approval` have dedicated pages;
// `automation_fired` has no per-item row.
const ACTIVITY_KIND_TABLE: Record<string, string> = {
  alert: 'alerts', recommendation: 'recommendations', communication: 'communications',
  decision: 'decisions', scoring: 'scoring',
}

export function ActivitySection() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const { manifest } = useManifest()
  const domainEntities: any[] = (manifest as any)?.domain_entities ?? []
  const slugForTable = (table: string): string | null => {
    const e = domainEntities.find((x) =>
      String(x?.table ?? '').toLowerCase() === table || String(x?.slug ?? '').toLowerCase() === table)
    return e?.slug ?? null
  }
  // The row's PRIMARY link: its own element. Falls back to the run/event only
  // when the element has no browsable surface (keeps old behaviour as a net).
  const elementLink = (it: any): string | null => {
    if (it.kind === 'approval') return '/approvals'
    // v7.49.0 — deep-link straight to the assignment's (`encargo`) detail (not the list) so the
    // Actividad feed opens the actual delegated task.
    if (it.kind === 'scheduled_item') return it.id != null ? `/encargos/${encodeURIComponent(String(it.id))}` : '/encargos'
    if (it.kind === 'automation_fired') return it.event_id ? `/events/${it.event_id}` : (it.run_id ? `/runs/${it.run_id}` : null)
    const table = ACTIVITY_KIND_TABLE[it.kind]
    if (table && it.id != null) {
      const slug = slugForTable(table)
      if (slug) return `/domain/${slug}/${encodeURIComponent(String(it.id))}`
    }
    return it.run_id ? `/runs/${it.run_id}` : it.event_id ? `/events/${it.event_id}` : null
  }
  const [days, setDays] = useState<number>(7)
  // Filter the feed by activity kind (null = all). Only kinds that actually
  // appear in the feed are clickable (e.g. automation_fired is counted but has
  // no per-item row, so it stays a plain badge).
  const [kindFilter, setKindFilter] = useState<string | null>(null)
  const q = useQuery({
    queryKey: ['agent-activity', days],
    queryFn: () => apiFetch(`/api/agent/activity?days=${days}`).then(async (r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      return r.json()
    }),
    staleTime: 30_000,
    retry: false,
  })
  const kindLabel = (k: string) =>
    t(`operations.activity.kind.${k}`, k.replace(/_/g, ' '))
  // Humanise the raw status/decision values (sent, deferred, no_action…) into
  // business language; unknown values fall back to a de-underscored string.
  const statusLabel = (s: string) =>
    t(`operations.activity.statusLabel.${s}`, s.replace(/_/g, ' '))
  const statusColor = (s: string): 'success' | 'error' | 'warning' | 'default' => {
    if (['sent', 'resolved', 'accepted', 'approved', 'executed', 'done', 'replied'].includes(s)) return 'success'
    if (['failed', 'rejected', 'error', 'overdue'].includes(s)) return 'error'
    if (['pending', 'open', 'draft', 'ready', 'scheduled', 'in_progress'].includes(s)) return 'warning'
    return 'default'
  }
  // What to show as the row's main text: decisions store the verb in `title`
  // (humanise it), scorings store a metric key (de-underscore it), the rest
  // show their title (or the type when blank).
  const displayTitle = (it: any): string => {
    if (it.kind === 'decision') return statusLabel(String(it.title || 'no_action'))
    if (it.kind === 'scoring') return String(it.title || '').replace(/_/g, ' ')
    return it.title || kindLabel(it.kind)
  }
  const totals: Record<string, number> = q.data?.totals ?? {}
  const feed: any[] = q.data?.feed ?? []
  const totalActions: number = q.data?.total_actions ?? 0
  const feedKinds = new Set<string>(feed.map((it) => it.kind))
  const shownFeed = kindFilter ? feed.filter((it) => it.kind === kindFilter) : feed
  // v7.45.0 — paginate the feed so a busy employee's history is not one giant
  // scroll. Resets to page 1 whenever the range or the kind filter changes.
  const PER_PAGE = 20
  const [page, setPage] = useState(1)
  useEffect(() => { setPage(1) }, [kindFilter, days])
  const pageCount = Math.max(1, Math.ceil(shownFeed.length / PER_PAGE))
  const curPage = Math.min(page, pageCount)
  const pageFeed = shownFeed.slice((curPage - 1) * PER_PAGE, curPage * PER_PAGE)

  return (
    <Box>
      <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mb: 2, flexWrap: 'wrap', gap: 1 }}>
        <Typography variant="body2" color="text.secondary">
          {t('operations.activity.intro', 'Lo que ha hecho el empleado, en lenguaje de negocio. (Para coste y rendimiento técnico, ve a Ejecuciones.)')}
        </Typography>
        <ToggleButtonGroup
          size="small"
          exclusive
          value={days}
          onChange={(_, v) => { if (v) setDays(v) }}
        >
          <ToggleButton value={7}>{t('operations.activity.d7', '7 días')}</ToggleButton>
          <ToggleButton value={30}>{t('operations.activity.d30', '30 días')}</ToggleButton>
        </ToggleButtonGroup>
      </Stack>

      {q.isLoading && <Skeleton variant="rounded" height={120} />}

      {q.isError && <Alert severity="error">{t('operations.loadError', 'No se pudo cargar. Reintenta en unos segundos.')}</Alert>}

      {!q.isLoading && !q.isError && totalActions === 0 && (
        <Alert severity="info">{t('operations.activity.empty', 'Sin actividad en este periodo.')}</Alert>
      )}

      {!q.isLoading && !q.isError && totalActions > 0 && (
        <>
          <Stack direction="row" spacing={1} sx={{ mb: 2, flexWrap: 'wrap', gap: 1 }}>
            <Chip
              label={t('operations.activity.filterAll', 'Todos')}
              color={kindFilter === null ? 'primary' : 'default'}
              variant={kindFilter === null ? 'filled' : 'outlined'}
              onClick={() => setKindFilter(null)}
            />
            {Object.entries(totals)
              .filter(([, n]) => (n as number) > 0)
              .map(([k, n]) => {
                const filterable = feedKinds.has(k)  // automation_fired has no per-item rows
                return (
                  <Chip
                    key={k}
                    icon={ACTIVITY_KIND_ICON[k]}
                    label={`${kindLabel(k)}: ${n}`}
                    color={kindFilter === k ? 'primary' : 'default'}
                    variant={kindFilter === k ? 'filled' : 'outlined'}
                    onClick={filterable ? () => setKindFilter(kindFilter === k ? null : k) : undefined}
                  />
                )
              })}
          </Stack>
          <Card variant="outlined">
            <List dense disablePadding>
              {pageFeed.map((it, i) => {
                // Decisions keep their verb in `title`, so don't also show a
                // raw status chip for them (it would duplicate / confuse).
                const status = it.kind !== 'decision' && it.status ? String(it.status) : ''
                // Deep-link to THIS element (the alert / recommendation / … it
                // represents), not the run — that was the confusing part. Falls
                // back to the run/event when the element has no browsable surface.
                const target = elementLink(it)
                const text = (
                  <ListItemText
                    primary={
                      <Stack direction="row" spacing={1} alignItems="center" sx={{ flexWrap: 'wrap' }} useFlexGap>
                        <Chip size="small" variant="outlined" label={kindLabel(it.kind)}
                          sx={{ height: 20, '& .MuiChip-label': { px: 0.75, fontSize: 11, fontWeight: 600 } }} />
                        <Typography variant="body2" component="span">{displayTitle(it)}</Typography>
                        {status && (
                          <Chip size="small" color={statusColor(status)} label={statusLabel(status)}
                            sx={{ height: 20, '& .MuiChip-label': { px: 0.75, fontSize: 11 } }} />
                        )}
                      </Stack>
                    }
                    secondary={formatDateTime(it.at)}
                  />
                )
                const icon = <Box sx={{ mr: 1.5, mt: 0.25, display: 'flex' }}>{ACTIVITY_KIND_ICON[it.kind] ?? <HistoryIcon fontSize="small" />}</Box>
                const key = `${it.kind}-${it.id}-${i}`
                return target ? (
                  <ListItemButton key={key} divider={i < pageFeed.length - 1} sx={{ alignItems: 'flex-start' }}
                    onClick={() => navigate(target)}
                    title={t('operations.activity.viewDetail', 'Ver el detalle de esta acción') as string}>
                    {icon}
                    {text}
                    <LinkIcon fontSize="small" sx={{ color: 'action.active', ml: 1, mt: 0.5 }} />
                  </ListItemButton>
                ) : (
                  <ListItem key={key} divider={i < pageFeed.length - 1} sx={{ alignItems: 'flex-start' }}>
                    {icon}
                    {text}
                  </ListItem>
                )
              })}
            </List>
          </Card>
          {shownFeed.length > PER_PAGE && (
            <Stack direction="row" spacing={2} alignItems="center" justifyContent="flex-end" sx={{ mt: 1.5 }}>
              <Typography variant="caption" color="text.secondary">
                {t('operations.activity.showing', {
                  from: (curPage - 1) * PER_PAGE + 1,
                  to: Math.min(curPage * PER_PAGE, shownFeed.length),
                  total: shownFeed.length,
                  defaultValue: `${(curPage - 1) * PER_PAGE + 1}–${Math.min(curPage * PER_PAGE, shownFeed.length)} / ${shownFeed.length}`,
                })}
              </Typography>
              <Button size="small" disabled={curPage <= 1} onClick={() => setPage((p) => Math.max(1, p - 1))}>
                {t('operations.activity.prev', 'Anterior')}
              </Button>
              <Button size="small" disabled={curPage >= pageCount} onClick={() => setPage((p) => Math.min(pageCount, p + 1))}>
                {t('operations.activity.next', 'Siguiente')}
              </Button>
            </Stack>
          )}
        </>
      )}
    </Box>
  )
}


// Pick the operator's locale from a bilingual {es,en} text bag. Returns
// null when the bag has no usable text (callers chain a fallback).
function pickLocaleText(bag: any, lang: string): string | null {
  if (!bag) return null
  if (typeof bag === 'string') return bag.trim() || null
  const short = (lang || 'es').slice(0, 2)
  const v = bag[short] || bag.es || bag.en || ''
  return String(v).trim() || null
}

type AgentSessionRow = {
  scope_key: string
  role: string
  user_email: string | null
  // v7.25.0 — explicit association: the PERSON (directory email + name),
  // the surface (chat|channel) and the channel it lives on.
  origin?: 'chat' | 'channel'
  memory_user?: string | null
  display_name?: string
  channel_id?: number | null
  channel_label?: string | null
  channel_kind?: string | null
  workspace_subdir: string
  message_count: number
  context_tokens?: number
  status: string
  session_id: string
  created_at: string | null
  last_used_at: string | null
  // v7.143.0 — which version of the instructions the session was primed with, and whether that
  // version is no longer current (it is re-primed on the next message).
  prompt_contract?: string
  prompt_contract_stale?: boolean
}

function CapabilityRow({ a }: { a: any }) {
  const { t, i18n } = useTranslation()
  const desc = pickLocaleText(a.business_description, i18n.language) ?? (a.summary || null)
  const where: string[] = (a.ui_effects ?? [])
    .map((u: any) => (typeof u === 'string' ? null : u?.label))
    .filter(Boolean)
  return (
    <Box sx={{ py: 1, '&:not(:last-of-type)': { borderBottom: '1px solid', borderColor: 'divider' } }}>
      <Typography variant="body2" sx={{ fontWeight: 600 }}>{a.label || a.type}</Typography>
      {desc && <Typography variant="body2" color="text.secondary">{desc}</Typography>}
      {where.length > 0 && (
        <Typography variant="caption" color="text.secondary">
          {t('operations.actions.appearsIn')}: {where.join(' · ')}
        </Typography>
      )}
    </Box>
  )
}

// Operations → "Rutina del empleado": the design-time routines (what the
// employee ALWAYS does, regardless of the boss's standing orders) + its base
// capabilities. Read/monitor + per-watcher tuning only. The operator-configured
// "Trabajo autónomo" + the initiative dials live on the user-facing page
// (pages/AutonomousWork.tsx), NOT in this admin surface.
function AutomaticWorkSection({ actions }: { actions: any[] }) {
  const { t } = useTranslation()
  // v7.27.0 — the design-time watcher cards ("disparadores") are GONE:
  // recurring domain work now lives in Trabajo autónomo (automations the
  // boss OR the employee itself creates via create_automation). What stays
  // here is the truly GENERIC routine of every employee: the autonomy
  // dials, the master kill-switch and the base capabilities.
  const domainActions = actions.filter((a) => !a.system)
  const systemActions = actions.filter((a) => a.system)
  const baseCapabilities = [...domainActions, ...systemActions]
  return (
    <Stack spacing={4}>
      <Box>
        <Typography variant="subtitle1" sx={{ fontWeight: 700 }}>
          {t('operations.auto.routinesTitle', 'Iniciativa del empleado')}
        </Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
          {t('operations.auto.movedHint', 'Los mandos de iniciativa —nivel de autonomía, interruptor de ejecución autónoma, concurrencia y auto-programación— se gestionan ahora en Comportamiento.')}
        </Typography>
        <Button component={RouterLink} to="/capacidades" size="small" variant="outlined" sx={{ textTransform: 'none' }}>
          {t('operations.auto.openBehavior', 'Ir a Comportamiento →')}
        </Button>
      </Box>

      {/* HOW IT WORKS BY DEFAULT — compact list, no controls, no raw tables. */}
      {baseCapabilities.length > 0 && <Divider />}
      {baseCapabilities.length > 0 && (
        <Box>
          <Typography variant="subtitle1" sx={{ fontWeight: 700 }}>
            {t('operations.auto.systemTitle', 'Cómo trabaja de base')}
          </Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
            {t('operations.auto.systemHint', 'Capacidades que el empleado siempre tiene: atiende encargos vencidos, tareas que le delegas y aprobaciones que le concedes.')}
          </Typography>
          <Card variant="outlined">
            <CardContent sx={{ py: 0.5, '&:last-child': { pb: 0.5 } }}>
              {baseCapabilities.map((a: any) => <CapabilityRow key={a.type} a={a} />)}
            </CardContent>
          </Card>
        </Box>
      )}
    </Stack>
  )
}

// Compact token count for the context-usage row ("12.3k", "850").
function fmtSessionTokens(n?: number): string {
  const v = Number(n) || 0
  if (v <= 0) return '—'
  if (v < 1000) return String(v)
  return `${(v / 1000).toFixed(v < 10000 ? 1 : 0)}k`
}

function SessionsBlock() {
  const { t, i18n } = useTranslation()
  const qc = useQueryClient()
  const { confirm, dialog: confirmDialog } = useConfirm()
  const [feedback, setFeedback] = useState<{ kind: 'success' | 'error'; msg: string } | null>(null)

  const query = useQuery({
    queryKey: ['agent-sessions'],
    queryFn: () => apiFetch('/api/agent/sessions').then(async (r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      return r.json() as Promise<{ sessions: AgentSessionRow[] }>
    }),
    refetchInterval: 30_000,
    refetchIntervalInBackground: false,
    retry: false,
  })

  const reset = useMutation({
    mutationFn: (scopeKey: string) => apiFetch('/api/agent/sessions/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scope_key: scopeKey }),
    }).then(async (r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      return r.json()
    }),
    onSuccess: () => {
      setFeedback({ kind: 'success', msg: t('operations.sessions.resetOk') })
      setTimeout(() => setFeedback(null), 5000)
      qc.invalidateQueries({ queryKey: ['agent-sessions'] })
    },
    onError: (err) => {
      setFeedback({ kind: 'error', msg: extractApiError(err) })
      setTimeout(() => setFeedback(null), 6000)
    },
  })

  const fmt = (iso: string | null) => formatDateTime(iso)
  // The logged-in operator's own interactive session — highlighted so they
  // can spot "this is me / the chat I'm using right now" at a glance.
  const myEmail = (() => {
    try { return (localStorage.getItem('email') || '').trim().toLowerCase() }
    catch { return '' }
  })()
  const sessions = query.data?.sessions ?? []
  // v7.25.0 — group by PERSON: one card per human with ALL their sessions
  // (chat + one per channel). They share one workspace and one memory; only
  // the conversational context is per-session. Unidentified senders
  // (unmapped phones) have no person and render as loose cards.
  const personKeyOf = (s: AgentSessionRow) => (
    s.memory_user || (s.origin !== 'channel' ? s.user_email : '') || ''
  ).trim().toLowerCase()
  const byPerson = new Map<string, AgentSessionRow[]>()
  const anonymous: AgentSessionRow[] = []
  for (const s of sessions) {
    const k = personKeyOf(s)
    if (!k) { anonymous.push(s); continue }
    byPerson.set(k, [...(byPerson.get(k) || []), s])
  }
  const lastUsed = (rows: AgentSessionRow[]) =>
    rows.map((r) => r.last_used_at || '').sort().reverse()[0] || ''
  const people = [...byPerson.entries()].sort((a, b) => {
    // The logged-in operator's card floats to the top; then by recency.
    const am = a[0] === myEmail ? 1 : 0
    const bm = b[0] === myEmail ? 1 : 0
    if (am !== bm) return bm - am
    return lastUsed(b[1]).localeCompare(lastUsed(a[1]))
  })

  if (query.isLoading) {
    return (
      <Grid container spacing={2}>
        {[0, 1, 2, 3].map((i) => (
          <Grid item xs={12} md={6} key={i}>
            <Card variant="outlined" sx={{ borderRadius: 2 }}>
              <CardContent>
                <Skeleton variant="text" width="60%" height={28} />
                <Skeleton variant="text" width="90%" />
                <Skeleton variant="text" width="80%" />
                <Skeleton variant="rounded" width={120} height={30} sx={{ mt: 1 }} />
              </CardContent>
            </Card>
          </Grid>
        ))}
      </Grid>
    )
  }
  if (query.error) {
    return <Alert severity="error">{String((query.error as any)?.message ?? query.error)}</Alert>
  }

  // One row inside a person card (their chat session or one channel session).
  const renderSessionLine = (s: AgentSessionRow) => {
    const isChat = (s.origin || (s.role === 'interactive' ? 'chat' : 'channel')) === 'chat'
    const label = isChat
      ? t('operations.sessions.chatSession', 'Chat')
      : `${s.channel_label || s.scope_key.replace(/^channel:/, '')}${s.channel_kind ? ` (${s.channel_kind})` : ''}`
    return (
      <Stack key={s.scope_key} direction="row" spacing={1} alignItems="center">
        {isChat
          ? <UserIcon fontSize="small" color="primary" />
          : <ChannelSessionIcon fontSize="small" color="secondary" />}
        <Box sx={{ flex: 1, minWidth: 0 }}>
          <Stack direction="row" spacing={0.75} alignItems="center">
            <Typography variant="body2" sx={{ fontWeight: 600 }}>{label}</Typography>
            {/* v7.143.0 — this session was primed with instructions OLDER than those
                of the deployed version, so the employee may deny here
                capabilities it already has. It is re-primed only on the next message;
                the notice is there so that an unexpected «I can't» has an explanation. */}
            {s.prompt_contract_stale && (
              <Tooltip title={t('operations.sessions.staleContractHint', 'Esta conversación empezó con una versión anterior de las instrucciones del empleado, así que puede que no conozca lo último (por ejemplo, guardar en su propia memoria). Se pondrá al día automáticamente en el siguiente mensaje.') as string}>
                <Chip
                  size="small" variant="outlined" color="warning"
                  label={t('operations.sessions.staleContract', 'Instrucciones por actualizar')}
                />
              </Tooltip>
            )}
          </Stack>
          <Typography variant="caption" color="text.secondary">
            {t('operations.sessions.messages')}: {s.message_count}
            {' · '}{t('operations.sessions.context')}: {fmtSessionTokens(s.context_tokens)} {t('operations.sessions.tokens')}
            {' · '}{t('operations.sessions.lastUsed')}: {fmt(s.last_used_at)}
            {' · '}<code>{s.session_id}…</code>
          </Typography>
        </Box>
        <Tooltip title={t('operations.sessions.reset') as string}>
          <span>
            <IconButton
              size="small"
              color="warning"
              disabled={reset.isPending}
              onClick={() => confirm(
                {
                  message: t('operations.sessions.resetConfirm', 'Esto borra la conversación/contexto de esta sesión del agente. ¿Continuar?'),
                  danger: true,
                  confirmLabel: t('operations.sessions.reset'),
                },
                () => reset.mutate(s.scope_key),
              )}
            >
              <ResetIcon fontSize="small" />
            </IconButton>
          </span>
        </Tooltip>
      </Stack>
    )
  }

  // A person = their chat session + one session per channel, sharing ONE
  // workspace folder and ONE memory (the association this card makes visible).
  const renderPersonCard = (personKey: string, rows: AgentSessionRow[]) => {
    const name = rows.map((r) => r.display_name).find(Boolean) || personKey
    const isMine = personKey === myEmail
    const workspace = rows[0]?.workspace_subdir
    const ordered = [...rows].sort((a, b) => {
      const ac = (a.origin || (a.role === 'interactive' ? 'chat' : 'channel')) === 'chat' ? 0 : 1
      const bc = (b.origin || (b.role === 'interactive' ? 'chat' : 'channel')) === 'chat' ? 0 : 1
      if (ac !== bc) return ac - bc
      return (b.last_used_at || '').localeCompare(a.last_used_at || '')
    })
    return (
      <Grid item xs={12} md={6} key={personKey}>
        <Card
          variant="outlined"
          sx={{
            borderRadius: 2, height: '100%',
            ...(isMine ? { borderColor: 'primary.main', borderWidth: 2, bgcolor: 'action.hover' } : {}),
          }}
        >
          <CardContent>
            <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.5 }}>
              <UserIcon fontSize="small" color="primary" />
              <Box sx={{ flex: 1, minWidth: 0 }}>
                <Typography variant="subtitle2" sx={{ wordBreak: 'break-all' }}>{name}</Typography>
                {name !== personKey && (
                  <Typography variant="caption" color="text.secondary" sx={{ wordBreak: 'break-all' }}>
                    {personKey}
                  </Typography>
                )}
              </Box>
              {isMine && (
                <Chip size="small" color="primary" label={t('operations.sessions.currentSession', 'Tú')} />
              )}
              <Chip
                size="small" variant="outlined"
                label={t('operations.sessions.sessionCount', '{{n}} sesiones', { n: ordered.length })}
              />
            </Stack>
            <Stack spacing={0.5} sx={{ mb: 1.5 }}>
              <Typography variant="caption" color="text.secondary">
                {t('operations.sessions.sharedWorkspace', 'Workspace compartido')}: <code>{workspace}</code>
              </Typography>
              <Typography variant="caption" color="text.secondary">
                {t('operations.sessions.sharedMemory', 'Memoria compartida')}: {personKey}
              </Typography>
            </Stack>
            <Stack spacing={1}>{ordered.map(renderSessionLine)}</Stack>
          </CardContent>
        </Card>
      </Grid>
    )
  }

  // Unidentified origin (unmapped phone): its own session, folder and no
  // memory — surfaced so the operator sees WHO should be added in Usuarios.
  const renderAnonymousCard = (s: AgentSessionRow) => (
    <Grid item xs={12} md={6} key={s.scope_key}>
      <Card variant="outlined" sx={{ borderRadius: 2, height: '100%' }}>
        <CardContent>
          <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
            <ChannelSessionIcon fontSize="small" color="secondary" />
            <Typography variant="subtitle2" sx={{ flex: 1, wordBreak: 'break-all' }}>
              {s.scope_key.replace(/^channel:/, '')}
            </Typography>
            <Chip size="small" color="secondary" variant="outlined"
              label={t('operations.sessions.roleChannel')} />
          </Stack>
          <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 1 }}>
            {t('operations.sessions.anonHint', 'Remitente sin ficha en Usuarios — sesión y carpeta propias, sin memoria unificada. Dale de alta para asociarlo.')}
          </Typography>
          <Stack spacing={1}>{renderSessionLine(s)}</Stack>
        </CardContent>
      </Card>
    </Grid>
  )

  return (
    <Stack spacing={2}>
      <Typography variant="body2" color="text.secondary">
        {t('operations.sessions.intro')}
      </Typography>
      {feedback && <Alert severity={feedback.kind}>{feedback.msg}</Alert>}

      {sessions.length === 0 ? (
        <Alert severity="info">{t('operations.sessions.empty')}</Alert>
      ) : (
        <>
          <Typography variant="overline" color="text.secondary">
            {t('operations.sessions.peopleGroup', 'Por persona')} ({people.length})
          </Typography>
          <Grid container spacing={2}>
            {people.map(([k, rows]) => renderPersonCard(k, rows))}
          </Grid>
          {anonymous.length > 0 && (
            <>
              <Typography variant="overline" color="text.secondary">
                {t('operations.sessions.anonGroup', 'Orígenes sin identificar')} ({anonymous.length})
              </Typography>
              <Grid container spacing={2}>{anonymous.map(renderAnonymousCard)}</Grid>
            </>
          )}
        </>
      )}
      {confirmDialog}
    </Stack>
  )
}

function ConnectorsBlock({
  caps, connectors, needingAuth, loading,
}: {
  caps: { enabled?: boolean } | null
  connectors: Channel[]
  needingAuth: number
  loading: boolean
}) {
  const { t } = useTranslation()
  if (loading) {
    return (
      <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}>
        <CircularProgress size={28} />
      </Box>
    )
  }
  const channelsDisabled = !!(caps && caps.enabled === false)
  return (
    <Stack spacing={3}>
      {/* External (Composio-backed) MCP servers — Odoo, Gmail, … The
          operator authenticates the autonomous user's account here so it
          matches what the agent uses at runtime. */}
      <McpConnectorsSection />

      <Divider flexItem />

      {/* Channels — email / WhatsApp / Slack the agent listens & replies on. */}
      <Stack spacing={2}>
        <Stack direction="row" spacing={1} alignItems="center">
          <ChannelSessionIcon fontSize="small" color="action" />
          <Typography variant="subtitle1" sx={{ fontWeight: 600, flex: 1 }}>
            {t('operations.connectors.channelsTitle')}
          </Typography>
          {!channelsDisabled && (
            <Button
              component={RouterLink}
              to="/channels"
              size="small"
              variant="outlined"
              endIcon={<LinkIcon fontSize="small" />}
              sx={{ textTransform: 'none', flexShrink: 0 }}
            >
              {t('operations.connectors.manage')}
            </Button>
          )}
        </Stack>

        {channelsDisabled ? (
          <Alert severity="info">{t('operations.connectors.disabled')}</Alert>
        ) : (
          <>
            <Typography variant="body2" color="text.secondary">
              {t('operations.connectors.intro')}
            </Typography>
            {connectors.length === 0 ? (
              <Alert severity="info">{t('operations.connectors.empty')}</Alert>
            ) : needingAuth > 0 ? (
              <Alert severity="warning" icon={<AuthIcon fontSize="small" />}>
                {t('operations.connectors.needsAttention', { count: needingAuth })}
              </Alert>
            ) : (
              <Alert severity="success">{t('operations.connectors.allOk')}</Alert>
            )}
            <Grid container spacing={2}>
              {connectors.map((c) => (
                <Grid item xs={12} md={6} key={c.id}>
                  <ConnectorCard c={c} />
                </Grid>
              ))}
            </Grid>
          </>
        )}
      </Stack>
    </Stack>
  )
}

function McpConnectorsSection() {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const query = useQuery({
    queryKey: ['mcp-connectors'],
    queryFn: () => mcpConnectorsApi.list(),
    staleTime: 15_000,
    refetchOnWindowFocus: true,
  })
  const data = query.data
  const connectors = data?.connectors ?? []
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['mcp-connectors'] })
    qc.invalidateQueries({ queryKey: ['employee-permissions'] })
  }

  return (
    <Stack spacing={2}>
      <Stack direction="row" spacing={1} alignItems="center">
        <ConnectorIcon fontSize="small" color="action" />
        <Typography variant="subtitle1" sx={{ fontWeight: 600, flex: 1 }}>
          {t('operations.mcpConnectors.title')}
        </Typography>
        <Tooltip title={t('operations.mcpConnectors.refresh')}>
          <span>
            <IconButton size="small" onClick={refresh} disabled={query.isFetching}>
              <RefreshIcon fontSize="small" />
            </IconButton>
          </span>
        </Tooltip>
      </Stack>
      <Typography variant="body2" color="text.secondary">
        {t('operations.mcpConnectors.intro')}
      </Typography>
      <Alert severity="info" icon={false} sx={{ py: 0.25, px: 1 }}>
        <Typography variant="caption">
          {t(
            'operations.mcpConnectors.scopeNote',
            'Estas conexiones pertenecen a ESTE agente autónomo (no a tu usuario): solo afectan a este agente y no a tus agentes de código o personalizados.',
          )}
        </Typography>
      </Alert>

      {query.isLoading ? (
        <Box sx={{ display: 'flex', justifyContent: 'center', py: 3 }}>
          <CircularProgress size={24} />
        </Box>
      ) : connectors.length === 0 ? (
        <Alert severity="info">{t('operations.mcpConnectors.empty')}</Alert>
      ) : (
        <>
          {data && data.available === false && (
            <Alert severity="warning">{t('operations.mcpConnectors.unavailable')}</Alert>
          )}
          {data?.error && <Alert severity="error">{data.error}</Alert>}
          {data?.autonomous_user_email && (
            <Typography variant="caption" color="text.secondary">
              {t('operations.mcpConnectors.identity', { email: data.autonomous_user_email })}
            </Typography>
          )}
          {/* 14.8.0 — the employee's permissions: the platform's table, its own layer
              (inherits the agent or tightens it, never more). One panel for every connector. */}
          <PermisosDeConectores />
          {/* 14.27.0 — who connected or disconnected each connector, and when. */}
          <HistorialDeConexiones nombres={Object.fromEntries(connectors.map((c) => [c.name, c.display_name || c.name]))} />
          <Grid container spacing={2}>
            {connectors.map((c) => (
              <Grid item xs={12} md={6} key={c.name}>
                <McpConnectorCard
                  c={c}
                  onChanged={refresh}
                />
              </Grid>
            ))}
          </Grid>
        </>
      )}
    </Stack>
  )
}

// Service-agnostic fallback help for the recurring field names, used only when
// Composio gives no description. Keyed by field name (shared across toolkits),
// never by toolkit — stays generic.
const GENERIC_FIELD_HELP: Record<string, string> = {
  client_id: 'Client ID (o App ID) de la app OAuth que registraste en el proveedor.',
  client_secret: 'Client Secret de esa misma app OAuth. Se almacena cifrado en Composio.',
  client_secret_key: 'Client Secret de esa misma app OAuth. Se almacena cifrado en Composio.',
  subdomain: 'Subdominio de tu cuenta: la parte antes del dominio del proveedor en la URL de tu instancia.',
  api_key: 'Clave de API generada en los ajustes de tu cuenta del proveedor.',
  apikey: 'Clave de API generada en los ajustes de tu cuenta del proveedor.',
  token: 'Token de acceso generado en los ajustes de tu cuenta del proveedor.',
  scopes: 'Permisos (scopes) que solicita la app. Déjalo como viene salvo que el proveedor pida otros.',
}

function fieldHelp(f: { name: string; description?: string }): string | undefined {
  return f.description || GENERIC_FIELD_HELP[(f.name || '').toLowerCase()] || undefined
}

type KnowledgeScope = 'all' | 'autonomous' | 'attending' | 'user'
const KNOWLEDGE_SCOPES: KnowledgeScope[] = ['all', 'autonomous', 'attending', 'user']

function KnowledgeSection() {
  const { t, i18n } = useTranslation()
  const lang = i18n.language?.startsWith('en') ? 'en' : 'es'
  const locStr = (v: any) => (v && typeof v === 'object' ? (v[lang] || v.es || v.en || '') : (v || ''))
  const { confirm, dialog: confirmDialog } = useConfirm()
  const qc = useQueryClient()
  const query = useQuery({
    queryKey: ['knowledge'],
    queryFn: () => apiFetch('/api/agent/knowledge').then(async (r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      return r.json()
    }),
    staleTime: 15_000,
  })
  const items: any[] = query.data?.items ?? []
  // Business CONTEXT (the semantic layer the employee operates on): the
  // declared domain entities + KPIs. Consolidated here so the operator sees
  // WHAT the employee knows structurally alongside the free-text knowledge
  // they add. Read-only — the context is declared in the manifest.
  const manifest = useQuery({
    queryKey: ['agent-manifest'],
    queryFn: () => apiFetch('/api/agent/manifest').then(async (r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      return r.json()
    }),
    staleTime: 60_000,
  })
  const ctxEntities: any[] = Array.isArray(manifest.data?.domain_entities) ? manifest.data.domain_entities : []
  const ctxKpis: any[] = Array.isArray(manifest.data?.business_kpis) ? manifest.data.business_kpis : []

  const [editing, setEditing] = useState<any | null>(null)
  const [form, setForm] = useState<{ title: string; content: string; scope: KnowledgeScope; user_email: string; enabled: boolean }>(
    { title: '', content: '', scope: 'all', user_email: '', enabled: true },
  )
  const [err, setErr] = useState<string | null>(null)
  const scopeLabel = (s: string) => t(`operations.knowledge.scope.${s}`, s)

  const openNew = () => { setForm({ title: '', content: '', scope: 'all', user_email: '', enabled: true }); setErr(null); setEditing({}) }
  const openEdit = (it: any) => { setForm({ title: it.title ?? '', content: it.content ?? '', scope: it.scope, user_email: it.user_email || '', enabled: !!it.enabled }); setErr(null); setEditing(it) }

  const save = useMutation({
    mutationFn: async () => {
      const isEdit = !!(editing && editing.id)
      const body: any = { title: form.title.trim(), content: form.content.trim(), scope: form.scope, enabled: form.enabled }
      if (form.scope === 'user') body.user_email = form.user_email.trim()
      const r = await apiFetch(isEdit ? `/api/agent/knowledge/${editing.id}` : '/api/agent/knowledge', {
        method: isEdit ? 'PATCH' : 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!r.ok) { const b = await r.json().catch(() => null); throw new Error(b?.detail || `HTTP ${r.status}`) }
      return r.json()
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['knowledge'] }); setEditing(null) },
    onError: (e: any) => setErr(String(e?.message ?? e)),
  })
  const patchEnabled = useMutation({
    mutationFn: async (v: { id: number; enabled: boolean }) => {
      const r = await apiFetch(`/api/agent/knowledge/${v.id}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: v.enabled }),
      })
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['knowledge'] }),
    // Without onError a rejected toggle threw into the void: the Switch
    // snapped back and the operator saw no reason. Surface it.
    onError: (e: any) => setErr(String(e?.message ?? e)),
  })
  const del = useMutation({
    mutationFn: async (id: number) => {
      const r = await apiFetch(`/api/agent/knowledge/${id}`, { method: 'DELETE' })
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['knowledge'] }),
    onError: (e: any) => setErr(String(e?.message ?? e)),
  })

  const canSave = form.title.trim() && form.content.trim() && (form.scope !== 'user' || form.user_email.trim())

  return (
    <Stack spacing={2}>
      {/* Structured business PARAMETERS the employee asked for (v7.43.0) — above
          the free-text knowledge below. */}
      <BusinessParameters />
      <Stack direction="row" alignItems="center" spacing={1}>
        <FactIcon fontSize="small" color="action" />
        <Typography variant="subtitle1" sx={{ fontWeight: 600, flex: 1 }}>
          {t('operations.knowledge.title', 'Conocimiento')}
        </Typography>
        <Button size="small" variant="contained" startIcon={<AddIcon />} onClick={openNew} sx={{ textTransform: 'none' }}>
          {t('operations.knowledge.add', 'Añadir')}
        </Button>
      </Stack>
      <Typography variant="body2" color="text.secondary">
        {t('operations.knowledge.intro', 'Instrucciones y conocimiento que el agente seguirá. Elige el ámbito: en todos los casos, solo en tareas autónomas, solo al atender a usuarios, o solo para un usuario concreto.')}
      </Typography>

      {/* List-level error: enable/delete failures fire from the list, where the
          dialog (which also shows `err`) is closed — without this the error
          was invisible. */}
      {err && !editing && (
        <Alert severity="error" onClose={() => setErr(null)}>{err}</Alert>
      )}

      {(ctxEntities.length > 0 || ctxKpis.length > 0) && (
        <Card variant="outlined" sx={{ bgcolor: (th) => alpha(th.palette.info.main, 0.04) }}>
          <CardContent sx={{ py: 1.5, '&:last-child': { pb: 1.5 } }}>
            <Typography variant="caption" color="text.secondary" sx={{ textTransform: 'uppercase', letterSpacing: '0.06em', display: 'block', mb: 1 }}>
              {t('operations.knowledge.contextTitle', 'Contexto de negocio del empleado')}
            </Typography>
            {ctxEntities.length > 0 && (
              <Stack direction="row" spacing={0.5} sx={{ flexWrap: 'wrap', gap: 0.5, mb: ctxKpis.length ? 1 : 0 }}>
                {ctxEntities.map((e: any) => (
                  <Chip key={e.slug || e.table} size="small" variant="outlined"
                    label={locStr(e.label) || e.slug || e.table} />
                ))}
              </Stack>
            )}
            {ctxKpis.length > 0 && (
              <Stack direction="row" spacing={0.5} sx={{ flexWrap: 'wrap', gap: 0.5 }}>
                {ctxKpis.map((k: any, i: number) => (
                  <Chip key={k.id || k.label || i} size="small" color="info" variant="outlined"
                    label={locStr(k.label) || k.id} />
                ))}
              </Stack>
            )}
            <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1 }}>
              {t('operations.knowledge.contextHint', 'Las entidades y KPIs sobre los que trabaja el empleado. Se definen al diseñar el empleado; el conocimiento de abajo lo complementa.')}
            </Typography>
          </CardContent>
        </Card>
      )}

      {query.isLoading ? (
        <Box sx={{ display: 'flex', justifyContent: 'center', py: 3 }}><CircularProgress size={24} /></Box>
      ) : query.isError ? (
        <Alert severity="error">{t('operations.loadError', 'No se pudo cargar. Reintenta en unos segundos.')}</Alert>
      ) : items.length === 0 ? (
        <Alert severity="info">{t('operations.knowledge.empty', 'Aún no has añadido conocimiento.')}</Alert>
      ) : (
        <Stack spacing={1}>
          {items.map((it) => (
            <Paper key={it.id} variant="outlined" sx={{ p: 1.5 }}>
              <Stack direction="row" spacing={1} alignItems="flex-start">
                <Box sx={{ flex: 1, minWidth: 0 }}>
                  <Stack direction="row" spacing={0.5} alignItems="center" sx={{ mb: 0.25, flexWrap: 'wrap' }}>
                    <Typography variant="body2" sx={{ fontWeight: 600 }}>{it.title}</Typography>
                    <Chip size="small" label={it.scope === 'user' ? `${scopeLabel('user')}: ${it.user_email}` : scopeLabel(it.scope)} />
                  </Stack>
                  <Typography variant="caption" color="text.secondary" sx={{ display: 'block', whiteSpace: 'pre-wrap' }}>
                    {(it.content || '').slice(0, 240)}{(it.content || '').length > 240 ? '…' : ''}
                  </Typography>
                </Box>
                <Tooltip title={t('operations.knowledge.enabledHint', 'Activado')}>
                  <Switch size="small" checked={!!it.enabled} onChange={(e) => patchEnabled.mutate({ id: it.id, enabled: e.target.checked })} />
                </Tooltip>
                <IconButton size="small" onClick={() => openEdit(it)}><EditIcon fontSize="small" /></IconButton>
                <IconButton size="small" color="error" onClick={() => confirm({ message: t('operations.knowledge.deleteConfirm', '¿Eliminar este conocimiento?'), danger: true }, () => del.mutate(it.id))}>
                  <DeleteIcon fontSize="small" />
                </IconButton>
              </Stack>
            </Paper>
          ))}
        </Stack>
      )}

      {/* v7.84.0 — the DOCUMENT library (files) below the free-text instructions:
          same «Conocimiento» (Knowledge) area, "what I teach it" + "the material I give it". */}
      <KnowledgeDocuments />

      <Dialog open={!!editing} onClose={() => setEditing(null)} maxWidth="sm" fullWidth>
        <DialogTitle>
          {editing && editing.id ? t('operations.knowledge.editTitle', 'Editar conocimiento') : t('operations.knowledge.addTitle', 'Añadir conocimiento')}
        </DialogTitle>
        <DialogContent>
          <Stack spacing={2} sx={{ mt: 0.5 }}>
            {err && <Alert severity="error" onClose={() => setErr(null)}>{err}</Alert>}
            <TextField label={t('operations.knowledge.fieldTitle', 'Título')} size="small" fullWidth
              value={form.title} onChange={(e) => setForm((f) => ({ ...f, title: e.target.value }))} />
            <TextField label={t('operations.knowledge.fieldContent', 'Contenido / instrucciones')} size="small" fullWidth multiline minRows={4}
              value={form.content} onChange={(e) => setForm((f) => ({ ...f, content: e.target.value }))} />
            <TextField select label={t('operations.knowledge.fieldScope', 'Ámbito')} size="small" fullWidth
              value={form.scope} onChange={(e) => setForm((f) => ({ ...f, scope: e.target.value as KnowledgeScope }))}>
              {KNOWLEDGE_SCOPES.map((s) => <MenuItem key={s} value={s}>{scopeLabel(s)}</MenuItem>)}
            </TextField>
            {form.scope === 'user' && (
              <TextField label={t('operations.knowledge.fieldUser', 'Email del usuario')} size="small" fullWidth
                value={form.user_email} onChange={(e) => setForm((f) => ({ ...f, user_email: e.target.value }))} />
            )}
            <Stack direction="row" spacing={1} alignItems="center">
              <Switch size="small" checked={form.enabled} onChange={(e) => setForm((f) => ({ ...f, enabled: e.target.checked }))} />
              <Typography variant="body2">{t('operations.knowledge.fieldEnabled', 'Activado')}</Typography>
            </Stack>
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setEditing(null)}>{t('common.cancel', 'Cancelar')}</Button>
          <Button variant="contained" disabled={!canSave || save.isPending} onClick={() => save.mutate()}>
            {t('common.save', 'Guardar')}
          </Button>
        </DialogActions>
      </Dialog>
      {confirmDialog}
    </Stack>
  )
}

function McpConnectorCard({
  c,
  onChanged,
}: {
  c: McpConnector
  onChanged: () => void
}) {
  const { t } = useTranslation()
  const [form, setForm] = useState<Record<string, string>>({})
  const [showForm, setShowForm] = useState(false)
  // Operator chose to re-enter the OAuth app credentials (custom_oauth already
  // registered, but client_id/secret were wrong). Forces the app-config step
  // and makes the register call replace the stale auth_config.
  const [reconfig, setReconfig] = useState(false)
  // Set THIS employee's OAuth application. It is a different thing from reconfiguring the
  // workspace's one: that one changes it for everyone and drops their connections; this one only affects this employee.
  const [appPropia, setAppPropia] = useState(false)
  // Connect in ANOTHER way (the Google Maps API key instead of the Composio app):
  // which way, with its fields. Kept apart from `appPropia` because it is not an OAuth app: the
  // credential belongs to the connected account, not to an application.
  const [formaElegida, setFormaElegida] = useState<FormaDeConexion | null>(null)
  const ESQUEMAS_OAUTH = ['OAUTH1', 'OAUTH2', 'DCR_OAUTH']
  const conClavePropia = !!c.forma_propia && !ESQUEMAS_OAUTH.includes(c.forma_propia.toUpperCase())
  const [copied, setCopied] = useState(false)
  const [confirmOpen, setConfirmOpen] = useState(false)
  const copyRedirect = async () => {
    if (!c.redirect_uri) return
    try {
      await navigator.clipboard.writeText(c.redirect_uri)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch { /* clipboard blocked — user can still select the text */ }
  }
  const [feedback, setFeedback] = useState<{ kind: 'success' | 'info' | 'error'; msg: string } | null>(null)
  const flash = (kind: 'success' | 'info' | 'error', msg: string, ms = 6000) => {
    setFeedback({ kind, msg })
    setTimeout(() => setFeedback(null), ms)
  }

  // OAuth polling: a human takes more than 15 s to complete the popup (the
  // previous approach — 4 fixed timers up to 15 s — gave up too early and
  // the card stayed frozen until a manual page refresh). Every 3 s
  // up to 5 min: on seeing `connected`, it ACTIVATES the connector (mints the router session
  // and tells the agent to apply it now) and refreshes the card.
  const oauthPollRef = useRef<number | null>(null)
  useEffect(() => () => {
    if (oauthPollRef.current !== null) window.clearTimeout(oauthPollRef.current)
  }, [])
  const startOauthPoll = () => {
    const deadline = Date.now() + 5 * 60_000
    const tick = async () => {
      oauthPollRef.current = null
      try {
        const data = await mcpConnectorsApi.list()
        const mine = (data.connectors || []).find((x) => x.name === c.name)
        if (mine?.connected) {
          try { await mcpConnectorsApi.activate(c.name) } catch { /* network: the agent's refresher applies it anyway */ }
          flash('success', t('operations.mcpConnectors.connected'))
          onChanged()
          return
        }
      } catch { /* transient (network/502): keep polling */ }
      if (Date.now() < deadline) {
        oauthPollRef.current = window.setTimeout(tick, 3000)
      } else {
        onChanged() // last refresh: so the card shows the real state
      }
    }
    if (oauthPollRef.current !== null) window.clearTimeout(oauthPollRef.current)
    oauthPollRef.current = window.setTimeout(tick, 3000)
  }

  const connect = useMutation({
    mutationFn: (vars?: {
      credentials?: Record<string, string>
      connection_fields?: Record<string, string>
      change_account?: boolean
      forma?: string
    }) =>
      mcpConnectorsApi.connect(c.name, {
        ...(vars?.credentials ? { credentials: vars.credentials } : {}),
        ...(vars?.forma ? { forma: vars.forma } : {}),
        ...(vars?.connection_fields ? { connection_fields: vars.connection_fields } : {}),
        ...(vars?.change_account ? { change_account: true } : {}),
        return_origin: window.location.origin,
      }),
    onSuccess: (r) => {
      if (r.mode === 'oauth' && r.authorize_url) {
        const w = 600, h = 700
        const left = window.screenX + Math.max(0, (window.outerWidth - w) / 2)
        const top = window.screenY + Math.max(0, (window.outerHeight - h) / 2)
        window.open(r.authorize_url, 'wai-mcp-oauth', `width=${w},height=${h},left=${left},top=${top}`)
        flash('info', t('operations.mcpConnectors.authorizeOpened'))
        startOauthPoll()
      } else {
        const conForma = !!formaElegida
        setShowForm(false)
        setForm({})
        setFormaElegida(null)
        if (conForma) {
          // The agent's gate now opens with this employee's configuration: it
          // activates NOW instead of waiting for the refresh (~2 min) with the previous one.
          mcpConnectorsApi.activate(c.name).catch(() => { /* the refresher applies it anyway */ })
          flash('success', t('operations.mcpConnectors.connectedWithForma',
            'Conectado con su credencial. Composio no la comprueba al conectar: si está mal, lo dirá la primera llamada del agente.'), 10000)
        } else {
          flash('success', t('operations.mcpConnectors.connected'))
        }
        onChanged()
      }
    },
    onError: (err) => flash('error', extractApiError(err)),
  })

  const disconnect = useMutation({
    mutationFn: () => mcpConnectorsApi.disconnect(c.name),
    onSuccess: () => {
      flash('success', t('operations.mcpConnectors.disconnected'))
      onChanged()
    },
    onError: (err) => flash('error', extractApiError(err)),
  })

  const register = useMutation({
    mutationFn: (vars: { credentials: Record<string, string>; replace?: boolean }) =>
      mcpConnectorsApi.registerAuthConfig(c.name, vars.credentials, vars.replace),
    onSuccess: () => {
      setShowForm(false)
      setForm({})
      setReconfig(false)
      flash('success', t('operations.mcpConnectors.appConfigured'))
      onChanged()
    },
    onError: (err) => flash('error', extractApiError(err)),
  })

  // ── Generic 3-mode flow, driven by toolkit metadata ──────────────────
  // custom_oauth needs the operator's OAuth app (client_id/secret) registered
  // ONCE before any account can connect; until then we collect auth_config_fields.
  // ``reconfig`` re-opens that step so a wrong client_id/secret can be replaced.
  const hayAppQueAdministrar = !!c.app_oauth_propia
  const needsAppConfig =
    hayAppQueAdministrar && (!c.auth_config_registered || reconfig)
  const canReconfigure = hayAppQueAdministrar && c.auth_config_registered
  const isOauth = c.mode === 'oauth'
  // The field set the form currently edits: the OAuth-app creds while
  // configuring; otherwise the per-user connection fields.
  const activeFields = formaElegida
    ? (formaElegida.connection_fields || [])
    : (needsAppConfig || appPropia)
      ? (c.auth_config_fields || [])
      : (c.connection_fields || c.fields || [])

  const collect = (): Record<string, string> | null => {
    const missing = activeFields.some(
      (f) => f.required && !(form[f.name] || '').trim(),
    )
    if (missing) {
      flash('error', t('operations.mcpConnectors.fieldsRequired'))
      return null
    }
    const out: Record<string, string> = {}
    for (const f of activeFields) {
      const v = (form[f.name] || '').trim()
      if (v) out[f.name] = v
    }
    return out
  }

  const ponerAppPropia = useMutation({
    mutationFn: (credentials: Record<string, string>) =>
      mcpConnectorsApi.setOwnAuthConfig(c.name, credentials),
    onSuccess: () => {
      setShowForm(false); setForm({}); setAppPropia(false)
      onChanged()
    },
  })

  const quitarAppPropia = useMutation({
    mutationFn: () => mcpConnectorsApi.clearOwnAuthConfig(c.name),
    onSuccess: () => onChanged(),
  })

  const onConnectClick = () => {
    // Another way to connect (the API key): the connection ends up with a configuration
    // of this employee, which the platform points to.
    if (formaElegida) {
      if (!showForm) { setShowForm(true); return }
      const creds = collect()
      if (creds) connect.mutate({ credentials: creds, forma: formaElegida.auth_scheme })
      return
    }
    // Set THIS employee's application: it does not touch the workspace's, so it goes through another
    // route even though the form is the same.
    if (appPropia) {
      if (!showForm) { setShowForm(true); return }
      const creds = collect()
      if (creds) ponerAppPropia.mutate(creds)
      return
    }
    // Step 1 (custom_oauth only): register the OAuth app credentials once
    // (or replace them when the operator is reconfiguring wrong credentials).
    if (needsAppConfig) {
      if (!showForm) { setShowForm(true); return }
      const creds = collect()
      if (creds) register.mutate({ credentials: creds, replace: reconfig })
      return
    }
    // OAuth (managed, or custom_oauth already configured).
    if (isOauth) {
      if (activeFields.length > 0) {
        if (!showForm) { setShowForm(true); return }
        const cf = collect()
        if (cf) connect.mutate({ connection_fields: cf })
        return
      }
      connect.mutate(undefined)
      return
    }
    // Credentials (API key / basic / …).
    if (!showForm) { setShowForm(true); return }
    const creds = collect()
    if (creds) connect.mutate({ credentials: creds })
  }

  const busy = connect.isPending || disconnect.isPending || register.isPending
    || ponerAppPropia.isPending || quitarAppPropia.isPending
  // CUSTOM MCP (connection_mode 'none', e.g. cluster-mcp): wired at DEPLOY time, no
  // per-user auth → no connect/disconnect UI. Its write-policy remains governable below.
  //
  // Two different cases that need no connecting, and are not named the same: one has its OWN
  // key set at deploy time (a Postgres, an API with api key) and the other does not authenticate
  // against anything. Saying "no authentication" of the first is false and leaves whoever reads it
  // believing their connector is open.
  const isCustom = c.connection_mode === 'none'

  return (
    <Card variant="outlined" sx={{ height: '100%' }}>
      <CardContent>
        <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
          <ConnectorIcon fontSize="small" color="action" />
          <Typography variant="subtitle1" sx={{ fontWeight: 600, flex: 1 }}>
            {c.display_name}
          </Typography>
          <Chip
            size="small"
            color={isCustom || c.connected ? 'success' : 'warning'}
            icon={isCustom || c.connected ? <OkIcon fontSize="small" /> : <AuthIcon fontSize="small" />}
            label={
              isCustom
                ? (c.credential_kind === 'propia'
                    ? t('operations.mcpConnectors.statusOwnKey', 'conectado · con su propia clave')
                    : t('operations.mcpConnectors.statusCustom', 'conectado · sin autenticación'))
                : c.connected
                  ? (conClavePropia
                      ? t('operations.mcpConnectors.statusOwnCredential', 'conectado · con su credencial')
                      : t('operations.mcpConnectors.statusConnected'))
                  : t('operations.mcpConnectors.statusDisconnected')
            }
          />
          {/* The connector's headline, read from the platform's permissions table
              (14.8.0): the most it can do with a write. The detail, below in Permisos. */}
          {c.write_policy && c.write_policy !== 'write' && (
            <Tooltip title={t('operations.mcpConnectors.policyHint', 'Potestad de escritura del conector. Se configura en la plataforma (admin del agente); la aplica el proxy de políticas fuera del alcance del modelo.') as string}>
              <Chip
                size="small"
                color={c.write_policy === 'read_only' ? 'default' : 'info'}
                variant="outlined"
                label={c.write_policy === 'read_only'
                  ? t('operations.mcpConnectors.policyReadOnly', 'Solo consulta')
                  : t('operations.mcpConnectors.policyApproval', 'Escritura con aprobación')}
              />
            </Tooltip>
          )}
        </Stack>

        <Typography variant="caption" color="text.secondary" display="block">
          {t('operations.mcpConnectors.toolkit', { toolkit: c.toolkit })}
        </Typography>
        {c.account && (
          <Typography variant="caption" color="text.secondary" display="block">
            {t('operations.mcpConnectors.account', { account: c.account })}
          </Typography>
        )}

        {c.auth_error && (
          <Alert severity="error" sx={{ mt: 1 }}>{c.auth_error}</Alert>
        )}
        {feedback && (
          <Alert severity={feedback.kind} sx={{ mt: 1 }}>{feedback.msg}</Alert>
        )}

        {needsAppConfig && !showForm && (
          <Alert severity="info" sx={{ mt: 1 }}>
            {t('operations.mcpConnectors.appConfigNeeded')}
          </Alert>
        )}

        {showForm && formaElegida && (
          <Alert severity="info" sx={{ mt: 1 }}>
            {t('operations.mcpConnectors.formaAyuda',
              'La credencial es del proyecto del cliente con el proveedor: el cupo y el gasto son suyos y se ven en su consola, en vez de compartir el de la app de Composio con todos sus clientes. Composio no la comprueba al conectar.')}
          </Alert>
        )}

        {showForm && activeFields.length > 0 && (
          <Stack spacing={1.5} sx={{ mt: 2 }}>
            {needsAppConfig && (
              <Box sx={{ bgcolor: 'action.hover', borderRadius: 1, p: 1.5 }}>
                <Typography variant="caption" color="text.secondary" component="div" sx={{ mb: c.redirect_uri ? 1 : 0 }}>
                  {t(
                    'operations.mcpConnectors.registerSteps',
                    'Crea una app OAuth en el panel de desarrollador de {{name}}, registra en ella la Redirect URI de abajo, y pega aquí el Client ID y el Client Secret que te dé.',
                    { name: c.display_name },
                  )}
                </Typography>
                {c.redirect_uri && (
                  <TextField
                    label={t('operations.mcpConnectors.redirectUri', 'Redirect URI (regístrala en el proveedor)')}
                    value={c.redirect_uri}
                    size="small"
                    fullWidth
                    InputProps={{
                      readOnly: true,
                      endAdornment: (
                        <InputAdornment position="end">
                          <Tooltip title={copied ? t('operations.mcpConnectors.copied', 'Copiado') : t('operations.mcpConnectors.copy', 'Copiar')}>
                            <IconButton edge="end" size="small" onClick={copyRedirect}>
                              {copied ? <OkIcon fontSize="small" color="success" /> : <CopyIcon fontSize="small" />}
                            </IconButton>
                          </Tooltip>
                        </InputAdornment>
                      ),
                    }}
                    helperText={t(
                      'operations.mcpConnectors.redirectUriHelp',
                      'Pega esta URL exacta como Redirect/Callback URI en la app OAuth del proveedor. No la escribas tú.',
                    )}
                  />
                )}
              </Box>
            )}
            {activeFields.map((f) => (
              <TextField
                key={f.name}
                label={f.label}
                size="small"
                fullWidth
                required={f.required}
                type={f.secret ? 'password' : 'text'}
                value={form[f.name] || ''}
                helperText={fieldHelp(f)}
                onChange={(e) => setForm((prev) => ({ ...prev, [f.name]: e.target.value }))}
              />
            ))}
          </Stack>
        )}

        {isCustom && (
          <Typography variant="caption" color="text.secondary" sx={{ mt: 1.5, display: 'block' }}>
            {t('operations.mcpConnectors.customNote', 'Conector interno: se configura al desplegar el empleado y no requiere autenticación. Puedes ajustar su potestad de escritura abajo.')}
          </Typography>
        )}
        {!isCustom && (
        <Stack direction="row" spacing={1} sx={{ mt: 2 }}>
          <Button
            variant="contained"
            size="small"
            startIcon={busy ? <CircularProgress size={14} /> : <AuthIcon fontSize="small" />}
            disabled={busy}
            onClick={onConnectClick}
            sx={{ textTransform: 'none' }}
          >
            {needsAppConfig
              ? (showForm ? t('operations.mcpConnectors.save') : t('operations.mcpConnectors.configureApp'))
              : c.connected
                ? t('operations.mcpConnectors.reconnect')
                : ((!isOauth || formaElegida) && showForm)
                  ? t('operations.mcpConnectors.save')
                  : t('operations.mcpConnectors.connect')}
          </Button>
          {!showForm && isOauth && (c.formas_alternativas || []).map((f) => (
            <Tooltip key={f.auth_scheme} title={t('operations.mcpConnectors.formaPorQue',
              'En vez de la aplicación de Composio (cupo compartido con todos sus clientes), una credencial del proyecto del cliente: el cupo y el gasto son suyos.') as string}>
              <Button size="small" disabled={busy}
                onClick={() => { setFormaElegida(f); setAppPropia(false); setReconfig(false); setForm({}); setShowForm(true) }}
                sx={{ textTransform: 'none' }}>
                {f.auth_scheme === 'API_KEY'
                  ? t('operations.mcpConnectors.formaApiKey', 'Con clave de API')
                  : t('operations.mcpConnectors.formaOtra', 'Con {{forma}}', { forma: f.auth_scheme })}
              </Button>
            </Tooltip>
          ))}
          {showForm && (
            <Button
              size="small"
              onClick={() => { setShowForm(false); setForm({}); setReconfig(false); setAppPropia(false); setFormaElegida(null) }}
              sx={{ textTransform: 'none' }}
            >
              {t('operations.mcpConnectors.cancel')}
            </Button>
          )}
          {canReconfigure && !showForm && (
            <Tooltip title={t('operations.mcpConnectors.spaceAppWhy',
              'La aplicación OAuth de TODO el espacio. Cambiarla afecta a todos y obliga a '
              + 'que vuelvan a conectarse.') as string}>
              <Button
                size="small"
                color="warning"
                disabled={busy}
                onClick={() => { setReconfig(true); setAppPropia(false); setForm({}); setShowForm(true) }}
                sx={{ textTransform: 'none' }}
              >
                {t('operations.mcpConnectors.spaceApp', 'App del espacio')}
              </Button>
            </Tooltip>
          )}
          {hayAppQueAdministrar && !showForm && (
            c.app_propia_mia ? (
              <Tooltip title={t('operations.mcpConnectors.ownAppOnWhy',
                'Este empleado usa SU propia aplicación para este servicio.') as string}>
                <Button size="small" color="secondary" disabled={busy}
                  onClick={() => quitarAppPropia.mutate()}
                  sx={{ textTransform: 'none' }}>
                  {t('operations.mcpConnectors.ownAppRemove', 'Usar la del espacio')}
                </Button>
              </Tooltip>
            ) : (
              <Tooltip title={t('operations.mcpConnectors.ownAppWhy',
                'Este empleado puede usar su propia aplicación OAuth en vez de la del '
                + 'espacio: la creas en el proveedor y solo le afecta a él.') as string}>
                <Button size="small" disabled={busy}
                  onClick={() => { setAppPropia(true); setReconfig(false); setForm({}); setShowForm(true) }}
                  sx={{ textTransform: 'none' }}>
                  {t('operations.mcpConnectors.ownApp', 'Usar su app')}
                </Button>
              </Tooltip>
            )
          )}
          {c.connected && c.mode === 'oauth' && (
            <Button
              size="small"
              disabled={busy}
              onClick={() => connect.mutate({ change_account: true })}
              sx={{ textTransform: 'none' }}
            >
              {t('operations.mcpConnectors.changeAccount')}
            </Button>
          )}
          {c.connected && (
            <Button
              size="small"
              color="error"
              startIcon={<DeleteIcon fontSize="small" />}
              disabled={busy}
              onClick={() => setConfirmOpen(true)}
              sx={{ textTransform: 'none', ml: 'auto' }}
            >
              {t('operations.mcpConnectors.disconnect')}
            </Button>
          )}
        </Stack>
        )}

        <Dialog open={confirmOpen} onClose={() => setConfirmOpen(false)} maxWidth="xs" fullWidth>
          <DialogTitle>
            {t('operations.mcpConnectors.disconnectTitle', 'Desconectar {{name}}', { name: c.display_name || c.name })}
          </DialogTitle>
          <DialogContent>
            <Typography variant="body2">
              {t(
                'operations.mcpConnectors.disconnectConfirm',
                'Esta conexión es de este agente y se eliminará; solo afecta a este agente. ¿Continuar?',
              )}
            </Typography>
          </DialogContent>
          <DialogActions>
            <Button onClick={() => setConfirmOpen(false)}>{t('common.cancel', 'Cancelar')}</Button>
            <Button
              variant="contained"
              color="error"
              onClick={() => { setConfirmOpen(false); disconnect.mutate() }}
            >
              {t('operations.mcpConnectors.disconnect', 'Desconectar')}
            </Button>
          </DialogActions>
        </Dialog>

      </CardContent>
    </Card>
  )
}

function ConnectorCard({ c }: { c: Channel }) {
  const { t, i18n } = useTranslation()
  const { prompt: promptAccount, dialog: promptDialog } = usePrompt()
  const qc = useQueryClient()
  const needsAuth = c.status === 'pending' || c.status === 'unauthorized' || c.status === 'error'
  const isOauth = c.auth_method === 'oauth_composio'
  const [feedback, setFeedback] = useState<{ kind: 'success' | 'info' | 'error'; msg: string } | null>(null)
  const flash = (kind: 'success' | 'info' | 'error', msg: string, ms = 6000) => {
    setFeedback({ kind, msg })
    setTimeout(() => setFeedback(null), ms)
  }

  const authorize = useMutation({
    mutationFn: (vars?: { changeAccount?: boolean; account?: string }) =>
      channelsApi.authorize(c.id, {
        return_origin: window.location.origin,
        ...(vars?.changeAccount ? { change_account: true } : {}),
        ...(vars?.account ? { composio_user_id: vars.account } : {}),
      }),
    onSuccess: (r) => {
      if (r.authorize_url) {
        // Same centred-popup UX as the channel-detail reauth flow so
        // the operator keeps the Operations page open underneath.
        const w = 600, h = 700
        const left = window.screenX + Math.max(0, (window.outerWidth - w) / 2)
        const top = window.screenY + Math.max(0, (window.outerHeight - h) / 2)
        window.open(r.authorize_url, 'wai-ops-oauth', `width=${w},height=${h},left=${left},top=${top}`)
        flash('info', t('operations.connectors.authorizeOpened'))
      } else if (r.reauthorized) {
        flash('success', t('operations.connectors.reauthorized'))
        qc.invalidateQueries({ queryKey: ['ops-channels'] })
      }
    },
    onError: (err) => flash('error', extractApiError(err)),
  })

  // "Change account" — let the operator connect a DIFFERENT account.
  // Optionally accept a new account email (blank ⇒ keep the same entity
  // and just pick the account in the provider's sign-in window). The
  // backend revokes the old connection and forces account selection.
  const onChangeAccount = () => {
    const current = c.composio_user_id || c.mailbox_address || ''
    promptAccount(
      { message: t('operations.connectors.changeAccountPrompt'), defaultValue: current },
      (answer) => {
        const account = answer.trim().toLowerCase()
        authorize.mutate({ changeAccount: true, account: account || undefined })
      },
    )
  }

  const test = useMutation({
    mutationFn: () => channelsApi.test(c.id),
    onSuccess: (r) => {
      flash(r.ok ? 'success' : 'error', r.message)
      // A passing test can self-heal a pending/unauthorized connector.
      if (r.ok) qc.invalidateQueries({ queryKey: ['ops-channels'] })
    },
    onError: (err) => flash('error', extractApiError(err)),
  })

  const fmt = (iso: string | null) => formatDateTime(iso)

  return (
    <>
    <Card
      variant="outlined"
      sx={{
        borderRadius: 2,
        height: '100%',
        borderLeft: '4px solid',
        borderLeftColor: needsAuth ? 'warning.main' : 'success.main',
      }}
    >
      <CardContent>
        <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }} flexWrap="wrap" useFlexGap>
          <Typography variant="subtitle1" sx={{ fontWeight: 700, flex: 1, minWidth: 0 }}>
            {c.display_name}
          </Typography>
          <StatusChip status={c.status} />
          <Chip size="small" variant="outlined" label={t(`channels.kind.${c.kind}`, { defaultValue: c.kind })} />
          <Chip size="small" variant="outlined" label={t(`channels.auth.${c.auth_method}`, { defaultValue: c.auth_method })} />
        </Stack>

        <Typography variant="body2" color="text.secondary" sx={{ mb: 0.5 }}>
          {t('operations.connectors.account')}: <b>{c.mailbox_address || c.composio_user_id || '—'}</b>
          {c.composio_toolkit ? ` · ${c.composio_toolkit}` : ''}
        </Typography>
        <Typography variant="caption" color="text.secondary" component="div" sx={{ mb: 1 }}>
          {t('operations.connectors.lastActivity')}: {fmt(c.last_message_at ?? c.last_polled_at)}
        </Typography>

        {c.status_reason && (
          <Alert severity={c.status === 'unauthorized' ? 'warning' : 'error'} sx={{ mb: 1, py: 0.25 }}>
            {c.status_reason}
          </Alert>
        )}
        {feedback && (
          <Alert severity={feedback.kind} sx={{ mb: 1, py: 0.25 }} onClose={() => setFeedback(null)}>
            {feedback.msg}
          </Alert>
        )}

        <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
          {isOauth && (
            <Tooltip title={t('operations.connectors.authorizeHint') as string}>
              <Button
                size="small"
                variant={needsAuth ? 'contained' : 'outlined'}
                color={needsAuth ? 'warning' : 'primary'}
                startIcon={<AuthIcon fontSize="small" />}
                disabled={authorize.isPending}
                onClick={() => authorize.mutate(undefined)}
                sx={{ textTransform: 'none' }}
              >
                {needsAuth && c.status === 'pending'
                  ? t('operations.connectors.authorize')
                  : t('operations.connectors.reauthorize')}
              </Button>
            </Tooltip>
          )}
          {isOauth && (
            <Tooltip title={t('operations.connectors.changeAccountHint') as string}>
              <Button
                size="small"
                variant="outlined"
                startIcon={<SwapAccountIcon fontSize="small" />}
                disabled={authorize.isPending}
                onClick={onChangeAccount}
                sx={{ textTransform: 'none' }}
              >
                {t('operations.connectors.changeAccount')}
              </Button>
            </Tooltip>
          )}
          {!isOauth && needsAuth && (
            <Tooltip title={t('operations.connectors.credentialsInDetail') as string}>
              <Button
                component={RouterLink}
                to={`/channels/${c.id}`}
                size="small"
                variant="contained"
                color="warning"
                startIcon={<AuthIcon fontSize="small" />}
                sx={{ textTransform: 'none' }}
              >
                {t('operations.connectors.reauthorize')}
              </Button>
            </Tooltip>
          )}
          <Button
            size="small"
            variant="outlined"
            disabled={test.isPending}
            onClick={() => test.mutate()}
            startIcon={<RunningIcon fontSize="small" />}
            sx={{ textTransform: 'none' }}
          >
            {test.isPending ? t('common.loading') : t('operations.connectors.test')}
          </Button>
          <Button
            component={RouterLink}
            to={`/channels/${c.id}`}
            size="small"
            endIcon={<LinkIcon fontSize="small" />}
            sx={{ textTransform: 'none' }}
          >
            {t('operations.connectors.open')}
          </Button>
        </Stack>
      </CardContent>
    </Card>
    {promptDialog}
    </>
  )
}

type DbUsage = {
  available: boolean
  path?: string
  total_bytes?: number
  files?: Array<{ name: string; bytes: number }>
  volume_total_bytes?: number | null
  volume_free_bytes?: number | null
  volume_used_bytes?: number | null
}

// Database monitoring tab — storage footprint of the agent's local
// application store (alerts, communications, decisions, scheduled items,
// run history…). Mirrors the Workspace usage card so an operator can
// watch disk pressure on the writable PVC. Read-only:
// no destructive actions on the live DB from the UI.
function DatabaseBlock() {
  const { t } = useTranslation()
  const query = useQuery({
    queryKey: ['db-usage'],
    queryFn: () => apiFetch('/api/agent/workspace/db-usage').then(async (r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      return r.json() as Promise<DbUsage>
    }),
    refetchInterval: 60_000,
    refetchIntervalInBackground: false,
    retry: false,
  })

  if (query.isLoading) {
    return <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}><CircularProgress /></Box>
  }
  if (query.error) {
    return <Alert severity="error">{String((query.error as any)?.message ?? query.error)}</Alert>
  }
  const usage = query.data
  if (!usage || usage.available === false) {
    return <Alert severity="info">{t('operations.database.unavailable')}</Alert>
  }
  const files = usage.files ?? []

  return (
    <Stack spacing={2}>
      <Typography variant="body2" color="text.secondary">
        {t('operations.database.intro')}
      </Typography>

      <StorageUsageCard
        occupiedLabel={t('operations.database.usageLabel')}
        occupiedBytes={usage.total_bytes ?? 0}
        volumeTotalBytes={usage.volume_total_bytes}
        volumeFreeBytes={usage.volume_free_bytes}
      />

      {files.length > 0 && (
        <Card variant="outlined" sx={{ borderRadius: 2 }}>
          <CardContent>
            <Typography variant="subtitle2" sx={{ mb: 1 }}>
              {t('operations.database.filesTitle')}
            </Typography>
            <List dense disablePadding>
              {files.map((f) => (
                <ListItem
                  key={f.name}
                  disableGutters
                  secondaryAction={
                    <Typography variant="body2" color="text.secondary">
                      {fmtBytes(f.bytes)}
                    </Typography>
                  }
                  sx={{ borderBottom: '1px solid', borderColor: 'divider', '&:last-child': { borderBottom: 0 } }}
                >
                  <ListItemText
                    primary={<code>{f.name}</code>}
                    secondary={t(`operations.database.fileHint.${dbFileRole(f.name)}`)}
                  />
                </ListItem>
              ))}
            </List>
          </CardContent>
        </Card>
      )}

      {usage.path && (
        <Typography variant="caption" color="text.secondary">
          {t('operations.database.path')}: <code>{usage.path}</code>
        </Typography>
      )}
    </Stack>
  )
}

// Map a local-store filename to a coarse role so we can show a friendly
// hint for the main DB vs. its write-ahead-log / shared-memory sidecars.
function dbFileRole(name: string): 'wal' | 'shm' | 'main' {
  if (name.endsWith('-wal')) return 'wal'
  if (name.endsWith('-shm')) return 'shm'
  return 'main'
}

function CapabilitiesCard({ agent }: { agent: any }) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  // What this employee HAS (decided by its deployment, not by these settings).
  const { manifest } = useManifest()
  const skills: any[] = Array.isArray(agent?.skills) ? agent.skills : []
  const mcps: any[] = Array.isArray(agent?.mcps) ? agent.mcps : []
  // The verdict comes as a CODE and is said here, in the person's language; the backend's
  // Spanish sentence is only the fallback for an employee older than 14.16.
  const permsCode = agent?.permissions?.summary_code as { kind?: string; own_rules?: number } | undefined
  const permsSummary = permsCode?.kind
    ? t(`operations.permsSummary.${permsCode.kind}`)
      + (permsCode.own_rules ? ' ' + t('operations.permsSummary.ownRules', { count: permsCode.own_rules }) : '')
    : (agent?.permissions?.summary ?? '—')

  // Operator-configurable output language for autonomous runs
  // (trigger summaries, persisted alerts/recommendations, proactive
  // notifications). Channel replies keep mirroring the sender.
  const { data: settings } = useQuery({
    queryKey: ['agent-settings'],
    queryFn: () => apiFetch('/api/agent/settings').then(async (r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      return r.json()
    }),
  })
  const agentLang: string = settings?.agent_language ?? 'es'
  const availableModels: string[] = Array.isArray(settings?.available_models)
    ? settings.available_models
    : []
  // Saved (server) value. Normalise an out-of-range stored model to the
  // empty sentinel so the Select never renders an unknown value (which
  // made "Predeterminado" look broken).
  const savedModelRaw: string = settings?.agent_model ?? ''
  const agentModel: string =
    savedModelRaw && availableModels.includes(savedModelRaw) ? savedModelRaw : ''
  const defaultModel: string = settings?.default_model ?? ''
  // v7.87.0 — session-level reasoning effort ('' = model default). Config-only,
  // so it applies when the employee's runtime restarts opencode (auto, ~2 min).
  const availableEfforts: string[] = Array.isArray(settings?.available_efforts)
    ? settings.available_efforts
    : []
  const agentEffort: string = settings?.agent_effort ?? ''
  // 12.23.0 — the SAME levels the person writing sees (Rápido/Normal/Profundo). Here one
  // chose between `none|low|medium|high|xhigh`, the provider's names, to decide
  // exactly the same thing the chat calls by its name: two vocabularies for one thing,
  // and neither said what was happening in the other. The saved VALUE does not change
  // (it is still the effort, which is what the pod injects into its config): the name changes.
  const nivelesRaz: Array<{ id: string; nombre: string; para: string; esfuerzo: string }> =
    Array.isArray(settings?.niveles_de_razonamiento) ? settings.niveles_de_razonamiento : []
  // What not setting any amounts to. The employee computes it from ITS model, so
  // if one day the catalog brings another family the screen stops claiming it instead of
  // lying. Empty = nothing can be claimed and nothing is said.
  const sinFijarEquivale: string = settings?.razonamiento_sin_fijar?.nombre || ''
  // How much it WRITES by default. Empty = whatever the model brings, and then the
  // channel rules (by voice, brief): it belongs more to the channel than to the request.
  const longitudes: Array<{ id: string; nombre: string; para: string }> =
    Array.isArray(settings?.longitudes_de_respuesta) ? settings.longitudes_de_respuesta : []
  const agentVerbosity: string = settings?.agent_verbosity ?? ''
  const atajosActivos: boolean = settings?.atajos_activos !== false
  const conversadorEligeNivel: boolean = settings?.conversador_elige_nivel !== false
  const recepcionActiva: boolean = settings?.recepcion_activa !== false
  // Does it HAVE it? It is a deployment decision and it is in beta. A switch over something that
  // is not deployed would be a control that governs nothing: it is hidden, not disabled.
  const tieneRecepcion: boolean = Boolean(manifest?.conversacion?.recepcion)
  const atajosDeclarados: number = Number(settings?.atajos_declarados ?? 0)

  // Staged edits — language + model are applied together with an
  // explicit Save (no surprise auto-save). Drafts re-sync whenever the
  // server value changes. Empty model string = "deploy default".
  const [draftLang, setDraftLang] = useState<string>(agentLang)
  const [draftModel, setDraftModel] = useState<string>(agentModel)
  const [draftEffort, setDraftEffort] = useState<string>(agentEffort)
  const [draftVerbosity, setDraftVerbosity] = useState<string>(agentVerbosity)
  const [draftAtajos, setDraftAtajos] = useState<boolean>(atajosActivos)
  const [draftConvNivel, setDraftConvNivel] = useState<boolean>(conversadorEligeNivel)
  const [draftRecepcion, setDraftRecepcion] = useState<boolean>(recepcionActiva)
  const [saveError, setSaveError] = useState<string | null>(null)
  useEffect(() => {
    setDraftLang(agentLang)
    setDraftModel(agentModel)
    setDraftEffort(agentEffort)
    setDraftVerbosity(agentVerbosity)
    setDraftAtajos(atajosActivos)
    setDraftConvNivel(conversadorEligeNivel)
    setDraftRecepcion(recepcionActiva)
  }, [agentLang, agentModel, agentEffort, agentVerbosity, atajosActivos,
      conversadorEligeNivel, recepcionActiva])

  const saveSettings = useMutation({
    mutationFn: () =>
      apiFetch('/api/agent/settings', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          agent_language: draftLang, agent_model: draftModel, agent_effort: draftEffort,
          agent_verbosity: draftVerbosity,
          atajos_activos: draftAtajos, conversador_elige_nivel: draftConvNivel,
          recepcion_activa: draftRecepcion,
        }),
      }).then(async (r) => {
        if (!r.ok) {
          const body = await r.json().catch(() => null)
          throw new Error(body?.detail || `HTTP ${r.status}`)
        }
        return r.json()
      }),
    onSuccess: (d) => { qc.setQueryData(['agent-settings'], d); setSaveError(null) },
    onError: (e: any) => setSaveError(String(e?.message ?? e)),
  })
  const settingsDirty = draftLang !== agentLang || draftModel !== agentModel
    || draftEffort !== agentEffort || draftVerbosity !== agentVerbosity
    || draftAtajos !== atajosActivos || draftConvNivel !== conversadorEligeNivel
    || draftRecepcion !== recepcionActiva

  // ── Extra FUNCTIONAL (global) skills — attach/detach LIVE ──────────
  // The operator self-services from the platform skill catalog; the change is
  // written to /workspace/.opencode/skills by the sidecar and applied when the
  // employee restarts opencode (~2 min, same path as effort).
  const { data: availableSkillsData } = useQuery({
    queryKey: ['agent-available-skills'],
    queryFn: () => apiFetch('/api/agent/available-skills').then(async (r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      return r.json()
    }),
  })
  const availableSkills: any[] = Array.isArray(availableSkillsData?.skills) ? availableSkillsData.skills : []
  const attachedExtra: any[] = Array.isArray(settings?.agent_extra_skills) ? settings.agent_extra_skills : []
  const attachedIds: number[] = attachedExtra.map((s: any) => Number(s.id)).filter((n: number) => !Number.isNaN(n))
  const [draftSkillIds, setDraftSkillIds] = useState<Set<number>>(new Set())
  useEffect(() => { setDraftSkillIds(new Set(attachedIds)) }, [attachedIds.join(',')])
  const [skillsError, setSkillsError] = useState<string | null>(null)
  const saveSkills = useMutation({
    mutationFn: () =>
      apiFetch('/api/agent/extra-skills', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ skill_ids: Array.from(draftSkillIds) }),
      }).then(async (r) => {
        if (!r.ok) {
          const body = await r.json().catch(() => null)
          throw new Error(body?.detail || `HTTP ${r.status}`)
        }
        return r.json()
      }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['agent-settings'] }); setSkillsError(null) },
    onError: (e: any) => setSkillsError(String(e?.message ?? e)),
  })
  const skillsDirty = draftSkillIds.size !== attachedIds.length
    || attachedIds.some((id) => !draftSkillIds.has(id))
  const toggleSkill = (id: number) => setDraftSkillIds((prev) => {
    const next = new Set(prev)
    if (next.has(id)) next.delete(id); else next.add(id)
    return next
  })

  return (
    <Card
      variant="outlined"
      sx={{
        mb: 3,
        borderRadius: 2,
        // A7: theme-independent overlay. The previous gradient used
        // theme.palette.primary.light + background.paper, which fails
        // contrast on dark themes and on certain light themes (the
        // gradient becomes the same color top-to-bottom).
        background: (theme) =>
          `linear-gradient(135deg, ${alpha(theme.palette.primary.main, 0.08)} 0%, transparent 65%)`,
      }}
    >
      <CardContent>
        {/* ── Identity ───────────────────────────────────────────── */}
        <Stack direction="row" spacing={1.5} alignItems="center" sx={{ mb: 1 }}>
          <CapabilityIcon color="primary" />
          <Typography variant="h6" sx={{ fontWeight: 700, flex: 1 }}>
            {agent?.display_name ?? agent?.name ?? '—'}
          </Typography>
          {agent?.version != null && (
            <Chip size="small" color="primary" label={`v${agent.version}`} />
          )}
        </Stack>
        {agent?.description && (
          <Typography color="text.secondary">{agent.description}</Typography>
        )}

        {/* ── Configuración del empleado (employee configuration) — one coherent entry
            point to every surface that SHAPES the employee (identity, how boldly it
            acts, what it knows, its standing orders). Monitoring tabs
            (Estado/Actividad/Ejecuciones) are intentionally NOT here. ── */}
        <Stack direction="row" spacing={1} alignItems="center" sx={{ mt: 1.5, flexWrap: 'wrap', gap: 1 }}>
          <Typography variant="caption" color="text.secondary" sx={{ textTransform: 'uppercase', letterSpacing: '0.06em' }}>
            {t('operations.employeeConfig.title', 'Configuración del empleado')}
          </Typography>
          <Chip size="small" clickable component={RouterLink} to="/identity"
            label={t('operations.employeeConfig.identity', 'Identidad')} variant="outlined" />
          <Chip size="small" clickable variant="outlined"
            label={t('operations.employeeConfig.autonomy', 'Autonomía e iniciativa')}
            onClick={() => { window.location.hash = '#disparadores' }} />
          <Chip size="small" clickable variant="outlined"
            label={t('operations.employeeConfig.knowledge', 'Conocimiento')}
            onClick={() => { window.location.hash = '#conocimiento' }} />
          <Chip size="small" clickable variant="outlined" component={RouterLink} to="/trabajo-autonomo"
            label={t('operations.employeeConfig.automations', 'Trabajo autónomo')} />
        </Stack>

        {/* ── Settings (staged, explicit Save) ───────────────────── */}
        <Divider sx={{ my: 2 }} />
        <Stack direction="row" alignItems="center" sx={{ mb: 1.5 }}>
          <Typography variant="subtitle2" sx={{ flex: 1, textTransform: 'uppercase', letterSpacing: '0.06em', color: 'text.secondary' }}>
            {t('operations.capabilities.settingsTitle')}
          </Typography>
          {settingsDirty && (
            <Button
              size="small"
              onClick={() => { setDraftLang(agentLang); setDraftModel(agentModel); setDraftEffort(agentEffort); setSaveError(null) }}
              disabled={saveSettings.isPending}
            >
              {t('common.cancel')}
            </Button>
          )}
          <Button
            size="small"
            variant="contained"
            onClick={() => saveSettings.mutate()}
            disabled={!settingsDirty || saveSettings.isPending}
            startIcon={saveSettings.isPending ? <CircularProgress size={14} /> : undefined}
          >
            {t('common.save')}
          </Button>
        </Stack>
        {saveError && <Alert severity="error" sx={{ mb: 1.5 }}>{saveError}</Alert>}
        {saveSettings.isSuccess && !settingsDirty && (
          <Alert severity="success" sx={{ mb: 1.5 }}>{t('operations.capabilities.saved')}</Alert>
        )}
        <Grid container spacing={2}>
          <Grid item xs={12} md={availableModels.length > 0 ? 6 : 12}>
            <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 0.5 }}>
              {t('operations.capabilities.agentLanguage')}
            </Typography>
            <ToggleButtonGroup
              size="small"
              exclusive
              value={draftLang}
              onChange={(_, v) => { if (v) setDraftLang(v) }}
            >
              <ToggleButton value="es" sx={{ px: 1.5, textTransform: 'none' }}>Español</ToggleButton>
              <ToggleButton value="en" sx={{ px: 1.5, textTransform: 'none' }}>English</ToggleButton>
            </ToggleButtonGroup>
            <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5 }}>
              {t('operations.capabilities.agentLanguageHelp')}
            </Typography>
          </Grid>
          {availableModels.length > 0 && (
            <Grid item xs={12} md={6}>
              <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 0.5 }}>
                {t('operations.capabilities.agentModel')}
              </Typography>
              <TextField
                select
                size="small"
                value={draftModel}
                onChange={(e) => setDraftModel(e.target.value)}
                SelectProps={{ displayEmpty: true }}
                fullWidth
                sx={{ maxWidth: 320 }}
              >
                <MenuItem value="">
                  {t('operations.capabilities.agentModelDefault', { model: defaultModel || '—' })}
                </MenuItem>
                {availableModels.map((m) => (
                  <MenuItem key={m} value={m} sx={{ fontFamily: 'monospace', fontSize: '0.8rem' }}>
                    {m}
                  </MenuItem>
                ))}
              </TextField>
              <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5 }}>
                {t('operations.capabilities.agentModelHelp')}
              </Typography>
            </Grid>
          )}
          {availableEfforts.length > 0 && (
            <Grid item xs={12} md={6}>
              <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 0.5 }}>
                {t('operations.capabilities.agentEffort', 'Cuánto piensa por defecto')}
              </Typography>
              <TextField
                select
                size="small"
                value={draftEffort}
                onChange={(e) => setDraftEffort(e.target.value)}
                SelectProps={{ displayEmpty: true }}
                fullWidth
                sx={{ maxWidth: 320 }}
              >
                <MenuItem value="">
                  {sinFijarEquivale
                    ? t('operations.capabilities.agentEffortUnsetAs',
                        'Sin fijar — lo pone el modelo (hoy: {{nivel}})', { nivel: sinFijarEquivale })
                    : t('operations.capabilities.agentEffortUnset', 'Sin fijar — lo pone el modelo')}
                </MenuItem>
                {nivelesRaz.map((n) => (
                  <MenuItem key={n.id} value={n.esfuerzo}>
                    {n.nombre}
                  </MenuItem>
                ))}
                {/* A previously saved effort that is no longer offered (`none`, `xhigh`) keeps
                    running: nobody changes it behind anyone's back. It is shown with its raw
                    name instead of pretending it is one of the three — otherwise the Select
                    would paint empty and look broken. */}
                {draftEffort && !nivelesRaz.some((n) => n.esfuerzo === draftEffort) && (
                  <MenuItem value={draftEffort} sx={{ fontFamily: 'monospace', fontSize: '0.8rem' }}>
                    {draftEffort}
                  </MenuItem>
                )}
              </TextField>
              <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5 }}>
                {t('operations.capabilities.agentEffortHelp', 'Con cuánto razonamiento trabaja cuando nadie elige otra cosa: las ejecuciones autónomas, los canales y los mensajes de quien no toca el selector del chat. Cada persona puede pedir otro nivel en cada mensaje, y dejarse fijado el suyo. OJO: no existe un «que decida el modelo» — el esfuerzo lo pone siempre quien llama, y «Sin fijar» significa que no mandamos ninguno y lo pone la capa de abajo. Se aplica al reiniciarse el empleado (automático en ~2 min tras guardar).')}
              </Typography>
            </Grid>
          )}

          {/* How much it WRITES, which is not the same as how much it thinks: you may want it to
              think a lot and answer in two lines. There is no per-message selector on
              purpose — it depends more on the channel (by voice, a long speech is always bad)
              and on the employee's character than on each request. */}
          {longitudes.length > 0 && (
            <Grid item xs={12} md={6}>
              <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 0.5 }}>
                {t('operations.capabilities.agentVerbosity', 'Cuánto escribe')}
              </Typography>
              <TextField
                select size="small" value={draftVerbosity}
                onChange={(e) => setDraftVerbosity(e.target.value)}
                SelectProps={{ displayEmpty: true }} fullWidth sx={{ maxWidth: 320 }}
              >
                <MenuItem value="">
                  {t('operations.capabilities.agentVerbosityDefault',
                     'Lo decide el canal (por voz, breve)')}
                </MenuItem>
                {longitudes.filter((l) => l.id !== 'normal').map((l) => (
                  <MenuItem key={l.id} value={l.id}>{l.nombre}</MenuItem>
                ))}
              </TextField>
              <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5 }}>
                {t('operations.capabilities.agentVerbosityHelp',
                   'Sin fijar nada, por voz contesta breve y por pantalla como traiga el '
                   + 'modelo. Si eliges uno, vale en todos los canales — lo elegiste tú.')}
              </Typography>
            </Grid>
          )}

          {/* What the machine decides on its own, and its two switches. They are here and
              not hidden: on by default because what they replace is a blind
              constant, and can be switched off at once if one day odd answers come out. */}
          <Grid item xs={12}>
            <Divider sx={{ my: 1 }} />
            <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 0.5 }}>
              {t('operations.capabilities.automatismos', 'Lo que decide solo')}
            </Typography>
            <Stack spacing={0.5}>
              <FormControlLabel
                control={<Switch size="small" checked={draftAtajos}
                  onChange={(e) => setDraftAtajos(e.target.checked)} />}
                label={
                  <Typography variant="body2">
                    {t('operations.capabilities.atajos',
                       'Resolver al instante lo que su definición marca como directo')}
                    {/* THE DEPENDENCY, spelled out. In written chat whoever RECOGNISES a shortcut is
                        the gateway; by voice, the conversationalist does. With the gateway off
                        this switch governs nothing in writing, and a control that governs
                        nothing is worse than not having it. */}
                    {atajosDeclarados > 0 && !(tieneRecepcion && draftRecepcion) && (
                      <Typography component="span" variant="caption" color="warning.main">
                        {' '}{t('operations.capabilities.atajosSinRecepcion',
                          '— por escrito los reconoce la pasarela: con ella apagada, solo saltan hablando')}
                      </Typography>
                    )}
                    {atajosDeclarados === 0 && (
                      <Typography component="span" variant="caption" color="text.secondary">
                        {' '}{t('operations.capabilities.atajosNinguno',
                                '— hoy no hay ninguna capacidad marcada así')}
                      </Typography>
                    )}
                  </Typography>
                }
              />
              {tieneRecepcion && (
              <FormControlLabel
                control={<Switch size="small" checked={draftRecepcion}
                  onChange={(e) => setDraftRecepcion(e.target.checked)} />}
                label={
                  <Typography variant="body2">
                    {t('operations.capabilities.recepcion',
                       'Atender por escrito lo que no necesita trabajo: cortesías, '
                       + 'preguntas generales y pedir el dato que falte')}
                  </Typography>
                }
              />
              )}
              <FormControlLabel
                control={<Switch size="small" checked={draftConvNivel}
                  onChange={(e) => setDraftConvNivel(e.target.checked)} />}
                label={
                  <Typography variant="body2">
                    {t('operations.capabilities.convNivel',
                       'Dejar que la conversación proponga cuánto pensar, cuando nadie lo ha elegido')}
                  </Typography>
                }
              />
            </Stack>
          </Grid>
        </Grid>

        {/* ── Extra functional skills (attach LIVE) ────────── */}
        {availableSkills.length > 0 && (
          <>
            <Divider sx={{ my: 2 }} />
            <Stack direction="row" alignItems="center" sx={{ mb: 1 }}>
              <Typography variant="subtitle2" sx={{ flex: 1, textTransform: 'uppercase', letterSpacing: '0.06em', color: 'text.secondary' }}>
                {t('operations.capabilities.extraSkillsTitle', 'Skills funcionales')}
              </Typography>
              {skillsDirty && (
                <Button
                  size="small"
                  onClick={() => { setDraftSkillIds(new Set(attachedIds)); setSkillsError(null) }}
                  disabled={saveSkills.isPending}
                >
                  {t('common.cancel')}
                </Button>
              )}
              <Button
                size="small"
                variant="contained"
                onClick={() => saveSkills.mutate()}
                disabled={!skillsDirty || saveSkills.isPending}
                startIcon={saveSkills.isPending ? <CircularProgress size={14} /> : undefined}
              >
                {t('common.save')}
              </Button>
            </Stack>
            <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 1 }}>
              {t('operations.capabilities.extraSkillsHelp', 'Adjunta skills globales para hacer al empleado más capaz. Se aplica al reiniciarse (automático en ~2 min tras guardar).')}
            </Typography>
            {skillsError && <Alert severity="error" sx={{ mb: 1 }}>{skillsError}</Alert>}
            <Stack direction="row" spacing={0.5} flexWrap="wrap" useFlexGap>
              {availableSkills.map((s: any) => {
                const id = Number(s.id)
                const on = draftSkillIds.has(id)
                return (
                  <Tooltip key={id} title={s.description || ''}>
                    <Chip
                      size="small"
                      label={s.display_name || s.name}
                      color={on ? 'primary' : 'default'}
                      variant={on ? 'filled' : 'outlined'}
                      onClick={() => toggleSkill(id)}
                    />
                  </Tooltip>
                )
              })}
            </Stack>
          </>
        )}

        {/* ── Declared capabilities ──────────────────────────────── */}
        <Divider sx={{ my: 2 }} />
        <Typography variant="subtitle2" sx={{ mb: 1.5, textTransform: 'uppercase', letterSpacing: '0.06em', color: 'text.secondary' }}>
          {t('operations.capabilities.declaredTitle')}
        </Typography>
        <Grid container spacing={2}>
          <Grid item xs={12} md={4}>
            <Typography variant="caption" color="text.secondary">
              {t('operations.capabilities.skills')}
            </Typography>
            <Stack direction="row" spacing={0.5} flexWrap="wrap" useFlexGap sx={{ mt: 0.5 }}>
              {skills.length === 0 && (
                <Typography variant="caption" color="text.secondary">
                  {t('operations.capabilities.skillsEmpty')}
                </Typography>
              )}
              {skills.map((s: any, i: number) => {
                // v7.89.0 — surface the skill TYPE (system / functional / agent)
                // when the manifest provides it, so the operator sees where each
                // capability comes from. Falls back to a neutral chip otherwise.
                const kind: string = s?.kind
                  || (s?.is_system ? 'system' : (s?.owner_agent_id ? 'business' : ''))
                const kindColor = kind === 'functional' ? 'success'
                  : kind === 'system' ? 'info' : 'default'
                const base = s.version ? `${s.name ?? s}@v${s.version}` : (s.name ?? String(s))
                return (
                  <Chip
                    key={s.id ?? s.name ?? i}
                    size="small"
                    color={kindColor as any}
                    variant={kind ? 'outlined' : 'filled'}
                    label={kind
                      ? `${base} · ${t(`operations.capabilities.skillKind_${kind}`, kind)}`
                      : base}
                  />
                )
              })}
            </Stack>
          </Grid>
          <Grid item xs={12} md={4}>
            <Typography variant="caption" color="text.secondary">
              {t('operations.capabilities.mcpServers')}
            </Typography>
            <Stack direction="row" spacing={0.5} flexWrap="wrap" useFlexGap sx={{ mt: 0.5 }}>
              {mcps.length === 0 && (
                <Typography variant="caption" color="text.secondary">
                  {t('operations.capabilities.mcpEmpty')}
                </Typography>
              )}
              {/* v7.158.2 — the chip states the EFFECTIVE write policy of each
                  server (the same one that decides a real call), not a binary label
                  guessed from the name: before, a read-only MCP such as
                  clusterwai was announced as «escritura local» (local write). */}
              {mcps.map((m: any, i: number) => {
                const policy: 'read_only' | 'write_with_approval' | 'write' =
                  m.kind === 'write' || m.kind === 'write_with_approval' ? m.kind : 'read_only'
                // The default literal goes here and not only in es.ts because the
                // i18n files are SCAFFOLD: an already generated employee that evolves does not
                // receive them, and without a fallback the chip would show the raw key.
                const etiqueta = {
                  read_only: 'solo lectura',
                  write_with_approval: 'escribe con tu aprobación',
                  write: 'escribe directamente',
                }[policy]
                return (
                  <Chip
                    key={m.id ?? m.name ?? i}
                    size="small"
                    color={policy === 'write' ? 'warning'
                      : policy === 'write_with_approval' ? 'primary' : 'default'}
                    label={`${m.name} · ${t(`operations.capabilities.mcpPolicy_${policy}`, etiqueta)}`}
                  />
                )
              })}
            </Stack>
          </Grid>
          <Grid item xs={12} md={4}>
            <Typography variant="caption" color="text.secondary">
              {t('operations.capabilities.permissions')}
            </Typography>
            <Typography sx={{ mt: 0.5 }}>{permsSummary}</Typography>
          </Grid>
        </Grid>
      </CardContent>
    </Card>
  )
}

type StatTileProps = {
  icon: React.ReactNode
  label: string
  value: string | number
  hint?: string
  tone?: 'default' | 'success' | 'warning' | 'error' | 'info' | 'primary'
}

function StatTile({ icon, label, value, hint, tone = 'default' }: StatTileProps) {
  const palette: Record<string, { bg: string; fg: string }> = {
    default: { bg: 'grey.100',     fg: 'text.primary' },
    success: { bg: 'success.50',   fg: 'success.main' },
    warning: { bg: 'warning.50',   fg: 'warning.main' },
    error:   { bg: 'error.50',     fg: 'error.main' },
    info:    { bg: 'info.50',      fg: 'info.main' },
    primary: { bg: 'primary.50',   fg: 'primary.main' },
  }
  const colors = palette[tone] ?? palette.default
  return (
    <Card variant="outlined" sx={{ borderRadius: 2, height: '100%' }}>
      <CardContent>
        <Stack direction="row" spacing={1.5} alignItems="center">
          <Box
            sx={{
              width: 40, height: 40, borderRadius: 2,
              backgroundColor: colors.bg, color: colors.fg,
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              flexShrink: 0,
            }}
          >
            {icon}
          </Box>
          <Box sx={{ minWidth: 0, flex: 1 }}>
            <Typography variant="caption" color="text.secondary" sx={{ display: 'block' }}>
              {label}
            </Typography>
            <Typography
              variant="h6"
              sx={{ fontWeight: 700, lineHeight: 1.2, wordBreak: 'break-word' }}
            >
              {value}
            </Typography>
            {hint && (
              <Typography variant="caption" color="text.secondary">
                {hint}
              </Typography>
            )}
          </Box>
        </Stack>
      </CardContent>
    </Card>
  )
}

function StatusBlock({ stats }: { stats: any }) {
  const { t, i18n } = useTranslation()
  // Route through fmtDateTime so the chip / hint follow the GLOBAL
  // display-timezone toggle and show the tz abbreviation ("16:42 CEST")
  // instead of misparsing a naive ISO as local time.
  const fmtTime = (iso?: string) => fmtDateTime(iso, i18n.language) ?? '—'
  const byStatus = stats?.by_status ?? {}
  const today = stats?.today ?? { runs: 0, completed: 0, failed: 0 }
  const chatToday: number = Number(stats?.chat_today ?? 0)
  const latency = stats?.latency_ms ?? { p50: 0, p95: 0, samples: 0 }
  // R-OPS-2: prefer the explicit autonomous-only list; fall back to
  // the legacy ``recent_runs`` for older backends that haven't been
  // restarted with the post-R-OPS-2 manifest_routes.py. The recent
  // chat block has its own (compact) section below.
  const recentRuns: any[] = Array.isArray(stats?.recent_autonomous_runs)
    ? stats.recent_autonomous_runs
    : (Array.isArray(stats?.recent_runs) ? stats.recent_runs : [])
  const recentChats: any[] = Array.isArray(stats?.recent_chat_runs)
    ? stats.recent_chat_runs
    : []
  const hasLatency = (latency.samples ?? 0) > 0
  // A2: distinguish "agent never ran" from "agent ran 0 times today".
  // The previous all-zero StatTile grid was indistinguishable from a
  // broken agent. Pull the standby card up so operators see the
  // "esperando un evento" (waiting for an event) copy first instead of four "0" tiles.
  const totalRuns =
    (Number(byStatus.PENDING) || 0) +
    (Number(byStatus.CLAIMED) || 0) +
    (Number(byStatus.COMPLETED) || 0) +
    (Number(byStatus.FAILED) || 0) +
    (Number(byStatus.RETRY_SCHEDULED) || 0)
  const standby = totalRuns === 0 && !stats?.last_error && !stats?.last_run
  if (standby) {
    return (
      <Stack spacing={2}>
        {/* R8: watcher-stale badge — surfaces the case where the
            watcher loop has gone silent (heartbeat older than 2× the
            slowest interval). Without it, an operator on the standby
            screen has no idea whether "no events" means everything
            is healthy or everything is dead. */}
        {stats?.watcher_stale && (
          <Alert
            severity="error"
            variant="outlined"
            sx={{ borderRadius: 2 }}
          >
            <AlertTitle>
              {t('operations.status.watcherStaleTitle')}
            </AlertTitle>
            {t('operations.status.watcherStaleBody', {
              minutes: stats?.watcher_pulse_at
                ? Math.round((Date.now() - new Date(stats.watcher_pulse_at).getTime()) / 60000)
                : '—',
            })}
          </Alert>
        )}
        <Alert
          severity="info"
          variant="outlined"
          icon={<PendingIcon />}
          sx={{ borderRadius: 2 }}
        >
          <AlertTitle>
            {t('operations.status.standbyTitle')}
          </AlertTitle>
          <Typography variant="body2" sx={{ mb: 1 }}>
            {t('operations.status.standbyBody')}
          </Typography>
          {stats?.next_tick_at && (
            <Typography variant="caption" color="text.secondary">
              {t('operations.status.nextTick', {
                when: fmtTime(stats.next_tick_at),
              })}
            </Typography>
          )}
        </Alert>
      </Stack>
    )
  }
  return (
    <Stack spacing={2}>
      <Grid container spacing={2}>
        <Grid item xs={6} md={3}>
          <StatTile
            icon={<PendingIcon fontSize="small" />}
            tone="warning"
            label={t('operations.status.pending')}
            value={byStatus.PENDING ?? 0}
          />
        </Grid>
        <Grid item xs={6} md={3}>
          <StatTile
            icon={<RunningIcon fontSize="small" />}
            tone="info"
            label={t('operations.status.claimed')}
            value={byStatus.CLAIMED ?? 0}
          />
        </Grid>
        <Grid item xs={6} md={3}>
          <StatTile
            icon={<OkIcon fontSize="small" />}
            tone="success"
            label={t('operations.status.completed')}
            value={byStatus.COMPLETED ?? 0}
          />
        </Grid>
        <Grid item xs={6} md={3}>
          <StatTile
            icon={<ErrorOutlineIcon fontSize="small" />}
            tone="error"
            label={t('operations.status.failed')}
            value={byStatus.FAILED ?? 0}
          />
        </Grid>
      </Grid>

      <Grid container spacing={2}>
        <Grid item xs={12} md={3}>
          <StatTile
            icon={<HistoryIcon fontSize="small" />}
            tone="primary"
            label={t('operations.status.today')}
            value={`${today.runs ?? 0}`}
            hint={`${today.completed ?? 0} OK · ${today.failed ?? 0} KO`}
          />
        </Grid>
        <Grid item xs={12} md={3}>
          <StatTile
            icon={<TriggerIcon fontSize="small" />}
            label={t('operations.status.lastRun')}
            value={stats?.last_run?.event_type ?? t('operations.status.noRunsYet')}
            hint={stats?.last_run ? fmtTime(stats.last_run.started_at) : undefined}
          />
        </Grid>
        <Grid item xs={12} md={3}>
          <StatTile
            icon={<SpeedIcon fontSize="small" />}
            tone={hasLatency ? 'info' : 'default'}
            label={t('operations.status.latency')}
            value={hasLatency ? `${latency.p50} ms` : t('operations.status.latencyMissing')}
            hint={hasLatency ? `p95 ${latency.p95} ms · n=${latency.samples}` : undefined}
          />
        </Grid>
        <Grid item xs={12} md={3}>
          <StatTile
            icon={<HistoryIcon fontSize="small" />}
            tone={chatToday > 0 ? 'info' : 'default'}
            label={t('operations.status.chatToday')}
            value={chatToday}
            hint={chatToday > 0 ? t('operations.status.chatTodayHint') as string : undefined}
          />
        </Grid>
      </Grid>

      {stats?.last_error && (
        <Alert severity="error" variant="outlined">
          <Typography variant="subtitle2">
            {t('operations.status.lastErrorTitle', {
              when: fmtTime(stats.last_error.started_at),
              type: stats.last_error.event_type ?? '—',
            })}
          </Typography>
          <Typography
            variant="body2"
            sx={{
              fontFamily: 'monospace', fontSize: '0.8rem',
              whiteSpace: 'pre-wrap', wordBreak: 'break-word',
              mt: 0.5,
            }}
          >
            {stats.last_error.message}
          </Typography>
          <Link component={RouterLink} to={`/runs/${stats.last_error.id}`}>
            {t('operations.status.openRun', { id: stats.last_error.id })}
          </Link>
        </Alert>
      )}

      <Card variant="outlined" sx={{ borderRadius: 2 }}>
        <CardContent>
          <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1 }}>
            <HistoryIcon fontSize="small" color="action" />
            <Typography variant="subtitle2">
              {t('operations.status.recentAutonomousRuns')}
            </Typography>
          </Stack>
          {recentRuns.length === 0 && (
            <Typography color="text.secondary" variant="body2">
              {t('operations.status.noRunsYet')}
            </Typography>
          )}
          <List dense>
            {recentRuns.map((r: any) => (
              <ListItem
                key={r.id} disableGutters
                secondaryAction={
                  r.duration_sec != null
                    ? <Typography variant="caption" color="text.secondary">{r.duration_sec}s</Typography>
                    : undefined
                }
                sx={{
                  borderBottom: '1px solid',
                  borderColor: 'divider',
                  '&:last-child': { borderBottom: 0 },
                }}
              >
                <Stack direction="row" spacing={1} alignItems="center" sx={{ flex: 1, minWidth: 0 }}>
                  <StatusChip status={r.status} />
                  <Typography variant="body2" sx={{ fontWeight: 600 }}>
                    {r.event_type ?? '—'}
                  </Typography>
                  {r.model && (
                    <Chip
                      size="small"
                      variant="outlined"
                      label={r.model}
                      sx={{ height: 18, '& .MuiChip-label': { px: 0.75, fontSize: 11 } }}
                    />
                  )}
                  <Typography variant="caption" color="text.secondary">
                    {fmtTime(r.started_at)}
                  </Typography>
                  <Box sx={{ flex: 1 }} />
                  <Link component={RouterLink} to={`/runs/${r.id}`}>
                    #{r.id}
                  </Link>
                </Stack>
              </ListItem>
            ))}
          </List>
        </CardContent>
      </Card>

      {/* R-OPS-2: chat conversations are surfaced separately so an
          operator scanning the autonomous control plane doesn't see
          their own chat sessions reported as "executions". Hidden
          when there are none — the rest of the page already covers
          the "no activity" empty state. */}
      {recentChats.length > 0 && (
        <Card variant="outlined" sx={{ borderRadius: 2 }}>
          <CardContent>
            <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1 }}>
              <HistoryIcon fontSize="small" color="action" />
              <Typography variant="subtitle2">
                {t('operations.status.recentChats')}
              </Typography>
              <Typography variant="caption" color="text.secondary" sx={{ ml: 'auto' }}>
                {t('operations.status.recentChatsHint')}
              </Typography>
            </Stack>
            <List dense>
              {recentChats.map((r: any) => (
                <ListItem
                  key={r.id} disableGutters
                  secondaryAction={
                    r.duration_sec != null
                      ? <Typography variant="caption" color="text.secondary">{r.duration_sec}s</Typography>
                      : undefined
                  }
                  sx={{
                    borderBottom: '1px solid',
                    borderColor: 'divider',
                    '&:last-child': { borderBottom: 0 },
                  }}
                >
                  <Stack direction="row" spacing={1} alignItems="center" sx={{ flex: 1, minWidth: 0 }}>
                    <StatusChip status={r.status} />
                    <Typography variant="body2" sx={{ fontWeight: 600 }}>
                      {t('operations.status.modeChat')}
                    </Typography>
                    {r.model && (
                      <Chip
                        size="small"
                        variant="outlined"
                        label={r.model}
                        sx={{ height: 18, '& .MuiChip-label': { px: 0.75, fontSize: 11 } }}
                      />
                    )}
                    <Typography variant="caption" color="text.secondary">
                      {fmtTime(r.started_at)}
                    </Typography>
                    <Box sx={{ flex: 1 }} />
                    <Link component={RouterLink} to={`/runs/${r.id}`}>
                      #{r.id}
                    </Link>
                  </Stack>
                </ListItem>
              ))}
            </List>
          </CardContent>
        </Card>
      )}
    </Stack>
  )
}
