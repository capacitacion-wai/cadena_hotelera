/**
 * Approvals — operator go/no-go on consequential employee actions.
 *
 * The employee parks an action (request_approval). The operator approves
 * (→ APPROVAL_GRANTED event → the employee executes it) or rejects with
 * feedback (→ a learned boundary in the affected user's memory).
 *
 * v7.38.0 — the list is now COLLAPSED: each item is an accordion showing just
 * the essentials (title · origin · status · date) and opens to the full detail.
 * A ``?id=N`` deep-link (from the autonomous-work history) opens the right tab
 * and expands that item. Approvals can be DELETED (with a clear "it is lost"
 * warning) and, once executed, link to their execution run.
 */
import { useEffect, useMemo, useState } from 'react'
import { Link as RouterLink, useSearchParams } from 'react-router-dom'
import {
  Accordion, AccordionDetails, AccordionSummary, Alert, Box, Button, Chip, Dialog,
  DialogActions, DialogContent, DialogContentText, DialogTitle, Divider, IconButton,
  Link, Snackbar, Stack, Tab, Tabs, TextField, Tooltip, Typography,
} from '@mui/material'
import FactCheckIcon from '@mui/icons-material/FactCheck'
import CheckCircleIcon from '@mui/icons-material/CheckCircle'
import CancelIcon from '@mui/icons-material/Cancel'
import ExpandMoreIcon from '@mui/icons-material/ExpandMore'
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline'
import { useTranslation } from 'react-i18next'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import PageHeader from '../components/PageHeader'
import EmptyState from '../components/EmptyState'
import StatusChip from '../components/StatusChip'
import LoadingRegion from '../components/LoadingRegion'
import { ApprovalSummary, ApprovalActionDetail, type ParkedCall } from '../components/ApprovalActionDetail'
import { apiClient, apiBase, apiFetch } from '../services/apiClient'
import { formatDateTime } from '../utils/datetime'
import {
  useAutomationProvenance,
  ProvenanceScopeAlert,
  AutomationOriginChip,
  AutomationProvenanceFilter,
} from '../surfaces/common/provenanceScope'

interface Approval {
  id: number
  title: string
  summary_md: string
  action_type: string
  action_payload: Record<string, unknown>
  risk: string
  requested_for: string | null
  status: string
  decided_by: string | null
  decided_at: string | null
  feedback: string | null
  created_by_run_id: number | null
  executed_by_run_id: number | null
  executed_at: string | null
  execution_result: string | null
  request?: ParkedCall[] | null
  created_at: string
  created_by_automation?: { title?: string; via?: string } | null
}

const RISK_COLOR: Record<string, 'success' | 'warning' | 'error'> = {
  low: 'success', medium: 'warning', high: 'error',
}

export default function Approvals() {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const [tab, setTab] = useState(0)
  const [snack, setSnack] = useState<string | null>(null)
  const [dialog, setDialog] = useState<{ a: Approval; mode: 'approve' | 'reject' } | null>(null)
  const [feedback, setFeedback] = useState('')
  const [expanded, setExpanded] = useState<number | false>(false)
  const [toDelete, setToDelete] = useState<Approval | null>(null)
  const [params, setParams] = useSearchParams()
  const focusId = params.get('id')
  const prov = useAutomationProvenance(undefined, 'local', {
    listPath: '/approvals',
    producersUrl: `${apiBase}/approvals/producers`,
  })
  const approvalQuery = useMemo(() => {
    const p = new URLSearchParams()
    if (prov.queryExtras.created_by_automation) {
      p.set('created_by_automation', prov.queryExtras.created_by_automation)
    }
    if (prov.queryExtras.only_open) p.set('only_open', '1')
    const qs = p.toString()
    return qs ? `?${qs}` : ''
  }, [prov.queryExtras.created_by_automation, prov.queryExtras.only_open])

  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['approvals', approvalQuery],
    queryFn: () => apiClient.get<{
      items: Approval[]
      filtered_by_automation?: { id: number; title: string; only_open?: boolean } | null
    }>(`/api/approvals${approvalQuery}`),
    refetchInterval: 8000,
  })
  const provContext = data?.filtered_by_automation ?? null

  // Deciding is a permission of its own (admin OR designated approver, v7.145.0),
  // so we ask instead of inferring it from the admin role.
  const { data: me } = useQuery({
    queryKey: ['users-me'],
    queryFn: () => apiClient.get<{ can_approve?: boolean; is_admin?: boolean }>('/api/agent/users/me'),
    staleTime: 60_000,
  })
  const canDecide = !!me?.can_approve
  const { data: approvers } = useQuery({
    queryKey: ['users-approvers'],
    queryFn: () => apiClient.get<{ items: { email: string; display_name: string | null }[] }>(
      '/api/agent/users/approvers'),
    enabled: !canDecide,
    staleTime: 60_000,
  })
  const approverNames = (approvers?.items ?? [])
    .map((p) => p.display_name || p.email).join(', ')

  const all = data?.items ?? []
  const pending = useMemo(() => all.filter((a) => a.status === 'pending'), [all])
  const history = useMemo(() => all.filter((a) => a.status !== 'pending'), [all])

  // Deep-link (?id=N): open the right tab + expand that approval, once.
  useEffect(() => {
    if (!focusId || all.length === 0) return
    const a = all.find((x) => String(x.id) === focusId)
    if (a) { setTab(a.status === 'pending' ? 0 : 1); setExpanded(a.id) }
  }, [focusId, all])

  const decide = useMutation({
    mutationFn: async ({ id, mode, fb }: { id: number; mode: string; fb: string }) => {
      const r = await apiFetch(`/api/approvals/${id}/${mode}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ feedback: fb }),
      })
      if (!r.ok) throw new Error(await r.text())
      return r.json()
    },
    onSuccess: (_d, vars) => {
      qc.invalidateQueries({ queryKey: ['approvals'] })
      setSnack(vars.mode === 'approve' ? t('approvals.approved') : t('approvals.rejected'))
      setDialog(null); setFeedback('')
    },
  })

  const remove = useMutation({
    mutationFn: async (id: number) => {
      const r = await apiFetch(`/api/approvals/${id}`, { method: 'DELETE' })
      if (!r.ok) throw new Error(await r.text())
      return r.json()
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['approvals'] })
      setSnack(t('approvals.deleted', 'Aprobación eliminada'))
      setToDelete(null)
    },
  })

  const list = tab === 0 ? pending : history

  const originLabel = (a: Approval) =>
    a.action_type === 'tool_call'
      ? t('approvals.origin.connector', 'Escritura en {{connector}}', { connector: String((a.action_payload as any)?.connector ?? '—') })
      : a.action_type === 'automation_reaction'
        ? t('approvals.origin.automation', 'Automatización')
        : t('approvals.origin.judgment', 'Criterio del empleado')

  return (
    <Box>
      <PageHeader icon={<FactCheckIcon />} title={t('approvals.title')} subtitle={t('approvals.subtitle')}
        actions={
          <AutomationProvenanceFilter
            value={prov.provAutomation}
            producers={prov.producers}
            onChange={prov.setProvenance}
          />
        }
      />

      <ProvenanceScopeAlert ctx={provContext} onClear={prov.clearProvenance} />

      <Tabs value={tab} onChange={(_e, v) => { setTab(v); setExpanded(false) }} sx={{ mb: 2 }}>
        <Tab label={`${t('approvals.pending')} (${pending.length})`} sx={{ textTransform: 'none' }} />
        <Tab label={`${t('approvals.history')} (${history.length})`} sx={{ textTransform: 'none' }} />
      </Tabs>

      {isLoading ? (
        <LoadingRegion />
      ) : isError ? (
        <EmptyState variant="error" title={t('common.error')}
                    actionLabel={t('common.retry')} onAction={() => refetch()} />
      ) : list.length === 0 ? (
        <EmptyState title={t('approvals.empty')} body={tab === 0 ? t('approvals.emptyBody') : undefined} />
      ) : (
        <Stack spacing={1}>
          {list.map((a) => (
            <Accordion key={a.id} disableGutters variant="outlined"
              expanded={expanded === a.id}
              onChange={(_e, isExp) => {
                setExpanded(isExp ? a.id : false)
                if (focusId) {
                  const next = new URLSearchParams(params)
                  next.delete('id')
                  setParams(next, { replace: true })
                }
              }}
              sx={{ borderRadius: 2, '&:before': { display: 'none' } }}>
              <AccordionSummary expandIcon={<ExpandMoreIcon />}>
                {/* Collapsed line: title · origin · status · date */}
                <Stack direction="row" spacing={1} alignItems="center" sx={{ width: '100%', pr: 1, flexWrap: 'wrap' }} useFlexGap>
                  <Typography variant="subtitle2" sx={{ fontWeight: 700, flex: 1, minWidth: 0 }} noWrap>
                    {a.title}
                  </Typography>
                  <Chip size="small" variant="outlined" label={originLabel(a)} />
                  <AutomationOriginChip origin={a.created_by_automation} />
                  {a.status === 'pending'
                    ? <Chip size="small" color="warning" label={t('approvals.pending', 'Pendiente')} />
                    : <StatusChip status={a.status} />}
                  <Typography variant="caption" color="text.secondary">
                    {formatDateTime(a.created_at)}
                  </Typography>
                </Stack>
              </AccordionSummary>
              <AccordionDetails>
                <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1, flexWrap: 'wrap' }} useFlexGap>
                  <Chip size="small" color={RISK_COLOR[a.risk] ?? 'default'} variant="outlined"
                        label={`${t('approvals.risk')}: ${t(`approvals.risks.${a.risk}`, { defaultValue: a.risk })}`} />
                  {a.requested_for && (
                    <Typography variant="caption" color="text.secondary">
                      {t('approvals.requestedFor')}: {a.requested_for}
                    </Typography>
                  )}
                </Stack>

                <ApprovalSummary md={a.summary_md} />
                <Typography variant="caption" color="text.secondary" display="block" sx={{ mb: 1 }}>
                  {a.action_type === 'tool_call'
                    ? t('approvals.semantics.platform', 'Al aprobar, la plataforma ejecuta esta acción por ti.')
                    : t('approvals.semantics.employee', 'Al aprobar, el empleado la ejecuta.')}
                </Typography>

                {a.status !== 'pending' && (a.decided_by || a.feedback) && (
                  <>
                    <Divider sx={{ my: 1 }} />
                    <Typography variant="caption" color="text.secondary">
                      {a.decided_by ? `${t('approvals.decidedBy')}: ${a.decided_by} · ${formatDateTime(a.decided_at)}` : ''}
                    </Typography>
                    {a.feedback && (
                      <Typography variant="body2" sx={{ mt: 0.5, fontStyle: 'italic' }}>“{a.feedback}”</Typography>
                    )}
                  </>
                )}

                {/* Standalone detail page (same view a `Ver aprobación #X` link opens). */}
                <Box sx={{ mt: 1 }}>
                  <Link component={RouterLink} to={`/approvals/${a.id}`} variant="body2">
                    {t('approvals.openDetail', 'Abrir ficha completa →')}
                  </Link>
                </Box>

                {/* Execution link — the run that actually carried out the approved action. */}
                {a.executed_by_run_id && (
                  <Box sx={{ mt: 1 }}>
                    <Link component={RouterLink} to={`/runs/${a.executed_by_run_id}`} variant="body2">
                      {t('approvals.viewExecution', 'Ver ejecución →')}
                    </Link>
                    {a.execution_result && (
                      <Typography variant="caption" color="text.secondary" display="block" sx={{ mt: 0.25 }}>
                        {a.execution_result}
                      </Typography>
                    )}
                  </Box>
                )}

                <Box sx={{ mt: 1 }}>
                  <ApprovalActionDetail actionType={a.action_type} payload={a.action_payload} request={a.request} />
                </Box>

                <Stack direction="row" spacing={1} sx={{ mt: 2 }} alignItems="center">
                  {a.status === 'pending' && (canDecide ? (
                    <>
                      <Button variant="contained" color="success" startIcon={<CheckCircleIcon />} sx={{ textTransform: 'none' }}
                              onClick={() => { setFeedback(''); setDialog({ a, mode: 'approve' }) }}>
                        {t('approvals.approve')}
                      </Button>
                      <Button variant="outlined" color="error" startIcon={<CancelIcon />} sx={{ textTransform: 'none' }}
                              onClick={() => { setFeedback(''); setDialog({ a, mode: 'reject' }) }}>
                        {t('approvals.reject')}
                      </Button>
                    </>
                  ) : (
                    // Without permission the buttons do not fail: we say WHO decides, which is
                    // what is needed so the action is not left hanging.
                    <Alert severity="info" sx={{ flex: 1 }}>
                      {approverNames
                        ? t('approvals.askThem', 'No puedes decidir esta acción. Pídeselo a: {{who}}', { who: approverNames })
                        : t('approvals.askAdmin', 'No puedes decidir esta acción; solo un administrador o un aprobador designado puede.')}
                    </Alert>
                  ))}
                  <Box sx={{ flex: 1 }} />
                  {/* Deleting the record is maintenance, not the decision: admin only. */}
                  {me?.is_admin && (
                    <Tooltip title={t('approvals.delete', 'Eliminar') as string}>
                      <IconButton size="small" color="error" onClick={() => setToDelete(a)}>
                        <DeleteOutlineIcon fontSize="small" />
                      </IconButton>
                    </Tooltip>
                  )}
                </Stack>
              </AccordionDetails>
            </Accordion>
          ))}
        </Stack>
      )}

      {/* approve / reject dialog */}
      <Dialog open={!!dialog} onClose={() => setDialog(null)} maxWidth="sm" fullWidth>
        <DialogTitle>
          {dialog?.mode === 'approve' ? t('approvals.approveTitle') : t('approvals.rejectTitle')}
        </DialogTitle>
        <DialogContent>
          <DialogContentText sx={{ mb: 2 }}>{dialog?.a.title}</DialogContentText>
          <TextField
            label={t('approvals.feedback')} value={feedback} multiline minRows={2} fullWidth autoFocus
            required={dialog?.mode === 'reject'}
            placeholder={(dialog?.mode === 'approve'
              ? t('approvals.feedbackPhApprove') : t('approvals.feedbackPhReject')) as string}
            onChange={(e) => setFeedback(e.target.value)}
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDialog(null)} sx={{ textTransform: 'none' }}>{t('common.cancel')}</Button>
          <Button variant="contained" color={dialog?.mode === 'approve' ? 'success' : 'error'}
            disabled={decide.isPending || (dialog?.mode === 'reject' && !feedback.trim())}
            onClick={() => dialog && decide.mutate({ id: dialog.a.id, mode: dialog.mode, fb: feedback })}
            sx={{ textTransform: 'none' }}>
            {dialog?.mode === 'approve' ? t('approvals.approve') : t('approvals.reject')}
          </Button>
        </DialogActions>
      </Dialog>

      {/* delete dialog — with the "it is lost" warning */}
      <Dialog open={!!toDelete} onClose={() => { if (!remove.isPending) setToDelete(null) }} maxWidth="xs" fullWidth>
        <DialogTitle sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
          <DeleteOutlineIcon color="error" fontSize="small" />
          {t('approvals.deleteTitle', 'Eliminar aprobación')}
        </DialogTitle>
        <DialogContent>
          <DialogContentText>
            {toDelete?.status === 'pending'
              ? t('approvals.deleteWarnPending', 'Si la eliminas se PIERDE: la acción aparcada NO se ejecutará. Si quieres que se haga, apruébala en su lugar. No se puede deshacer.')
              : t('approvals.deleteWarn', 'Si la eliminas se pierde este registro del historial. No se puede deshacer.')}
          </DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setToDelete(null)} disabled={remove.isPending}>{t('common.cancel')}</Button>
          <Button color="error" variant="contained" disabled={remove.isPending}
            onClick={() => toDelete && remove.mutate(toDelete.id)}>
            {t('approvals.delete', 'Eliminar')}
          </Button>
        </DialogActions>
      </Dialog>

      <Snackbar open={!!snack} autoHideDuration={4000} onClose={() => setSnack(null)}
                anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}>
        <Alert severity="success" variant="filled" onClose={() => setSnack(null)}>{snack}</Alert>
      </Snackbar>
    </Box>
  )
}
