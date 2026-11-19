/**
 * Documentos del empleado — the operator surface for the DOCUMENT KNOWLEDGE
 * LIBRARY (v7.84.0). Lives inside Configuración → Conocimiento, UNDER the
 * free-text instructions, so the operator manages "lo que le enseño" (texto) and
 * "el material que le doy" (archivos) in one place.
 *
 * Deliberately NON-TECHNICAL wording: "material de referencia", "documentos",
 * "indexando" — never "RAG / embeddings / chunks / vectores". The employee reads
 * these files ONLY through its own tools; here the operator uploads, scopes and
 * curates them, and sees which ones actually get used.
 *
 * Governance per document: enabled, ámbito / scope (everyone / autonomous / serving /
 * one person), and visibilidad / visibility (compartible = it may quote it to people;
 * interno = only for reasoning, it does not reproduce it outside). Mutations require admin.
 *
 * INVARIANT (skill-owned): every agent-app ships this component.
 */
import { useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert, Box, Button, Card, CardContent, Chip, CircularProgress, Dialog,
  DialogActions, DialogContent, DialogTitle, Divider, IconButton, LinearProgress,
  MenuItem, Select, Stack, Switch, TextField, Tooltip, Typography,
} from '@mui/material'
import {
  Article as DocIcon, Delete as DeleteIcon, Download as DownloadIcon,
  Refresh as RetryIcon, UploadFile as UploadIcon, Visibility as PreviewIcon,
  Lock as InternalIcon, Public as ShareableIcon,
} from '@mui/icons-material'
import { apiFetch } from '../services/apiClient'
import { fmtBytes } from './WorkspaceViewer'

type DocScope = 'all' | 'autonomous' | 'attending' | 'user'
type DocVisibility = 'internal' | 'shareable'

// Authenticated download: a plain <a href> would NOT carry the Bearer token, so
// fetch via apiFetch → blob → object URL (same pattern as WorkspaceViewer).
async function saveBlob(url: string, filename: string) {
  const res = await apiFetch(url)
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  const blob = await res.blob()
  const obj = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = obj
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(obj)
}

interface KDoc {
  id: number; title: string; ext: string | null; size_bytes: number
  status: 'uploading' | 'indexing' | 'ready' | 'error'; error: string | null
  enabled: boolean; scope: DocScope; user_email: string | null
  visibility: DocVisibility; tags: string | null; summary: string | null
  char_count: number; use_count: number; last_used_at: string | null
  valid_until: string | null; uploaded_by: string | null; created_at: string
}

const ACCEPT = '.pdf,.docx,.txt,.md,.csv,.xlsx'

export default function KnowledgeDocuments() {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const me = useQuery({
    queryKey: ['users-me'],
    queryFn: () => apiFetch('/api/agent/users/me').then((r) => (r.ok ? r.json() : { is_admin: false })),
    staleTime: 60_000,
  })
  const isAdmin = me.data?.is_admin === true

  const listQ = useQuery({
    queryKey: ['knowledge-docs'],
    queryFn: () => apiFetch('/api/agent/knowledge/docs').then((r) => r.json()),
    // Poll while anything is still being processed so the status chips advance
    // (Subiendo → Indexando → Listo) without the operator refreshing.
    refetchInterval: (q) => {
      const docs: KDoc[] = (q.state.data as any)?.documents ?? []
      return docs.some((d) => d.status === 'uploading' || d.status === 'indexing') ? 2500 : false
    },
  })
  const summaryQ = useQuery({
    queryKey: ['knowledge-docs-summary'],
    queryFn: () => apiFetch('/api/agent/knowledge/docs/summary').then((r) => r.json()),
    staleTime: 10_000,
  })

  const enabled = summaryQ.data?.enabled !== false && listQ.data?.enabled !== false
  const docs: KDoc[] = listQ.data?.documents ?? []
  const limits = summaryQ.data?.limits ?? {}
  const usedBytes = summaryQ.data?.total_bytes ?? 0
  const count = summaryQ.data?.count ?? docs.length

  const [err, setErr] = useState<string | null>(null)
  const [uploadOpen, setUploadOpen] = useState(false)
  const [preview, setPreview] = useState<KDoc | null>(null)

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['knowledge-docs'] })
    qc.invalidateQueries({ queryKey: ['knowledge-docs-summary'] })
  }

  const patch = useMutation({
    mutationFn: async (v: { id: number; body: Record<string, unknown> }) => {
      const r = await apiFetch(`/api/agent/knowledge/docs/${v.id}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(v.body),
      })
      if (!r.ok) { const b = await r.json().catch(() => null); throw new Error(b?.detail || `HTTP ${r.status}`) }
    },
    onSuccess: invalidate,
    onError: (e: any) => setErr(String(e?.message ?? e)),
  })
  const del = useMutation({
    mutationFn: async (id: number) => {
      const r = await apiFetch(`/api/agent/knowledge/docs/${id}`, { method: 'DELETE' })
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
    },
    onSuccess: invalidate,
    onError: (e: any) => setErr(String(e?.message ?? e)),
  })
  const reindex = useMutation({
    mutationFn: async (id: number) => {
      const r = await apiFetch(`/api/agent/knowledge/docs/${id}/reindex`, { method: 'POST' })
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
    },
    onSuccess: invalidate,
    onError: (e: any) => setErr(String(e?.message ?? e)),
  })

  if (!enabled) return null

  return (
    <Stack spacing={1.5}>
      <Divider sx={{ my: 1 }} />
      <Stack direction="row" alignItems="center" spacing={1}>
        <DocIcon fontSize="small" color="action" />
        <Typography variant="subtitle1" sx={{ fontWeight: 600, flex: 1 }}>
          {t('operations.knowledgeDocs.title', 'Documentos del empleado')}
        </Typography>
        {isAdmin && (
          <Button size="small" variant="contained" startIcon={<UploadIcon />}
            onClick={() => { setErr(null); setUploadOpen(true) }} sx={{ textTransform: 'none' }}>
            {t('operations.knowledgeDocs.add', 'Subir documento')}
          </Button>
        )}
      </Stack>
      <Typography variant="body2" color="text.secondary">
        {t('operations.knowledgeDocs.intro', 'Sube el material de referencia con el que quieres que responda: catálogos, tarifas, políticas, requisitos, preguntas frecuentes. El empleado lo consultará y citará la fuente; no lo usará para inventar.')}
      </Typography>
      {typeof limits.max_total_bytes === 'number' && (
        <Typography variant="caption" color="text.secondary">
          {t('operations.knowledgeDocs.usage', '{{count}} documentos · {{used}} de {{total}} usados', {
            count, used: fmtBytes(usedBytes), total: fmtBytes(limits.max_total_bytes),
          })}
        </Typography>
      )}

      {err && <Alert severity="error" onClose={() => setErr(null)}>{err}</Alert>}

      {docs.length === 0 ? (
        <Card variant="outlined"><CardContent sx={{ py: 2 }}>
          <Typography variant="body2" color="text.secondary">
            {t('operations.knowledgeDocs.empty', 'Todavía no hay documentos. Sube el primero para que el empleado responda con tu material oficial.')}
          </Typography>
        </CardContent></Card>
      ) : (
        <Stack spacing={1}>
          {docs.map((d) => (
            <DocRow key={d.id} d={d} isAdmin={isAdmin}
              onToggle={(en) => patch.mutate({ id: d.id, body: { enabled: en } })}
              onScope={(scope, user_email) => patch.mutate({ id: d.id, body: { scope, user_email } })}
              onVisibility={(visibility) => patch.mutate({ id: d.id, body: { visibility } })}
              onDelete={() => del.mutate(d.id)}
              onReindex={() => reindex.mutate(d.id)}
              onPreview={() => setPreview(d)} />
          ))}
        </Stack>
      )}

      <SensitiveTopicsCard isAdmin={isAdmin} onError={setErr} />

      {uploadOpen && (
        <UploadDialog limits={limits} onClose={() => setUploadOpen(false)}
          onDone={() => { setUploadOpen(false); invalidate() }} onError={setErr} />
      )}
      {preview && <PreviewDialog doc={preview} onClose={() => setPreview(null)} />}
    </Stack>
  )
}

// ── one document row ──────────────────────────────────────────────────
function DocRow({ d, isAdmin, onToggle, onScope, onVisibility, onDelete, onReindex, onPreview }: {
  d: KDoc; isAdmin: boolean
  onToggle: (enabled: boolean) => void
  onScope: (scope: DocScope, user_email: string) => void
  onVisibility: (v: DocVisibility) => void
  onDelete: () => void; onReindex: () => void; onPreview: () => void
}) {
  const { t } = useTranslation()
  const busy = d.status === 'uploading' || d.status === 'indexing'
  const statusChip = (() => {
    if (d.status === 'ready') return <Chip size="small" color="success" label={t('operations.knowledgeDocs.status.ready', 'Listo')} />
    if (d.status === 'error') return <Chip size="small" color="error" label={t('operations.knowledgeDocs.status.error', 'Error')} />
    const label = d.status === 'uploading'
      ? t('operations.knowledgeDocs.status.uploading', 'Subiendo')
      : t('operations.knowledgeDocs.status.indexing', 'Indexando')
    return <Chip size="small" color="info" icon={<CircularProgress size={12} thickness={6} sx={{ color: 'inherit' }} />} label={label} />
  })()

  return (
    <Card variant="outlined" sx={{ opacity: d.enabled ? 1 : 0.6 }}>
      {busy && <LinearProgress />}
      <CardContent sx={{ py: 1.25, '&:last-child': { pb: 1.25 } }}>
        <Stack direction="row" alignItems="center" spacing={1} sx={{ flexWrap: 'wrap' }}>
          <Typography variant="subtitle2" sx={{ fontWeight: 600, flex: 1, minWidth: 180 }}>
            {d.title}
          </Typography>
          {statusChip}
          {d.ext && <Chip size="small" variant="outlined" label={d.ext.toUpperCase()} />}
          <Typography variant="caption" color="text.secondary">{fmtBytes(d.size_bytes)}</Typography>
          {isAdmin && (
            <Tooltip title={d.enabled ? t('operations.knowledgeDocs.on', 'Activo') : t('operations.knowledgeDocs.off', 'Inactivo')}>
              <Switch size="small" checked={d.enabled} onChange={(e) => onToggle(e.target.checked)} />
            </Tooltip>
          )}
        </Stack>

        {d.status === 'error' && d.error && (
          <Alert severity="warning" sx={{ mt: 1 }}
            action={isAdmin ? <Button size="small" startIcon={<RetryIcon />} onClick={onReindex}>{t('operations.knowledgeDocs.retry', 'Reintentar')}</Button> : undefined}>
            {d.error}
          </Alert>
        )}
        {d.summary && d.status === 'ready' && (
          <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5 }}>
            {d.summary}
          </Typography>
        )}

        <Stack direction="row" alignItems="center" spacing={1} sx={{ mt: 1, flexWrap: 'wrap', gap: 1 }}>
          {/* ámbito */}
          <ScopeSelect value={d.scope} userEmail={d.user_email || ''} disabled={!isAdmin}
            onChange={onScope} />
          {/* visibilidad */}
          <VisibilityToggle value={d.visibility} disabled={!isAdmin} onChange={onVisibility} />
          {d.tags && d.tags.split(',').map((tg) => tg.trim()).filter(Boolean).map((tg) => (
            <Chip key={tg} size="small" variant="outlined" label={tg} />
          ))}
          <Box sx={{ flex: 1 }} />
          {d.use_count > 0 && (
            <Tooltip title={t('operations.knowledgeDocs.usedTip', 'Veces que el empleado lo ha usado como fuente')}>
              <Chip size="small" variant="outlined" color="success"
                label={t('operations.knowledgeDocs.used', 'Usado {{n}}×', { n: d.use_count })} />
            </Tooltip>
          )}
          {d.status === 'ready' && (
            <Tooltip title={t('operations.knowledgeDocs.preview', 'Ver extracto')}>
              <IconButton size="small" onClick={onPreview}><PreviewIcon fontSize="small" /></IconButton>
            </Tooltip>
          )}
          <Tooltip title={t('operations.knowledgeDocs.download', 'Descargar original')}>
            <IconButton size="small" onClick={() => saveBlob(`/api/agent/knowledge/docs/${d.id}/download`, `${d.title}${d.ext ? '.' + d.ext : ''}`)}>
              <DownloadIcon fontSize="small" />
            </IconButton>
          </Tooltip>
          {isAdmin && (
            <Tooltip title={t('operations.knowledgeDocs.delete', 'Eliminar')}>
              <IconButton size="small" color="error" onClick={onDelete}><DeleteIcon fontSize="small" /></IconButton>
            </Tooltip>
          )}
        </Stack>
      </CardContent>
    </Card>
  )
}

function ScopeSelect({ value, userEmail, disabled, onChange }: {
  value: DocScope; userEmail: string; disabled: boolean
  onChange: (scope: DocScope, user_email: string) => void
}) {
  const { t } = useTranslation()
  // Local scope so picking "una persona" reveals the email field WITHOUT
  // persisting (the backend rejects a user scope with no email); we persist on
  // email blur. Non-user scopes persist immediately.
  const [localScope, setLocalScope] = useState<DocScope>(value)
  const [email, setEmail] = useState(userEmail)
  const scope = localScope
  return (
    <Stack direction="row" alignItems="center" spacing={0.5}>
      <Select size="small" value={scope} disabled={disabled} sx={{ minWidth: 150 }}
        onChange={(e) => {
          const s = e.target.value as DocScope
          setLocalScope(s)
          if (s !== 'user') onChange(s, '')
          else if (email.trim()) onChange('user', email.trim())
        }}>
        <MenuItem value="all">{t('operations.knowledgeDocs.scope.all', 'En todos los casos')}</MenuItem>
        <MenuItem value="attending">{t('operations.knowledgeDocs.scope.attending', 'Al atender a personas')}</MenuItem>
        <MenuItem value="autonomous">{t('operations.knowledgeDocs.scope.autonomous', 'Solo tareas autónomas')}</MenuItem>
        <MenuItem value="user">{t('operations.knowledgeDocs.scope.user', 'Solo una persona')}</MenuItem>
      </Select>
      {scope === 'user' && (
        <TextField size="small" placeholder="email@empresa.com" value={email} disabled={disabled}
          onChange={(e) => setEmail(e.target.value)}
          onBlur={() => email.trim() && email.trim() !== userEmail && onChange('user', email.trim())}
          sx={{ minWidth: 180 }} />
      )}
    </Stack>
  )
}

function VisibilityToggle({ value, disabled, onChange }: {
  value: DocVisibility; disabled: boolean; onChange: (v: DocVisibility) => void
}) {
  const { t } = useTranslation()
  const isInternal = value === 'internal'
  return (
    <Tooltip title={isInternal
      ? t('operations.knowledgeDocs.vis.internalTip', 'Interno: el empleado lo usa para razonar, pero NO lo cita ni reenvía a personas.')
      : t('operations.knowledgeDocs.vis.shareableTip', 'Compartible: el empleado puede citarlo y reproducirlo a personas.')}>
      <Chip size="small" clickable={!disabled} icon={isInternal ? <InternalIcon /> : <ShareableIcon />}
        color={isInternal ? 'warning' : 'default'}
        variant={isInternal ? 'filled' : 'outlined'}
        label={isInternal ? t('operations.knowledgeDocs.vis.internal', 'Interno') : t('operations.knowledgeDocs.vis.shareable', 'Compartible')}
        onClick={disabled ? undefined : () => onChange(isInternal ? 'shareable' : 'internal')} />
    </Tooltip>
  )
}

// ── upload dialog ─────────────────────────────────────────────────────
function UploadDialog({ limits, onClose, onDone, onError }: {
  limits: any; onClose: () => void; onDone: () => void; onError: (m: string) => void
}) {
  const { t } = useTranslation()
  const fileRef = useRef<HTMLInputElement>(null)
  const [file, setFile] = useState<File | null>(null)
  const [title, setTitle] = useState('')
  const [scope, setScope] = useState<DocScope>('all')
  const [userEmail, setUserEmail] = useState('')
  const [visibility, setVisibility] = useState<DocVisibility>('shareable')
  const [tags, setTags] = useState('')
  const [busy, setBusy] = useState(false)
  const maxFile = typeof limits.max_file_bytes === 'number' ? limits.max_file_bytes : 0

  const submit = async () => {
    if (!file) return
    if (maxFile && file.size > maxFile) { onError(t('operations.knowledgeDocs.tooBig', 'El archivo supera el límite permitido.')); return }
    const fd = new FormData()
    fd.append('file', file)
    fd.append('title', title.trim())
    fd.append('scope', scope)
    fd.append('visibility', visibility)
    fd.append('tags', tags.trim())
    if (scope === 'user') fd.append('user_email', userEmail.trim())
    setBusy(true)
    try {
      const r = await apiFetch('/api/agent/knowledge/docs', { method: 'POST', body: fd })
      if (!r.ok) { const b = await r.json().catch(() => null); throw new Error(b?.detail || `HTTP ${r.status}`) }
      onDone()
    } catch (e: any) {
      onError(String(e?.message ?? e))
    } finally { setBusy(false) }
  }

  const canSave = !!file && (scope !== 'user' || userEmail.trim())
  return (
    <Dialog open onClose={busy ? undefined : onClose} maxWidth="sm" fullWidth>
      <DialogTitle>{t('operations.knowledgeDocs.add', 'Subir documento')}</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 0.5 }}>
          <Button variant="outlined" startIcon={<UploadIcon />} onClick={() => fileRef.current?.click()}
            sx={{ textTransform: 'none', justifyContent: 'flex-start' }}>
            {file ? file.name : t('operations.knowledgeDocs.choose', 'Elegir archivo (PDF, Word, Excel, texto…)')}
          </Button>
          <input ref={fileRef} type="file" accept={ACCEPT} hidden
            onChange={(e) => { const f = e.target.files?.[0] || null; setFile(f); if (f && !title) setTitle(f.name.replace(/\.[^.]+$/, '')) }} />

          <TextField label={t('operations.knowledgeDocs.titleField', 'Título')} size="small" value={title}
            onChange={(e) => setTitle(e.target.value)} fullWidth />

          <Stack direction="row" spacing={1}>
            <Select size="small" value={scope} onChange={(e) => setScope(e.target.value as DocScope)} sx={{ minWidth: 170 }}>
              <MenuItem value="all">{t('operations.knowledgeDocs.scope.all', 'En todos los casos')}</MenuItem>
              <MenuItem value="attending">{t('operations.knowledgeDocs.scope.attending', 'Al atender a personas')}</MenuItem>
              <MenuItem value="autonomous">{t('operations.knowledgeDocs.scope.autonomous', 'Solo tareas autónomas')}</MenuItem>
              <MenuItem value="user">{t('operations.knowledgeDocs.scope.user', 'Solo una persona')}</MenuItem>
            </Select>
            {scope === 'user' && (
              <TextField size="small" placeholder="email@empresa.com" value={userEmail}
                onChange={(e) => setUserEmail(e.target.value)} sx={{ flex: 1 }} />
            )}
          </Stack>

          <Stack direction="row" spacing={1} alignItems="center">
            <Select size="small" value={visibility} onChange={(e) => setVisibility(e.target.value as DocVisibility)} sx={{ minWidth: 170 }}>
              <MenuItem value="shareable">{t('operations.knowledgeDocs.vis.shareable', 'Compartible')}</MenuItem>
              <MenuItem value="internal">{t('operations.knowledgeDocs.vis.internal', 'Interno')}</MenuItem>
            </Select>
            <Typography variant="caption" color="text.secondary">
              {visibility === 'internal'
                ? t('operations.knowledgeDocs.vis.internalTip', 'Interno: el empleado lo usa para razonar, pero NO lo cita ni reenvía a personas.')
                : t('operations.knowledgeDocs.vis.shareableTip', 'Compartible: el empleado puede citarlo y reproducirlo a personas.')}
            </Typography>
          </Stack>

          <TextField label={t('operations.knowledgeDocs.tags', 'Etiquetas (opcional, separadas por comas)')} size="small"
            value={tags} onChange={(e) => setTags(e.target.value)} fullWidth
            placeholder={t('operations.knowledgeDocs.tagsHint', 'p.ej. tarifas, devoluciones')} />
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} disabled={busy}>{t('common.cancel', 'Cancelar')}</Button>
        <Button variant="contained" onClick={submit} disabled={!canSave || busy}
          startIcon={busy ? <CircularProgress size={16} /> : <UploadIcon />}>
          {t('operations.knowledgeDocs.upload', 'Subir')}
        </Button>
      </DialogActions>
    </Dialog>
  )
}

function PreviewDialog({ doc, onClose }: { doc: KDoc; onClose: () => void }) {
  const { t } = useTranslation()
  const q = useQuery({
    queryKey: ['knowledge-doc-preview', doc.id],
    queryFn: () => apiFetch(`/api/agent/knowledge/docs/${doc.id}/preview`).then((r) => r.json()),
  })
  return (
    <Dialog open onClose={onClose} maxWidth="md" fullWidth>
      <DialogTitle>{doc.title}</DialogTitle>
      <DialogContent dividers>
        {q.isLoading ? <CircularProgress size={20} /> : (
          <Typography variant="body2" component="pre" sx={{ whiteSpace: 'pre-wrap', fontFamily: 'inherit' }}>
            {q.data?.preview || q.data?.summary || t('operations.knowledgeDocs.noPreview', 'Sin extracto disponible.')}
          </Typography>
        )}
      </DialogContent>
      <DialogActions><Button onClick={onClose}>{t('common.close', 'Cerrar')}</Button></DialogActions>
    </Dialog>
  )
}

// ── sensitive topics ──────────────────────────────────────────────────
function SensitiveTopicsCard({ isAdmin, onError }: { isAdmin: boolean; onError: (m: string) => void }) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const q = useQuery({
    queryKey: ['knowledge-settings'],
    queryFn: () => apiFetch('/api/agent/knowledge/settings').then((r) => r.json()),
    staleTime: 30_000,
  })
  const server: string[] = q.data?.sensitive_topics ?? []
  const [draft, setDraft] = useState<string | null>(null)
  const value = draft ?? server.join(', ')
  const save = useMutation({
    mutationFn: async () => {
      const r = await apiFetch('/api/agent/knowledge/settings', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sensitive_topics: value }),
      })
      if (!r.ok) { const b = await r.json().catch(() => null); throw new Error(b?.detail || `HTTP ${r.status}`) }
    },
    onSuccess: () => { setDraft(null); qc.invalidateQueries({ queryKey: ['knowledge-settings'] }) },
    onError: (e: any) => onError(String(e?.message ?? e)),
  })
  const dirty = draft !== null && draft !== server.join(', ')
  return (
    <Card variant="outlined" sx={{ mt: 1 }}>
      <CardContent sx={{ py: 1.5, '&:last-child': { pb: 1.5 } }}>
        <Typography variant="subtitle2" sx={{ fontWeight: 600 }}>
          {t('operations.knowledgeDocs.sensitive.title', 'Temas delicados')}
        </Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
          {t('operations.knowledgeDocs.sensitive.intro', 'Temas sobre los que el empleado NO debe comprometerse por su cuenta (prometer, conceder, aplicar). Si alguien le pide algo sobre estos temas, lo pasará por aprobación antes de confirmarlo.')}
        </Typography>
        {isAdmin ? (
          <Stack direction="row" spacing={1} alignItems="flex-start">
            <TextField size="small" fullWidth multiline minRows={1} value={value}
              onChange={(e) => setDraft(e.target.value)}
              placeholder={t('operations.knowledgeDocs.sensitive.hint', 'p.ej. descuentos, devoluciones, plazos de entrega, condiciones legales')} />
            <Button variant="contained" size="small" disabled={!dirty || save.isPending}
              onClick={() => save.mutate()} sx={{ mt: 0.5 }}>
              {t('common.save', 'Guardar')}
            </Button>
          </Stack>
        ) : (
          <Stack direction="row" spacing={0.5} sx={{ flexWrap: 'wrap', gap: 0.5 }}>
            {server.length ? server.map((s) => <Chip key={s} size="small" label={s} />)
              : <Typography variant="caption" color="text.secondary">{t('operations.knowledgeDocs.sensitive.none', 'Ninguno configurado.')}</Typography>}
          </Stack>
        )}
      </CardContent>
    </Card>
  )
}
