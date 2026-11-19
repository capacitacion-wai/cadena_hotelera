/**
 * Calendar — the shared agenda between the operator and the AI employee.
 *
 * Custom month grid (no heavy external calendar dep) backed by the
 * unified ``scheduled_items`` table: the employee's own scheduled work
 * and follow-ups, plus tasks/reminders for people. Everything renders in
 * the BUSINESS timezone via utils/datetime.
 */
import { useMemo, useState } from 'react'
import {
  Alert, AlertTitle, Box, Button, Card, Chip, Collapse, Dialog, DialogActions,
  DialogContent, DialogTitle, Divider, IconButton, MenuItem, Snackbar, Stack,
  TextField, Tooltip, Typography, useTheme, alpha,
} from '@mui/material'
import CalendarMonthIcon from '@mui/icons-material/CalendarMonth'
import ChevronLeftIcon from '@mui/icons-material/ChevronLeft'
import ChevronRightIcon from '@mui/icons-material/ChevronRight'
import AddIcon from '@mui/icons-material/Add'
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline'
import HelpOutlineIcon from '@mui/icons-material/HelpOutline'
import TouchAppIcon from '@mui/icons-material/TouchApp'
import OpenInNewIcon from '@mui/icons-material/OpenInNew'
import ReplayIcon from '@mui/icons-material/Replay'
import PlayArrowIcon from '@mui/icons-material/PlayArrow'
import { useTranslation } from 'react-i18next'
import { Link as RouterLink } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import PageHeader from '../components/PageHeader'
import LoadingRegion from '../components/LoadingRegion'
import ArtifactList, { Artifact } from '../components/ArtifactList'
import { useConfirm } from '../components/ConfirmDialog'
import UserPicker from '../components/UserPicker'
import { apiClient, apiFetch } from '../services/apiClient'
import {
  formatTime, formatDateTime, fromLocalInputValue, toLocalInputValue, localeSeguro } from '../utils/datetime'

interface Item {
  id: number
  title: string
  description: string
  kind: string
  assignee: string
  origin: string
  status: string
  priority: string
  due_at: string | null
  start_at: string | null
  end_at: string | null
  all_day: boolean
  recurrence: string | null
  series_id: string | null
  assigned_to: string | null
  created_by: string | null
  result_md: string | null
  last_run_id: number | null
  completed_at: string | null
  artifacts?: Artifact[]
}

// A new entry is always a point-in-time item: a TASK (work to do) or a
// FOLLOW-UP (check back on something). Legacy 'event'/'reminder' rows
// still render; the dialog re-adds the current kind when editing one.
const KINDS = ['task', 'followup']
const ASSIGNEES = ['agent', 'user']
const PRIORITIES = ['low', 'medium', 'high', 'urgent']

function pad(n: number) { return String(n).padStart(2, '0') }
function dateKey(d: Date) { return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` }

/** Business-tz YYYY-MM-DD key for an ISO instant. */
function bizDateKey(iso: string | null): string | null {
  if (!iso) return null
  try {
    const tz = (window as unknown as { __waiTimezone?: string }).__waiTimezone
    const parts = new Intl.DateTimeFormat('en-CA', {
      timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit',
    }).formatToParts(new Date(iso))
    const get = (t: string) => parts.find((p) => p.type === t)?.value ?? '00'
    return `${get('year')}-${get('month')}-${get('day')}`
  } catch {
    return iso.slice(0, 10)
  }
}

function anchorIso(it: Item): string | null { return it.start_at || it.due_at }

export default function CalendarPage() {
  const { t, i18n } = useTranslation()
  const qc = useQueryClient()
  const theme = useTheme()
  const { confirm, dialog: confirmDialog } = useConfirm()
  const [cursor, setCursor] = useState(() => { const d = new Date(); return new Date(d.getFullYear(), d.getMonth(), 1) })
  const [editing, setEditing] = useState<Item | null>(null)
  const [creating, setCreating] = useState<string | null>(null) // prefilled date key
  const [dayDetail, setDayDetail] = useState<string | null>(null) // "see all N items of this day"
  const [snack, setSnack] = useState<string | null>(null)
  // Help is collapsed by default; the operator opens it on demand with
  // the "How to use it" button.
  const [showHelp, setShowHelp] = useState(false)
  const dismissHelp = () => setShowHelp(false)

  const year = cursor.getFullYear()
  const month = cursor.getMonth()

  // 6-week grid, Monday-first.
  const gridStart = useMemo(() => {
    const first = new Date(year, month, 1)
    const offset = (first.getDay() + 6) % 7
    return new Date(year, month, 1 - offset)
  }, [year, month])
  const days = useMemo(
    () => Array.from({ length: 42 }, (_v, i) =>
      new Date(gridStart.getFullYear(), gridStart.getMonth(), gridStart.getDate() + i)),
    [gridStart],
  )

  const from = `${dateKey(days[0])}T00:00`
  const to = `${dateKey(days[41])}T23:59`

  const { data, isLoading } = useQuery({
    queryKey: ['calendar', from, to],
    queryFn: () => apiClient.get<{ items: Item[] }>(`/api/calendar?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`),
    refetchInterval: 15000,
  })
  const items = data?.items ?? []

  const byDay = useMemo(() => {
    const map: Record<string, Item[]> = {}
    for (const it of items) {
      const k = bizDateKey(anchorIso(it))
      if (!k) continue
      ;(map[k] ||= []).push(it)
    }
    for (const k of Object.keys(map)) {
      map[k].sort((a, b) => (anchorIso(a) || '').localeCompare(anchorIso(b) || ''))
    }
    return map
  }, [items])

  const [delTarget, setDelTarget] = useState<Item | null>(null)
  const del = useMutation({
    mutationFn: ({ id, scope }: { id: number; scope: 'single' | 'series' }) =>
      apiFetch(`/api/calendar/${id}?scope=${scope}`, { method: 'DELETE' }).then((r) => r.json()),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['calendar'] })
      setEditing(null); setDelTarget(null); setSnack(t('calendar.deleted'))
    },
  })

  const rerun = useMutation({
    mutationFn: (id: number) => apiClient.post(`/api/calendar/${id}/run`, {}),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['calendar'] })
      setEditing(null)
      setSnack(t('calendar.rerunStarted'))
    },
  })

  const weekdays = useMemo(() => {
    const base = new Date(2024, 0, 1) // a Monday
    return Array.from({ length: 7 }, (_v, i) =>
      new Intl.DateTimeFormat(localeSeguro(i18n.language), { weekday: 'short' })
        .format(new Date(base.getFullYear(), base.getMonth(), base.getDate() + i)))
  }, [i18n.language])

  const todayKey = dateKey(new Date())

  const monthNames = useMemo(
    () => Array.from({ length: 12 }, (_v, i) =>
      new Intl.DateTimeFormat(localeSeguro(i18n.language), { month: 'long' }).format(new Date(2024, i, 1))),
    [i18n.language],
  )
  const yearOptions = useMemo(() => {
    const base = new Date().getFullYear()
    const set = new Set<number>()
    for (let y = base - 3; y <= base + 3; y++) set.add(y)
    set.add(year)
    return Array.from(set).sort((a, b) => a - b)
  }, [year])

  const itemColor = (it: Item): string => {
    if (it.origin === 'followup' || it.kind === 'followup') return theme.palette.secondary.main
    if (it.assignee === 'user') return theme.palette.warning.main
    return theme.palette.primary.main
  }

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', minHeight: 0, flex: 1 }}>
      <PageHeader
        icon={<CalendarMonthIcon />}
        title={t('calendar.title')}
        subtitle={t('calendar.subtitle')}
        actions={
          <Stack direction="row" spacing={1}>
            <Button variant="outlined" startIcon={<HelpOutlineIcon />} sx={{ textTransform: 'none' }}
                    onClick={() => setShowHelp((v) => !v)}>
              {t('calendar.help.button')}
            </Button>
            <Button variant="contained" startIcon={<AddIcon />} sx={{ textTransform: 'none' }}
                    onClick={() => setCreating(todayKey)}>
              {t('calendar.newItem')}
            </Button>
          </Stack>
        }
      />

      <Collapse in={showHelp}>
        <Alert severity="info" icon={<TouchAppIcon />} onClose={dismissHelp}
               sx={{ mb: 1.5, borderRadius: 2, '& .MuiAlert-message': { width: '100%' } }}>
          <AlertTitle sx={{ fontWeight: 700 }}>{t('calendar.help.title')}</AlertTitle>
          <Typography variant="body2" sx={{ mb: 1 }}>{t('calendar.help.intro')}</Typography>
          <Stack component="ul" spacing={0.5} sx={{ pl: 2.5, m: 0 }}>
            {['b1', 'b2', 'b3', 'b4', 'b5', 'b6'].map((b) => (
              <Typography key={b} component="li" variant="body2">
                {t(`calendar.help.${b}`)}
              </Typography>
            ))}
          </Stack>
          <Divider sx={{ my: 1.25 }} />
          <Stack direction="row" spacing={2} flexWrap="wrap" useFlexGap alignItems="center">
            <Typography variant="caption" sx={{ fontWeight: 700, color: 'text.secondary' }}>
              {t('calendar.help.colors')}
            </Typography>
            <Legend color={theme.palette.primary.main} label={t('calendar.legend.agent')} />
            <Legend color={theme.palette.secondary.main} label={t('calendar.legend.followup')} />
            <Legend color={theme.palette.warning.main} label={t('calendar.legend.user')} />
            <Legend color={theme.palette.error.main} label={t('calendar.legend.failed')} />
          </Stack>
        </Alert>
      </Collapse>

      <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mb: 1.5 }}>
        <Stack direction="row" alignItems="center" spacing={1}>
          <IconButton onClick={() => setCursor(new Date(year, month - 1, 1))}><ChevronLeftIcon /></IconButton>
          <Button size="small" variant="outlined" sx={{ textTransform: 'none' }}
                  onClick={() => { const d = new Date(); setCursor(new Date(d.getFullYear(), d.getMonth(), 1)) }}>
            {t('calendar.today')}
          </Button>
          <IconButton onClick={() => setCursor(new Date(year, month + 1, 1))}><ChevronRightIcon /></IconButton>
          <TextField select size="small" value={month} aria-label={t('calendar.month')}
                     onChange={(e) => setCursor(new Date(year, Number(e.target.value), 1))}
                     sx={{ ml: 1, minWidth: 140, '& .MuiSelect-select': { textTransform: 'capitalize', fontWeight: 700 } }}>
            {monthNames.map((m, i) => (
              <MenuItem key={i} value={i} sx={{ textTransform: 'capitalize' }}>{m}</MenuItem>
            ))}
          </TextField>
          <TextField select size="small" value={year} aria-label={t('calendar.year')}
                     onChange={(e) => setCursor(new Date(Number(e.target.value), month, 1))}
                     sx={{ minWidth: 96, '& .MuiSelect-select': { fontWeight: 700 } }}>
            {yearOptions.map((y) => <MenuItem key={y} value={y}>{y}</MenuItem>)}
          </TextField>
        </Stack>
        <Stack direction="row" spacing={2} alignItems="center" sx={{ display: { xs: 'none', md: 'flex' } }}>
          <Stack direction="row" spacing={0.5} alignItems="center" sx={{ color: 'text.secondary' }}>
            <TouchAppIcon sx={{ fontSize: 16 }} />
            <Typography variant="caption">{t('calendar.hint')}</Typography>
          </Stack>
          <Divider orientation="vertical" flexItem />
          <Legend color={theme.palette.primary.main} label={t('calendar.legend.agent')} />
          <Legend color={theme.palette.secondary.main} label={t('calendar.legend.followup')} />
          <Legend color={theme.palette.warning.main} label={t('calendar.legend.user')} />
        </Stack>
      </Stack>

      {isLoading ? <LoadingRegion /> : (
        <Card variant="outlined" sx={{ borderRadius: 2, overflow: 'hidden', flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
          <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(7, 1fr)', borderBottom: 1, borderColor: 'divider' }}>
            {weekdays.map((w) => (
              <Box key={w} sx={{ p: 1, textAlign: 'center', fontWeight: 700, fontSize: '0.75rem',
                                 color: 'text.secondary', textTransform: 'capitalize' }}>{w}</Box>
            ))}
          </Box>
          <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(7, 1fr)',
                     gridAutoRows: '1fr', flex: 1, minHeight: 0 }}>
            {days.map((d) => {
              const k = dateKey(d)
              const inMonth = d.getMonth() === month
              const isToday = k === todayKey
              const dayItems = byDay[k] || []
              return (
                <Box key={k}
                     onClick={() => setCreating(k)}
                     sx={{
                       borderRight: 1, borderBottom: 1, borderColor: 'divider', p: 0.5,
                       minHeight: 92, cursor: 'pointer', overflow: 'hidden',
                       bgcolor: inMonth ? 'background.paper' : alpha(theme.palette.action.hover, 0.4),
                       '&:hover': { bgcolor: alpha(theme.palette.primary.main, 0.04) },
                     }}>
                  <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    {/* Task-count badge (top-left, always visible so it can't be
                        clipped like the bottom "+N más"): click → see ALL the
                        day's tasks, however many there are. */}
                    {dayItems.length > 0 ? (
                      <Tooltip title={t('calendar.moreTooltip', 'Ver las {{count}} entradas de este día', { count: dayItems.length }) as string}>
                        <Box
                          onClick={(e) => { e.stopPropagation(); setDayDetail(k) }}
                          sx={{
                            display: 'flex', alignItems: 'center', justifyContent: 'center',
                            minWidth: 20, height: 20, px: 0.5, borderRadius: 1,
                            fontSize: '0.7rem', fontWeight: 700, cursor: 'pointer',
                            color: 'primary.main', bgcolor: alpha(theme.palette.primary.main, 0.12),
                            '&:hover': { bgcolor: alpha(theme.palette.primary.main, 0.24) },
                          }}>
                          {dayItems.length}
                        </Box>
                      </Tooltip>
                    ) : <Box />}
                    <Box sx={{
                      width: 24, height: 24, borderRadius: '50%', display: 'flex',
                      alignItems: 'center', justifyContent: 'center', fontSize: '0.78rem',
                      fontWeight: isToday ? 800 : 500,
                      color: isToday ? '#fff' : inMonth ? 'text.primary' : 'text.disabled',
                      bgcolor: isToday ? 'primary.main' : 'transparent',
                    }}>{d.getDate()}</Box>
                  </Box>
                  <Stack spacing={0.4} sx={{ mt: 0.3 }}>
                    {dayItems.slice(0, 3).map((it) => (
                      <Box key={it.id}
                           onClick={(e) => { e.stopPropagation(); setEditing(it) }}
                           sx={{
                             px: 0.6, py: 0.2, borderRadius: 0.75, fontSize: '0.68rem',
                             lineHeight: 1.3, color: '#fff', cursor: 'pointer',
                             bgcolor: it.status === 'failed' ? theme.palette.error.main : itemColor(it),
                             whiteSpace: 'nowrap', overflow: 'hidden',
                             textOverflow: 'ellipsis',
                             opacity: it.status === 'done' || it.status === 'cancelled' ? 0.5 : 1,
                             textDecoration: it.status === 'done' ? 'line-through' : 'none',
                           }}>
                        {it.status === 'failed' ? '⚠ ' : ''}{!it.all_day && anchorIso(it) ? `${formatTime(anchorIso(it))} · ` : ''}{it.title}
                      </Box>
                    ))}
                    {/* No inline "+N más" here: it sat at the bottom of a
                        fixed-height cell and got clipped. The always-visible
                        top-left count badge (above) shows the total and opens
                        the full-day list, so the overflow affordance lives there. */}
                  </Stack>
                </Box>
              )
            })}
          </Box>
        </Card>
      )}

      <ItemDialog
        key={editing ? `e${editing.id}` : creating ? `c${creating}` : 'none'}
        open={!!editing || !!creating}
        item={editing}
        prefillDate={creating}
        onClose={() => { setEditing(null); setCreating(null) }}
        onDelete={editing ? () => {
          const it = editing
          if (it.series_id) { setEditing(null); setDelTarget(it) }
          else confirm(
            { message: t('calendar.deleteConfirm'), danger: true, confirmLabel: t('common.delete') },
            () => del.mutate({ id: it.id, scope: 'single' }),
          )
        } : undefined}
        onRerun={editing && editing.assignee === 'agent'
          ? () => rerun.mutate(editing.id) : undefined}
        rerunBusy={rerun.isPending}
        onSaved={(updated) => {
          setEditing(null); setCreating(null)
          qc.invalidateQueries({ queryKey: ['calendar'] })
          setSnack(updated ? t('calendar.updated') : t('calendar.created'))
        }}
      />

      {/* "See all N items of this day" — the day cell only shows the first 3;
          this lists every scheduled entry for the day, each clickable to open
          its detail, plus a shortcut to add a new one on that date. */}
      {dayDetail && (
        <Dialog open onClose={() => setDayDetail(null)} maxWidth="xs" fullWidth>
          <DialogTitle sx={{ pb: 0.5, textTransform: 'capitalize' }}>
            {new Intl.DateTimeFormat(localeSeguro(i18n.language), { weekday: 'long', day: 'numeric', month: 'long' }).format(new Date(`${dayDetail}T00:00:00`))}
            <Typography variant="caption" color="text.secondary" sx={{ display: 'block', textTransform: 'none' }}>
              {t('calendar.dayCount', '{{count}} entradas programadas', { count: (byDay[dayDetail] || []).length })}
            </Typography>
          </DialogTitle>
          <DialogContent>
            <Stack spacing={0.75} sx={{ mt: 1 }}>
              {(byDay[dayDetail] || []).map((it) => (
                <Box key={it.id}
                     onClick={() => { setDayDetail(null); setEditing(it) }}
                     sx={{
                       display: 'flex', alignItems: 'center', gap: 1, p: 1, borderRadius: 1,
                       cursor: 'pointer', border: 1, borderColor: 'divider',
                       '&:hover': { bgcolor: alpha(theme.palette.primary.main, 0.05) },
                       opacity: it.status === 'done' || it.status === 'cancelled' ? 0.6 : 1,
                     }}>
                  <Box sx={{
                    width: 10, height: 10, borderRadius: '50%', flexShrink: 0,
                    bgcolor: it.status === 'failed' ? theme.palette.error.main : itemColor(it),
                  }} />
                  <Box sx={{ minWidth: 0, flex: 1 }}>
                    <Typography variant="body2" noWrap
                      sx={{ textDecoration: it.status === 'done' ? 'line-through' : 'none' }}>
                      {it.status === 'failed' ? '⚠ ' : ''}{it.title}
                    </Typography>
                    {!it.all_day && anchorIso(it) && (
                      <Typography variant="caption" color="text.secondary">{formatTime(anchorIso(it))}</Typography>
                    )}
                  </Box>
                </Box>
              ))}
            </Stack>
          </DialogContent>
          <DialogActions>
            <Button sx={{ textTransform: 'none' }}
                    onClick={() => { const d = dayDetail; setDayDetail(null); setCreating(d) }}>
              {t('calendar.newItem', 'Nueva entrada')}
            </Button>
            <Button variant="contained" sx={{ textTransform: 'none' }} onClick={() => setDayDetail(null)}>
              {t('common.close', 'Cerrar')}
            </Button>
          </DialogActions>
        </Dialog>
      )}

      <Dialog open={!!delTarget} onClose={() => setDelTarget(null)} maxWidth="xs" fullWidth>
        <DialogTitle sx={{ fontWeight: 700 }}>{t('calendar.deleteSeriesTitle')}</DialogTitle>
        <DialogContent>
          <Typography variant="body2" color="text.secondary">
            {t('calendar.deleteSeriesBody')}
          </Typography>
        </DialogContent>
        <DialogActions sx={{ flexWrap: 'wrap', gap: 1 }}>
          <Button onClick={() => setDelTarget(null)} sx={{ textTransform: 'none' }}>
            {t('common.cancel')}
          </Button>
          <Button color="error" disabled={del.isPending} sx={{ textTransform: 'none' }}
                  onClick={() => delTarget && del.mutate({ id: delTarget.id, scope: 'single' })}>
            {t('calendar.deleteOnlyThis')}
          </Button>
          <Button variant="contained" color="error" disabled={del.isPending} sx={{ textTransform: 'none' }}
                  onClick={() => delTarget && del.mutate({ id: delTarget.id, scope: 'series' })}>
            {t('calendar.deleteWholeSeries')}
          </Button>
        </DialogActions>
      </Dialog>

      <Snackbar open={!!snack} autoHideDuration={3500} onClose={() => setSnack(null)}
                anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}>
        <Alert severity="success" variant="filled" onClose={() => setSnack(null)}>{snack}</Alert>
      </Snackbar>

      {confirmDialog}
    </Box>
  )
}

function Legend({ color, label }: { color: string; label: string }) {
  return (
    <Stack direction="row" spacing={0.75} alignItems="center">
      <Box sx={{ width: 12, height: 12, borderRadius: 0.5, bgcolor: color }} />
      <Typography variant="caption" color="text.secondary">{label}</Typography>
    </Stack>
  )
}

function ItemDialog({ open, item, prefillDate, onClose, onSaved, onDelete, onRerun, rerunBusy }: {
  open: boolean
  item: Item | null
  prefillDate: string | null
  onClose: () => void
  onSaved: (updated: boolean) => void
  onDelete?: () => void
  onRerun?: () => void
  rerunBusy?: boolean
}) {
  const { t } = useTranslation()
  const [title, setTitle] = useState(item?.title ?? '')
  const [description, setDescription] = useState(item?.description ?? '')
  const [kind, setKind] = useState(item?.kind ?? 'task')
  // Default to the AI employee (the common case: you delegate work to it). Pick
  // "user" only to put a reminder on a person's calendar.
  const [assignee, setAssignee] = useState(item?.assignee ?? 'agent')
  const [assignedTo, setAssignedTo] = useState(item?.assigned_to ?? '')
  const [priority, setPriority] = useState(item?.priority ?? 'medium')
  const [allDay] = useState(item?.all_day ?? false)
  const [start, setStart] = useState(
    item ? toLocalInputValue(item.start_at || item.due_at)
         : (prefillDate ? `${prefillDate}T09:00` : ''))
  const [repeat, setRepeat] = useState<'none' | 'weekly'>('none')
  const [busy, setBusy] = useState(false)
  // Editing a legacy 'event'/'reminder' row: keep its kind selectable so
  // the dropdown isn't left on an out-of-range value.
  const kindOptions = item && !KINDS.includes(item.kind)
    ? [item.kind, ...KINDS] : KINDS

  const submit = async () => {
    if (!title.trim()) return
    setBusy(true)
    try {
      const body: Record<string, unknown> = {
        title, description, kind, assignee, priority, all_day: allDay,
        assigned_to: assignee === 'user' ? assignedTo : null,
      }
      // Tasks and follow-ups are single point-in-time items, so the one
      // datetime the operator picks is always the due/anchor time — for
      // both the employee (the watcher fires it) and a person (it can go
      // overdue). No more start/end block to reason about.
      const startIso = start ? fromLocalInputValue(start) : null
      body.due_at = startIso
      body.start_at = null
      body.end_at = null
      if (item) {
        const r = await apiFetch(`/api/calendar/${item.id}`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        })
        if (!r.ok) throw new Error(await r.text())
        onSaved(true)
      } else {
        if (repeat === 'weekly') body.repeat = 'weekly'
        await apiClient.post('/api/calendar', body)
        onSaved(false)
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open={open} onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle>{item ? t('calendar.detailTitle') : t('calendar.newItem')}</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 1 }}>
          {item && (
            <Stack direction="row" spacing={1}>
              <Chip size="small" label={t(`calendar.kinds.${item.kind}`, { defaultValue: item.kind })} />
              <Chip size="small" variant="outlined"
                    label={t(`status.${item.status}`, { defaultValue: item.status })} />
              {item.series_id && (
                <Chip size="small" color="info" variant="outlined"
                      label={t('calendar.seriesChip')} />
              )}
              {item.created_by && (
                <Chip size="small" variant="outlined" label={item.created_by} />
              )}
            </Stack>
          )}
          <TextField label={t('calendar.fields.title')} value={title} autoFocus required
                     placeholder={t('calendar.fields.titlePh') as string}
                     onChange={(e) => setTitle(e.target.value)} fullWidth />
          <TextField label={t('calendar.fields.description')} value={description}
                     multiline minRows={5} maxRows={16}
                     placeholder={t('calendar.fields.descriptionPh') as string}
                     onChange={(e) => setDescription(e.target.value)} fullWidth />
          <Stack direction="row" spacing={2}>
            <TextField label={t('calendar.fields.kind')} value={kind} select fullWidth
                       helperText={t('calendar.fields.kindHelp')}
                       onChange={(e) => setKind(e.target.value)}>
              {kindOptions.map((k) => (
                <MenuItem key={k} value={k}>
                  {t(`calendar.kinds.${k}`, { defaultValue: k })}
                </MenuItem>
              ))}
            </TextField>
            <TextField label={t('calendar.fields.assignee')} value={assignee} select fullWidth
                       onChange={(e) => setAssignee(e.target.value)}>
              {ASSIGNEES.map((a) => <MenuItem key={a} value={a}>{t(`calendar.assignees.${a}`)}</MenuItem>)}
            </TextField>
          </Stack>
          {assignee === 'user' && (
            <UserPicker kind="email" size="medium"
                        label={t('calendar.fields.assignedTo')}
                        placeholder={t('calendar.fields.assignedToPh') as string}
                        helperText={t('calendar.fields.assignedToHelp', 'Personas del directorio (Usuarios)')}
                        value={assignedTo || null}
                        onChange={(v) => setAssignedTo(v ?? '')} />
          )}
          <TextField label={t('calendar.fields.datetime')} type="datetime-local" value={start}
                     onChange={(e) => setStart(e.target.value)} fullWidth
                     InputLabelProps={{ shrink: true }}
                     helperText={t('calendar.fields.datetimeHelp')} />
          <TextField label={t('calendar.fields.priority')} value={priority} select fullWidth
                     onChange={(e) => setPriority(e.target.value)}>
            {PRIORITIES.map((p) => <MenuItem key={p} value={p}>{p}</MenuItem>)}
          </TextField>
          {!item && (
            <TextField label={t('calendar.fields.repeat')} value={repeat} select fullWidth
                       helperText={repeat === 'weekly' ? t('calendar.fields.repeatHelp') : undefined}
                       onChange={(e) => setRepeat(e.target.value as 'none' | 'weekly')}>
              <MenuItem value="none">{t('calendar.repeat.none')}</MenuItem>
              <MenuItem value="weekly">{t('calendar.repeat.weekly')}</MenuItem>
            </TextField>
          )}
          {item && (item.start_at || item.due_at) && (
            <Typography variant="caption" color="text.secondary">
              {formatDateTime(item.start_at || item.due_at)}
            </Typography>
          )}
          {item && item.assignee === 'agent'
            && (item.last_run_id || item.result_md || item.status === 'failed') && (
            <Box sx={{ p: 1.5, borderRadius: 1, bgcolor: 'action.hover',
                       border: 1, borderColor: item.status === 'failed' ? 'error.light' : 'divider' }}>
              <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mb: 0.5 }}>
                <Typography variant="caption" sx={{ fontWeight: 700, color: 'text.secondary' }}>
                  {t('calendar.trace.title')}
                </Typography>
                {item.last_run_id && (
                  <Button size="small" startIcon={<OpenInNewIcon fontSize="small" />}
                          component={RouterLink} to={`/runs/${item.last_run_id}`}
                          sx={{ textTransform: 'none' }}>
                    {t('calendar.trace.run')}
                  </Button>
                )}
              </Stack>
              <Typography variant="body2" sx={{ whiteSpace: 'pre-wrap' }}>
                {item.result_md || t('calendar.trace.noResult')}
              </Typography>
              {item.artifacts && item.artifacts.length > 0 && (
                <Box sx={{ mt: 1.5 }}>
                  <Typography variant="caption" sx={{ fontWeight: 700, color: 'text.secondary',
                              display: 'block', mb: 0.75 }}>
                    {t('artifacts.title')}
                  </Typography>
                  <ArtifactList artifacts={item.artifacts} />
                </Box>
              )}
            </Box>
          )}
        </Stack>
      </DialogContent>
      <DialogActions sx={{ justifyContent: 'space-between' }}>
        <Box>
          {onDelete && (
            <Tooltip title={t('common.delete') as string}>
              <IconButton color="error" onClick={onDelete}><DeleteOutlineIcon /></IconButton>
            </Tooltip>
          )}
          {onRerun && (
            <Tooltip title={t(item?.last_run_id ? 'calendar.rerun' : 'calendar.runNow') as string}>
              <span>
                <IconButton color="primary" disabled={rerunBusy} onClick={onRerun}>
                  {item?.last_run_id ? <ReplayIcon /> : <PlayArrowIcon />}
                </IconButton>
              </span>
            </Tooltip>
          )}
        </Box>
        <Box>
          <Button onClick={onClose} sx={{ textTransform: 'none' }}>{t('common.cancel')}</Button>
          <Button variant="contained" onClick={submit} disabled={busy || !title.trim()}
                  sx={{ textTransform: 'none' }}>
            {busy ? t('common.saving') : t('common.save')}
          </Button>
        </Box>
      </DialogActions>
    </Dialog>
  )
}
