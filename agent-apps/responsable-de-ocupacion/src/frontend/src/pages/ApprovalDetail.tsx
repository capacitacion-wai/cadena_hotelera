/**
 * Approval detail — the full view of ONE approval request, reached from a
 * «Ver aprobación #X» link or the Approvals list. Shows what the employee wants
 * to do and why, the risk, the decision, and — when executed — a link to the run
 * that carried it out. Read-only: approve/reject stay on the Approvals board.
 */
import { ReactNode } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  Alert, Box, Button, Card, CardContent, Chip, Divider, Stack, Typography,
} from '@mui/material'
import ArrowBackIcon from '@mui/icons-material/ArrowBack'
import { useTranslation } from 'react-i18next'
import PageHeader from '../components/PageHeader'
import StatusChip from '../components/StatusChip'
import LoadingRegion from '../components/LoadingRegion'
import { apiClient } from '../services/apiClient'
import { formatDateTime } from '../utils/datetime'
import { EntityLink } from '../surfaces/common/primitives'
import { ApprovalSummary, ApprovalActionDetail, type ParkedCall } from '../components/ApprovalActionDetail'

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
  created_at: string | null
}

const RISK_TONE: Record<string, 'error' | 'warning' | 'success' | 'default'> = {
  high: 'error', medium: 'warning', low: 'success',
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  if (children == null || children === '' || children === '—') return null
  return (
    <Stack direction="row" spacing={1.5} sx={{ py: 0.5 }}>
      <Typography variant="body2" color="text.secondary" sx={{ minWidth: 150, flexShrink: 0 }}>{label}</Typography>
      <Typography variant="body2" component="div" sx={{ fontWeight: 500, wordBreak: 'break-word' }}>{children}</Typography>
    </Stack>
  )
}

export default function ApprovalDetail() {
  const { id } = useParams<{ id: string }>()
  const { t } = useTranslation()
  const navigate = useNavigate()
  const { data, isLoading, isError } = useQuery({
    queryKey: ['approval', id],
    queryFn: () => apiClient.get<Approval>(`/api/approvals/${id}`),
    enabled: !!id,
  })

  return (
    <Box>
      <PageHeader
        title={data?.title || t('approvalDetail.title', 'Aprobación')}
        subtitle={`${t('approvalDetail.subtitle', 'Solicitud de aprobación')} · #${id}`}
        actions={
          <Button startIcon={<ArrowBackIcon />} onClick={() => navigate('/approvals')}>
            {t('common.back', 'Volver')}
          </Button>
        }
      />
      {isLoading && <LoadingRegion />}
      {isError && <Alert severity="error">{t('approvalDetail.notFound', 'No se encontró la aprobación.')}</Alert>}
      {data && (
        <Stack spacing={2}>
          <Card variant="outlined">
            <CardContent>
              <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap sx={{ mb: 1 }}>
                <StatusChip status={data.status} />
                {data.risk && (
                  <Chip size="small" color={RISK_TONE[data.risk] ?? 'default'}
                    label={`${t('approvalDetail.risk', 'Riesgo')}: ${t(`approvalDetail.risks.${data.risk}`, data.risk)}`} />
                )}
                {data.action_type && <Chip size="small" variant="outlined" label={data.action_type} />}
              </Stack>
              <ApprovalSummary md={data.summary_md} />
              <Divider sx={{ my: 1 }} />
              <Row label={t('approvalDetail.requestedFor', 'Afecta a')}>{data.requested_for || '—'}</Row>
              <Row label={t('approvalDetail.decidedBy', 'Decidido por')}>{data.decided_by || '—'}</Row>
              <Row label={t('approvalDetail.decidedAt', 'Decidido el')}>{data.decided_at ? formatDateTime(data.decided_at) : '—'}</Row>
              <Row label={t('approvalDetail.feedback', 'Comentario')}>{data.feedback || '—'}</Row>
              <Row label={t('fields.created_at', 'Creado')}>{data.created_at ? formatDateTime(data.created_at) : '—'}</Row>
            </CardContent>
          </Card>

          {(data.created_by_run_id || data.executed_by_run_id) && (
            <Card variant="outlined">
              <CardContent>
                <Typography variant="overline" color="text.secondary">{t('approvalDetail.related', 'Relacionado')}</Typography>
                <Stack direction="row" spacing={2} flexWrap="wrap" useFlexGap sx={{ mt: 0.5 }}>
                  {data.created_by_run_id && <EntityLink runId={data.created_by_run_id} label={t('approvalDetail.viewRequest', 'Ver solicitud (ejecución)')} />}
                  {data.executed_by_run_id && <EntityLink runId={data.executed_by_run_id} label={t('approvalDetail.viewExecution', 'Ver ejecución')} />}
                </Stack>
                {data.execution_result && (
                  <Typography variant="body2" color="text.secondary" sx={{ whiteSpace: 'pre-wrap', mt: 1 }}>
                    <b>{t('approvalDetail.executionResult', 'Resultado de la ejecución')}:</b> {data.execution_result}
                  </Typography>
                )}
              </CardContent>
            </Card>
          )}

          {Object.keys(data.action_payload || {}).length > 0 && (
            <Card variant="outlined">
              <CardContent>
                <ApprovalActionDetail actionType={data.action_type} payload={data.action_payload} request={data.request} />
              </CardContent>
            </Card>
          )}
        </Stack>
      )}
    </Box>
  )
}
