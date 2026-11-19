/**
 * Small chip for event/run/watcher statuses.
 *
 * Uses STATUS_COLORS (semantic traffic-light palette) when the
 * status code maps to ok/warn/error/info/neutral. Unknown statuses
 * fall back to text.secondary grey.
 *
 * Extracted verbatim from patterns/operations-page.md.
 */
import { Chip, alpha } from '@mui/material'
import { useTranslation } from 'react-i18next'
import { STATUS_COLORS } from '../theme/chart-colors'

const STATUS_TO_SEMANTIC: Record<string, keyof typeof STATUS_COLORS> = {
  PENDING: 'info',
  CLAIMED: 'info',
  RUNNING: 'info',
  COMPLETED: 'ok',
  SUCCESS: 'ok',
  FAILED: 'error',
  ERROR: 'error',
  CANCELLED: 'neutral',
  RETRY_SCHEDULED: 'warn',
  PARTIAL: 'warn',
  // ScheduledItem (Encargos / Calendar) lifecycle (stored lowercase).
  SCHEDULED: 'info',
  IN_PROGRESS: 'info',
  DONE: 'ok',
  OVERDUE: 'warn',
  // Channel + channel_message lifecycles (stored lowercase; the
  // lookup uppercases). Without these every channel chip rendered
  // the grey fallback.
  ACTIVE: 'ok',
  UNAUTHORIZED: 'warn',
  PROCESSING: 'info',
  REPLIED: 'ok',
  SKIPPED: 'neutral',
  FILTERED: 'neutral',
}

const STATUS_I18N_KEYS: Record<string, string> = {
  PENDING: 'status.pending',
  CLAIMED: 'status.claimed',
  RUNNING: 'status.running',
  COMPLETED: 'status.completed',
  SUCCESS: 'status.success',
  FAILED: 'status.failed',
  ERROR: 'status.error',
  CANCELLED: 'status.cancelled',
  RETRY_SCHEDULED: 'status.retry_scheduled',
  PARTIAL: 'status.partial',
  SCHEDULED: 'status.scheduled',
  IN_PROGRESS: 'status.in_progress',
  DONE: 'status.done',
  OVERDUE: 'status.overdue',
  ACTIVE: 'status.active',
  UNAUTHORIZED: 'status.unauthorized',
  PROCESSING: 'status.processing',
  REPLIED: 'status.replied',
  SKIPPED: 'status.skipped',
  FILTERED: 'status.filtered',
}

export default function StatusChip({ status }: { status: string }) {
  const { t } = useTranslation()
  const key = (status ?? '').toUpperCase()
  const semantic = STATUS_TO_SEMANTIC[key]
  const color = semantic ? STATUS_COLORS[semantic] : STATUS_COLORS.neutral
  const i18nKey = STATUS_I18N_KEYS[key]
  const label = i18nKey ? t(i18nKey) : status
  return (
    <Chip
      size="small"
      label={label}
      sx={{ bgcolor: alpha(color, 0.12), color, fontWeight: 700 }}
    />
  )
}
