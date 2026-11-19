// Reusable per-user memory viewer/editor. Extracted from Operations.tsx
// so the agent chat can show "Mi memoria" for the logged-in user.
//
// Server-side querying: every layer (facts / episodes / files) is fetched
// paginated + filtered from the backend, so the UI stays usable no matter
// how big the memory grows. A /block call tells us exactly which rows are
// injected into the prompt right now, so each entry is badged
// applied / stored-not-injected / obsolete.
//
// Props:
//   userEmail       fix the viewer to one user (chat self-service mode)
//   hideUserPicker  hide the left user list (implies a fixed userEmail)
//   readOnly        hide every mutation control (view only)
//   showCompact     show the "Optimizar" (deterministic + agent) button
//   showHelp        show the memory-types explainer block
import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Accordion, AccordionDetails, AccordionSummary, Alert, Avatar, Box, Button,
  Checkbox, Chip, CircularProgress, Dialog, DialogActions, DialogContent,
  DialogTitle, Divider, Grid, IconButton, InputAdornment, List, ListItem,
  ListItemButton, ListItemText, MenuItem, Paper, Stack, Switch, TextField,
  ToggleButton, ToggleButtonGroup, Tooltip, Typography, useMediaQuery,
  useTheme,
} from '@mui/material'
import {
  InsertDriveFileOutlined as FileIcon,
  PushPin as PinIcon, PushPinOutlined as PinOutlineIcon,
  Add as AddIcon, Search as SearchIcon, Refresh as RefreshIcon,
  Download as DownloadIcon, DeleteOutline as DeleteIcon,
  LightbulbOutlined as FactIcon, TuneOutlined as PreferenceIcon,
  AccountTreeOutlined as ProcedureIcon, HistoryToggleOff as EpisodeIcon,
  AutoFixHigh as OptimizeIcon, ExpandMore as ExpandMoreIcon,
  HelpOutline as HelpIcon, EditOutlined as EditIcon,
  VisibilityOutlined as ViewBlockIcon, FilterListOutlined as FilterIcon,
  Close as CloseIcon,
} from '@mui/icons-material'
import { apiFetch } from '../services/apiClient'
import { formatDateTime } from '../utils/datetime'
import { fmtBytes } from './WorkspaceViewer'
import { useUsersDirectory } from './UserPicker'

// A person is shown by their directory NAME + avatar (never a raw email) —
// consistent with the Sessions view. The email stays as a tooltip.
const personInitials = (s: string) =>
  s.split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0]?.toUpperCase() ?? '').join('') || '·'

type MemUser = {
  user_email: string
  facts: number
  episodes: number
  files: number
  last_active: string | null
}
type MemArtifact = { filename: string | null; workspace_path: string }
type MemFact = {
  id: number
  kind: string
  content: string
  tags: string | null
  pinned: boolean
  source: string
  confidence: number | null
  superseded_by: number | null
  artifacts?: MemArtifact[]
  access_count?: number
  last_accessed_at?: string | null
  created_at: string | null
  updated_at: string | null
}
type MemEpisode = {
  id: number
  summary: string
  source: string
  outcome: string | null
  artifacts?: MemArtifact[]
  created_at: string | null
}
type MemFile = {
  id: number
  filename: string
  workspace_path: string
  mime: string | null
  size: number | null
  source: string
  description: string | null
  created_at: string | null
}
type Page<T> = { items: T[]; total: number; limit: number; offset: number }
type MemStats = {
  facts_live: number; facts_superseded: number; facts_total: number
  episodes_total: number; files_total: number
  chars_facts: number; block_chars: number; block_tokens?: number
  oldest: string | null; newest: string | null
  facts_cap: number; autocompact_over: number
  over_facts_cap: boolean; over_autocompact: boolean
}
type MemBlock = {
  user_email: string; text: string; empty: boolean
  included_fact_ids: number[]; included_episode_ids: number[]; included_file_ids: number[]
}

const PAGE = 25

function useDebounced<T>(value: T, ms = 350): T {
  const [v, setV] = useState(value)
  useEffect(() => {
    const id = setTimeout(() => setV(value), ms)
    return () => clearTimeout(id)
  }, [value, ms])
  return v
}

function qs(params: Record<string, unknown>): string {
  const u = new URLSearchParams()
  Object.entries(params).forEach(([k, v]) => {
    if (v === undefined || v === null || v === '') return
    u.set(k, String(v))
  })
  return u.toString()
}

type FactEditor = {
  open: boolean
  id?: number
  content: string
  kind: string
  tags: string
  pinned: boolean
}
const CLOSED_FACT: FactEditor = { open: false, content: '', kind: 'fact', tags: '', pinned: true }

type EpisodeEditor = { open: boolean; id?: number; summary: string; outcome: string }
const CLOSED_EP: EpisodeEditor = { open: false, summary: '', outcome: '' }

export default function MemoryViewer({
  userEmail,
  hideUserPicker = false,
  readOnly = false,
  showCompact = true,
  showHelp = true,
}: {
  userEmail?: string
  hideUserPicker?: boolean
  readOnly?: boolean
  showCompact?: boolean
  showHelp?: boolean
}) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const theme = useTheme()
  const fullScreen = useMediaQuery(theme.breakpoints.down('sm'))

  const [selected, setSelected] = useState<string | null>(null)
  const [userSearch, setUserSearch] = useState('')
  const debouncedUserSearch = useDebounced(userSearch)

  // Layer filters (server-side).
  const [search, setSearch] = useState('')
  const debouncedSearch = useDebounced(search)
  const [kindFilter, setKindFilter] = useState('')
  const [sourceFilter, setSourceFilter] = useState('')
  const [sort, setSort] = useState('recent')
  const [showObsolete, setShowObsolete] = useState(false)
  const [datePreset, setDatePreset] = useState('all')
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')
  const [filtersOpen, setFiltersOpen] = useState(false)

  // Pagination (load-more grows the page size; server caps at 200).
  const [factsLimit, setFactsLimit] = useState(PAGE)
  const [episodesLimit, setEpisodesLimit] = useState(PAGE)
  const [filesLimit, setFilesLimit] = useState(PAGE)

  // Selection / editors / dialogs.
  const [selectedFacts, setSelectedFacts] = useState<Set<number>>(new Set())
  const [factEditor, setFactEditor] = useState<FactEditor>(CLOSED_FACT)
  const [epEditor, setEpEditor] = useState<EpisodeEditor>(CLOSED_EP)
  const [blockOpen, setBlockOpen] = useState(false)
  const [feedback, setFeedback] = useState<{ kind: 'success' | 'error'; msg: string } | null>(null)
  const [busy, setBusy] = useState(false)
  const [confirmState, setConfirmState] = useState<
    { msg: string; danger?: boolean; onConfirm: () => void } | null
  >(null)

  const flash = (kind: 'success' | 'error', msg: string) => {
    setFeedback({ kind, msg })
    setTimeout(() => setFeedback(null), 5000)
  }
  const askConfirm = (msg: string, onConfirm: () => void, danger = false) =>
    setConfirmState({ msg, danger, onConfirm })

  const usersQuery = useQuery({
    queryKey: ['mem-users', debouncedUserSearch],
    enabled: !hideUserPicker,
    queryFn: () => apiFetch(`/api/agent/memory/users?${qs({ q: debouncedUserSearch })}`).then(async (r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      return r.json() as Promise<{ users: MemUser[] }>
    }),
    refetchInterval: 60_000,
    retry: false,
  })

  const users = usersQuery.data?.users ?? []
  // Resolve a memory user's email → directory display name (like Sessions).
  const { data: directory } = useUsersDirectory()
  const nameByEmail = useMemo(() => {
    const m = new Map<string, string>()
    for (const u of (directory?.items ?? [])) {
      if (u.email) m.set(u.email.toLowerCase(), u.display_name || u.alias || u.email)
    }
    return m
  }, [directory])
  const nameFor = (email: string) => nameByEmail.get((email || '').toLowerCase()) || email
  const rawActive = hideUserPicker
    ? (userEmail ?? null)
    : (selected ?? userEmail ?? users[0]?.user_email ?? null)
  const activeUser = rawActive?.trim().toLowerCase() || null

  // Reset pagination + selection whenever the user or filters change.
  useEffect(() => {
    setFactsLimit(PAGE); setEpisodesLimit(PAGE); setFilesLimit(PAGE)
    setSelectedFacts(new Set())
  }, [activeUser, debouncedSearch, kindFilter, sourceFilter, sort, showObsolete, datePreset, dateFrom, dateTo])

  const dateParams = useMemo(() => {
    if (datePreset === 'all') return { date_from: '', date_to: '' }
    if (datePreset === 'custom') return { date_from: dateFrom, date_to: dateTo }
    const days = datePreset === '7' ? 7 : datePreset === '30' ? 30 : 90
    const d = new Date(); d.setDate(d.getDate() - days)
    return { date_from: d.toISOString().slice(0, 10), date_to: '' }
  }, [datePreset, dateFrom, dateTo])

  const statsQuery = useQuery({
    queryKey: ['mem-stats', activeUser],
    enabled: !!activeUser,
    queryFn: () => apiFetch(`/api/agent/memory/stats?${qs({ user_email: activeUser })}`).then(async (r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      return r.json() as Promise<MemStats>
    }),
    retry: false,
  })

  const blockQuery = useQuery({
    queryKey: ['mem-block', activeUser],
    enabled: !!activeUser,
    queryFn: () => apiFetch(`/api/agent/memory/block?${qs({ user_email: activeUser })}`).then(async (r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      return r.json() as Promise<MemBlock>
    }),
    retry: false,
  })

  const factsQuery = useQuery({
    queryKey: ['mem-facts', activeUser, debouncedSearch, kindFilter, sourceFilter, sort, showObsolete, dateParams, factsLimit],
    enabled: !!activeUser,
    queryFn: () => apiFetch(`/api/agent/memory/facts?${qs({
      user_email: activeUser, q: debouncedSearch, kind: kindFilter, source: sourceFilter,
      sort, include_superseded: showObsolete, ...dateParams, limit: factsLimit, offset: 0,
    })}`).then(async (r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      return r.json() as Promise<Page<MemFact>>
    }),
    retry: false,
  })

  const episodesQuery = useQuery({
    queryKey: ['mem-episodes', activeUser, debouncedSearch, sourceFilter, sort, dateParams, episodesLimit],
    enabled: !!activeUser,
    queryFn: () => apiFetch(`/api/agent/memory/episodes?${qs({
      user_email: activeUser, q: debouncedSearch, source: sourceFilter,
      sort, ...dateParams, limit: episodesLimit, offset: 0,
    })}`).then(async (r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      return r.json() as Promise<Page<MemEpisode>>
    }),
    retry: false,
  })

  const filesQuery = useQuery({
    queryKey: ['mem-files', activeUser, debouncedSearch, sourceFilter, sort, dateParams, filesLimit],
    enabled: !!activeUser,
    queryFn: () => apiFetch(`/api/agent/memory/files?${qs({
      user_email: activeUser, q: debouncedSearch, source: sourceFilter,
      sort, ...dateParams, limit: filesLimit, offset: 0,
    })}`).then(async (r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      return r.json() as Promise<Page<MemFile>>
    }),
    retry: false,
  })

  const refreshAll = () => {
    for (const k of ['mem-users', 'mem-stats', 'mem-block', 'mem-facts', 'mem-episodes', 'mem-files']) {
      qc.invalidateQueries({ queryKey: [k] })
    }
  }

  // Business-timezone render (utils/datetime), not the raw browser locale.
  const fmt = (iso: string | null) => formatDateTime(iso)

  async function call(url: string, init?: RequestInit) {
    const res = await apiFetch(url, init)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    return res.json().catch(() => ({}))
  }
  const JSON_HEADERS = { 'Content-Type': 'application/json' }
  const userQs = (extra?: Record<string, unknown>) =>
    qs({ user_email: activeUser, ...extra })

  const includedFacts = useMemo(
    () => new Set(blockQuery.data?.included_fact_ids ?? []),
    [blockQuery.data],
  )
  const includedEpisodes = useMemo(
    () => new Set(blockQuery.data?.included_episode_ids ?? []),
    [blockQuery.data],
  )
  const includedFiles = useMemo(
    () => new Set(blockQuery.data?.included_file_ids ?? []),
    [blockQuery.data],
  )

  const factStatus = (f: MemFact): 'applied' | 'stored' | 'obsolete' =>
    f.superseded_by != null ? 'obsolete' : includedFacts.has(f.id) ? 'applied' : 'stored'

  const episodeStatus = (ep: MemEpisode): 'applied' | 'stored' =>
    includedEpisodes.has(ep.id) ? 'applied' : 'stored'

  const fileStatus = (fl: MemFile): 'applied' | 'stored' =>
    includedFiles.has(fl.id) ? 'applied' : 'stored'

  const StatusBadge = ({ status }: { status: 'applied' | 'stored' | 'obsolete' }) => {
    const map = {
      applied: { color: 'success' as const, label: t('operations.memory.statusApplied'), hint: t('operations.memory.statusAppliedHint') },
      stored: { color: 'default' as const, label: t('operations.memory.statusStored'), hint: t('operations.memory.statusStoredHint') },
      obsolete: { color: 'warning' as const, label: t('operations.memory.statusObsolete'), hint: t('operations.memory.statusObsoleteHint') },
    }[status]
    return (
      <Tooltip title={map.hint}>
        <Chip size="small" color={map.color} variant={status === 'stored' ? 'outlined' : 'filled'} label={map.label} />
      </Tooltip>
    )
  }

  // ── Mutations ───────────────────────────────────────────────────────
  const togglePin = async (f: MemFact) => {
    if (readOnly || !activeUser) return
    try {
      await call(`/api/agent/memory/facts/${f.id}?${userQs()}`, {
        method: 'PATCH', headers: JSON_HEADERS, body: JSON.stringify({ pinned: !f.pinned }),
      })
      refreshAll()
    } catch (e: any) { flash('error', e?.message || t('operations.memory.error')) }
  }

  const removeFact = (f: MemFact) => askConfirm(t('operations.memory.confirmDelete'), async () => {
    if (!activeUser) return
    try {
      await call(`/api/agent/memory/facts/${f.id}?${userQs()}`, { method: 'DELETE' })
      flash('success', t('operations.memory.deleted')); refreshAll()
    } catch (e: any) { flash('error', e?.message || t('operations.memory.error')) }
  }, true)

  const removeEpisode = (ep: MemEpisode) => askConfirm(t('operations.memory.confirmDelete'), async () => {
    try {
      await call(`/api/agent/memory/episodes/${ep.id}?${userQs()}`, { method: 'DELETE' })
      flash('success', t('operations.memory.deleted')); refreshAll()
    } catch (e: any) { flash('error', e?.message || t('operations.memory.error')) }
  }, true)

  const removeFile = (fl: MemFile) => askConfirm(t('operations.memory.confirmDeleteFile'), async () => {
    try {
      await call(`/api/agent/memory/files/${fl.id}?${userQs()}`, { method: 'DELETE' })
      flash('success', t('operations.memory.deleted')); refreshAll()
    } catch (e: any) { flash('error', e?.message || t('operations.memory.error')) }
  }, true)

  const forgetUser = () => {
    if (!activeUser) return
    askConfirm(t('operations.memory.confirmForget', { user: activeUser }), async () => {
      try {
        await call(`/api/agent/memory/users/${encodeURIComponent(activeUser)}`, { method: 'DELETE' })
        flash('success', t('operations.memory.forgotten')); setSelected(null); refreshAll()
      } catch (e: any) { flash('error', e?.message || t('operations.memory.error')) }
    }, true)
  }

  const optimize = () => {
    if (!activeUser) return
    askConfirm(t('operations.memory.confirmOptimize'), async () => {
      setBusy(true)
      try {
        const r = await call(`/api/agent/memory/compact?${qs({ user_email: activeUser })}`, { method: 'POST' })
        const rep = r?.report || {}
        flash('success', t('operations.memory.optimized', {
          merged: rep.merged_facts ?? 0, capped: rep.capped_facts ?? 0,
          files: rep.deduped_files ?? 0,
          episodes: (rep.pruned_episodes ?? 0) + (rep.ttl_pruned_episodes ?? 0),
        }))
        refreshAll()
      } catch (e: any) {
        flash('error', e?.message || t('operations.memory.error'))
      } finally { setBusy(false) }
    })
  }

  const exportMemory = async () => {
    if (!activeUser) return
    setBusy(true)
    try {
      const data = await call(`/api/agent/memory/export?${qs({ user_email: activeUser })}`)
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `memoria-${activeUser.replace(/[^\w.@-]+/g, '_')}.json`
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
    } catch (e: any) {
      flash('error', e?.message || t('operations.memory.error'))
    } finally { setBusy(false) }
  }

  const saveFact = async () => {
    const content = factEditor.content.trim()
    if (!content || !activeUser) return
    setBusy(true)
    try {
      if (factEditor.id) {
        await call(`/api/agent/memory/facts/${factEditor.id}?${userQs()}`, {
          method: 'PATCH', headers: JSON_HEADERS,
          body: JSON.stringify({ content, kind: factEditor.kind, tags: factEditor.tags.trim() || null, pinned: factEditor.pinned }),
        })
        flash('success', t('operations.memory.updated'))
      } else {
        await call('/api/agent/memory/facts', {
          method: 'POST', headers: JSON_HEADERS,
          body: JSON.stringify({ user_email: activeUser, content, kind: factEditor.kind, tags: factEditor.tags.trim() || null, pinned: factEditor.pinned }),
        })
        flash('success', t('operations.memory.added'))
      }
      setFactEditor(CLOSED_FACT); refreshAll()
    } catch (e: any) { flash('error', e?.message || t('operations.memory.error')) }
    finally { setBusy(false) }
  }

  const saveEpisode = async () => {
    if (!epEditor.id || !activeUser) return
    const summary = epEditor.summary.trim()
    if (!summary) return
    setBusy(true)
    try {
      await call(`/api/agent/memory/episodes/${epEditor.id}?${userQs()}`, {
        method: 'PATCH', headers: JSON_HEADERS,
        body: JSON.stringify({ summary, outcome: epEditor.outcome.trim() || null }),
      })
      flash('success', t('operations.memory.updated')); setEpEditor(CLOSED_EP); refreshAll()
    } catch (e: any) { flash('error', e?.message || t('operations.memory.error')) }
    finally { setBusy(false) }
  }

  const bulkAction = async (action: 'delete' | 'pin' | 'unpin' | 'supersede') => {
    const ids = Array.from(selectedFacts)
    if (ids.length === 0 || !activeUser) return
    const run = async () => {
      setBusy(true)
      try {
        await call('/api/agent/memory/facts/bulk', {
          method: 'POST', headers: JSON_HEADERS,
          body: JSON.stringify({ ids, action, user_email: activeUser }),
        })
        flash('success', t('operations.memory.bulkDone', { count: ids.length }))
        setSelectedFacts(new Set()); refreshAll()
      } catch (e: any) { flash('error', e?.message || t('operations.memory.error')) }
      finally { setBusy(false) }
    }
    if (action === 'delete') askConfirm(t('operations.memory.confirmBulkDelete', { count: ids.length }), run, true)
    else run()
  }

  async function download(path: string, filename: string) {
    try {
      const res = await apiFetch(`/api/agent/workspace/download?path=${encodeURIComponent(path)}`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const blob = await res.blob()
      const obj = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = obj; a.download = filename
      document.body.appendChild(a); a.click(); a.remove()
      URL.revokeObjectURL(obj)
    } catch (e: any) { flash('error', e?.message || t('operations.memory.error')) }
  }

  const kindIcon = (kind: string) => {
    if (kind === 'preference') return <PreferenceIcon fontSize="small" color="secondary" />
    if (kind === 'procedure') return <ProcedureIcon fontSize="small" color="info" />
    return <FactIcon fontSize="small" color="warning" />
  }

  const renderArtifacts = (arts?: MemArtifact[]) => {
    if (!arts || arts.length === 0) return null
    return (
      <Stack direction="row" spacing={0.5} flexWrap="wrap" sx={{ mt: 0.5 }}>
        {arts.map((a) => (
          <Chip
            key={a.workspace_path}
            size="small"
            icon={<FileIcon fontSize="small" />}
            label={a.filename || a.workspace_path.split('/').pop()}
            onClick={() => download(a.workspace_path, a.filename || a.workspace_path.split('/').pop() || 'file')}
            sx={{ maxWidth: 260 }}
          />
        ))}
      </Stack>
    )
  }

  const toggleSelect = (id: number) => setSelectedFacts((prev) => {
    const n = new Set(prev)
    if (n.has(id)) n.delete(id); else n.add(id)
    return n
  })

  const facts = factsQuery.data?.items ?? []
  const episodes = episodesQuery.data?.items ?? []
  const files = filesQuery.data?.items ?? []
  const factsTotal = factsQuery.data?.total ?? 0
  const episodesTotal = episodesQuery.data?.total ?? 0
  const filesTotal = filesQuery.data?.total ?? 0
  const stats = statsQuery.data

  const hasActiveFilters = !!(debouncedSearch || kindFilter || sourceFilter || showObsolete || datePreset !== 'all' || sort !== 'recent')
  const clearFilters = () => {
    setSearch(''); setKindFilter(''); setSourceFilter(''); setShowObsolete(false)
    setDatePreset('all'); setDateFrom(''); setDateTo(''); setSort('recent')
  }

  // ── Sub-renders ─────────────────────────────────────────────────────
  const helpBlock = showHelp ? (
    <Accordion variant="outlined" disableGutters sx={{ borderRadius: 2, '&:before': { display: 'none' } }}>
      <AccordionSummary expandIcon={<ExpandMoreIcon />}>
        <Stack direction="row" spacing={1} alignItems="center">
          <HelpIcon fontSize="small" color="action" />
          <Typography variant="body2">{t('operations.memory.help.title')}</Typography>
        </Stack>
      </AccordionSummary>
      <AccordionDetails>
        <Stack spacing={1}>
          <Typography variant="body2" color="text.secondary">{t('operations.memory.help.intro')}</Typography>
          <Stack direction="row" spacing={1} alignItems="flex-start">
            <FactIcon fontSize="small" color="warning" />
            <Typography variant="body2"><strong>{t('operations.memory.help.factsTitle')}</strong> — {t('operations.memory.help.factsBody')}</Typography>
          </Stack>
          <Stack direction="row" spacing={1} alignItems="flex-start">
            <EpisodeIcon fontSize="small" color="action" />
            <Typography variant="body2"><strong>{t('operations.memory.help.episodesTitle')}</strong> — {t('operations.memory.help.episodesBody')}</Typography>
          </Stack>
          <Stack direction="row" spacing={1} alignItems="flex-start">
            <FileIcon fontSize="small" color="action" />
            <Typography variant="body2"><strong>{t('operations.memory.help.filesTitle')}</strong> — {t('operations.memory.help.filesBody')}</Typography>
          </Stack>
          <Typography variant="body2" color="text.secondary">{t('operations.memory.help.concepts')}</Typography>
        </Stack>
      </AccordionDetails>
    </Accordion>
  ) : null

  const healthHeader = stats ? (
    <Paper variant="outlined" sx={{ p: 1.25, borderRadius: 2 }}>
      <Stack direction="row" spacing={1.5} alignItems="center" flexWrap="wrap" useFlexGap>
        <Stack>
          <Typography variant="caption" color="text.secondary">{t('operations.memory.healthLive')}</Typography>
          <Typography variant="body2"><strong>{stats.facts_live}</strong>{stats.facts_cap > 0 ? ` / ${stats.facts_cap}` : ''}</Typography>
        </Stack>
        <Divider orientation="vertical" flexItem />
        <Stack>
          <Typography variant="caption" color="text.secondary">{t('operations.memory.healthObsolete')}</Typography>
          <Typography variant="body2">{stats.facts_superseded}</Typography>
        </Stack>
        <Divider orientation="vertical" flexItem />
        <Stack>
          <Typography variant="caption" color="text.secondary">{t('operations.memory.healthEpisodes')}</Typography>
          <Typography variant="body2">{stats.episodes_total}</Typography>
        </Stack>
        <Divider orientation="vertical" flexItem />
        <Stack>
          <Typography variant="caption" color="text.secondary">{t('operations.memory.healthFiles')}</Typography>
          <Typography variant="body2">{stats.files_total}</Typography>
        </Stack>
        <Divider orientation="vertical" flexItem />
        <Stack>
          <Typography variant="caption" color="text.secondary">{t('operations.memory.healthBlock')}</Typography>
          <Typography variant="body2">
            {stats.block_tokens
              ? t('operations.memory.healthTokens', { n: stats.block_tokens })
              : t('operations.memory.healthChars', { n: stats.block_chars })}
          </Typography>
        </Stack>
        <Box sx={{ flex: 1 }} />
        <Button
          size="small" variant="text" startIcon={<ViewBlockIcon fontSize="small" />}
          onClick={() => setBlockOpen(true)} sx={{ textTransform: 'none' }}
        >
          {t('operations.memory.viewBlock')}
        </Button>
      </Stack>
      {stats.over_autocompact && (
        <Alert severity="info" sx={{ mt: 1, py: 0 }}>{t('operations.memory.healthOver')}</Alert>
      )}
    </Paper>
  ) : statsQuery.isLoading && activeUser ? (
    <Box sx={{ display: 'flex', justifyContent: 'center', py: 1 }}><CircularProgress size={20} /></Box>
  ) : statsQuery.error ? (
    <Alert severity="warning">{String((statsQuery.error as any)?.message ?? statsQuery.error)}</Alert>
  ) : null

  const blockErrorAlert = blockQuery.error ? (
    <Alert severity="warning">{String((blockQuery.error as any)?.message ?? blockQuery.error)}</Alert>
  ) : null

  const filterBar = (
    <Stack spacing={1}>
      <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
        <TextField
          size="small"
          placeholder={t('operations.memory.searchPlaceholder')}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          InputProps={{
            startAdornment: <InputAdornment position="start"><SearchIcon fontSize="small" /></InputAdornment>,
            endAdornment: search ? (
              <InputAdornment position="end">
                <IconButton size="small" onClick={() => setSearch('')}><CloseIcon fontSize="small" /></IconButton>
              </InputAdornment>
            ) : undefined,
          }}
          sx={{ flex: 1, minWidth: 180 }}
        />
        <Button
          size="small" variant={filtersOpen || hasActiveFilters ? 'contained' : 'outlined'}
          color={hasActiveFilters ? 'primary' : 'inherit'}
          startIcon={<FilterIcon fontSize="small" />}
          onClick={() => setFiltersOpen((v) => !v)} sx={{ textTransform: 'none' }}
        >
          {t('operations.memory.filters')}
        </Button>
        {!readOnly && (
          <Button
            size="small" variant="outlined" startIcon={<AddIcon fontSize="small" />}
            onClick={() => setFactEditor({ ...CLOSED_FACT, open: true })} disabled={!activeUser}
            sx={{ textTransform: 'none' }}
          >
            {t('operations.memory.addFact')}
          </Button>
        )}
        {showCompact && !readOnly && (
          <Button
            size="small" variant="outlined" color="primary" startIcon={<OptimizeIcon fontSize="small" />}
            onClick={optimize} disabled={!activeUser || busy} sx={{ textTransform: 'none' }}
          >
            {t('operations.memory.optimize')}
          </Button>
        )}
        <Tooltip title={t('operations.memory.refresh')}>
          <span><IconButton size="small" onClick={refreshAll}><RefreshIcon fontSize="small" /></IconButton></span>
        </Tooltip>
        <Tooltip title={t('operations.memory.export', 'Exportar (JSON)')}>
          <span>
            <IconButton size="small" onClick={exportMemory} disabled={!activeUser || busy}>
              <DownloadIcon fontSize="small" />
            </IconButton>
          </span>
        </Tooltip>
        {!readOnly && !hideUserPicker && (
          <Button
            size="small" variant="outlined" color="error" startIcon={<DeleteIcon fontSize="small" />}
            onClick={forgetUser} disabled={!activeUser} sx={{ textTransform: 'none' }}
          >
            {t('operations.memory.forget')}
          </Button>
        )}
      </Stack>

      {filtersOpen && (
        <Paper variant="outlined" sx={{ p: 1.25, borderRadius: 2 }}>
          <Stack spacing={1.25}>
            <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
              <TextField
                select size="small" label={t('operations.memory.kindLabel')}
                value={kindFilter} onChange={(e) => setKindFilter(e.target.value)} sx={{ minWidth: 150 }}
              >
                <MenuItem value="">{t('operations.memory.filterKindAll')}</MenuItem>
                <MenuItem value="fact">{t('operations.memory.kind.fact')}</MenuItem>
                <MenuItem value="preference">{t('operations.memory.kind.preference')}</MenuItem>
                <MenuItem value="procedure">{t('operations.memory.kind.procedure')}</MenuItem>
              </TextField>
              <TextField
                select size="small" label={t('operations.memory.sourceLabel')}
                value={sourceFilter} onChange={(e) => setSourceFilter(e.target.value)} sx={{ minWidth: 150 }}
              >
                <MenuItem value="">{t('operations.memory.filterSourceAll')}</MenuItem>
                <MenuItem value="agent">agent</MenuItem>
                <MenuItem value="operator">operator</MenuItem>
                <MenuItem value="chat">chat</MenuItem>
                <MenuItem value="email">email</MenuItem>
              </TextField>
              <TextField
                select size="small" label={t('operations.memory.sortLabel')}
                value={sort} onChange={(e) => setSort(e.target.value)} sx={{ minWidth: 160 }}
              >
                <MenuItem value="recent">{t('operations.memory.sortRecent')}</MenuItem>
                <MenuItem value="oldest">{t('operations.memory.sortOldest')}</MenuItem>
                <MenuItem value="pinned">{t('operations.memory.sortPinned')}</MenuItem>
              </TextField>
            </Stack>
            <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
              <Typography variant="caption" color="text.secondary">{t('operations.memory.date')}</Typography>
              <ToggleButtonGroup
                size="small" exclusive value={datePreset}
                onChange={(_e, v) => v && setDatePreset(v)}
              >
                <ToggleButton value="all" sx={{ textTransform: 'none' }}>{t('operations.memory.dateAll')}</ToggleButton>
                <ToggleButton value="7" sx={{ textTransform: 'none' }}>{t('operations.memory.date7')}</ToggleButton>
                <ToggleButton value="30" sx={{ textTransform: 'none' }}>{t('operations.memory.date30')}</ToggleButton>
                <ToggleButton value="90" sx={{ textTransform: 'none' }}>{t('operations.memory.date90')}</ToggleButton>
                <ToggleButton value="custom" sx={{ textTransform: 'none' }}>{t('operations.memory.dateCustom')}</ToggleButton>
              </ToggleButtonGroup>
              {datePreset === 'custom' && (
                <>
                  <TextField
                    size="small" type="date" label={t('operations.memory.dateFrom')}
                    value={dateFrom} onChange={(e) => setDateFrom(e.target.value)}
                    InputLabelProps={{ shrink: true }}
                  />
                  <TextField
                    size="small" type="date" label={t('operations.memory.dateTo')}
                    value={dateTo} onChange={(e) => setDateTo(e.target.value)}
                    InputLabelProps={{ shrink: true }}
                  />
                </>
              )}
            </Stack>
            <Stack direction="row" spacing={1} alignItems="center" justifyContent="space-between" flexWrap="wrap">
              <Stack direction="row" spacing={1} alignItems="center">
                <Switch size="small" checked={showObsolete} onChange={(e) => setShowObsolete(e.target.checked)} />
                <Typography variant="body2">{t('operations.memory.showObsolete')}</Typography>
              </Stack>
              {hasActiveFilters && (
                <Button size="small" onClick={clearFilters} sx={{ textTransform: 'none' }}>
                  {t('operations.memory.clearFilters')}
                </Button>
              )}
            </Stack>
          </Stack>
        </Paper>
      )}
    </Stack>
  )

  const bulkBar = (!readOnly && selectedFacts.size > 0) ? (
    <Paper variant="outlined" sx={{ p: 1, borderRadius: 2, bgcolor: 'action.hover' }}>
      <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
        <Typography variant="body2">{t('operations.memory.selectedCount', { count: selectedFacts.size })}</Typography>
        <Box sx={{ flex: 1 }} />
        <Button size="small" startIcon={<PinIcon fontSize="small" />} onClick={() => bulkAction('pin')} sx={{ textTransform: 'none' }}>{t('operations.memory.bulkPin')}</Button>
        <Button size="small" startIcon={<PinOutlineIcon fontSize="small" />} onClick={() => bulkAction('unpin')} sx={{ textTransform: 'none' }}>{t('operations.memory.bulkUnpin')}</Button>
        <Button size="small" color="warning" onClick={() => bulkAction('supersede')} sx={{ textTransform: 'none' }}>{t('operations.memory.bulkSupersede')}</Button>
        <Button size="small" color="error" startIcon={<DeleteIcon fontSize="small" />} onClick={() => bulkAction('delete')} sx={{ textTransform: 'none' }}>{t('operations.memory.bulkDelete')}</Button>
        <Button size="small" onClick={() => setSelectedFacts(new Set())} sx={{ textTransform: 'none' }}>{t('operations.memory.bulkClear')}</Button>
      </Stack>
    </Paper>
  ) : null

  const loadMoreBtn = (shown: number, total: number, onMore: () => void) => (
    total > shown ? (
      <Stack alignItems="center" sx={{ mt: 1 }}>
        <Button size="small" onClick={onMore} sx={{ textTransform: 'none' }}>
          {t('operations.memory.loadMore')} ({t('operations.memory.showing', { shown, total })})
        </Button>
      </Stack>
    ) : null
  )

  const anyLoading = factsQuery.isLoading || episodesQuery.isLoading || filesQuery.isLoading
  const anyError = factsQuery.error || episodesQuery.error || filesQuery.error

  const detailColumn = (
    <Stack spacing={2}>
      {healthHeader}
      {blockErrorAlert}
      {filterBar}
      {bulkBar}

      {!activeUser ? (
        <Alert severity="info">{t('operations.memory.empty')}</Alert>
      ) : anyError ? (
        <Alert severity="error">{String((anyError as any)?.message ?? anyError)}</Alert>
      ) : (
        <>
          {/* Facts / preferences / procedures */}
          <Box>
            <Typography variant="overline" color="text.secondary">
              {t('operations.memory.facts')} ({factsTotal})
            </Typography>
            {factsQuery.isLoading && facts.length === 0 ? (
              <Box sx={{ display: 'flex', justifyContent: 'center', py: 2 }}><CircularProgress size={20} /></Box>
            ) : facts.length === 0 ? (
              <Typography variant="body2" color="text.secondary">
                {hasActiveFilters ? t('operations.memory.noResults') : t('operations.memory.noFacts')}
              </Typography>
            ) : (
              <Stack spacing={1} sx={{ mt: 0.5 }}>
                {facts.map((f) => (
                  <Paper key={f.id} variant="outlined" sx={{ p: 1.25, borderRadius: 2, opacity: f.superseded_by != null ? 0.7 : 1 }}>
                    <Stack direction="row" spacing={1} alignItems="flex-start">
                      {!readOnly && (
                        <Checkbox
                          size="small" sx={{ p: 0.25 }}
                          checked={selectedFacts.has(f.id)}
                          onChange={() => toggleSelect(f.id)}
                        />
                      )}
                      {kindIcon(f.kind)}
                      <Box sx={{ flex: 1, minWidth: 0 }}>
                        <Typography variant="body2" sx={{ textDecoration: f.superseded_by != null ? 'line-through' : 'none' }}>{f.content}</Typography>
                        <Stack direction="row" spacing={0.5} alignItems="center" sx={{ mt: 0.5 }} flexWrap="wrap" useFlexGap>
                          <StatusBadge status={factStatus(f)} />
                          <Chip size="small" variant="outlined" label={t(`operations.memory.kind.${f.kind}`)} />
                          <Chip size="small" variant="outlined" label={f.source} />
                          {f.tags && <Chip size="small" variant="outlined" label={f.tags} />}
                          {!!f.access_count && (
                            <Tooltip title={t('operations.memory.usedHint', 'Veces que el empleado lo ha usado en una conversación')}>
                              <Chip size="small" variant="outlined" color="info" label={t('operations.memory.usedCount', { count: f.access_count })} />
                            </Tooltip>
                          )}
                          <Typography variant="caption" color="text.secondary">{fmt(f.updated_at)}</Typography>
                        </Stack>
                        {renderArtifacts(f.artifacts)}
                      </Box>
                      {!readOnly && (
                        <Tooltip title={f.pinned ? t('operations.memory.unpin') : t('operations.memory.pin')}>
                          <IconButton size="small" onClick={() => togglePin(f)} color={f.pinned ? 'primary' : 'default'}>
                            {f.pinned ? <PinIcon fontSize="small" /> : <PinOutlineIcon fontSize="small" />}
                          </IconButton>
                        </Tooltip>
                      )}
                      {!readOnly && (
                        <>
                          <Tooltip title={t('operations.memory.edit')}>
                            <IconButton size="small" onClick={() => setFactEditor({ open: true, id: f.id, content: f.content, kind: f.kind, tags: f.tags || '', pinned: f.pinned })}>
                              <EditIcon fontSize="small" />
                            </IconButton>
                          </Tooltip>
                          <Tooltip title={t('operations.memory.delete')}>
                            <IconButton size="small" color="error" onClick={() => removeFact(f)}>
                              <DeleteIcon fontSize="small" />
                            </IconButton>
                          </Tooltip>
                        </>
                      )}
                    </Stack>
                  </Paper>
                ))}
              </Stack>
            )}
            {loadMoreBtn(facts.length, factsTotal, () => setFactsLimit((l) => l + PAGE))}
          </Box>

          {/* Episodes */}
          <Box>
            <Typography variant="overline" color="text.secondary">
              {t('operations.memory.episodes')} ({episodesTotal})
            </Typography>
            {episodesQuery.isLoading && episodes.length === 0 ? (
              <Box sx={{ display: 'flex', justifyContent: 'center', py: 2 }}><CircularProgress size={20} /></Box>
            ) : episodes.length === 0 ? (
              <Typography variant="body2" color="text.secondary">
                {hasActiveFilters ? t('operations.memory.noResults') : t('operations.memory.noEpisodes')}
              </Typography>
            ) : (
              <Stack spacing={1} sx={{ mt: 0.5 }}>
                {episodes.map((ep) => (
                  <Paper key={ep.id} variant="outlined" sx={{ p: 1.25, borderRadius: 2 }}>
                    <Stack direction="row" spacing={1} alignItems="flex-start">
                      <EpisodeIcon fontSize="small" color="action" />
                      <Box sx={{ flex: 1, minWidth: 0 }}>
                        <Typography variant="body2">{ep.summary}</Typography>
                        <Stack direction="row" spacing={0.5} alignItems="center" sx={{ mt: 0.5 }} flexWrap="wrap" useFlexGap>
                          <StatusBadge status={episodeStatus(ep)} />
                          <Chip size="small" variant="outlined" label={ep.source} />
                          {ep.outcome && <Chip size="small" variant="outlined" label={ep.outcome} />}
                          <Typography variant="caption" color="text.secondary">{fmt(ep.created_at)}</Typography>
                        </Stack>
                        {renderArtifacts(ep.artifacts)}
                      </Box>
                      {!readOnly && (
                        <>
                          <Tooltip title={t('operations.memory.edit')}>
                            <IconButton size="small" onClick={() => setEpEditor({ open: true, id: ep.id, summary: ep.summary, outcome: ep.outcome || '' })}>
                              <EditIcon fontSize="small" />
                            </IconButton>
                          </Tooltip>
                          <Tooltip title={t('operations.memory.delete')}>
                            <IconButton size="small" color="error" onClick={() => removeEpisode(ep)}>
                              <DeleteIcon fontSize="small" />
                            </IconButton>
                          </Tooltip>
                        </>
                      )}
                    </Stack>
                  </Paper>
                ))}
              </Stack>
            )}
            {loadMoreBtn(episodes.length, episodesTotal, () => setEpisodesLimit((l) => l + PAGE))}
          </Box>

          {/* Files */}
          <Box>
            <Typography variant="overline" color="text.secondary">
              {t('operations.memory.files')} ({filesTotal})
            </Typography>
            {filesQuery.isLoading && files.length === 0 ? (
              <Box sx={{ display: 'flex', justifyContent: 'center', py: 2 }}><CircularProgress size={20} /></Box>
            ) : files.length === 0 ? (
              <Typography variant="body2" color="text.secondary">
                {hasActiveFilters ? t('operations.memory.noResults') : t('operations.memory.noFiles')}
              </Typography>
            ) : (
              <Stack spacing={1} sx={{ mt: 0.5 }}>
                {files.map((fl) => (
                  <Paper key={fl.id} variant="outlined" sx={{ p: 1.25, borderRadius: 2 }}>
                    <Stack direction="row" spacing={1} alignItems="center">
                      <FileIcon fontSize="small" color="action" />
                      <Box sx={{ flex: 1, minWidth: 0 }}>
                        <Stack direction="row" spacing={0.5} alignItems="center" flexWrap="wrap" useFlexGap>
                          <Typography variant="body2" sx={{ wordBreak: 'break-all' }}>{fl.filename}</Typography>
                          <StatusBadge status={fileStatus(fl)} />
                        </Stack>
                        {fl.description && (
                          <Typography variant="caption" color="text.secondary">{fl.description}</Typography>
                        )}
                        <Typography variant="caption" color="text.secondary" sx={{ display: 'block' }}>
                          <code>{fl.workspace_path}</code> · {fmtBytes(fl.size)} · {fmt(fl.created_at)}
                        </Typography>
                      </Box>
                      <Tooltip title={t('operations.memory.download')}>
                        <IconButton size="small" onClick={() => download(fl.workspace_path, fl.filename)}>
                          <DownloadIcon fontSize="small" />
                        </IconButton>
                      </Tooltip>
                      {!readOnly && (
                        <Tooltip title={t('operations.memory.removeFromIndex')}>
                          <IconButton size="small" color="error" onClick={() => removeFile(fl)}>
                            <DeleteIcon fontSize="small" />
                          </IconButton>
                        </Tooltip>
                      )}
                    </Stack>
                  </Paper>
                ))}
              </Stack>
            )}
            {loadMoreBtn(files.length, filesTotal, () => setFilesLimit((l) => l + PAGE))}
          </Box>
        </>
      )}
      {anyLoading && (facts.length > 0 || episodes.length > 0 || files.length > 0) && (
        <Box sx={{ display: 'flex', justifyContent: 'center' }}><CircularProgress size={18} /></Box>
      )}
    </Stack>
  )

  return (
    <Stack spacing={2}>
      {!hideUserPicker && (
        <Typography variant="body2" color="text.secondary">
          {t('operations.memory.intro')}
        </Typography>
      )}
      {helpBlock}
      {feedback && <Alert severity={feedback.kind}>{feedback.msg}</Alert>}

      {hideUserPicker ? (
        detailColumn
      ) : usersQuery.error ? (
        <Alert severity="error">{String((usersQuery.error as any)?.message ?? usersQuery.error)}</Alert>
      ) : (
        <Grid container spacing={2}>
          {/* User picker */}
          <Grid item xs={12} md={4} lg={3}>
            <Stack spacing={1}>
              <TextField
                size="small"
                placeholder={t('operations.memory.userSearchPlaceholder')}
                value={userSearch}
                onChange={(e) => setUserSearch(e.target.value)}
                InputProps={{ startAdornment: <InputAdornment position="start"><SearchIcon fontSize="small" /></InputAdornment> }}
              />
              <Paper variant="outlined" sx={{ borderRadius: 2, maxHeight: { md: 520 }, overflow: 'auto' }}>
                {usersQuery.isLoading ? (
                  <Box sx={{ display: 'flex', justifyContent: 'center', py: 3 }}><CircularProgress size={22} /></Box>
                ) : users.length === 0 ? (
                  <Typography variant="body2" color="text.secondary" sx={{ p: 2 }}>{t('operations.memory.empty')}</Typography>
                ) : (
                  <List dense disablePadding>
                    {users.map((u) => {
                      const label = nameFor(u.user_email)
                      return (
                        <ListItem key={u.user_email} disablePadding>
                          <ListItemButton
                            selected={u.user_email === activeUser}
                            onClick={() => setSelected(u.user_email)}
                          >
                            <Tooltip title={u.user_email}>
                              <Avatar sx={{ width: 26, height: 26, fontSize: 12, mr: 1 }}>
                                {personInitials(label)}
                              </Avatar>
                            </Tooltip>
                            <ListItemText
                              primary={label}
                              secondary={t('operations.memory.userSummary', {
                                facts: u.facts, episodes: u.episodes, files: u.files,
                              })}
                              primaryTypographyProps={{ variant: 'body2', sx: { wordBreak: 'break-word' } }}
                              secondaryTypographyProps={{ variant: 'caption' }}
                            />
                          </ListItemButton>
                        </ListItem>
                      )
                    })}
                  </List>
                )}
              </Paper>
            </Stack>
          </Grid>

          {/* Detail */}
          <Grid item xs={12} md={8} lg={9}>
            {detailColumn}
          </Grid>
        </Grid>
      )}

      {/* Fact editor (create + edit) — large, mobile-fullscreen */}
      <Dialog
        open={factEditor.open} onClose={() => setFactEditor(CLOSED_FACT)}
        fullWidth maxWidth="md" fullScreen={fullScreen}
      >
        <DialogTitle>{factEditor.id ? t('operations.memory.editFact') : t('operations.memory.addFact')}</DialogTitle>
        <DialogContent>
          <Stack spacing={2} sx={{ mt: 1 }}>
            <TextField
              label={t('operations.memory.content')}
              value={factEditor.content}
              onChange={(e) => setFactEditor((s) => ({ ...s, content: e.target.value }))}
              multiline minRows={6} maxRows={18} fullWidth autoFocus
            />
            <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2}>
              <TextField
                select label={t('operations.memory.kindLabel')}
                value={factEditor.kind} onChange={(e) => setFactEditor((s) => ({ ...s, kind: e.target.value }))}
                sx={{ minWidth: 180 }}
              >
                <MenuItem value="fact">{t('operations.memory.kind.fact')}</MenuItem>
                <MenuItem value="preference">{t('operations.memory.kind.preference')}</MenuItem>
                <MenuItem value="procedure">{t('operations.memory.kind.procedure')}</MenuItem>
              </TextField>
              <TextField
                label={t('operations.memory.tags')} helperText={t('operations.memory.tagsHint')}
                value={factEditor.tags} onChange={(e) => setFactEditor((s) => ({ ...s, tags: e.target.value }))}
                fullWidth
              />
            </Stack>
            <Stack direction="row" spacing={1} alignItems="center">
              <Switch checked={factEditor.pinned} onChange={(e) => setFactEditor((s) => ({ ...s, pinned: e.target.checked }))} />
              <Typography variant="body2">{t('operations.memory.pinnedHint')}</Typography>
            </Stack>
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setFactEditor(CLOSED_FACT)}>{t('operations.memory.cancel')}</Button>
          <Button variant="contained" onClick={saveFact} disabled={!factEditor.content.trim() || busy}>
            {t('operations.memory.save')}
          </Button>
        </DialogActions>
      </Dialog>

      {/* Episode editor */}
      <Dialog
        open={epEditor.open} onClose={() => setEpEditor(CLOSED_EP)}
        fullWidth maxWidth="sm" fullScreen={fullScreen}
      >
        <DialogTitle>{t('operations.memory.editEpisode')}</DialogTitle>
        <DialogContent>
          <Stack spacing={2} sx={{ mt: 1 }}>
            <TextField
              label={t('operations.memory.summary')}
              value={epEditor.summary}
              onChange={(e) => setEpEditor((s) => ({ ...s, summary: e.target.value }))}
              multiline minRows={4} maxRows={12} fullWidth autoFocus
            />
            <TextField
              label={t('operations.memory.outcome')}
              value={epEditor.outcome}
              onChange={(e) => setEpEditor((s) => ({ ...s, outcome: e.target.value }))}
              fullWidth
            />
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setEpEditor(CLOSED_EP)}>{t('operations.memory.cancel')}</Button>
          <Button variant="contained" onClick={saveEpisode} disabled={!epEditor.summary.trim() || busy}>
            {t('operations.memory.save')}
          </Button>
        </DialogActions>
      </Dialog>

      {/* "What the agent sees now" */}
      <Dialog open={blockOpen} onClose={() => setBlockOpen(false)} fullWidth maxWidth="md" fullScreen={fullScreen}>
        <DialogTitle>{t('operations.memory.blockTitle')}</DialogTitle>
        <DialogContent>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
            {t('operations.memory.blockIntro')}
          </Typography>
          {blockQuery.data?.empty || !blockQuery.data?.text ? (
            <Alert severity="info">{t('operations.memory.blockEmpty')}</Alert>
          ) : (
            <Box
              component="pre"
              sx={{
                whiteSpace: 'pre-wrap', wordBreak: 'break-word', m: 0, p: 1.5,
                bgcolor: 'action.hover', borderRadius: 2, fontSize: 13,
                fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
              }}
            >
              {blockQuery.data.text.trim()}
            </Box>
          )}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setBlockOpen(false)}>{t('operations.memory.cancel')}</Button>
        </DialogActions>
      </Dialog>

      {/* Confirmation modal */}
      <Dialog open={!!confirmState} onClose={() => setConfirmState(null)} maxWidth="xs" fullWidth>
        <DialogTitle>{t('operations.memory.confirmTitle')}</DialogTitle>
        <DialogContent>
          <Typography variant="body2" sx={{ mt: 1, whiteSpace: 'pre-line' }}>
            {confirmState?.msg}
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setConfirmState(null)}>{t('operations.memory.cancel')}</Button>
          <Button
            variant="contained"
            color={confirmState?.danger ? 'error' : 'primary'}
            onClick={() => { const act = confirmState?.onConfirm; setConfirmState(null); act?.() }}
          >
            {t('operations.memory.continue')}
          </Button>
        </DialogActions>
      </Dialog>
    </Stack>
  )
}
