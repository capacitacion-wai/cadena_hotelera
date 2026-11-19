/**
 * Business PARAMETERS the employee asked for (v7.43.0) — the operator side of
 * `request_knowledge`. Lives in Operations → Conocimiento, above the free-text
 * knowledge. Two lists:
 *   1. "El empleado necesita saber" — pending requests (and member proposals to
 *      confirm), each with a TYPED input (number+unit / text / switch / select /
 *      date) so filling a threshold is a one-glance action.
 *   2. "Parámetros que le has dado" — answered values, editable/clearable.
 * Plus "Añadir parámetro" so the operator can seed one before the agent asks.
 *
 * `useKnowledgeRequests()` is shared with the Conocimiento tab badge (react-query
 * dedupes the fetch), so the badge count and this panel never drift.
 */
import { useState } from 'react'
import {
  Alert, Autocomplete, Box, Button, Card, CardContent, Chip, Dialog, DialogActions, DialogContent,
  DialogTitle, IconButton, InputAdornment, MenuItem, Stack, Switch, TextField, Tooltip, Typography,
} from '@mui/material'
import AddIcon from '@mui/icons-material/Add'
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline'
import EditOutlinedIcon from '@mui/icons-material/EditOutlined'
import { useTranslation } from 'react-i18next'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from '../services/apiClient'

export interface KnowledgeReq {
  id: number
  key: string
  label: string
  question: string
  label_en: string | null
  question_en: string | null
  value_type: string
  unit: string | null
  options: string[]
  suggested: string | null
  value: string | null
  status: string
  origin: string
  proposed_value: string | null
  proposed_by: string | null
  has_proposal: boolean
  provided_by: string | null
}

const VALUE_TYPES = ['text', 'number', 'boolean', 'list', 'date']

/** Pick the title/question in the viewer's active language. The agent stores the
 * Spanish text in label/question and the English translation in *_en; when the
 * viewer is on English we show the *_en pair, falling back to the Spanish text if
 * the agent didn't translate it (older rows, or a hand-seeded parameter). */
function localizedLabel(item: Pick<KnowledgeReq, 'label' | 'label_en'>, lang: string): string {
  return lang.startsWith('en') ? (item.label_en || item.label) : item.label
}
function localizedQuestion(item: Pick<KnowledgeReq, 'question' | 'question_en'>, lang: string): string {
  return lang.startsWith('en') ? (item.question_en || item.question || '') : (item.question || '')
}

/** Shared query — the badge and this panel read the same cache. */
export function useKnowledgeRequests() {
  const q = useQuery({
    queryKey: ['knowledge-requests'],
    queryFn: () => apiFetch('/api/agent/knowledge-requests').then(async (r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      return r.json()
    }),
    staleTime: 15_000,
  })
  return {
    items: (q.data?.items ?? []) as KnowledgeReq[],
    pending: (q.data?.pending_count ?? 0) as number,
    isLoading: q.isLoading,
    isError: q.isError,
  }
}

/** A typed input bound to a draft string value. */
function ValueInput({ item, value, onChange, autoFocus }: {
  item: Pick<KnowledgeReq, 'value_type' | 'unit' | 'options'>
  value: string
  onChange: (v: string) => void
  autoFocus?: boolean
}) {
  const { t } = useTranslation()
  if (item.value_type === 'boolean') {
    return (
      <Stack direction="row" spacing={1} alignItems="center">
        <Switch checked={value === 'true'} onChange={(e) => onChange(e.target.checked ? 'true' : 'false')} />
        <Typography variant="body2" color="text.secondary">
          {value === 'true' ? t('operations.params.yes', 'Sí') : t('operations.params.no', 'No')}
        </Typography>
      </Stack>
    )
  }
  if (item.value_type === 'list') {
    // A list parameter holds ONE OR SEVERAL values (e.g. «tipos de entidad
    // prioritarios»: leads, contacts…). Stored as a comma-separated string so the
    // prompt block reads naturally; the operator adds each value as a chip and can
    // both pick from `options` (suggestions) and type free values (freeSolo).
    const parts = value ? value.split(',').map((s) => s.trim()).filter(Boolean) : []
    return (
      <Autocomplete
        multiple
        freeSolo
        size="small"
        options={item.options || []}
        value={parts}
        onChange={(_e, v) => onChange((v as string[]).map((s) => s.trim()).filter(Boolean).join(', '))}
        renderTags={(vals, getTagProps) =>
          vals.map((val, i) => {
            const { key, ...tagProps } = getTagProps({ index: i })
            return <Chip size="small" label={val} key={key} {...tagProps} />
          })}
        renderInput={(params) => (
          <TextField {...params} autoFocus={autoFocus}
            placeholder={parts.length ? '' : t('operations.params.listPlaceholder', 'Escribe un valor y pulsa Enter…')} />
        )}
        sx={{ minWidth: 260 }}
      />
    )
  }
  if (item.value_type === 'date') {
    return (
      <TextField type="date" size="small" value={value} onChange={(e) => onChange(e.target.value)}
        InputLabelProps={{ shrink: true }} autoFocus={autoFocus} />
    )
  }
  if (item.value_type === 'number') {
    return (
      <TextField type="number" size="small" value={value} onChange={(e) => onChange(e.target.value)}
        sx={{ maxWidth: 220 }} autoFocus={autoFocus}
        InputProps={item.unit ? { endAdornment: <InputAdornment position="end">{item.unit}</InputAdornment> } : undefined} />
    )
  }
  return (
    <TextField size="small" value={value} onChange={(e) => onChange(e.target.value)} fullWidth autoFocus={autoFocus}
      placeholder={t('operations.params.valuePlaceholder', 'Valor…')} />
  )
}

const fmtValue = (item: KnowledgeReq, v: string | null): string => {
  if (v == null || v === '') return '—'
  if (item.value_type === 'boolean') return v === 'true' ? '✓' : '✗'
  return item.unit ? `${v} ${item.unit}` : v
}

export default function BusinessParameters() {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const { items, isLoading } = useKnowledgeRequests()
  const [adding, setAdding] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const invalidate = () => qc.invalidateQueries({ queryKey: ['knowledge-requests'] })
  const call = async (path: string, method: string, body?: any) => {
    const r = await apiFetch(path, {
      method,
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    })
    if (!r.ok) { const b = await r.json().catch(() => null); throw new Error(b?.detail || `HTTP ${r.status}`) }
    return r.json()
  }
  const mut = useMutation({
    mutationFn: (v: { path: string; method: string; body?: any }) => call(v.path, v.method, v.body),
    onSuccess: invalidate,
    onError: (e: any) => setErr(String(e?.message ?? e)),
  })

  const needsAttention = items.filter((i) => i.status !== 'provided' || i.has_proposal)
  const provided = items.filter((i) => i.status === 'provided' && !i.has_proposal)

  return (
    <Card variant="outlined" sx={{ mb: 2 }}>
      <CardContent>
        <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 0.5 }}>
          <Typography variant="subtitle1" sx={{ fontWeight: 700, flex: 1 }}>
            {t('operations.params.title', 'Parámetros de negocio')}
          </Typography>
          <Button size="small" startIcon={<AddIcon />} onClick={() => { setErr(null); setAdding(true) }}>
            {t('operations.params.add', 'Añadir parámetro')}
          </Button>
        </Stack>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
          {t('operations.params.subtitle', 'Datos que el empleado necesita para funcionar (umbrales, objetivos, criterios). Los pide cuando le faltan; tú los rellenas aquí y pasan a formar parte de lo que sabe.')}
        </Typography>
        {err && <Alert severity="error" sx={{ mb: 1 }} onClose={() => setErr(null)}>{err}</Alert>}
        {isLoading && <Typography variant="body2" color="text.secondary">{t('common.loading', 'Cargando…')}</Typography>}

        {/* Needs attention: pending requests + member proposals. */}
        {needsAttention.length > 0 && (
          <Box sx={{ mb: provided.length ? 2 : 0 }}>
            <Typography variant="overline" color="warning.main">
              {t('operations.params.needed', 'El empleado necesita saber')} ({needsAttention.length})
            </Typography>
            <Stack spacing={1} sx={{ mt: 0.5 }}>
              {needsAttention.map((it) => (
                <PendingCard key={it.id} item={it} busy={mut.isPending}
                  onSave={(v) => mut.mutate({ path: `/api/agent/knowledge-requests/${it.id}`, method: 'PATCH', body: { value: v } })}
                  onConfirm={() => mut.mutate({ path: `/api/agent/knowledge-requests/${it.id}`, method: 'PATCH', body: { confirm_proposal: true } })}
                  onReject={() => mut.mutate({ path: `/api/agent/knowledge-requests/${it.id}`, method: 'PATCH', body: { reject_proposal: true } })}
                  onDelete={() => mut.mutate({ path: `/api/agent/knowledge-requests/${it.id}`, method: 'DELETE' })} />
              ))}
            </Stack>
          </Box>
        )}

        {/* Answered parameters. */}
        {provided.length > 0 && (
          <Box>
            <Typography variant="overline" color="text.secondary">
              {t('operations.params.given', 'Parámetros que le has dado')} ({provided.length})
            </Typography>
            <Stack spacing={0.5} sx={{ mt: 0.5 }}>
              {provided.map((it) => (
                <ProvidedRow key={it.id} item={it} busy={mut.isPending}
                  onSave={(v) => mut.mutate({ path: `/api/agent/knowledge-requests/${it.id}`, method: 'PATCH', body: { value: v } })}
                  onClear={() => mut.mutate({ path: `/api/agent/knowledge-requests/${it.id}`, method: 'PATCH', body: { value: '' } })}
                  onDelete={() => mut.mutate({ path: `/api/agent/knowledge-requests/${it.id}`, method: 'DELETE' })} />
              ))}
            </Stack>
          </Box>
        )}

        {needsAttention.length === 0 && provided.length === 0 && !isLoading && (
          <Typography variant="body2" color="text.secondary">
            {t('operations.params.empty', 'Aún no hay parámetros. El empleado los pedirá cuando los necesite, o puedes añadir uno.')}
          </Typography>
        )}
      </CardContent>

      {adding && (
        <AddDialog
          onClose={() => setAdding(false)}
          onCreate={(body) => mut.mutate({ path: '/api/agent/knowledge-requests', method: 'POST', body }, { onSuccess: () => { invalidate(); setAdding(false) } })}
        />
      )}
    </Card>
  )
}

function PendingCard({ item, busy, onSave, onConfirm, onReject, onDelete }: {
  item: KnowledgeReq; busy: boolean
  onSave: (v: string) => void; onConfirm: () => void; onReject: () => void; onDelete: () => void
}) {
  const { t, i18n } = useTranslation()
  const [draft, setDraft] = useState<string>(item.value ?? item.suggested ?? '')
  const question = localizedQuestion(item, i18n.language)
  return (
    <Card variant="outlined" sx={{ borderLeft: '3px solid', borderLeftColor: 'warning.main' }}>
      <CardContent sx={{ py: 1.25, '&:last-child': { pb: 1.25 } }}>
        <Stack direction="row" alignItems="flex-start" spacing={1}>
          <Box sx={{ flex: 1, minWidth: 0 }}>
            <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>{localizedLabel(item, i18n.language)}</Typography>
            {question && (
              <Typography variant="body2" color="text.secondary" sx={{ mb: 0.5 }}>{question}</Typography>
            )}
            {item.has_proposal && (
              <Alert severity="info" sx={{ py: 0, mb: 1 }}
                action={
                  <Stack direction="row" spacing={0.5}>
                    <Button size="small" color="inherit" disabled={busy} onClick={onReject}>{t('operations.params.reject', 'Descartar')}</Button>
                    <Button size="small" variant="contained" disabled={busy} onClick={onConfirm}>{t('operations.params.confirm', 'Confirmar')}</Button>
                  </Stack>
                }>
                {t('operations.params.proposal', { value: fmtValue(item, item.proposed_value), by: item.proposed_by || '—', defaultValue: `Propuesta: ${fmtValue(item, item.proposed_value)} (de ${item.proposed_by || '—'})` })}
              </Alert>
            )}
            <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
              <ValueInput item={item} value={draft} onChange={setDraft} />
              <Button size="small" variant="contained" disabled={busy || draft === '' || draft == null}
                onClick={() => onSave(draft)}>
                {t('operations.params.save', 'Guardar')}
              </Button>
              {item.suggested && item.status !== 'provided' && (
                <Typography variant="caption" color="text.secondary">
                  {t('operations.params.suggested', { value: fmtValue(item, item.suggested), defaultValue: `El empleado usa mientras tanto: ${fmtValue(item, item.suggested)}` })}
                </Typography>
              )}
            </Stack>
          </Box>
          <Tooltip title={t('operations.params.delete', 'Eliminar')}>
            <span><IconButton size="small" disabled={busy} onClick={onDelete}><DeleteOutlineIcon fontSize="small" /></IconButton></span>
          </Tooltip>
        </Stack>
      </CardContent>
    </Card>
  )
}

function ProvidedRow({ item, busy, onSave, onClear, onDelete }: {
  item: KnowledgeReq; busy: boolean
  onSave: (v: string) => void; onClear: () => void; onDelete: () => void
}) {
  const { t, i18n } = useTranslation()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState<string>(item.value ?? '')
  return (
    <Stack direction="row" spacing={1} alignItems="center" sx={{ py: 0.4, px: 0.5, borderRadius: 1, '&:hover': { bgcolor: 'action.hover' } }}>
      <Typography variant="body2" sx={{ minWidth: 200, flexShrink: 0 }}>{localizedLabel(item, i18n.language)}</Typography>
      {editing ? (
        <>
          <ValueInput item={item} value={draft} onChange={setDraft} autoFocus />
          <Button size="small" variant="contained" disabled={busy} onClick={() => { onSave(draft); setEditing(false) }}>
            {t('operations.params.save', 'Guardar')}
          </Button>
          <Button size="small" color="inherit" onClick={() => { setDraft(item.value ?? ''); setEditing(false) }}>
            {t('common.cancel', 'Cancelar')}
          </Button>
        </>
      ) : (
        <>
          {item.value_type === 'list' && item.value ? (
            <Stack direction="row" spacing={0.5} sx={{ flexWrap: 'wrap' }} useFlexGap>
              {item.value.split(',').map((s) => s.trim()).filter(Boolean).map((v) => (
                <Chip key={v} size="small" label={v} />
              ))}
            </Stack>
          ) : (
            <Chip size="small" label={fmtValue(item, item.value)} />
          )}
          <Box sx={{ flex: 1 }} />
          <Tooltip title={t('operations.params.edit', 'Editar')}>
            <span><IconButton size="small" disabled={busy} onClick={() => { setDraft(item.value ?? ''); setEditing(true) }}><EditOutlinedIcon fontSize="small" /></IconButton></span>
          </Tooltip>
          <Tooltip title={t('operations.params.delete', 'Eliminar')}>
            <span><IconButton size="small" disabled={busy} onClick={onDelete}><DeleteOutlineIcon fontSize="small" /></IconButton></span>
          </Tooltip>
        </>
      )}
    </Stack>
  )
}

function AddDialog({ onClose, onCreate }: { onClose: () => void; onCreate: (body: any) => void }) {
  const { t } = useTranslation()
  const [f, setF] = useState<{ label: string; question: string; label_en: string; question_en: string; value_type: string; unit: string; options: string; value: string }>(
    { label: '', question: '', label_en: '', question_en: '', value_type: 'text', unit: '', options: '', value: '' },
  )
  const submit = () => {
    const body: any = {
      label: f.label.trim(),
      question: f.question.trim(),
      label_en: f.label_en.trim() || undefined,
      question_en: f.question_en.trim() || undefined,
      value_type: f.value_type,
      unit: f.unit.trim() || undefined,
      value: f.value.trim() || undefined,
    }
    if (f.value_type === 'list') body.options = f.options.split(',').map((s) => s.trim()).filter(Boolean)
    onCreate(body)
  }
  return (
    <Dialog open onClose={onClose} fullWidth maxWidth="sm">
      <DialogTitle>{t('operations.params.addTitle', 'Añadir parámetro de negocio')}</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 0.5 }}>
          <TextField label={t('operations.params.fLabel', 'Nombre')} value={f.label} autoFocus
            onChange={(e) => setF({ ...f, label: e.target.value })} fullWidth size="small" />
          <TextField label={t('operations.params.fQuestion', 'Descripción (para qué sirve)')} value={f.question}
            onChange={(e) => setF({ ...f, question: e.target.value })} fullWidth size="small" multiline minRows={2} />
          <TextField label={t('operations.params.fLabelEn', 'Nombre en inglés (opcional)')} value={f.label_en}
            onChange={(e) => setF({ ...f, label_en: e.target.value })} fullWidth size="small" />
          <TextField label={t('operations.params.fQuestionEn', 'Descripción en inglés (opcional)')} value={f.question_en}
            onChange={(e) => setF({ ...f, question_en: e.target.value })} fullWidth size="small" multiline minRows={2} />
          <Stack direction="row" spacing={2}>
            <TextField select label={t('operations.params.fType', 'Tipo')} value={f.value_type}
              onChange={(e) => setF({ ...f, value_type: e.target.value })} size="small" sx={{ minWidth: 160 }}>
              {VALUE_TYPES.map((vt) => <MenuItem key={vt} value={vt}>{t(`operations.params.type.${vt}`, vt)}</MenuItem>)}
            </TextField>
            {f.value_type === 'number' && (
              <TextField label={t('operations.params.fUnit', 'Unidad')} value={f.unit}
                onChange={(e) => setF({ ...f, unit: e.target.value })} size="small" placeholder="€, días, %" />
            )}
          </Stack>
          {f.value_type === 'list' && (
            <TextField label={t('operations.params.fOptions', 'Opciones (separadas por comas)')} value={f.options}
              onChange={(e) => setF({ ...f, options: e.target.value })} fullWidth size="small" />
          )}
          <TextField label={t('operations.params.fValue', 'Valor (opcional, puedes rellenarlo ahora)')} value={f.value}
            onChange={(e) => setF({ ...f, value: e.target.value })} fullWidth size="small" />
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>{t('common.cancel', 'Cancelar')}</Button>
        <Button variant="contained" disabled={!f.label.trim()} onClick={submit}>{t('common.save', 'Guardar')}</Button>
      </DialogActions>
    </Dialog>
  )
}
