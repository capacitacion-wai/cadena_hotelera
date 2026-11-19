/**
 * Approval presentation — shared by the Approvals board (expanded accordion) and
 * the standalone Approval detail page so BOTH read the same way.
 *
 * Two pieces the operator actually needs:
 *   • `ApprovalSummary` — the employee's request rendered as real MARKDOWN
 *     (headings, bold, lists) instead of a raw `## Pasos` pre-wrap dump.
 *   • `ApprovalActionDetail` — the parked action in BUSINESS language (which
 *     connector, which automation, who triggered it) instead of a JSON blob.
 *     The raw JSON is still one click away under "Ver datos técnicos" for audit,
 *     so nothing is lost — it's just no longer the default face of an approval.
 *
 * Kept an INVARIANT (see verify_generated_app.sh): the shape is generic, driven
 * only by `action_type` + `action_payload`, never by a specific domain.
 */
import { useState } from 'react'
import { Box, Chip, Collapse, Link, Stack, Typography } from '@mui/material'
import ExpandMoreIcon from '@mui/icons-material/ExpandMore'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeSanitize from 'rehype-sanitize'
import { useTranslation } from 'react-i18next'
import '../styles/markdown.css'

/** The employee's request, as rendered markdown. */
export function ApprovalSummary({ md }: { md?: string | null }) {
  if (!md || !md.trim()) return null
  return (
    <Box className="markdown-body" sx={{ fontSize: '0.9rem', wordBreak: 'break-word', mb: 1 }}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeSanitize]}>{md}</ReactMarkdown>
    </Box>
  )
}

/** One labelled fact line (label · value). Skips empty values. */
function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  if (children == null || children === '' || children === '—') return null
  return (
    <Stack direction="row" spacing={1} sx={{ py: 0.25 }}>
      <Typography variant="caption" color="text.secondary" sx={{ minWidth: 120, flexShrink: 0 }}>{label}</Typography>
      <Typography variant="caption" sx={{ fontWeight: 600, wordBreak: 'break-word', whiteSpace: 'pre-wrap' }} component="div">{children}</Typography>
    </Stack>
  )
}

// Technical keys we never surface as a "fact" (they belong to the raw JSON only).
const NOISE_KEYS = new Set(['bucket', 'automation_id', 'request_ref', 'idempotency_key', 'context'])

function humanKey(k: string): string {
  return k.replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase())
}
function humanValue(v: unknown): string {
  if (v == null) return ''
  if (Array.isArray(v)) return v.map((x) => String(x)).join(', ')
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

/**
 * Business-readable rendering of an approval's parked action, discriminated by
 * `action_type`. Always followed by a collapsed raw-JSON escape hatch.
 */
/** One tool the platform will run on approval, with the exact arguments it will send. */
export interface ParkedCall { tool: string; arguments: Record<string, unknown> }

export function ApprovalActionDetail({ actionType, payload, request }: {
  actionType?: string | null
  payload?: Record<string, unknown> | null
  /** What will actually be sent (the backend reads it from the parked request). Without it
   *  people approved blind: the text of a reply, the comment it answers, were nowhere. */
  request?: ParkedCall[] | null
}) {
  const { t } = useTranslation()
  const [showRaw, setShowRaw] = useState(false)
  const p = payload || {}
  const hasPayload = Object.keys(p).length > 0

  // Per-type readable facts. Unknown types fall back to a generic key/value list
  // of the non-technical fields.
  let facts: React.ReactNode = null
  if (actionType === 'tool_call') {
    const tools = Array.isArray((p as any).tools) ? (p as any).tools : []
    facts = (
      <>
        <Fact label={t('approvals.detail.connector', 'Conector')}>
          <Chip size="small" label={String((p as any).connector ?? '—')} />
        </Fact>
        {request && request.length ? request.map((c, i) => (
          <Box key={i} sx={{ mt: 0.75, pl: 1, borderLeft: 2, borderColor: 'divider' }}>
            <Typography variant="caption" color="text.secondary" sx={{ display: 'block' }}>
              {t('approvals.detail.willSend', 'Lo que se enviará')} · <code>{c.tool}</code>
            </Typography>
            {Object.entries(c.arguments || {}).map(([k, v]) => (
              <Fact key={k} label={humanKey(k)}>{typeof v === 'string' ? v : JSON.stringify(v, null, 2)}</Fact>
            ))}
          </Box>
        )) : (
          <Fact label={t('approvals.detail.actions', 'Acciones')}>
            {tools.length ? tools.join(', ') : '—'}
          </Fact>
        )}
        <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5 }}>
          {t('approvals.detail.platformNote', 'Al aprobar, la plataforma ejecuta exactamente esta petición.')}
        </Typography>
      </>
    )
  } else if (actionType === 'automation_reaction') {
    const ctx = ((p as any).context || {}) as Record<string, unknown>
    // v7.63.0 check-before-approve: the employee VERIFIED the condition (read-only)
    // BEFORE this approval — surface the evidence so the operator decides informed.
    const preChecked = ctx.precheck_holds === true
    const evidence = String(ctx.check_evidence ?? '').trim()
    const unconfirmed = ctx.check_unconfirmed === true
    facts = (
      <>
        <Fact label={t('approvals.detail.automation', 'Automatización')}>{String((p as any).title ?? '—')}</Fact>
        {ctx.manual ? (
          <Fact label={t('approvals.detail.triggeredBy', 'Disparada por')}>{String(ctx.triggered_by ?? '—')}</Fact>
        ) : (
          <Fact label={t('approvals.detail.trigger', 'Disparo')}>{t('approvals.detail.triggerAuto', 'Automático (señal de la automatización)')}</Fact>
        )}
        {preChecked && (
          <Fact label={t('approvals.detail.preCheck', 'Comprobación previa')}>
            <Chip size="small" color={unconfirmed ? 'warning' : 'success'} variant="outlined"
              label={unconfirmed
                ? t('approvals.detail.preCheckUnconfirmed', 'ejecutada — sin confirmar')
                : t('approvals.detail.preCheckOk', 'condición verificada')} />
          </Fact>
        )}
        {preChecked && evidence && (
          <Fact label={t('approvals.detail.evidence', 'Evidencia')}>{evidence}</Fact>
        )}
        <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5 }}>
          {preChecked
            ? t('approvals.detail.employeeNotePreChecked', 'El empleado ya comprobó que la condición se cumple. Al aprobar, ejecutará la automatización (incluidas las escrituras que implique).')
            : t('approvals.detail.employeeNote', 'Al aprobar, el empleado ejecuta la automatización (incluidas las escrituras que implique).')}
        </Typography>
      </>
    )
  } else if (hasPayload) {
    const rows = Object.entries(p).filter(([k]) => !NOISE_KEYS.has(k))
    facts = rows.length ? (
      <>{rows.map(([k, v]) => <Fact key={k} label={humanKey(k)}>{humanValue(v)}</Fact>)}</>
    ) : null
  }

  if (!facts && !hasPayload) return null

  return (
    <Box>
      <Typography variant="overline" color="text.secondary">
        {t('approvals.detail.title', 'Qué se va a hacer')}
      </Typography>
      <Box sx={{ mt: 0.25 }}>{facts}</Box>
      {hasPayload && (
        <Box sx={{ mt: 0.75 }}>
          <Link component="button" type="button" variant="caption" underline="hover"
            onClick={() => setShowRaw((v) => !v)}
            sx={{ display: 'inline-flex', alignItems: 'center', gap: 0.25 }}>
            <ExpandMoreIcon fontSize="inherit"
              sx={{ transform: showRaw ? 'rotate(180deg)' : 'none', transition: 'transform .15s' }} />
            {t('approvals.detail.rawToggle', 'Ver datos técnicos')}
          </Link>
          <Collapse in={showRaw} unmountOnExit>
            <Box component="pre" sx={{
              m: 0, mt: 0.5, p: 1, bgcolor: 'action.hover', borderRadius: 1,
              fontSize: '0.72rem', overflow: 'auto', maxHeight: 200,
            }}>
              {JSON.stringify(p, null, 2)}
            </Box>
          </Collapse>
        </Box>
      )}
    </Box>
  )
}
