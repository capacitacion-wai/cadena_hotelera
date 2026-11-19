// Shared time-range picker: "1 día / 7 / 30 / 90 / personalizado" (v7.140.0).
//
// Every governance number is meaningless without a window: "ha disparado 4
// veces" is good or terrible depending on whether that is a day or a quarter.
// The pattern was already solved in pages/Runs.tsx; it is extracted here because
// the automation governance panel needs the SAME semantics (a preset the backend
// understands, plus an explicit custom range) and two implementations of "last 7
// days" would eventually disagree.
//
// Deliberately NOT retrofitted into the screens that already have their own
// picker (Actividad, Consumo, Memoria): that would be a wide refactor unrelated
// to this change. New surfaces use this one.
import { Stack, TextField, ToggleButton, ToggleButtonGroup } from '@mui/material'
import { useTranslation } from 'react-i18next'

export type RangePreset = '1d' | '7d' | '30d' | '90d' | 'custom'

export type DateRange = {
  preset: RangePreset
  /** ISO date (YYYY-MM-DD) — only meaningful when preset === 'custom'. */
  since?: string
  until?: string
}

/** Query params for the backend. A preset is sent as ``range``; a custom window
 *  as ``since``/``until`` — which is exactly what ``automation_stats.resolve_range``
 *  expects, so the frontend never computes day boundaries itself (they belong in
 *  business time, which only the backend knows). */
export function rangeParams(r: DateRange): string {
  if (r.preset !== 'custom') return `range=${r.preset}`
  const parts: string[] = []
  if (r.since) parts.push(`since=${encodeURIComponent(r.since)}`)
  if (r.until) parts.push(`until=${encodeURIComponent(r.until)}`)
  return parts.length ? parts.join('&') : 'range=7d'
}

export const DEFAULT_RANGE: DateRange = { preset: '7d' }

export default function DateRangeFilter({ value, onChange, dense = false }: {
  value: DateRange
  onChange: (r: DateRange) => void
  dense?: boolean
}) {
  const { t } = useTranslation()
  return (
    <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} alignItems={{ sm: 'center' }}>
      <ToggleButtonGroup size="small" exclusive value={value.preset}
        onChange={(_, v) => { if (v) onChange({ ...value, preset: v as RangePreset }) }}>
        <ToggleButton value="1d" sx={{ textTransform: 'none' }}>{t('range.d1', '1 día')}</ToggleButton>
        <ToggleButton value="7d" sx={{ textTransform: 'none' }}>{t('range.d7', '7 días')}</ToggleButton>
        <ToggleButton value="30d" sx={{ textTransform: 'none' }}>{t('range.d30', '30 días')}</ToggleButton>
        {!dense && (
          <ToggleButton value="90d" sx={{ textTransform: 'none' }}>{t('range.d90', '90 días')}</ToggleButton>
        )}
        <ToggleButton value="custom" sx={{ textTransform: 'none' }}>{t('range.custom', 'Personalizado')}</ToggleButton>
      </ToggleButtonGroup>
      {value.preset === 'custom' && (
        <Stack direction="row" spacing={1}>
          <TextField size="small" type="date" label={t('range.from', 'Desde')}
            InputLabelProps={{ shrink: true }} value={value.since ?? ''}
            onChange={(e) => onChange({ ...value, since: e.target.value })} />
          <TextField size="small" type="date" label={t('range.to', 'Hasta')}
            InputLabelProps={{ shrink: true }} value={value.until ?? ''}
            onChange={(e) => onChange({ ...value, until: e.target.value })} />
        </Stack>
      )}
    </Stack>
  )
}
