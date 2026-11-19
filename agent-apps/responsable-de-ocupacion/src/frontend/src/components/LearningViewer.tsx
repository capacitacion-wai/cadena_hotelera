// Operaciones → Aprendizaje: what the employee learned about ITS ENVIRONMENT,
// and the full trail from a learning to the rules it produced (v7.140.0).
//
// Sibling of MemoryViewer, and the split is the point: that one is keyed by
// PERSON ("Ana prefers PDF"), this one by SUBJECT ("job:indexing takes 30-45 min
// and that is normal here"). Mixing them would have made "forget everything
// about Ana" also erase what the employee knows about the cluster.
//
// Three questions, three sections — deliberately in this order:
//   1. Lo que ha aprendido      → what it believes, with EVIDENCE (how many times
//                                 it observed it) and whether it is in its head
//                                 right now ("en su contexto").
//   2. Te está pidiendo permiso → proposals. A learning NEVER changes behaviour on
//                                 its own; turning one into a rule is an explicit,
//                                 admin-only act.
//   3. Qué ha cambiado en su comportamiento → the effects, each with one-click
//                                 revert. This is the answer to "why is this rule
//                                 here?" six months later, and the reason the
//                                 rules homes don't silently fill up.
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Accordion, AccordionDetails, AccordionSummary, Alert, AlertTitle, Box, Button, Checkbox,
  Chip, CircularProgress, Dialog, DialogActions, DialogContent, DialogTitle,
  Divider, IconButton, InputAdornment, LinearProgress, MenuItem, Paper, Stack,
  Switch, TextField, ToggleButton, ToggleButtonGroup, Tooltip, Typography,
  useMediaQuery, useTheme,
} from '@mui/material'
import {
  Add as AddIcon, AutoAwesome as LearnedIcon, Check as CheckIcon,
  Close as CloseIcon, DeleteOutline as DeleteIcon, EditOutlined as EditIcon,
  ExpandMore as ExpandMoreIcon, HelpOutline as HelpIcon,
  HistoryToggleOff as EpisodeIcon, Insights as BaselineIcon,
  LightbulbOutlined as LessonIcon, PushPin as PinIcon,
  PushPinOutlined as PinOutlineIcon, Refresh as RefreshIcon,
  Rule as RuleIcon, Science as ObservationIcon, Undo as UndoIcon,
  VisibilityOutlined as ViewBlockIcon, AccountTreeOutlined as ProcedureIcon,
} from '@mui/icons-material'
import { apiFetch } from '../services/apiClient'
import { formatDateTime } from '../utils/datetime'

type Entry = {
  id: number
  kind: string
  subject: string
  content: string
  tags: string | null
  observations: number
  first_seen_at: string | null
  last_confirmed_at: string | null
  valid_until: string | null
  expired: boolean
  confidence: number | null
  importance: number | null
  pinned: boolean
  review_state: string
  /** v7.154.0 — trust DERIVED from the evidence (times seen, used, confirmed).
   *  Replaces «sin validar» (unvalidated) as the reliability signal: asking for a
   *  signature on 107 entries was work nobody did, so the mark ended up on all of
   *  them and stopped meaning anything. The human signature is kept for where it decides something. */
  trust?: 'alta' | 'media' | 'baja'
  access_count?: number
  reviewed_by: string | null
  behavior_effect: string | null
  repropose_blocked: boolean
  in_context: boolean
  superseded_by: number | null
  source: string
  created_by_run_id: number | null
  created_at: string | null
  updated_at: string | null
}
type Episode = {
  id: number; subject: string; summary: string; outcome: string | null
  source: string; created_by_run_id: number | null; created_at: string | null
}
type Subject = { subject: string; entries: number; observations: number; last_update: string | null }
type Attention = {
  total?: number
  contradictions?: { subject: string; older_id: number; newer_id: number; older: string; newer: string }[]
  thin_but_important?: { id: number; subject: string; content: string; importance: number }[]
  /** Contradictions the EVIDENCE already resolved (a newer, better-backed version
   *  dominates the other). They do not count toward the total: they are not your
   *  work, they are shown so you can see they were resolved and why. */
  resolvable?: { subject: string; keep_id: number; drop_id: number; keep: string; drop: string }[]
}
type Stats = {
  enabled?: boolean
  entries_live?: number; entries_total?: number; observations?: number
  baselines?: number; lessons?: number; expired?: number
  retired?: number; merged?: number
  episodes_total?: number; subjects?: number
  block_chars?: number; block_tokens?: number; injected?: number
  pending_review?: number
  low_trust?: number
  // v7.143.0 — the last round's decision about learning: tells "there was
  // nothing" apart from "it was not asked to" when the list is empty.
  last_verdict?: {
    run_id: number; at: string; learnings: number; reinforced: number
    nothing_new: string; absent: boolean
  } | null
}
type Proposal = {
  effect_id: number
  target: string
  label: string
  detail?: string | null
  proposed_at: string | null
  learning_id: number | null
  learning_subject?: string | null
  learning_content?: string | null
  origin_run_id?: number | null
}
type Effect = {
  id: number
  target: string
  target_id: string
  label: string
  state: string
  proposed_at: string | null
  approved_by: string | null
  approved_at: string | null
  approved_where: string | null
  reverted_by: string | null
  reverted_at: string | null
  revert_reason: string | null
  learning_id: number | null
  learning_subject?: string | null
  learning_content?: string | null
}

const JSON_HEADERS = { 'Content-Type': 'application/json' }
const PAGE = 25

function qs(params: Record<string, unknown>): string {
  const u = new URLSearchParams()
  Object.entries(params).forEach(([k, v]) => {
    if (v === undefined || v === null || v === '') return
    u.set(k, String(v))
  })
  return u.toString()
}

function useDebounced<T>(value: T, ms = 350): T {
  const [v, setV] = useState(value)
  useEffect(() => {
    const id = setTimeout(() => setV(value), ms)
    return () => clearTimeout(id)
  }, [value, ms])
  return v
}

const kindIcon = (kind: string) => {
  if (kind === 'baseline') return <BaselineIcon fontSize="small" color="success" />
  if (kind === 'lesson') return <LessonIcon fontSize="small" color="warning" />
  if (kind === 'procedure') return <ProcedureIcon fontSize="small" color="info" />
  if (kind === 'entity') return <LearnedIcon fontSize="small" color="secondary" />
  return <ObservationIcon fontSize="small" color="action" />
}

type Editor = {
  open: boolean; id?: number; subject: string; content: string
  kind: string; validDays: string; pinned: boolean
}
const CLOSED: Editor = {
  open: false, subject: '', content: '', kind: 'baseline', validDays: '', pinned: true,
}

export default function LearningViewer({ focusLearningId }: { focusLearningId?: number | null }) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const theme = useTheme()
  const fullScreen = useMediaQuery(theme.breakpoints.down('sm'))

  const [search, setSearch] = useState('')
  const debouncedSearch = useDebounced(search)
  const [kindFilter, setKindFilter] = useState('')
  const [reviewFilter, setReviewFilter] = useState('')
  const [subjectFilter, setSubjectFilter] = useState('')
  const [sort, setSort] = useState('evidence')
  const [showSuperseded, setShowSuperseded] = useState(false)
  const [limit, setLimit] = useState(PAGE)
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [editor, setEditor] = useState<Editor>(CLOSED)
  const [blockOpen, setBlockOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  // 'info' (7.164.0): "one was already queued" is neither a success nor an error —
  // painting it green would suggest something was launched that was not.
  const [feedback, setFeedback] = useState<{ kind: 'success' | 'error' | 'info'; msg: string } | null>(null)
  const [confirmState, setConfirmState] = useState<
    { msg: string; danger?: boolean; onConfirm: () => void } | null
  >(null)
  // Which learning to highlight — arrives from a "Aprendido" chip in another
  // surface (a silence, a parameter, an automation). Cleared once seen so it
  // doesn't stick on the next visit.
  const [focused, setFocused] = useState<number | null>(focusLearningId ?? null)
  useEffect(() => { setFocused(focusLearningId ?? null) }, [focusLearningId])

  const flash = (kind: 'success' | 'error' | 'info', msg: string) => {
    setFeedback({ kind, msg })
    setTimeout(() => setFeedback(null), 5000)
  }
  const askConfirm = (msg: string, onConfirm: () => void, danger = false) =>
    setConfirmState({ msg, danger, onConfirm })

  async function call(url: string, init?: RequestInit) {
    const res = await apiFetch(url, init)
    if (!res.ok) {
      let detail = `HTTP ${res.status}`
      try {
        const body = await res.json()
        if (body?.detail) detail = String(body.detail)
      } catch { /* keep the status */ }
      throw new Error(detail)
    }
    return res.json().catch(() => ({}))
  }

  const refreshAll = () => {
    // 7.166.0 — `self-attention` was MISSING here. Effect: you confirmed or discarded a
    // card from the queue and the card stayed on screen, because the only list that was
    // not re-fetched was exactly the one you had just touched. It looked like the button
    // did nothing; the change had in fact been saved.
    for (const k of ['self-entries', 'self-stats', 'self-subjects', 'self-block',
                     'self-attention', 'self-proposals', 'self-effects', 'self-episodes']) {
      qc.invalidateQueries({ queryKey: [k] })
    }
    // Rules that may have changed home: the badges live in those surfaces.
    for (const k of ['suppressions', 'knowledge-requests', 'automations']) {
      qc.invalidateQueries({ queryKey: [k] })
    }
  }

  const statsQuery = useQuery({
    queryKey: ['self-stats'],
    queryFn: () => call('/api/agent/self-memory/stats') as Promise<Stats>,
    refetchInterval: 60_000, retry: false,
  })
  // v7.156.0 — THE SHORT QUEUE. The panel used to invite you to "validate" all 107
  // entries, which is work nobody does. Only what truly needs human judgement shows up
  // here: contradictions the machine cannot resolve and important things with a single
  // observation. If it comes out empty, there is nothing to decide — and that is said too.
  const attentionQuery = useQuery({
    queryKey: ['self-attention'],
    queryFn: () => call('/api/agent/self-memory/attention') as Promise<Attention>,
    refetchInterval: 60_000, retry: false,
  })
  const subjectsQuery = useQuery({
    queryKey: ['self-subjects'],
    queryFn: () => call('/api/agent/self-memory/subjects') as Promise<{ items: Subject[] }>,
    retry: false,
  })
  const entriesQuery = useQuery({
    queryKey: ['self-entries', debouncedSearch, kindFilter, reviewFilter, subjectFilter, sort, showSuperseded, limit],
    queryFn: () => call(`/api/agent/self-memory/entries?${qs({
      q: debouncedSearch, kind: kindFilter, review_state: reviewFilter,
      subject: subjectFilter, sort, include_superseded: showSuperseded,
      limit, offset: 0,
    })}`) as Promise<{ total: number; items: Entry[] }>,
    retry: false,
  })
  const episodesQuery = useQuery({
    queryKey: ['self-episodes', subjectFilter],
    queryFn: () => call(`/api/agent/self-memory/episodes?${qs({ subject: subjectFilter, limit: 20 })}`) as Promise<{ total: number; items: Episode[] }>,
    retry: false,
  })
  const proposalsQuery = useQuery({
    queryKey: ['self-proposals'],
    queryFn: () => call('/api/agent/self-memory/proposals') as Promise<{ items: Proposal[]; total: number }>,
    refetchInterval: 30_000, retry: false,
  })
  const effectsQuery = useQuery({
    queryKey: ['self-effects'],
    queryFn: () => call('/api/agent/self-memory/effects?limit=100') as Promise<{ items: Effect[]; total: number }>,
    retry: false,
  })
  const blockQuery = useQuery({
    queryKey: ['self-block'],
    enabled: blockOpen,
    queryFn: () => call('/api/agent/self-memory/block') as Promise<{ text: string; empty: boolean; tokens: number }>,
    retry: false,
  })

  const stats = statsQuery.data
  const entries = entriesQuery.data?.items ?? []
  const total = entriesQuery.data?.total ?? 0
  const subjects = subjectsQuery.data?.items ?? []
  const proposals = proposalsQuery.data?.items ?? []
  const effects = effectsQuery.data?.items ?? []
  const episodes = episodesQuery.data?.items ?? []
  const fmt = (iso: string | null | undefined) => formatDateTime(iso ?? null)

  const hasFilters = !!(debouncedSearch || kindFilter || reviewFilter || subjectFilter
                        || showSuperseded || sort !== 'evidence')
  const clearFilters = () => {
    setSearch(''); setKindFilter(''); setReviewFilter(''); setSubjectFilter('')
    setShowSuperseded(false); setSort('evidence')
  }
  useEffect(() => { setLimit(PAGE); setSelected(new Set()) },
    [debouncedSearch, kindFilter, reviewFilter, subjectFilter, sort, showSuperseded])

  // ── Mutations ───────────────────────────────────────────────────────
  /** A single review call, by id. Used by the list and also by the
   *  «Necesita tu criterio» (Needs your judgement) queue, which until 7.161.0 ASKED for
   *  a decision and gave no way to make it: the notice said "confirm the true one and
   *  discard the other" and there was not a single button. Asking without offering the gesture is not asking. */
  const reviewById = (id: number, state: 'validated' | 'rejected') =>
    call(`/api/agent/self-memory/entries/${id}/review`, {
      method: 'POST', headers: JSON_HEADERS,
      body: JSON.stringify({ review_state: state }),
    })

  /** Resolving a contradiction is ONE gesture, not two: the valid one is protected and
   *  the other discarded at the same time. Left half-done it would show up in the queue again. */
  const resolveContradiction = async (keepId: number, dropId: number) => {
    try {
      await reviewById(keepId, 'validated')
      await reviewById(dropId, 'rejected')
      flash('success', t('learning.attention.resolved',
        'Resuelto: la que confirmaste queda protegida y la otra descartada.'))
      refreshAll()
    } catch (err: any) { flash('error', err?.message || t('learning.error', 'No se pudo completar')) }
  }

  const [tidying, setTidying] = useState(false)
  const tidyNow = async () => {
    setTidying(true)
    try {
      const r = await call('/api/agent/self-memory/consolidate', { method: 'POST' }) as { queued?: boolean }
      flash(r?.queued ? 'success' : 'info', r?.queued
        ? t('learning.tidyQueued', 'Se lo he pedido: en su próxima vuelta fusionará lo repetido y destilará lo que se sostenga.')
        : t('learning.tidyAlready', 'Ya tenía una puesta en orden pendiente de esta hora — no se pide dos veces para no cobrarla dos veces.'))
      refreshAll()
    } catch (err: any) { flash('error', err?.message || t('learning.error', 'No se pudo completar')) }
    finally { setTidying(false) }
  }

  const review = async (e: Entry, state: 'validated' | 'rejected') => {
    try {
      await reviewById(e.id, state)
      flash('success', state === 'validated'
        ? t('learning.validated', 'Aprendizaje validado — el empleado puede apoyarse en él.')
        : t('learning.rejected', 'Aprendizaje descartado.'))
      refreshAll()
    } catch (err: any) { flash('error', err?.message || t('learning.error', 'No se pudo completar')) }
  }

  const togglePin = async (e: Entry) => {
    try {
      await call(`/api/agent/self-memory/entries/${e.id}`, {
        method: 'PATCH', headers: JSON_HEADERS,
        body: JSON.stringify({ pinned: !e.pinned }),
      })
      refreshAll()
    } catch (err: any) { flash('error', err?.message || t('learning.error', 'No se pudo completar')) }
  }

  const remove = (e: Entry) => askConfirm(
    e.behavior_effect
      ? t('learning.confirmDeleteWithRule', 'Este aprendizaje sostiene una regla activa. Revierte primero el efecto en «Qué ha cambiado en su comportamiento».')
      : t('learning.confirmDelete', '¿Borrar este aprendizaje? El empleado dejará de saberlo.'),
    async () => {
      try {
        await call(`/api/agent/self-memory/entries/${e.id}`, { method: 'DELETE' })
        flash('success', t('learning.deleted', 'Aprendizaje borrado.')); refreshAll()
      } catch (err: any) { flash('error', err?.message || t('learning.error', 'No se pudo completar')) }
    }, true)

  const bulk = async (action: 'validated' | 'rejected' | 'delete') => {
    const ids = Array.from(selected)
    if (ids.length === 0) return
    const run = async () => {
      setBusy(true)
      try {
        const r = await call('/api/agent/self-memory/entries/bulk', {
          method: 'POST', headers: JSON_HEADERS,
          body: JSON.stringify({ ids, action }),
        })
        if (r?.skipped_with_rule) {
          flash('success', t('learning.bulkSkipped', {
            defaultValue: '{{done}} procesados. {{skipped}} no se borraron: sostienen una regla activa.',
            done: r.deleted ?? 0, skipped: r.skipped_with_rule,
          }))
        } else {
          flash('success', t('learning.bulkDone', { defaultValue: '{{count}} aprendizajes actualizados.', count: ids.length }))
        }
        setSelected(new Set()); refreshAll()
      } catch (err: any) { flash('error', err?.message || t('learning.error', 'No se pudo completar')) }
      finally { setBusy(false) }
    }
    if (action === 'delete') {
      askConfirm(t('learning.confirmBulkDelete', { defaultValue: '¿Borrar {{count}} aprendizajes?', count: ids.length }), run, true)
    } else run()
  }

  const save = async () => {
    const subject = editor.subject.trim()
    const content = editor.content.trim()
    if (!subject || !content) return
    setBusy(true)
    try {
      if (editor.id) {
        await call(`/api/agent/self-memory/entries/${editor.id}`, {
          method: 'PATCH', headers: JSON_HEADERS,
          body: JSON.stringify({
            content, kind: editor.kind, pinned: editor.pinned,
            valid_until: editor.validDays ? Number(editor.validDays) : null,
          }),
        })
      } else {
        await call('/api/agent/self-memory/entries', {
          method: 'POST', headers: JSON_HEADERS,
          body: JSON.stringify({
            subject, content, kind: editor.kind, pinned: editor.pinned,
            valid_until: editor.validDays ? Number(editor.validDays) : null,
          }),
        })
      }
      flash('success', t('learning.saved', 'Guardado.'))
      setEditor(CLOSED); refreshAll()
    } catch (err: any) { flash('error', err?.message || t('learning.error', 'No se pudo completar')) }
    finally { setBusy(false) }
  }

  const acceptProposal = (p: Proposal) => askConfirm(
    t('learning.confirmAccept', {
      defaultValue: 'Vas a poner en marcha «{{label}}». A partir de ahora afectará al comportamiento del empleado; podrás revertirlo en un clic.',
      label: p.label,
    }),
    async () => {
      setBusy(true)
      try {
        await call(`/api/agent/self-memory/proposals/${p.effect_id}/accept`, {
          method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ where: 'queue' }),
        })
        flash('success', t('learning.accepted', 'Regla activada.')); refreshAll()
      } catch (err: any) { flash('error', err?.message || t('learning.error', 'No se pudo completar')) }
      finally { setBusy(false) }
    })

  const rejectProposal = (p: Proposal) => askConfirm(
    t('learning.confirmReject', 'Se descarta la propuesta y el empleado no volverá a pedir lo mismo.'),
    async () => {
      setBusy(true)
      try {
        await call(`/api/agent/self-memory/proposals/${p.effect_id}/reject`, {
          method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({}),
        })
        flash('success', t('learning.proposalRejected', 'Propuesta descartada.')); refreshAll()
      } catch (err: any) { flash('error', err?.message || t('learning.error', 'No se pudo completar')) }
      finally { setBusy(false) }
    }, true)

  const revert = (e: Effect) => askConfirm(
    t('learning.confirmRevert', {
      defaultValue: '¿Deshacer «{{label}}»? El empleado seguirá sabiendo lo que aprendió, pero dejará de aplicarlo.',
      label: e.label,
    }),
    async () => {
      setBusy(true)
      try {
        await call(`/api/agent/self-memory/effects/${e.id}/revert`, {
          method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({}),
        })
        flash('success', t('learning.reverted', 'Efecto revertido.')); refreshAll()
      } catch (err: any) { flash('error', err?.message || t('learning.error', 'No se pudo completar')) }
      finally { setBusy(false) }
    }, true)

  const toggleSelect = (id: number) => setSelected((prev) => {
    const n = new Set(prev)
    if (n.has(id)) n.delete(id); else n.add(id)
    return n
  })

  const targetLabel = (target: string) => ({
    suppression: t('learning.target.suppression', 'Silencio'),
    parameter: t('learning.target.parameter', 'Dato de negocio'),
    instruction: t('learning.target.instruction', 'Instrucción'),
    automation: t('learning.target.automation', 'Automatización'),
  }[target] || target)

  // ── Sub-renders ─────────────────────────────────────────────────────
  const help = (
    <Accordion variant="outlined" disableGutters sx={{ borderRadius: 2, '&:before': { display: 'none' } }}>
      <AccordionSummary expandIcon={<ExpandMoreIcon />}>
        <Stack direction="row" spacing={1} alignItems="center">
          <HelpIcon fontSize="small" color="action" />
          <Typography variant="body2">{t('learning.help.title', '¿Qué es esto?')}</Typography>
        </Stack>
      </AccordionSummary>
      <AccordionDetails>
        <Stack spacing={1}>
          <Typography variant="body2" color="text.secondary">
            {t('learning.help.intro', 'Aquí ves lo que el empleado ha aprendido del ENTORNO trabajando: qué es normal, qué le enseñó un fallo, cómo revisar algo. Es distinto de la memoria de personas (eso va en Memoria).')}
          </Typography>
          <Stack direction="row" spacing={1} alignItems="flex-start">
            <ObservationIcon fontSize="small" color="action" />
            <Typography variant="body2">
              <strong>{t('learning.kind.observation', 'Observación')}</strong> — {t('learning.help.observation', 'un dato de un día concreto. Si lo vuelve a observar, refuerza la evidencia en vez de duplicarse.')}
            </Typography>
          </Stack>
          <Stack direction="row" spacing={1} alignItems="flex-start">
            <BaselineIcon fontSize="small" color="success" />
            <Typography variant="body2">
              <strong>{t('learning.kind.baseline', 'Línea base')}</strong> — {t('learning.help.baseline', 'lo que ya sabe que es NORMAL aquí. Es lo que evita el falso positivo de «tarda 40 minutos».')}
            </Typography>
          </Stack>
          <Stack direction="row" spacing={1} alignItems="flex-start">
            <LessonIcon fontSize="small" color="warning" />
            <Typography variant="body2">
              <strong>{t('learning.kind.lesson', 'Lección')}</strong> — {t('learning.help.lesson', 'una conclusión que cambia cómo juzga lo que ve.')}
            </Typography>
          </Stack>
          <Alert severity="info" sx={{ mt: 0.5 }}>
            {t('learning.help.governance', 'Aprender NO le da permiso para cambiar su comportamiento. Para dejar de avisar de algo, cambiar un umbral o crear una automatización tiene que PEDÍRTELO, y lo apruebas tú (aquí o en la propia conversación). Todo lo aprobado se puede deshacer en un clic.')}
          </Alert>
        </Stack>
      </AccordionDetails>
    </Accordion>
  )

  // The short-queue block: painted at the TOP, before the list, because it is the only
  // thing that asks for an action. When empty it says so positively and takes one line.
  const att = attentionQuery.data
  // Collapsed by default: it is a work queue, not a headline. Open, it took half the
  // screen above what the operator comes to see, and with six cards it pushed the
  // list below the fold. The header counter already says whether there is anything to decide.
  const attentionBody = (
    <Box>
      {/* The title and the counter live in the accordion header; here only the
          explanation, so as not to say the same thing twice when expanding. */}
      <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: att?.total ? 1 : 0 }}>
        <Typography variant="caption" color="text.secondary">
          {att?.total
            ? t('learning.attention.hint', 'Lo demás se contrasta solo con la evidencia: no hace falta que valides nada más.')
            : t('learning.attention.empty', 'Nada pendiente: lo que ha aprendido se sostiene con su propia evidencia.')}
        </Typography>
      </Stack>
      {(att?.contradictions || []).map((c) => (
        <Alert key={`c-${c.older_id}-${c.newer_id}`} severity="warning" sx={{ mt: 1 }}>
          <AlertTitle sx={{ fontSize: 13 }}>
            {t('learning.attention.contradiction', 'Se contradice consigo mismo')} — <code>{c.subject}</code>
          </AlertTitle>
          <Typography variant="body2" sx={{ mb: 0.5 }}>· {c.newer}</Typography>
          <Typography variant="body2" color="text.secondary">· {c.older}</Typography>
          <Typography variant="caption" color="text.secondary">
            {t('learning.attention.contradictionHint',
               'Confirma la que sea cierta hoy y descarta la otra: al confirmarla queda protegida.')}
          </Typography>
          {/* The missing gesture. Each button names the sentence it keeps, not an
              abstract "accept": in a notice with two similar lines you must be able to
              choose without re-reading which one was "the first". */}
          <Stack direction="row" spacing={1} sx={{ mt: 1 }} flexWrap="wrap" useFlexGap>
            <Button size="small" variant="contained"
                    onClick={() => resolveContradiction(c.newer_id, c.older_id)}>
              {t('learning.attention.keepNewer', 'Vale la de arriba')}
            </Button>
            <Button size="small" variant="outlined"
                    onClick={() => resolveContradiction(c.older_id, c.newer_id)}>
              {t('learning.attention.keepOlder', 'Vale la de abajo')}
            </Button>
          </Stack>
        </Alert>
      ))}
      {!!(att?.resolvable || []).length && (
        <Alert key="auto" severity="success" sx={{ mt: 1 }}>
          <AlertTitle sx={{ fontSize: 13 }}>
            {t('learning.attention.autoResolved', 'Resueltas solas, con la evidencia')}
          </AlertTitle>
          {(att?.resolvable || []).map((r) => (
            <Typography key={r.drop_id} variant="body2" color="text.secondary">
              · <code>{r.subject}</code> — {t('learning.attention.autoKept', 'se queda con')} «{r.keep}»
            </Typography>
          ))}
          <Typography variant="caption" color="text.secondary">
            {t('learning.attention.autoHint',
               'Una versión era más reciente Y estaba mejor respaldada, así que no hacía falta tu criterio. Lo que una persona confirmó nunca se descarta por esta vía.')}
          </Typography>
        </Alert>
      )}
      {(att?.thin_but_important || []).map((x) => (
        <Alert key={`t-${x.id}`} severity="info" sx={{ mt: 1 }}>
          <AlertTitle sx={{ fontSize: 13 }}>
            {t('learning.attention.thin', 'Importante, pero lo ha visto una sola vez')} — <code>{x.subject}</code>
          </AlertTitle>
          <Typography variant="body2">{x.content}</Typography>
          <Stack direction="row" spacing={1} sx={{ mt: 1 }} flexWrap="wrap" useFlexGap>
            <Button size="small" variant="contained"
                    onClick={async () => {
                      try {
                        await reviewById(x.id, 'validated')
                        flash('success', t('learning.validated',
                          'Aprendizaje validado — el empleado puede apoyarse en él.'))
                        refreshAll()
                      } catch (err: any) { flash('error', err?.message || t('learning.error', 'No se pudo completar')) }
                    }}>
              {t('learning.attention.confirm', 'Es cierto')}
            </Button>
            <Button size="small" variant="outlined" color="inherit"
                    onClick={async () => {
                      try {
                        await reviewById(x.id, 'rejected')
                        flash('success', t('learning.rejected', 'Aprendizaje descartado.'))
                        refreshAll()
                      } catch (err: any) { flash('error', err?.message || t('learning.error', 'No se pudo completar')) }
                    }}>
              {t('learning.attention.discard', 'No es cierto')}
            </Button>
          </Stack>
        </Alert>
      ))}
    </Box>
  )

  const attention = (
    <Accordion
      disableGutters
      defaultExpanded={false}
      sx={{ borderRadius: 2, '&:before': { display: 'none' }, border: 1, borderColor: 'divider' }}
    >
      <AccordionSummary expandIcon={<ExpandMoreIcon />} sx={{ minHeight: 44 }}>
        <Stack direction="row" spacing={1} alignItems="center">
          <Typography variant="subtitle2">
            {t('learning.attention.title', 'Necesita tu criterio')}
          </Typography>
          <Chip size="small" color={att?.total ? 'warning' : 'success'}
                label={att?.total ?? 0} sx={{ height: 20 }} />
        </Stack>
      </AccordionSummary>
      <AccordionDetails sx={{ pt: 0 }}>
        {attentionBody}
      </AccordionDetails>
    </Accordion>
  )
  const health = stats ? (
    <Paper variant="outlined" sx={{ p: 1.25, borderRadius: 2 }}>
      <Stack direction="row" spacing={1.5} alignItems="center" flexWrap="wrap" useFlexGap>
        <Stack>
          <Typography variant="caption" color="text.secondary">{t('learning.healthLive', 'Aprendizajes')}</Typography>
          <Typography variant="body2"><strong>{stats.entries_live ?? 0}</strong></Typography>
        </Stack>
        <Divider orientation="vertical" flexItem />
        <Stack>
          <Typography variant="caption" color="text.secondary">{t('learning.healthBaselines', 'Líneas base')}</Typography>
          <Typography variant="body2">{stats.baselines ?? 0}</Typography>
        </Stack>
        <Divider orientation="vertical" flexItem />
        <Stack>
          <Typography variant="caption" color="text.secondary">{t('learning.healthSubjects', 'Cosas observadas')}</Typography>
          <Typography variant="body2">{stats.subjects ?? subjects.length}</Typography>
        </Stack>
        {/* 7.162.0 — pruning has to be VISIBLE. Without this figure, a lifecycle that
            works and a broken one look exactly the same from here. */}
        {!!((stats.retired ?? 0) + (stats.merged ?? 0)) && (
          <>
            <Divider orientation="vertical" flexItem />
            <Tooltip title={t('learning.healthTidiedHint',
              'Lo que el empleado ya ha dejado de usar por su cuenta: retirado por caducado, por no usarse nunca o por no poder reforzarse; y fusionado con otra entrada que decía lo mismo.')}>
              <Stack>
                <Typography variant="caption" color="text.secondary">
                  {t('learning.healthTidied', 'Ya depurados')}
                </Typography>
                <Typography variant="body2" color="success.main">
                  {(stats.retired ?? 0) + (stats.merged ?? 0)}
                </Typography>
              </Stack>
            </Tooltip>
          </>
        )}
        <Divider orientation="vertical" flexItem />
        <Tooltip title={t('learning.healthInjectedHint', 'Lo que lleva AHORA en la cabeza en cada trabajo: el bloque que se le inyecta.')}>
          <Stack>
            <Typography variant="caption" color="text.secondary">{t('learning.healthInjected', 'En su contexto')}</Typography>
            <Typography variant="body2">
              {stats.injected ?? 0}
              {stats.block_tokens ? ` · ${t('learning.healthTokens', { defaultValue: '{{n}} tokens', n: stats.block_tokens })}` : ''}
            </Typography>
          </Stack>
        </Tooltip>
        {!!stats.expired && (
          <>
            <Divider orientation="vertical" flexItem />
            <Tooltip title={t('learning.healthExpiredHint', 'Caducados: el entorno cambia, así que una línea base tiene fecha. Ya no se le inyectan.')}>
              <Stack>
                <Typography variant="caption" color="text.secondary">{t('learning.healthExpired', 'Caducados')}</Typography>
                <Typography variant="body2">{stats.expired}</Typography>
              </Stack>
            </Tooltip>
          </>
        )}
        <Box sx={{ flex: 1 }} />
        {/* 7.164.0 — merging duplicates has no clock: it is queued when the employee
            works. A STOPPED employee accumulates repetition and never cleans up
            internally. This button requests it by hand; the one-per-hour brake still
            stands, so pressing twice does not charge twice. */}
        <Tooltip title={t('learning.tidyHint',
          'Le pide que funda ahora lo que dice lo mismo y destile líneas base. Cuesta una llamada al modelo, así que solo se lanza una por hora.')}>
          <span>
            <Button
              size="small" variant="text" startIcon={<LearnedIcon fontSize="small" />}
              disabled={tidying} onClick={tidyNow} sx={{ textTransform: 'none' }}
            >
              {tidying
                ? t('learning.tidyRunning', 'Poniendo en orden…')
                : t('learning.tidy', 'Poner en orden ahora')}
            </Button>
          </span>
        </Tooltip>
        <Button
          size="small" variant="text" startIcon={<ViewBlockIcon fontSize="small" />}
          onClick={() => setBlockOpen(true)} sx={{ textTransform: 'none' }}
        >
          {t('learning.viewBlock', 'Ver lo que lleva en la cabeza')}
        </Button>
      </Stack>
      {stats.enabled === false && (
        <Alert severity="warning" sx={{ mt: 1, py: 0 }}>
          {t('learning.disabled', 'El aprendizaje del entorno está desactivado en la configuración: no se guarda nada nuevo.')}
        </Alert>
      )}
      {/* v7.143.0 — what the LAST round decided. With the list empty, "there was
          nothing to learn" and "it did not learn" look the same; this separates them. */}
      {stats.last_verdict && (
        <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1 }}>
          {stats.last_verdict.learnings > 0 || stats.last_verdict.reinforced > 0
            ? t('learning.verdictLearned', {
                defaultValue: 'Última ronda: guardó {{n}} y confirmó {{r}} de lo que ya sabía.',
                n: stats.last_verdict.learnings, r: stats.last_verdict.reinforced,
              })
            : stats.last_verdict.nothing_new
              ? t('learning.verdictNothingNew', {
                  defaultValue: 'Última ronda: no aprendió nada nuevo — «{{why}}».',
                  why: stats.last_verdict.nothing_new,
                })
              : t('learning.verdictAbsent', 'Última ronda: no dijo si había aprendido algo. Si se repite, revisa que la ronda esté usando la versión actual del empleado.')}
        </Typography>
      )}
    </Paper>
  ) : statsQuery.isLoading ? <LinearProgress /> : null

  const proposalsSection = (
    <Box>
      <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.5 }}>
        <RuleIcon fontSize="small" color="primary" />
        <Typography variant="overline" color="text.secondary">
          {t('learning.proposals.title', 'Te está pidiendo permiso')} ({proposals.length})
        </Typography>
      </Stack>
      {proposals.length === 0 ? (
        <Typography variant="body2" color="text.secondary">
          {t('learning.proposals.empty', 'Nada pendiente: no te está pidiendo cambiar ninguna regla.')}
        </Typography>
      ) : (
        <Stack spacing={1}>
          {proposals.map((p) => (
            <Paper key={p.effect_id} variant="outlined"
              sx={{ p: 1.5, borderRadius: 2, borderColor: 'primary.main' }}>
              <Stack spacing={1}>
                <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
                  <Chip size="small" color="primary" label={targetLabel(p.target)} />
                  <Typography variant="body2" sx={{ fontWeight: 600 }}>{p.label}</Typography>
                  <Box sx={{ flex: 1 }} />
                  <Typography variant="caption" color="text.secondary">{fmt(p.proposed_at)}</Typography>
                </Stack>
                {p.detail && (
                  <Typography variant="body2" color="text.secondary">{p.detail}</Typography>
                )}
                {p.learning_content && (
                  <Alert severity="info" icon={<LearnedIcon fontSize="small" />} sx={{ py: 0 }}>
                    <Typography variant="caption">
                      {t('learning.proposals.because', 'Lo pide porque aprendió')}
                      {p.learning_subject ? ` (${p.learning_subject})` : ''}: “{p.learning_content}”
                    </Typography>
                  </Alert>
                )}
                <Stack direction="row" spacing={1}>
                  <Button
                    size="small" variant="contained" startIcon={<CheckIcon fontSize="small" />}
                    onClick={() => acceptProposal(p)} disabled={busy} sx={{ textTransform: 'none' }}
                  >
                    {t('learning.proposals.accept', 'Autorizar')}
                  </Button>
                  <Button
                    size="small" variant="outlined" color="inherit"
                    onClick={() => rejectProposal(p)} disabled={busy} sx={{ textTransform: 'none' }}
                  >
                    {t('learning.proposals.reject', 'No, gracias')}
                  </Button>
                  {p.origin_run_id && (
                    <Button
                      size="small" variant="text" href={`/runs/${p.origin_run_id}`}
                      sx={{ textTransform: 'none' }}
                    >
                      {t('learning.proposals.origin', 'Ver de dónde sale')}
                    </Button>
                  )}
                </Stack>
              </Stack>
            </Paper>
          ))}
        </Stack>
      )}
    </Box>
  )

  const effectsSection = (
    <Box>
      <Typography variant="overline" color="text.secondary">
        {t('learning.effects.title', 'Qué ha cambiado en su comportamiento')} ({effects.length})
      </Typography>
      {effects.length === 0 ? (
        <Typography variant="body2" color="text.secondary">
          {t('learning.effects.empty', 'Ninguna regla nació de un aprendizaje: todo lo que hay lo has puesto tú.')}
        </Typography>
      ) : (
        <Stack spacing={1} sx={{ mt: 0.5 }}>
          {effects.map((e) => {
            const active = e.state === 'active'
            return (
              <Paper key={e.id} variant="outlined"
                sx={{ p: 1.25, borderRadius: 2, opacity: active ? 1 : 0.7 }}>
                <Stack direction="row" spacing={1} alignItems="flex-start">
                  <RuleIcon fontSize="small" color={active ? 'primary' : 'disabled'} />
                  <Box sx={{ flex: 1, minWidth: 0 }}>
                    <Typography variant="body2" sx={{ textDecoration: active ? 'none' : 'line-through' }}>
                      {e.label}
                    </Typography>
                    <Stack direction="row" spacing={0.5} alignItems="center" flexWrap="wrap" useFlexGap sx={{ mt: 0.5 }}>
                      <Chip size="small" variant="outlined" label={targetLabel(e.target)} />
                      <Chip
                        size="small"
                        color={active ? 'success' : e.state === 'proposed' ? 'primary' : 'default'}
                        variant={active ? 'filled' : 'outlined'}
                        label={t(`learning.state.${e.state}`, e.state)}
                      />
                      {e.approved_by && (
                        <Typography variant="caption" color="text.secondary">
                          {t('learning.effects.approvedBy', {
                            defaultValue: 'autorizado por {{who}} · {{when}}',
                            who: e.approved_by, when: fmt(e.approved_at),
                          })}
                          {e.approved_where === 'chat'
                            ? ` · ${t('learning.origin.inChat', 'en una conversación')}`
                            : ''}
                        </Typography>
                      )}
                      {e.reverted_at && (
                        <Typography variant="caption" color="text.secondary">
                          {t('learning.effects.revertedBy', {
                            defaultValue: 'deshecho por {{who}} · {{when}}',
                            who: e.reverted_by || '—', when: fmt(e.reverted_at),
                          })}
                        </Typography>
                      )}
                    </Stack>
                    {e.learning_content && (
                      <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5 }}>
                        <LearnedIcon sx={{ fontSize: 12, verticalAlign: 'text-bottom' }} />{' '}
                        {e.learning_subject ? `${e.learning_subject}: ` : ''}“{e.learning_content}”
                      </Typography>
                    )}
                    {e.revert_reason && (
                      <Typography variant="caption" color="text.secondary" sx={{ display: 'block' }}>
                        {e.revert_reason}
                      </Typography>
                    )}
                  </Box>
                  {active && (
                    <Tooltip title={t('learning.effects.revert', 'Deshacer')}>
                      <IconButton size="small" color="warning" onClick={() => revert(e)} disabled={busy}>
                        <UndoIcon fontSize="small" />
                      </IconButton>
                    </Tooltip>
                  )}
                </Stack>
              </Paper>
            )
          })}
        </Stack>
      )}
    </Box>
  )

  const filterBar = (
    <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
      <TextField
        size="small" placeholder={t('learning.searchPlaceholder', 'Buscar en lo aprendido…')}
        value={search} onChange={(e) => setSearch(e.target.value)}
        InputProps={{
          startAdornment: <InputAdornment position="start"><LearnedIcon fontSize="small" /></InputAdornment>,
          endAdornment: search ? (
            <InputAdornment position="end">
              <IconButton size="small" onClick={() => setSearch('')}><CloseIcon fontSize="small" /></IconButton>
            </InputAdornment>
          ) : undefined,
        }}
        sx={{ flex: 1, minWidth: 200 }}
      />
      <TextField
        select size="small" label={t('learning.subjectLabel', 'Sobre')}
        value={subjectFilter} onChange={(e) => setSubjectFilter(e.target.value)}
        sx={{ minWidth: 190 }}
      >
        <MenuItem value="">{t('learning.subjectAll', 'Todo')}</MenuItem>
        {subjects.map((s) => (
          <MenuItem key={s.subject} value={s.subject}>
            {s.subject} ({s.entries})
          </MenuItem>
        ))}
      </TextField>
      <TextField
        select size="small" label={t('learning.kindLabel', 'Tipo')}
        value={kindFilter} onChange={(e) => setKindFilter(e.target.value)}
        sx={{ minWidth: 160 }}
      >
        <MenuItem value="">{t('learning.kindAll', 'Todos')}</MenuItem>
        <MenuItem value="observation">{t('learning.kind.observation', 'Observación')}</MenuItem>
        <MenuItem value="baseline">{t('learning.kind.baseline', 'Línea base')}</MenuItem>
        <MenuItem value="lesson">{t('learning.kind.lesson', 'Lección')}</MenuItem>
        <MenuItem value="procedure">{t('learning.kind.procedure', 'Procedimiento')}</MenuItem>
        <MenuItem value="entity">{t('learning.kind.entity', 'Cosa del entorno')}</MenuItem>
      </TextField>
      <ToggleButtonGroup
        size="small" exclusive value={reviewFilter}
        onChange={(_e, v) => setReviewFilter(v ?? '')}
      >
        <ToggleButton value="" sx={{ textTransform: 'none' }}>{t('learning.reviewAll', 'Todos')}</ToggleButton>
        <ToggleButton value="new" sx={{ textTransform: 'none' }}>{t('learning.reviewNew', 'Sin validar')}</ToggleButton>
        <ToggleButton value="validated" sx={{ textTransform: 'none' }}>{t('learning.reviewValidated', 'Validados')}</ToggleButton>
      </ToggleButtonGroup>
      <TextField
        select size="small" label={t('learning.sortLabel', 'Orden')}
        value={sort} onChange={(e) => setSort(e.target.value)} sx={{ minWidth: 150 }}
      >
        <MenuItem value="evidence">{t('learning.sortEvidence', 'Más evidencia')}</MenuItem>
        <MenuItem value="recent">{t('learning.sortRecent', 'Más reciente')}</MenuItem>
        <MenuItem value="subject">{t('learning.sortSubject', 'Por cosa observada')}</MenuItem>
      </TextField>
      <Stack direction="row" spacing={0.5} alignItems="center">
        <Switch size="small" checked={showSuperseded} onChange={(e) => setShowSuperseded(e.target.checked)} />
        <Typography variant="body2">{t('learning.showSuperseded', 'Ver reemplazados')}</Typography>
      </Stack>
      <Button
        size="small" variant="outlined" startIcon={<AddIcon fontSize="small" />}
        onClick={() => setEditor({ ...CLOSED, open: true })} sx={{ textTransform: 'none' }}
      >
        {t('learning.teach', 'Enseñarle algo')}
      </Button>
      <Tooltip title={t('learning.refresh', 'Actualizar')}>
        <IconButton size="small" onClick={refreshAll}><RefreshIcon fontSize="small" /></IconButton>
      </Tooltip>
      {hasFilters && (
        <Button size="small" onClick={clearFilters} sx={{ textTransform: 'none' }}>
          {t('learning.clearFilters', 'Quitar filtros')}
        </Button>
      )}
    </Stack>
  )

  const bulkBar = selected.size > 0 ? (
    <Paper variant="outlined" sx={{ p: 1, borderRadius: 2, bgcolor: 'action.hover' }}>
      <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
        <Typography variant="body2">
          {t('learning.selectedCount', { defaultValue: '{{count}} seleccionados', count: selected.size })}
        </Typography>
        <Box sx={{ flex: 1 }} />
        <Button size="small" startIcon={<CheckIcon fontSize="small" />} onClick={() => bulk('validated')} sx={{ textTransform: 'none' }}>
          {t('learning.bulkValidate', 'Validar')}
        </Button>
        <Button size="small" color="warning" onClick={() => bulk('rejected')} sx={{ textTransform: 'none' }}>
          {t('learning.bulkReject', 'Descartar')}
        </Button>
        <Button size="small" color="error" startIcon={<DeleteIcon fontSize="small" />} onClick={() => bulk('delete')} sx={{ textTransform: 'none' }}>
          {t('learning.bulkDelete', 'Borrar')}
        </Button>
        <Button size="small" onClick={() => setSelected(new Set())} sx={{ textTransform: 'none' }}>
          {t('learning.bulkClear', 'Quitar selección')}
        </Button>
      </Stack>
    </Paper>
  ) : null

  return (
    <Stack spacing={2}>
      <Typography variant="body2" color="text.secondary">
        {t('learning.intro', 'Lo que el empleado ha aprendido del entorno trabajando — y qué reglas nacieron de ello.')}
      </Typography>
      {help}
      {feedback && <Alert severity={feedback.kind}>{feedback.msg}</Alert>}
      {health}
      {attention}

      {proposals.length > 0 && proposalsSection}

      {filterBar}
      {bulkBar}

      <Box>
        <Typography variant="overline" color="text.secondary">
          {t('learning.entries', 'Lo que ha aprendido')} ({total})
        </Typography>
        {entriesQuery.isLoading && entries.length === 0 ? (
          <Box sx={{ display: 'flex', justifyContent: 'center', py: 2 }}><CircularProgress size={20} /></Box>
        ) : entriesQuery.error ? (
          <Alert severity="error">{String((entriesQuery.error as any)?.message ?? entriesQuery.error)}</Alert>
        ) : entries.length === 0 ? (
          <Typography variant="body2" color="text.secondary">
            {hasFilters
              ? t('learning.noResults', 'Nada con esos filtros.')
              : t('learning.noEntries', 'Todavía no ha aprendido nada del entorno. Aparecerá solo, a medida que trabaje.')}
          </Typography>
        ) : (
          <Stack spacing={1} sx={{ mt: 0.5 }}>
            {entries.map((e) => (
              <Paper
                key={e.id} variant="outlined" id={`learning-${e.id}`}
                sx={{
                  p: 1.25, borderRadius: 2,
                  opacity: e.superseded_by != null ? 0.65 : 1,
                  borderColor: focused === e.id ? 'secondary.main' : undefined,
                  borderWidth: focused === e.id ? 2 : 1,
                }}
              >
                <Stack direction="row" spacing={1} alignItems="flex-start">
                  <Checkbox
                    size="small" sx={{ p: 0.25 }}
                    checked={selected.has(e.id)} onChange={() => toggleSelect(e.id)}
                  />
                  {kindIcon(e.kind)}
                  <Box sx={{ flex: 1, minWidth: 0 }}>
                    <Typography variant="body2" sx={{ textDecoration: e.superseded_by != null ? 'line-through' : 'none' }}>
                      {e.content}
                    </Typography>
                    <Stack direction="row" spacing={0.5} alignItems="center" flexWrap="wrap" useFlexGap sx={{ mt: 0.5 }}>
                      <Chip size="small" variant="outlined" label={e.subject} />
                      <Chip size="small" variant="outlined" label={t(`learning.kind.${e.kind}`, e.kind)} />
                      {/* EVIDENCE is the number that turns an anecdote into a
                          baseline — it is what the operator judges by. */}
                      <Tooltip title={t('learning.observationsHint', 'Veces que lo ha observado. Es la evidencia en la que se apoya.')}>
                        <Chip
                          size="small" variant="outlined"
                          color={e.observations >= 3 ? 'success' : 'default'}
                          label={t('learning.observationsCount', { defaultValue: 'visto {{count}}×', count: e.observations })}
                        />
                      </Tooltip>
                      {e.in_context && (
                        <Tooltip title={t('learning.inContextHint', 'Ahora mismo se le inyecta en cada trabajo.')}>
                          <Chip size="small" color="success" label={t('learning.inContext', 'En su contexto')} />
                        </Tooltip>
                      )}
                      {/* v7.156.0 — TRUST instead of «sin validar» (unvalidated). Asking for a signature on
                          each entry was work nobody did (103 of 107 unvalidated), so the label ended up
                          on all of them and stopped warning about anything. Trust is derived from the
                          evidence; the human signature is kept for the queue above, where its judgement
                          decides something. */}
                      {e.review_state !== 'validated' && (
                        <Tooltip title={t('learning.trustHint',
                          'Confianza calculada con su evidencia: cuántas veces lo ha visto y cuántas le ha servido. No hace falta que valides nada para que suba.') as string}>
                          <Chip size="small" variant="outlined"
                            color={e.trust === 'alta' ? 'success' : e.trust === 'media' ? 'info' : 'warning'}
                            label={t(`learning.trust.${e.trust || 'baja'}`,
                              e.trust === 'alta' ? 'Contrastado'
                                : e.trust === 'media' ? 'Con indicios' : 'Poco contrastado')} />
                        </Tooltip>
                      )}
                      {e.review_state === 'validated' && (
                        <Tooltip title={t('learning.confirmedHint',
                          'Lo has confirmado tú: queda protegido — no lo retira la limpieza automática ni lo sustituye una entrada nueva.') as string}>
                          <Chip size="small" color="success" label={t('learning.reviewValidated', 'Confirmado por ti')} />
                        </Tooltip>
                      )}
                      {e.behavior_effect && (
                        <Tooltip title={t('learning.behaviorHint', 'De este aprendizaje salió una regla que SÍ afecta a su comportamiento. Míralo abajo, en «Qué ha cambiado en su comportamiento».')}>
                          <Chip size="small" color="primary" icon={<RuleIcon fontSize="small" />}
                            label={targetLabel(e.behavior_effect)} />
                        </Tooltip>
                      )}
                      {e.expired && (
                        <Chip size="small" color="default" variant="outlined" label={t('learning.expired', 'Caducado')} />
                      )}
                      {e.source === 'operator' && (
                        <Chip size="small" variant="outlined" label={t('learning.taught', 'Se lo enseñaste tú')} />
                      )}
                      {e.valid_until && (
                        <Typography variant="caption" color="text.secondary">
                          {t('learning.validUntilOn', { defaultValue: 'válido hasta {{when}}', when: fmt(e.valid_until) })}
                        </Typography>
                      )}
                      <Typography variant="caption" color="text.secondary">{fmt(e.updated_at)}</Typography>
                      {e.created_by_run_id && (
                        <Button size="small" variant="text" href={`/runs/${e.created_by_run_id}`} sx={{ textTransform: 'none', minWidth: 0, p: 0 }}>
                          {t('learning.fromRun', 'ver trabajo')}
                        </Button>
                      )}
                    </Stack>
                  </Box>
                  {e.review_state === 'new' && (
                    <Tooltip title={t('learning.validate', 'Validar: es correcto')}>
                      <IconButton size="small" color="success" onClick={() => review(e, 'validated')}>
                        <CheckIcon fontSize="small" />
                      </IconButton>
                    </Tooltip>
                  )}
                  <Tooltip title={e.pinned ? t('learning.unpin', 'Quitar prioridad') : t('learning.pin', 'Priorizar')}>
                    <IconButton size="small" onClick={() => togglePin(e)} color={e.pinned ? 'primary' : 'default'}>
                      {e.pinned ? <PinIcon fontSize="small" /> : <PinOutlineIcon fontSize="small" />}
                    </IconButton>
                  </Tooltip>
                  <Tooltip title={t('learning.edit', 'Corregir')}>
                    <IconButton
                      size="small"
                      onClick={() => setEditor({
                        open: true, id: e.id, subject: e.subject, content: e.content,
                        kind: e.kind, validDays: '', pinned: e.pinned,
                      })}
                    >
                      <EditIcon fontSize="small" />
                    </IconButton>
                  </Tooltip>
                  <Tooltip title={t('learning.delete', 'Borrar')}>
                    <IconButton size="small" color="error" onClick={() => remove(e)}>
                      <DeleteIcon fontSize="small" />
                    </IconButton>
                  </Tooltip>
                </Stack>
              </Paper>
            ))}
          </Stack>
        )}
        {total > entries.length && (
          <Stack alignItems="center" sx={{ mt: 1 }}>
            <Button size="small" onClick={() => setLimit((l) => l + PAGE)} sx={{ textTransform: 'none' }}>
              {t('learning.loadMore', 'Ver más')} ({entries.length}/{total})
            </Button>
          </Stack>
        )}
      </Box>

      {effectsSection}

      {episodes.length > 0 && (
        <Box>
          <Typography variant="overline" color="text.secondary">
            {t('learning.episodes', 'Revisiones que ha hecho')} ({episodesQuery.data?.total ?? episodes.length})
          </Typography>
          <Stack spacing={1} sx={{ mt: 0.5 }}>
            {episodes.map((ep) => (
              <Paper key={ep.id} variant="outlined" sx={{ p: 1.25, borderRadius: 2 }}>
                <Stack direction="row" spacing={1} alignItems="flex-start">
                  <EpisodeIcon fontSize="small" color="action" />
                  <Box sx={{ flex: 1, minWidth: 0 }}>
                    <Typography variant="body2">{ep.summary}</Typography>
                    <Stack direction="row" spacing={0.5} alignItems="center" flexWrap="wrap" useFlexGap sx={{ mt: 0.5 }}>
                      <Chip size="small" variant="outlined" label={ep.subject} />
                      {ep.outcome && <Chip size="small" variant="outlined" label={ep.outcome} />}
                      <Typography variant="caption" color="text.secondary">{fmt(ep.created_at)}</Typography>
                    </Stack>
                  </Box>
                </Stack>
              </Paper>
            ))}
          </Stack>
        </Box>
      )}

      {/* Teach / correct */}
      <Dialog open={editor.open} onClose={() => setEditor(CLOSED)} fullWidth maxWidth="sm" fullScreen={fullScreen}>
        <DialogTitle>
          {editor.id ? t('learning.editTitle', 'Corregir aprendizaje') : t('learning.teachTitle', 'Enseñarle algo del entorno')}
        </DialogTitle>
        <DialogContent>
          <Stack spacing={2} sx={{ mt: 1 }}>
            <TextField
              label={t('learning.subjectField', 'Sobre qué')}
              helperText={t('learning.subjectHint', 'La identidad de lo observado: job:indexing, tenant:acme, deployment:web…')}
              value={editor.subject} disabled={!!editor.id}
              onChange={(ev) => setEditor((s) => ({ ...s, subject: ev.target.value }))}
              fullWidth autoFocus
            />
            <TextField
              label={t('learning.contentField', 'Qué debe saber')}
              value={editor.content}
              onChange={(ev) => setEditor((s) => ({ ...s, content: ev.target.value }))}
              multiline minRows={4} maxRows={12} fullWidth
            />
            <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2}>
              <TextField
                select label={t('learning.kindLabel', 'Tipo')} value={editor.kind}
                onChange={(ev) => setEditor((s) => ({ ...s, kind: ev.target.value }))}
                sx={{ minWidth: 180 }}
              >
                <MenuItem value="baseline">{t('learning.kind.baseline', 'Línea base')}</MenuItem>
                <MenuItem value="lesson">{t('learning.kind.lesson', 'Lección')}</MenuItem>
                <MenuItem value="procedure">{t('learning.kind.procedure', 'Procedimiento')}</MenuItem>
                <MenuItem value="entity">{t('learning.kind.entity', 'Cosa del entorno')}</MenuItem>
                <MenuItem value="observation">{t('learning.kind.observation', 'Observación')}</MenuItem>
              </TextField>
              <TextField
                label={t('learning.validDays', 'Caduca en (días)')}
                helperText={t('learning.validDaysHint', 'Vacío = no caduca. El entorno cambia: ponle fecha a lo que puede dejar de ser cierto.')}
                value={editor.validDays} type="number"
                onChange={(ev) => setEditor((s) => ({ ...s, validDays: ev.target.value }))}
                fullWidth
              />
            </Stack>
            <Stack direction="row" spacing={1} alignItems="center">
              <Switch checked={editor.pinned} onChange={(ev) => setEditor((s) => ({ ...s, pinned: ev.target.checked }))} />
              <Typography variant="body2">{t('learning.pinnedHint', 'Prioritario: entra siempre en su contexto.')}</Typography>
            </Stack>
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setEditor(CLOSED)}>{t('learning.cancel', 'Cancelar')}</Button>
          <Button variant="contained" onClick={save}
            disabled={busy || !editor.subject.trim() || !editor.content.trim()}>
            {t('learning.save', 'Guardar')}
          </Button>
        </DialogActions>
      </Dialog>

      {/* The literal injected block */}
      <Dialog open={blockOpen} onClose={() => setBlockOpen(false)} fullWidth maxWidth="md" fullScreen={fullScreen}>
        <DialogTitle>{t('learning.blockTitle', 'Lo que lleva en la cabeza sobre tu entorno')}</DialogTitle>
        <DialogContent>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
            {t('learning.blockIntro', 'Este texto EXACTO se le añade en cada trabajo. Lo que no esté aquí, no lo tiene presente aunque esté guardado.')}
          </Typography>
          {blockQuery.isLoading ? (
            <LinearProgress />
          ) : blockQuery.data?.empty || !blockQuery.data?.text ? (
            <Alert severity="info">{t('learning.blockEmpty', 'Todavía no se le inyecta nada.')}</Alert>
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
          <Button onClick={() => setBlockOpen(false)}>{t('learning.close', 'Cerrar')}</Button>
        </DialogActions>
      </Dialog>

      {/* Confirmation */}
      <Dialog open={!!confirmState} onClose={() => setConfirmState(null)} maxWidth="xs" fullWidth>
        <DialogTitle>{t('learning.confirmTitle', 'Confirma')}</DialogTitle>
        <DialogContent>
          <Typography variant="body2" sx={{ mt: 1, whiteSpace: 'pre-line' }}>{confirmState?.msg}</Typography>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setConfirmState(null)}>{t('learning.cancel', 'Cancelar')}</Button>
          <Button
            variant="contained" color={confirmState?.danger ? 'error' : 'primary'}
            onClick={() => { const act = confirmState?.onConfirm; setConfirmState(null); act?.() }}
          >
            {t('learning.continue', 'Continuar')}
          </Button>
        </DialogActions>
      </Dialog>
    </Stack>
  )
}
