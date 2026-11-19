/**
 * Encargos — delegate tasks to the AI employee.
 *
 * A human briefs the employee from here; the task runs now (a
 * DELEGATED_TASK event → autonomous run) or is scheduled for later. The
 * board shows every task assigned to the employee with its status,
 * result and a deep-link to the run.
 */
import { Fragment, useMemo, useState } from 'react'
import {
  Box, Button, Card, CardContent, Chip, Dialog, DialogActions,
  DialogContent, DialogTitle, IconButton, InputAdornment, MenuItem, Stack, Table,
  TableBody, TableCell, TableHead, TablePagination, TableRow, TableSortLabel, TextField,
  ToggleButton, ToggleButtonGroup, Tooltip, Typography, Collapse, Snackbar, Alert,
} from '@mui/material'
import AssignmentTurnedInIcon from '@mui/icons-material/AssignmentTurnedIn'
import SearchIcon from '@mui/icons-material/Search'
import AddIcon from '@mui/icons-material/Add'
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline'
import OpenInNewIcon from '@mui/icons-material/OpenInNew'
import ExpandMoreIcon from '@mui/icons-material/ExpandMore'
import ReplayIcon from '@mui/icons-material/Replay'
import { useTranslation } from 'react-i18next'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Link as RouterLink } from 'react-router-dom'
import PageHeader from '../components/PageHeader'
import EmptyState from '../components/EmptyState'
import StatusChip from '../components/StatusChip'
import LoadingRegion from '../components/LoadingRegion'
import ArtifactList, { Artifact } from '../components/ArtifactList'
import { useConfirm } from '../components/ConfirmDialog'
import { apiClient, apiBase, apiFetch } from '../services/apiClient'
import { formatDateTime, fromLocalInputValue } from '../utils/datetime'
import {
  useAutomationProvenance,
  ProvenanceScopeAlert,
  AutomationOriginChip,
  AutomationProvenanceFilter,
} from '../surfaces/common/provenanceScope'

interface Task {
  id: number
  title: string
  description: string
  status: string
  priority: string
  due_at: string | null
  created_at: string
  result_md: string | null
  last_run_id: number | null
  artifacts?: Artifact[]
  created_by_automation?: { title?: string; via?: string } | null
}

const PRIORITIES = ['low', 'medium', 'high', 'urgent']

// Sortable columns. Priority sorts by SEVERITY (not alphabetically); the two
// date columns sort chronologically; the rest are case-insensitive strings.
type SortKey = 'title' | 'status' | 'priority' | 'due_at' | 'created_at'
const PRIORITY_RANK: Record<string, number> = { urgent: 4, high: 3, medium: 2, low: 1 }

export default function Encargos() {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const { confirm, dialog: confirmDialog } = useConfirm()
  const prov = useAutomationProvenance(undefined, 'local', {
    listPath: '/encargos',
    producersUrl: `${apiBase}/agent/tasks/producers`,
  })
  const taskQuery = useMemo(() => {
    const p = new URLSearchParams()
    if (prov.queryExtras.created_by_automation) {
      p.set('created_by_automation', prov.queryExtras.created_by_automation)
    }
    if (prov.queryExtras.only_open) p.set('only_open', '1')
    const qs = p.toString()
    return qs ? `?${qs}` : ''
  }, [prov.queryExtras.created_by_automation, prov.queryExtras.only_open])
  const [open, setOpen] = useState(false)
  const [expanded, setExpanded] = useState<number | null>(null)
  const [snack, setSnack] = useState<string | null>(null)
  const [orderBy, setOrderBy] = useState<SortKey>('created_at')
  const [order, setOrder] = useState<'asc' | 'desc'>('desc')
  const [search, setSearch] = useState('')
  const [statusFilter, setStatusFilter] = useState('all')
  const [priorityFilter, setPriorityFilter] = useState('all')
  // 'all' | 'immediate' (no due date → run now) | 'scheduled' (a future due date)
  const [typeFilter, setTypeFilter] = useState('all')
  const [page, setPage] = useState(0)
  const [rowsPerPage, setRowsPerPage] = useState(10)

  const handleSort = (key: SortKey) => {
    if (orderBy === key) {
      setOrder(order === 'asc' ? 'desc' : 'asc')
    } else {
      setOrderBy(key)
      // Dates and priority feel most useful newest/highest-first by default.
      setOrder(key === 'title' || key === 'status' ? 'asc' : 'desc')
    }
  }

  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['encargos', taskQuery],
    queryFn: () => apiClient.get<{ items: Task[]; filtered_by_automation?: { id: number; title: string; only_open?: boolean } | null }>(
      `/api/agent/tasks${taskQuery}`),
    refetchInterval: 8000,
  })

  const items = data?.items ?? []
  const provContext = data?.filtered_by_automation ?? null
  const sorted = [...items].sort((a, b) => {
    const dir = order === 'asc' ? 1 : -1
    let cmp = 0
    if (orderBy === 'priority') {
      cmp = (PRIORITY_RANK[a.priority] ?? 0) - (PRIORITY_RANK[b.priority] ?? 0)
    } else if (orderBy === 'due_at' || orderBy === 'created_at') {
      const av = a[orderBy] ? new Date(a[orderBy] as string).getTime() : 0
      const bv = b[orderBy] ? new Date(b[orderBy] as string).getTime() : 0
      cmp = av - bv
    } else {
      cmp = String(a[orderBy] ?? '').localeCompare(String(b[orderBy] ?? ''))
    }
    return cmp * dir
  })

  // Filters (status / priority / free text over title+description) + client
  // pagination over the sorted list. ``safePage`` keeps the page in range when
  // a filter shrinks the result set below the current page.
  const statuses = Array.from(new Set(items.map((i) => i.status))).sort()
  const q = search.trim().toLowerCase()
  const filtered = sorted.filter((it) => {
    if (statusFilter !== 'all' && it.status !== statusFilter) return false
    if (priorityFilter !== 'all' && it.priority !== priorityFilter) return false
    // Scheduled = has an execution date (due_at); immediate = no due_at
    // (tasks born from recommendations/decisions run right away).
    if (typeFilter === 'scheduled' && !it.due_at) return false
    if (typeFilter === 'immediate' && it.due_at) return false
    if (q && !`${it.title} ${it.description || ''}`.toLowerCase().includes(q)) return false
    return true
  })
  const safePage = Math.min(page, Math.max(0, Math.ceil(filtered.length / rowsPerPage) - 1))
  const paged = filtered.slice(safePage * rowsPerPage, safePage * rowsPerPage + rowsPerPage)

  const del = useMutation({
    mutationFn: (id: number) =>
      apiFetch(`/api/calendar/${id}`, { method: 'DELETE' }).then((r) => r.json()),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['encargos'] }),
  })

  const rerun = useMutation({
    mutationFn: (id: number) => apiClient.post(`/api/agent/tasks/${id}/retry`, {}),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['encargos'] })
      setSnack(t('encargos.rerunStarted'))
    },
  })

  return (
    <Box>
      <PageHeader
        icon={<AssignmentTurnedInIcon />}
        title={t('encargos.title')}
        subtitle={t('encargos.subtitle')}
        actions={
          <Button variant="contained" startIcon={<AddIcon />} onClick={() => setOpen(true)}
                  sx={{ textTransform: 'none' }}>
            {t('encargos.newTask')}
          </Button>
        }
      />

      <ProvenanceScopeAlert ctx={provContext} onClear={prov.clearProvenance} />

      {isLoading ? (
        <LoadingRegion />
      ) : isError ? (
        <EmptyState variant="error" title={t('common.error')}
                    actionLabel={t('common.retry')} onAction={() => refetch()} />
      ) : items.length === 0 ? (
        <EmptyState title={t('encargos.empty')} body={t('encargos.emptyBody')}
                    actionLabel={t('encargos.newTask')} onAction={() => setOpen(true)} />
      ) : (
        <>
          <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1.5} sx={{ mb: 2 }}>
            <TextField
              size="small"
              value={search}
              onChange={(e) => { setSearch(e.target.value); setPage(0) }}
              placeholder={t('encargos.filters.searchPh', 'Buscar por título o descripción…') as string}
              sx={{ flex: 1, minWidth: 200 }}
              InputProps={{ startAdornment: (
                <InputAdornment position="start"><SearchIcon fontSize="small" /></InputAdornment>
              ) }}
            />
            <TextField select size="small" label={t('encargos.filters.status', 'Estado')}
                       value={statusFilter} sx={{ minWidth: 150 }}
                       onChange={(e) => { setStatusFilter(e.target.value); setPage(0) }}>
              <MenuItem value="all">{t('encargos.filters.all', 'Todos')}</MenuItem>
              {statuses.map((s) => (
                <MenuItem key={s} value={s} sx={{ textTransform: 'capitalize' }}>{s}</MenuItem>
              ))}
            </TextField>
            <TextField select size="small" label={t('encargos.filters.priority', 'Prioridad')}
                       value={priorityFilter} sx={{ minWidth: 150 }}
                       onChange={(e) => { setPriorityFilter(e.target.value); setPage(0) }}>
              <MenuItem value="all">{t('encargos.filters.all', 'Todos')}</MenuItem>
              {PRIORITIES.map((p) => (
                <MenuItem key={p} value={p} sx={{ textTransform: 'capitalize' }}>{p}</MenuItem>
              ))}
            </TextField>
            <TextField select size="small" label={t('encargos.filters.type', 'Tipo')}
                       value={typeFilter} sx={{ minWidth: 150 }}
                       onChange={(e) => { setTypeFilter(e.target.value); setPage(0) }}>
              <MenuItem value="all">{t('encargos.filters.all', 'Todos')}</MenuItem>
              <MenuItem value="immediate">{t('encargos.filters.immediate', 'Inmediatos')}</MenuItem>
              <MenuItem value="scheduled">{t('encargos.filters.scheduled', 'Programados')}</MenuItem>
            </TextField>
            <AutomationProvenanceFilter
              value={prov.provAutomation}
              producers={prov.producers}
              onChange={prov.setProvenance}
            />
          </Stack>
          {filtered.length === 0 ? (
            <EmptyState title={t('encargos.noMatches', 'Ningún encargo coincide con los filtros.')} />
          ) : (
          <Card variant="outlined" sx={{ borderRadius: 2 }}>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell sx={{ width: 40 }} />
                {([
                  ['title', 'encargos.columns.title'],
                  ['status', 'encargos.columns.status'],
                  ['priority', 'encargos.columns.priority'],
                  ['due_at', 'encargos.columns.due'],
                  ['created_at', 'encargos.columns.created'],
                ] as [SortKey, string][]).map(([key, label]) => (
                  <TableCell key={key} sortDirection={orderBy === key ? order : false}>
                    <TableSortLabel
                      active={orderBy === key}
                      direction={orderBy === key ? order : 'asc'}
                      onClick={() => handleSort(key)}
                    >
                      {t(label)}
                    </TableSortLabel>
                  </TableCell>
                ))}
                <TableCell align="right" />
              </TableRow>
            </TableHead>
            <TableBody>
              {paged.map((it) => (
                <Fragment key={it.id}>
                  <TableRow hover sx={{ '& td': { borderBottom: expanded === it.id ? 0 : undefined } }}>
                    <TableCell>
                      <IconButton size="small"
                                  onClick={() => setExpanded(expanded === it.id ? null : it.id)}>
                        <ExpandMoreIcon
                          fontSize="small"
                          sx={{ transform: expanded === it.id ? 'rotate(180deg)' : 'none',
                                transition: '0.2s' }}
                        />
                      </IconButton>
                    </TableCell>
                    <TableCell sx={{ fontWeight: 600 }}>
                      <RouterLink to={`/encargos/${it.id}`} style={{ color: 'inherit', textDecoration: 'none' }}
                        onMouseOver={(e) => { (e.currentTarget as HTMLElement).style.textDecoration = 'underline' }}
                        onMouseOut={(e) => { (e.currentTarget as HTMLElement).style.textDecoration = 'none' }}>
                        {it.title}
                      </RouterLink>
                      <Box sx={{ mt: 0.5 }}>
                        <AutomationOriginChip origin={it.created_by_automation} />
                      </Box>
                    </TableCell>
                    <TableCell><StatusChip status={it.status} /></TableCell>
                    <TableCell><PriorityChip priority={it.priority} /></TableCell>
                    <TableCell>{it.due_at ? formatDateTime(it.due_at) : '—'}</TableCell>
                    <TableCell>{formatDateTime(it.created_at)}</TableCell>
                    <TableCell align="right">
                      <Stack direction="row" spacing={0.5} justifyContent="flex-end">
                        {it.last_run_id && (
                          <Tooltip title={t('encargos.run') as string}>
                            <IconButton size="small" component={RouterLink} to={`/runs/${it.last_run_id}`}>
                              <OpenInNewIcon fontSize="small" />
                            </IconButton>
                          </Tooltip>
                        )}
                        {['failed', 'cancelled', 'done'].includes(it.status) && (
                          <Tooltip title={t('encargos.rerun') as string}>
                            <IconButton size="small" color="primary"
                                        disabled={rerun.isPending}
                                        onClick={() => rerun.mutate(it.id)}>
                              <ReplayIcon fontSize="small" />
                            </IconButton>
                          </Tooltip>
                        )}
                        <Tooltip title={t('common.delete') as string}>
                          <IconButton size="small" color="error"
                                      onClick={() => confirm(
                                        { message: t('encargos.deleteConfirm'), danger: true, confirmLabel: t('common.delete') },
                                        () => del.mutate(it.id),
                                      )}>
                            <DeleteOutlineIcon fontSize="small" />
                          </IconButton>
                        </Tooltip>
                      </Stack>
                    </TableCell>
                  </TableRow>
                  <TableRow>
                    <TableCell colSpan={7} sx={{ py: 0, border: 0 }}>
                      <Collapse in={expanded === it.id} unmountOnExit>
                        <Box sx={{ p: 2, bgcolor: 'action.hover', borderRadius: 1, my: 1 }}>
                          {it.description && (
                            <Typography variant="body2" sx={{ mb: 1, whiteSpace: 'pre-wrap' }}>
                              {it.description}
                            </Typography>
                          )}
                          <Typography variant="caption" color="text.secondary" sx={{ fontWeight: 700 }}>
                            {t('encargos.result')}
                          </Typography>
                          <Typography variant="body2" sx={{ whiteSpace: 'pre-wrap', mt: 0.5 }}>
                            {it.result_md || t('encargos.noResult')}
                          </Typography>
                          {it.artifacts && it.artifacts.length > 0 && (
                            <Box sx={{ mt: 1.5 }}>
                              <Typography variant="caption" color="text.secondary"
                                          sx={{ fontWeight: 700, display: 'block', mb: 0.75 }}>
                                {t('artifacts.title')}
                              </Typography>
                              <ArtifactList artifacts={it.artifacts}
                                            onError={(m) => setSnack(m)} />
                            </Box>
                          )}
                        </Box>
                      </Collapse>
                    </TableCell>
                  </TableRow>
                </Fragment>
              ))}
            </TableBody>
          </Table>
          <TablePagination
            component="div"
            count={filtered.length}
            page={safePage}
            onPageChange={(_e, p) => setPage(p)}
            rowsPerPage={rowsPerPage}
            onRowsPerPageChange={(e) => { setRowsPerPage(parseInt(e.target.value, 10)); setPage(0) }}
            rowsPerPageOptions={[10, 25, 50]}
            labelRowsPerPage={t('encargos.pagination.perPage', 'Por página')}
          />
          </Card>
          )}
        </>
      )}

      <NewTaskDialog
        open={open}
        onClose={() => setOpen(false)}
        onCreated={(scheduled) => {
          setOpen(false)
          qc.invalidateQueries({ queryKey: ['encargos'] })
          setSnack(scheduled ? t('encargos.createdScheduled') : t('encargos.createdNow'))
        }}
      />

      <Snackbar open={!!snack} autoHideDuration={4000} onClose={() => setSnack(null)}
                anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}>
        <Alert severity="success" variant="filled" onClose={() => setSnack(null)}>{snack}</Alert>
      </Snackbar>

      {confirmDialog}
    </Box>
  )
}

function PriorityChip({ priority }: { priority: string }) {
  const color = priority === 'urgent' ? 'error'
    : priority === 'high' ? 'warning'
    : priority === 'low' ? 'default' : 'info'
  return <Chip size="small" color={color as any} variant="outlined" label={priority} />
}

function NewTaskDialog({ open, onClose, onCreated }: {
  open: boolean
  onClose: () => void
  onCreated: (scheduled: boolean) => void
}) {
  const { t } = useTranslation()
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [priority, setPriority] = useState('medium')
  const [when, setWhen] = useState<'now' | 'scheduled'>('now')
  const [due, setDue] = useState('')
  const [busy, setBusy] = useState(false)

  const reset = () => {
    setTitle(''); setDescription(''); setPriority('medium'); setWhen('now'); setDue('')
  }

  const submit = async () => {
    if (!title.trim()) return
    setBusy(true)
    try {
      const body: Record<string, unknown> = { title, description, priority }
      if (when === 'scheduled' && due) body.due_at = fromLocalInputValue(due)
      await apiClient.post('/api/agent/tasks', body)
      reset()
      onCreated(when === 'scheduled')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open={open} onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle>{t('encargos.newTask')}</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 1 }}>
          <TextField label={t('encargos.fields.title')} value={title} autoFocus required
                     placeholder={t('encargos.fields.titlePh') as string}
                     onChange={(e) => setTitle(e.target.value)} fullWidth />
          <TextField label={t('encargos.fields.description')} value={description}
                     multiline minRows={6} maxRows={18}
                     placeholder={t('encargos.fields.descriptionPh') as string}
                     helperText={t('encargos.fields.descriptionHelp')}
                     onChange={(e) => setDescription(e.target.value)} fullWidth />
          <TextField label={t('encargos.fields.priority')} value={priority} select
                     onChange={(e) => setPriority(e.target.value)} fullWidth>
            {PRIORITIES.map((p) => <MenuItem key={p} value={p}>{p}</MenuItem>)}
          </TextField>
          <Box>
            <Typography variant="caption" color="text.secondary">{t('encargos.fields.when')}</Typography>
            <ToggleButtonGroup exclusive size="small" value={when} fullWidth sx={{ mt: 0.5 }}
                               onChange={(_e, v) => v && setWhen(v)}>
              <ToggleButton value="now" sx={{ textTransform: 'none' }}>{t('encargos.fields.now')}</ToggleButton>
              <ToggleButton value="scheduled" sx={{ textTransform: 'none' }}>{t('encargos.fields.scheduled')}</ToggleButton>
            </ToggleButtonGroup>
          </Box>
          {when === 'scheduled' && (
            <TextField label={t('encargos.fields.dueAt')} type="datetime-local" value={due}
                       onChange={(e) => setDue(e.target.value)} fullWidth
                       InputLabelProps={{ shrink: true }} />
          )}
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} sx={{ textTransform: 'none' }}>{t('common.cancel')}</Button>
        <Button variant="contained" onClick={submit} disabled={busy || !title.trim()}
                sx={{ textTransform: 'none' }}>
          {busy ? t('common.saving') : t('encargos.submit')}
        </Button>
      </DialogActions>
    </Dialog>
  )
}
