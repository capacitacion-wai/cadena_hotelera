/**
 * Encargo detail — the full view of ONE delegated task / calendar item, reached
 * from the «→ Encargo #X» links on alerts / recommendations / decisions and from
 * the Encargos board. Beyond the status + outcome it shows a link BACK to the
 * signal it was created from, closing the round-trip that was previously a dead
 * end (the link used to drop the operator on the list, not the item).
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
import ArtifactList, { Artifact } from '../components/ArtifactList'
import { apiClient } from '../services/apiClient'
import { formatDateTime } from '../utils/datetime'
import { EntityLink } from '../surfaces/common/primitives'

interface Encargo {
  id: number
  title: string
  description: string
  kind: string
  assignee: string
  origin: string
  status: string
  priority: string
  due_at: string | null
  fired_at: string | null
  completed_at: string | null
  result_md: string | null
  last_run_id: number | null
  created_by: string | null
  assigned_to: string | null
  created_at: string | null
  artifacts?: Artifact[]
  source?: { entity?: string; table?: string; pk?: string | number; action?: string } | null
}

const PRIORITY_TONE: Record<string, 'error' | 'warning' | 'info' | 'default'> = {
  urgent: 'error', high: 'warning', medium: 'info', low: 'default',
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

export default function EncargoDetail() {
  const { id } = useParams<{ id: string }>()
  const { t } = useTranslation()
  const navigate = useNavigate()
  const { data, isLoading, isError } = useQuery({
    queryKey: ['encargo', id],
    queryFn: () => apiClient.get<Encargo>(`/api/agent/tasks/${id}`),
    enabled: !!id,
  })

  return (
    <Box>
      <PageHeader
        title={data?.title || t('encargoDetail.title', 'Encargo')}
        subtitle={`${t('encargoDetail.subtitle', 'Encargo del empleado')} · #${id}`}
        actions={
          <Button startIcon={<ArrowBackIcon />} onClick={() => navigate('/encargos')}>
            {t('common.back', 'Volver')}
          </Button>
        }
      />
      {isLoading && <LoadingRegion />}
      {isError && <Alert severity="error">{t('encargoDetail.notFound', 'No se encontró el encargo.')}</Alert>}
      {data && (
        <Stack spacing={2}>
          <Card variant="outlined">
            <CardContent>
              <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap sx={{ mb: 1 }}>
                <StatusChip status={data.status} />
                {data.priority && (
                  <Chip size="small" color={PRIORITY_TONE[data.priority] ?? 'default'}
                    label={t(`states.${data.priority}`, data.priority)} />
                )}
              </Stack>
              {data.description && (
                <Typography variant="body1" sx={{ whiteSpace: 'pre-wrap', mb: 1 }}>{data.description}</Typography>
              )}
              <Divider sx={{ my: 1 }} />
              <Row label={t('fields.due_at', 'Vence')}>{data.due_at ? formatDateTime(data.due_at) : '—'}</Row>
              <Row label={t('encargoDetail.assignee', 'Asignado a')}>
                {data.assigned_to || (data.assignee === 'agent' ? t('encargoDetail.theEmployee', 'El empleado') : data.assignee)}
              </Row>
              <Row label={t('encargoDetail.createdBy', 'Creado por')}>{data.created_by || '—'}</Row>
              <Row label={t('fields.created_at', 'Creado')}>{data.created_at ? formatDateTime(data.created_at) : '—'}</Row>
              <Row label={t('encargoDetail.firedAt', 'Lanzado')}>{data.fired_at ? formatDateTime(data.fired_at) : '—'}</Row>
              <Row label={t('encargoDetail.completedAt', 'Completado')}>{data.completed_at ? formatDateTime(data.completed_at) : '—'}</Row>
            </CardContent>
          </Card>

          {(data.source?.table || data.last_run_id) && (
            <Card variant="outlined">
              <CardContent>
                <Typography variant="overline" color="text.secondary">{t('encargoDetail.related', 'Relacionado')}</Typography>
                <Stack direction="row" spacing={2} flexWrap="wrap" useFlexGap sx={{ mt: 0.5 }}>
                  {data.source?.table && data.source?.pk != null && (
                    <EntityLink kind={String(data.source.table)} id={data.source.pk}
                      label={t('encargoDetail.fromSource', {
                        kind: String(data.source.table), id: data.source.pk,
                        defaultValue: `← Origen: ${data.source.table} #${data.source.pk}`,
                      })} />
                  )}
                  {data.last_run_id && <EntityLink runId={data.last_run_id} label={t('encargoDetail.viewRun', 'Ver ejecución')} />}
                </Stack>
              </CardContent>
            </Card>
          )}

          {data.result_md && (
            <Card variant="outlined">
              <CardContent>
                <Typography variant="overline" color="text.secondary">{t('encargoDetail.result', 'Resultado')}</Typography>
                <Typography variant="body1" sx={{ whiteSpace: 'pre-wrap', mt: 0.5 }}>{data.result_md}</Typography>
              </CardContent>
            </Card>
          )}

          {data.artifacts && data.artifacts.length > 0 && (
            <Card variant="outlined">
              <CardContent>
                <Typography variant="overline" color="text.secondary">{t('encargoDetail.artifacts', 'Archivos generados')}</Typography>
                <Box sx={{ mt: 0.5 }}><ArtifactList artifacts={data.artifacts} /></Box>
              </CardContent>
            </Card>
          )}
        </Stack>
      )}
    </Box>
  )
}
