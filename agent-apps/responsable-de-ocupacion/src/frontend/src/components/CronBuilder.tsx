/**
 * CronBuilder — visual editor for the 5-field cron expression
 * (minute hour day-of-month month day-of-week) so operators don't
 * need to memorise cron syntax.
 *
 * UX overview
 * -----------
 * The component is a small state machine over a handful of named
 * "modes":
 *
 *   • ``minutes`` — fire every N minutes (5, 10, 15, 20, 30, custom)
 *   • ``hourly``  — fire once per hour at minute MM
 *   • ``hours``   — fire every N hours at minute MM (default for new
 *                  watchers — 2 h is the sane fallback that avoids
 *                  burning project DB cycles every minute)
 *   • ``daily``   — fire once per day at HH:MM
 *   • ``weekly``  — fire on selected weekdays at HH:MM
 *   • ``monthly`` — fire on day D of each month at HH:MM
 *   • ``advanced``— raw cron expression for power users
 *
 * Each mode owns a tiny cluster of MUI controls (chips for weekdays,
 * a numeric input for "every N", a 24-hour HH:MM picker built from
 * two MenuItem-backed selects). Whenever the user touches a
 * control we recompute the cron expression from scratch via
 * ``buildCron(state)`` and surface it as a copy-friendly chip plus a
 * human-readable sentence (e.g. "Every Monday and Wednesday at
 * 09:30", in the UI language) so the operator can sanity-check what
 * they actually scheduled.
 *
 * Bidirectional contract
 * ----------------------
 * On open we run ``parseCron(initialValue)`` to pick the *least
 * surprising* mode that round-trips to the same expression
 * (a "every 15 minutes" expression maps to minutes/15, a
 * "0 9 * * 1-5" expression maps to weekly Mon-Fri at 09:00).
 * Anything that doesn't match a known shape falls back to
 * ``advanced`` so the user can still see and edit the raw text;
 * we never silently rewrite a custom expression.
 *
 * The component is fully controlled: the parent owns the cron
 * string in its own state and we call ``onChange`` on every edit.
 * That makes the editor trivial to wire into the existing
 * ``ScheduleEditor`` dialog (which already keeps ``cron`` in
 * ``useState``).
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import {
  Box, Chip, MenuItem, Select, Stack, TextField, ToggleButton, ToggleButtonGroup,
  Typography,
} from '@mui/material'

// ─────────────────────────────────────────────────────────────────────
// Types
// ─────────────────────────────────────────────────────────────────────

export type CronMode = 'minutes' | 'hourly' | 'hours' | 'daily' | 'weekly' | 'monthly' | 'advanced'

interface CronState {
  mode: CronMode
  everyMinutes: number        // 1..59 — for ``minutes`` mode
  hourlyMinute: number        // 0..59 — for ``hourly`` mode
  everyHours: number          // 2..23 — for ``hours`` mode
  hoursAtMinute: number       // 0..59 — minute offset for ``hours`` mode
  dailyHour: number           // 0..23
  dailyMinute: number         // 0..59
  weeklyDays: number[]        // each 0..6 (0=Sun, 1=Mon, … 6=Sat)
  weeklyHour: number
  weeklyMinute: number
  monthlyDay: number          // 1..31
  monthlyHour: number
  monthlyMinute: number
  advancedExpr: string
}

// R-CRON-1: ``hours`` mode at every 2h is the default seed for new
// watchers. It used to be ``daily`` at 09:00, which forced operators
// to either accept an unusably-coarse cadence or learn cron syntax
// to bring it down. Two hours is the cadence we recommend in the
// skill patterns for ``monitoring``-flavoured watchers and it
// matches what verifier presets generate for the manifest.
const DEFAULT_STATE: CronState = {
  mode: 'hours',
  everyMinutes: 15,
  hourlyMinute: 0,
  everyHours: 2,
  hoursAtMinute: 0,
  dailyHour: 9,
  dailyMinute: 0,
  weeklyDays: [1, 2, 3, 4, 5],
  weeklyHour: 9,
  weeklyMinute: 0,
  monthlyDay: 1,
  monthlyHour: 9,
  monthlyMinute: 0,
  advancedExpr: '',
}

// ─────────────────────────────────────────────────────────────────────
// build + parse
// ─────────────────────────────────────────────────────────────────────

function pad2(n: number): string { return n < 10 ? `0${n}` : String(n) }

export function buildCron(state: CronState): string {
  switch (state.mode) {
    case 'minutes': {
      const n = clamp(state.everyMinutes, 1, 59)
      return n === 1 ? '* * * * *' : `*/${n} * * * *`
    }
    case 'hourly':
      return `${clamp(state.hourlyMinute, 0, 59)} * * * *`
    case 'hours': {
      const n = clamp(state.everyHours, 2, 23)
      return `${clamp(state.hoursAtMinute, 0, 59)} */${n} * * *`
    }
    case 'daily':
      return `${clamp(state.dailyMinute, 0, 59)} ${clamp(state.dailyHour, 0, 23)} * * *`
    case 'weekly': {
      const days = [...state.weeklyDays].filter((d) => d >= 0 && d <= 6).sort()
      const dayPart = days.length === 7 || days.length === 0 ? '*' : days.join(',')
      return `${clamp(state.weeklyMinute, 0, 59)} ${clamp(state.weeklyHour, 0, 23)} * * ${dayPart}`
    }
    case 'monthly':
      return `${clamp(state.monthlyMinute, 0, 59)} ${clamp(state.monthlyHour, 0, 23)} ${clamp(state.monthlyDay, 1, 31)} * *`
    case 'advanced':
      return (state.advancedExpr || '').trim()
  }
}

function clamp(n: number, lo: number, hi: number): number {
  if (!Number.isFinite(n)) return lo
  return Math.max(lo, Math.min(hi, Math.round(n)))
}

/**
 * Best-effort parse: try each known shape in turn, fall through to
 * the ``advanced`` bucket when nothing matches. Returns a fully
 * populated ``CronState`` so the UI never needs to deal with
 * partially-defined data.
 */
export function parseCron(raw: string): CronState {
  const expr = (raw || '').trim()
  if (!expr) return { ...DEFAULT_STATE, advancedExpr: '' }
  const parts = expr.split(/\s+/)
  if (parts.length !== 5) {
    return { ...DEFAULT_STATE, mode: 'advanced', advancedExpr: expr }
  }
  const [minute, hour, dom, month, dow] = parts

  // every N minutes — */N * * * *  (N ∈ 1..59)
  if (month === '*' && dom === '*' && dow === '*' && hour === '*') {
    if (minute === '*') {
      return { ...DEFAULT_STATE, mode: 'minutes', everyMinutes: 1, advancedExpr: expr }
    }
    const m = /^\*\/(\d{1,2})$/.exec(minute)
    if (m) {
      const n = Number(m[1])
      if (n >= 1 && n <= 59) {
        return { ...DEFAULT_STATE, mode: 'minutes', everyMinutes: n, advancedExpr: expr }
      }
    }
    if (/^\d{1,2}$/.test(minute)) {
      const v = Number(minute)
      if (v >= 0 && v <= 59) {
        return { ...DEFAULT_STATE, mode: 'hourly', hourlyMinute: v, advancedExpr: expr }
      }
    }
  }

  // every N hours — MM */N * * *  (N ∈ 2..23, MM ∈ 0..59)
  // R-CRON-2: order matters — this must run BEFORE the ``daily``
  // branch below, because ``daily`` also matches a numeric minute
  // when hour is a literal number, and ``*/N`` looks like that
  // unless we filter it out here first.
  if (dom === '*' && month === '*' && dow === '*' && /^\d{1,2}$/.test(minute)) {
    const m2 = /^\*\/(\d{1,2})$/.exec(hour)
    if (m2) {
      const n = Number(m2[1])
      const mm = Number(minute)
      if (n >= 2 && n <= 23 && mm >= 0 && mm <= 59) {
        return {
          ...DEFAULT_STATE,
          mode: 'hours',
          everyHours: n,
          hoursAtMinute: mm,
          advancedExpr: expr,
        }
      }
    }
  }

  const numericMinute = /^\d{1,2}$/.test(minute) ? Number(minute) : null
  const numericHour = /^\d{1,2}$/.test(hour) ? Number(hour) : null

  // daily — MM HH * * *
  if (numericMinute != null && numericHour != null && dom === '*' && month === '*' && dow === '*') {
    return {
      ...DEFAULT_STATE, mode: 'daily',
      dailyMinute: numericMinute, dailyHour: numericHour,
      advancedExpr: expr,
    }
  }

  // monthly — MM HH D * *
  if (
    numericMinute != null && numericHour != null
    && /^\d{1,2}$/.test(dom) && month === '*' && dow === '*'
  ) {
    const day = Number(dom)
    if (day >= 1 && day <= 31) {
      return {
        ...DEFAULT_STATE, mode: 'monthly',
        monthlyMinute: numericMinute, monthlyHour: numericHour, monthlyDay: day,
        advancedExpr: expr,
      }
    }
  }

  // weekly — MM HH * * D[,D,…] or MM HH * * D-D
  if (
    numericMinute != null && numericHour != null
    && dom === '*' && month === '*' && dow !== '*'
  ) {
    const days = parseWeekdayList(dow)
    if (days) {
      return {
        ...DEFAULT_STATE, mode: 'weekly',
        weeklyMinute: numericMinute, weeklyHour: numericHour, weeklyDays: days,
        advancedExpr: expr,
      }
    }
  }

  return { ...DEFAULT_STATE, mode: 'advanced', advancedExpr: expr }
}

/** Accept "1,2,3" or "1-5" or single "0" — return a sorted unique list of 0..6 or null. */
function parseWeekdayList(s: string): number[] | null {
  const out: Set<number> = new Set()
  for (const chunk of s.split(',')) {
    const ch = chunk.trim()
    if (!ch) continue
    const range = /^(\d)-(\d)$/.exec(ch)
    if (range) {
      let a = Number(range[1])
      let b = Number(range[2])
      if (a > b) [a, b] = [b, a]
      if (a < 0 || b > 6) return null
      for (let i = a; i <= b; i++) out.add(i)
      continue
    }
    if (/^\d$/.test(ch)) {
      const v = Number(ch)
      if (v >= 0 && v <= 6) { out.add(v); continue }
    }
    return null
  }
  return [...out].sort()
}

// ─────────────────────────────────────────────────────────────────────
// Human-readable summary
// ─────────────────────────────────────────────────────────────────────

export function describeCron(state: CronState, t: any): string {
  switch (state.mode) {
    case 'minutes':
      return state.everyMinutes === 1
        ? t('operations.triggers.cronDesc.everyMinute')
        : t('operations.triggers.cronDesc.everyNMinutes', { count: state.everyMinutes })
    case 'hourly':
      return t('operations.triggers.cronDesc.hourly', { minute: pad2(state.hourlyMinute) })
    case 'hours':
      return t('operations.triggers.cronDesc.everyNHours', {
        count:  state.everyHours,
        minute: pad2(state.hoursAtMinute),
      })
    case 'daily':
      return t('operations.triggers.cronDesc.daily', {
        time: `${pad2(state.dailyHour)}:${pad2(state.dailyMinute)}`,
      })
    case 'weekly': {
      const dayNames = state.weeklyDays.length === 0
        ? t('operations.triggers.cronDesc.noDay')
        : formatDayList(state.weeklyDays, t)
      return t('operations.triggers.cronDesc.weekly', {
        days: dayNames,
        time: `${pad2(state.weeklyHour)}:${pad2(state.weeklyMinute)}`,
      })
    }
    case 'monthly':
      return t('operations.triggers.cronDesc.monthly', {
        day: state.monthlyDay,
        time: `${pad2(state.monthlyHour)}:${pad2(state.monthlyMinute)}`,
      })
    case 'advanced':
      return state.advancedExpr || ''
  }
}

function formatDayList(days: number[], t: any): string {
  if (days.length === 7) return t('operations.triggers.cronDesc.everyDay')
  const sorted = [...days].sort()
  // Detect Mon–Fri = [1,2,3,4,5]
  if (sorted.length === 5 && sorted.join(',') === '1,2,3,4,5') {
    return t('operations.triggers.cronDesc.weekdays')
  }
  if (sorted.length === 2 && sorted.join(',') === '0,6') {
    return t('operations.triggers.cronDesc.weekend')
  }
  return sorted.map((d) => t(`operations.triggers.cronDesc.day_${d}`)).join(', ')
}

// ─────────────────────────────────────────────────────────────────────
// Component
// ─────────────────────────────────────────────────────────────────────

export interface CronBuilderProps {
  value: string
  onChange: (next: string) => void
}

export function CronBuilder({ value, onChange }: CronBuilderProps) {
  const { t } = useTranslation()
  // Initial state comes from the prop; we deliberately do NOT
  // re-parse on every value change, otherwise typing in an
  // intermediate input would trigger a round-trip that resets
  // controls (e.g. "every N minutes" while the user is in the
  // middle of typing "1" → "10"). We only re-sync when the
  // *external* value differs from what we last emitted.
  const lastEmittedRef = useRef<string>('')
  const [state, setState] = useState<CronState>(() => parseCron(value))

  // EMIT-ON-MOUNT: when seeded with an empty/partial value we still render a
  // live preview (parseCron('') → default → e.g. "0 */2 * * *"), but onChange
  // only fired on user interaction — so the parent kept '' and the schedule
  // the operator SAW was not the one SAVED. Emit the canonical expression once
  // so what's shown is what's persisted.
  useEffect(() => {
    const canonical = buildCron(state)
    if (canonical && canonical !== value) {
      lastEmittedRef.current = canonical
      onChange(canonical)
    }
    // mount only — subsequent syncs go through the value effect / update()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (value !== lastEmittedRef.current) {
      setState(parseCron(value))
    }
  }, [value])

  const update = (patch: Partial<CronState>) => {
    setState((prev) => {
      const next = { ...prev, ...patch }
      const expr = buildCron(next)
      lastEmittedRef.current = expr
      onChange(expr)
      return next
    })
  }

  const expr = useMemo(() => buildCron(state), [state])
  const summary = useMemo(() => describeCron(state, t), [state, t])

  return (
    <Stack spacing={1.5}>
      <Box>
        <Typography variant="caption" color="text.secondary" component="div" sx={{ mb: 0.5 }}>
          {t('operations.triggers.cronMode')}
        </Typography>
        <ToggleButtonGroup
          exclusive
          size="small"
          value={state.mode}
          onChange={(_, m: CronMode | null) => { if (m) update({ mode: m }) }}
          sx={{ flexWrap: 'wrap', '& .MuiToggleButton-root': { textTransform: 'none', py: 0.4 } }}
        >
          <ToggleButton value="minutes">{t('operations.triggers.cronModes.minutes')}</ToggleButton>
          <ToggleButton value="hourly">{t('operations.triggers.cronModes.hourly')}</ToggleButton>
          <ToggleButton value="hours">{t('operations.triggers.cronModes.hours')}</ToggleButton>
          <ToggleButton value="daily">{t('operations.triggers.cronModes.daily')}</ToggleButton>
          <ToggleButton value="weekly">{t('operations.triggers.cronModes.weekly')}</ToggleButton>
          <ToggleButton value="monthly">{t('operations.triggers.cronModes.monthly')}</ToggleButton>
          <ToggleButton value="advanced">{t('operations.triggers.cronModes.advanced')}</ToggleButton>
        </ToggleButtonGroup>
      </Box>

      {/* per-mode controls */}
      {state.mode === 'minutes' && (
        <ModeMinutes value={state.everyMinutes} onChange={(n) => update({ everyMinutes: n })} />
      )}
      {state.mode === 'hourly' && (
        <ModeHourly minute={state.hourlyMinute} onChange={(m) => update({ hourlyMinute: m })} />
      )}
      {state.mode === 'hours' && (
        <ModeHours
          everyHours={state.everyHours}
          minute={state.hoursAtMinute}
          onChange={(n, m) => update({ everyHours: n, hoursAtMinute: m })}
        />
      )}
      {state.mode === 'daily' && (
        <ModeTime
          hour={state.dailyHour}
          minute={state.dailyMinute}
          onChange={(h, m) => update({ dailyHour: h, dailyMinute: m })}
        />
      )}
      {state.mode === 'weekly' && (
        <ModeWeekly
          days={state.weeklyDays}
          hour={state.weeklyHour}
          minute={state.weeklyMinute}
          onChange={(days, hour, minute) => update({ weeklyDays: days, weeklyHour: hour, weeklyMinute: minute })}
        />
      )}
      {state.mode === 'monthly' && (
        <ModeMonthly
          day={state.monthlyDay}
          hour={state.monthlyHour}
          minute={state.monthlyMinute}
          onChange={(day, hour, minute) => update({ monthlyDay: day, monthlyHour: hour, monthlyMinute: minute })}
        />
      )}
      {state.mode === 'advanced' && (
        <TextField
          fullWidth
          size="small"
          value={state.advancedExpr}
          onChange={(e) => update({ advancedExpr: e.target.value })}
          placeholder="0 */2 * * *"
          helperText={t('operations.triggers.cronHelp')}
          sx={{ '& input': { fontFamily: 'monospace' } }}
        />
      )}

      {/* Live preview — cron string + human description */}
      <Box
        sx={{
          mt: 0.5, p: 1,
          border: '1px dashed',
          borderColor: 'divider',
          borderRadius: 1.5,
          bgcolor: (th) => (th.palette.mode === 'dark' ? 'action.hover' : 'grey.50'),
        }}
      >
        <Stack direction="row" alignItems="center" spacing={1} flexWrap="wrap" useFlexGap>
          <Typography variant="caption" color="text.secondary" sx={{ fontWeight: 700 }}>
            {t('operations.triggers.cronPreview')}
          </Typography>
          <Chip
            size="small"
            label={expr || '—'}
            sx={{ fontFamily: 'monospace', fontSize: '0.7rem', height: 20 }}
          />
        </Stack>
        {summary && (
          <Typography variant="body2" color="text.primary" sx={{ mt: 0.5 }}>
            {summary}
          </Typography>
        )}
      </Box>
    </Stack>
  )
}

// ─────────────────────────────────────────────────────────────────────
// Sub-components for each mode
// ─────────────────────────────────────────────────────────────────────

function ModeMinutes({ value, onChange }: { value: number; onChange: (n: number) => void }) {
  const { t } = useTranslation()
  const presets = [1, 5, 10, 15, 20, 30]
  return (
    <Stack direction="row" alignItems="center" spacing={1} flexWrap="wrap" useFlexGap>
      <Typography variant="body2" color="text.secondary">
        {t('operations.triggers.cronModes.everyLabel')}
      </Typography>
      {presets.map((p) => (
        <Chip
          key={p}
          size="small"
          variant={value === p ? 'filled' : 'outlined'}
          color={value === p ? 'primary' : 'default'}
          label={p === 1 ? t('operations.triggers.cronModes.oneMinute') : t('operations.triggers.cronModes.nMinutes', { n: p })}
          onClick={() => onChange(p)}
        />
      ))}
      <TextField
        type="number"
        size="small"
        value={value}
        onChange={(e) => onChange(clamp(Number(e.target.value), 1, 59))}
        inputProps={{ min: 1, max: 59, step: 1, style: { width: 60 } }}
      />
      <Typography variant="body2" color="text.secondary">
        {t('operations.triggers.cronModes.minutesSuffix')}
      </Typography>
    </Stack>
  )
}

function ModeHourly({ minute, onChange }: { minute: number; onChange: (m: number) => void }) {
  const { t } = useTranslation()
  return (
    <Stack direction="row" alignItems="center" spacing={1} flexWrap="wrap" useFlexGap>
      <Typography variant="body2" color="text.secondary">
        {t('operations.triggers.cronModes.atMinuteLabel')}
      </Typography>
      <Select
        size="small"
        value={minute}
        onChange={(e) => onChange(Number(e.target.value))}
        sx={{ minWidth: 80 }}
      >
        {[0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55].map((m) => (
          <MenuItem key={m} value={m}>{`:${pad2(m)}`}</MenuItem>
        ))}
      </Select>
    </Stack>
  )
}

function ModeHours({
  everyHours, minute, onChange,
}: {
  everyHours: number
  minute: number
  onChange: (n: number, m: number) => void
}) {
  const { t } = useTranslation()
  const presets = [2, 3, 4, 6, 8, 12]
  return (
    <Stack spacing={1}>
      <Stack direction="row" alignItems="center" spacing={1} flexWrap="wrap" useFlexGap>
        <Typography variant="body2" color="text.secondary">
          {t('operations.triggers.cronModes.everyLabel')}
        </Typography>
        {presets.map((p) => (
          <Chip
            key={p}
            size="small"
            variant={everyHours === p ? 'filled' : 'outlined'}
            color={everyHours === p ? 'primary' : 'default'}
            label={t('operations.triggers.cronModes.nHours', { n: p })}
            onClick={() => onChange(p, minute)}
          />
        ))}
        <TextField
          type="number"
          size="small"
          value={everyHours}
          onChange={(e) => onChange(clamp(Number(e.target.value), 2, 23), minute)}
          inputProps={{ min: 2, max: 23, step: 1, style: { width: 60 } }}
        />
        <Typography variant="body2" color="text.secondary">
          {t('operations.triggers.cronModes.hoursSuffix')}
        </Typography>
      </Stack>
      <Stack direction="row" alignItems="center" spacing={1} flexWrap="wrap" useFlexGap>
        <Typography variant="body2" color="text.secondary">
          {t('operations.triggers.cronModes.atMinuteLabel')}
        </Typography>
        <Select
          size="small"
          value={minute}
          onChange={(e) => onChange(everyHours, Number(e.target.value))}
          sx={{ minWidth: 80 }}
        >
          {[0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55].map((m) => (
            <MenuItem key={m} value={m}>{`:${pad2(m)}`}</MenuItem>
          ))}
        </Select>
      </Stack>
    </Stack>
  )
}

function ModeTime({ hour, minute, onChange }: { hour: number; minute: number; onChange: (h: number, m: number) => void }) {
  const { t } = useTranslation()
  return (
    <Stack direction="row" alignItems="center" spacing={1} flexWrap="wrap" useFlexGap>
      <Typography variant="body2" color="text.secondary">
        {t('operations.triggers.cronModes.atTimeLabel')}
      </Typography>
      <HourMinutePicker hour={hour} minute={minute} onChange={onChange} />
    </Stack>
  )
}

function ModeWeekly({
  days, hour, minute, onChange,
}: {
  days: number[]
  hour: number
  minute: number
  onChange: (days: number[], hour: number, minute: number) => void
}) {
  const { t } = useTranslation()
  // 0=Sun … 6=Sat. We render Mon..Sun (i18n-friendly order: most
  // operators in our market start the week on Monday).
  const orderForDisplay = [1, 2, 3, 4, 5, 6, 0]
  const toggleDay = (d: number) => {
    const has = days.includes(d)
    const next = has ? days.filter((x) => x !== d) : [...days, d]
    onChange(next.sort((a, b) => a - b), hour, minute)
  }
  const setPreset = (preset: number[]) => onChange(preset, hour, minute)
  return (
    <Stack spacing={1}>
      <Stack direction="row" alignItems="center" spacing={0.5} flexWrap="wrap" useFlexGap>
        {orderForDisplay.map((d) => (
          <Chip
            key={d}
            size="small"
            variant={days.includes(d) ? 'filled' : 'outlined'}
            color={days.includes(d) ? 'primary' : 'default'}
            label={t(`operations.triggers.cronDesc.dayShort_${d}`)}
            onClick={() => toggleDay(d)}
          />
        ))}
      </Stack>
      <Stack direction="row" alignItems="center" spacing={1} flexWrap="wrap" useFlexGap>
        <Chip size="small" variant="outlined"
              label={t('operations.triggers.cronDesc.weekdays')}
              onClick={() => setPreset([1, 2, 3, 4, 5])} />
        <Chip size="small" variant="outlined"
              label={t('operations.triggers.cronDesc.weekend')}
              onClick={() => setPreset([0, 6])} />
        <Chip size="small" variant="outlined"
              label={t('operations.triggers.cronDesc.everyDay')}
              onClick={() => setPreset([0, 1, 2, 3, 4, 5, 6])} />
      </Stack>
      <Stack direction="row" alignItems="center" spacing={1} flexWrap="wrap" useFlexGap>
        <Typography variant="body2" color="text.secondary">
          {t('operations.triggers.cronModes.atTimeLabel')}
        </Typography>
        <HourMinutePicker
          hour={hour}
          minute={minute}
          onChange={(h, m) => onChange(days, h, m)}
        />
      </Stack>
    </Stack>
  )
}

function ModeMonthly({
  day, hour, minute, onChange,
}: {
  day: number
  hour: number
  minute: number
  onChange: (day: number, hour: number, minute: number) => void
}) {
  const { t } = useTranslation()
  return (
    <Stack direction="row" alignItems="center" spacing={1} flexWrap="wrap" useFlexGap>
      <Typography variant="body2" color="text.secondary">
        {t('operations.triggers.cronModes.dayOfMonthLabel')}
      </Typography>
      <Select
        size="small"
        value={day}
        onChange={(e) => onChange(Number(e.target.value), hour, minute)}
        sx={{ minWidth: 80 }}
      >
        {Array.from({ length: 31 }, (_, i) => i + 1).map((d) => (
          <MenuItem key={d} value={d}>{d}</MenuItem>
        ))}
      </Select>
      <Typography variant="body2" color="text.secondary">
        {t('operations.triggers.cronModes.atTimeLabel')}
      </Typography>
      <HourMinutePicker
        hour={hour}
        minute={minute}
        onChange={(h, m) => onChange(day, h, m)}
      />
    </Stack>
  )
}

function HourMinutePicker({
  hour, minute, onChange,
}: {
  hour: number
  minute: number
  onChange: (h: number, m: number) => void
}) {
  return (
    <Stack direction="row" alignItems="center" spacing={0.5}>
      <Select
        size="small"
        value={hour}
        onChange={(e) => onChange(Number(e.target.value), minute)}
        sx={{ minWidth: 72 }}
      >
        {Array.from({ length: 24 }, (_, h) => h).map((h) => (
          <MenuItem key={h} value={h}>{pad2(h)}</MenuItem>
        ))}
      </Select>
      <Typography variant="body2" color="text.secondary">:</Typography>
      <Select
        size="small"
        value={minute}
        onChange={(e) => onChange(hour, Number(e.target.value))}
        sx={{ minWidth: 72 }}
      >
        {[0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55].map((m) => (
          <MenuItem key={m} value={m}>{pad2(m)}</MenuItem>
        ))}
      </Select>
    </Stack>
  )
}

export default CronBuilder
