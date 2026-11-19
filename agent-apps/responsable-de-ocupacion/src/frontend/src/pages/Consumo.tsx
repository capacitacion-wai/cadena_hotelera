/**
 * Consumo — a first-level admin page (nav, below "Eventos") for this employee's
 * credit spend. Two things, deliberately separate:
 *   1. The BUDGET gauge: rolling-30-day spend vs the deploy-time cap — the exact
 *      computation the enforcement gates use (``GET /api/agent/budget``).
 *   2. Rich, DATE-RANGED analytics (``GET /api/agent/budget/consumption``): a
 * daily time-series split by concept (`autónomo` vs `petición`), the by-concept
 *      totals for the range, and the by-user breakdown of *petición* spend (who
 *      ran the boss chat / which channel sender).
 *
 * Moved out of Operations → "Consumo" tab so it has room to breathe.
 */
import { localeSeguro } from '../utils/datetime'
import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery } from '@tanstack/react-query'
import {
  Alert, Box, Card, CardContent, Chip, CircularProgress, Grid, LinearProgress,
  Stack, Table, TableBody, TableCell, TableHead, TableRow, TextField,
  ToggleButton, ToggleButtonGroup, Typography, useTheme,
} from '@mui/material'
import PaidOutlinedIcon from '@mui/icons-material/PaidOutlined'
import {
  Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer,
  Tooltip as RTooltip, XAxis, YAxis,
} from 'recharts'

import PageHeader from '../components/PageHeader'
import { apiFetch } from '../services/apiClient'

function isoDate(d: Date): string {
  return d.toISOString().slice(0, 10)
}
function daysAgo(n: number): Date {
  const d = new Date()
  d.setDate(d.getDate() - n)
  return d
}

export default function Consumo() {
  const { t, i18n } = useTranslation()
  const theme = useTheme()
  const fmtCr = (n: any) => (n == null ? '—' : Number(n).toLocaleString(localeSeguro(i18n.language), { maximumFractionDigits: 1 }))

  // Rolling-30d budget gauge (independent of the range selector below).
  const budget = useQuery({
    queryKey: ['agent-budget'],
    queryFn: () => apiFetch('/api/agent/budget').then(async (r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      return r.json()
    }),
    refetchInterval: 60_000,
  })

  // Date range for the analytics. Presets set the two dates; the operator can
  // also pick a custom range with the date fields.
  const [from, setFrom] = useState(() => isoDate(daysAgo(30)))
  const [to, setTo] = useState(() => isoDate(new Date()))
  const [preset, setPreset] = useState<string>('30d')
  const applyPreset = (p: string) => {
    setPreset(p)
    if (p === '7d') setFrom(isoDate(daysAgo(7)))
    else if (p === '30d') setFrom(isoDate(daysAgo(30)))
    else if (p === '90d') setFrom(isoDate(daysAgo(90)))
    setTo(isoDate(new Date()))
  }

  const cons = useQuery({
    queryKey: ['agent-consumption', from, to],
    queryFn: () => apiFetch(
      `/api/agent/budget/consumption?from=${encodeURIComponent(`${from}T00:00:00`)}&to=${encodeURIComponent(`${to}T23:59:59`)}`,
    ).then(async (r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      return r.json()
    }),
  })

  const shortDate = (s: string) => {
    try {
      return new Intl.DateTimeFormat(localeSeguro(i18n.language), { day: '2-digit', month: 'short' }).format(new Date(`${s}T00:00:00`))
    } catch { return s }
  }
  const series = cons.data?.series ?? []
  const byUser = cons.data?.by_user ?? []

  // Resolve the raw by-user key (email/phone) → a friendly name via the
  // managed-users directory, so the table shows "Juan Pérez", not "+34…".
  const usersDir = useQuery({
    queryKey: ['users-directory'],
    queryFn: () => apiFetch('/api/agent/users').then((r) => (r.ok ? r.json() : { items: [] })),
    staleTime: 60_000,
  })
  const nameByKey = useMemo(() => {
    const m: Record<string, string> = {}
    for (const u of (usersDir.data?.items ?? []) as Array<{ email?: string; phones?: string[]; display_name?: string; alias?: string }>) {
      const label = u.display_name || u.alias || u.email || ''
      if (!label) continue
      if (u.email) m[u.email.toLowerCase()] = label
      for (const p of u.phones || []) m[p] = label
    }
    return m
  }, [usersDir.data])
  const cAuto = theme.palette.primary.main
  const cPet = theme.palette.warning.main
  // v7.157.0 — third concept: what it costs for the employee to keep itself TIDY
  // (distilling its memory and its learning). Mixed with the work it was not visible, and it is
  // exactly the metric that exposes a consolidation that burns tokens without merging anything.
  const cOrden = theme.palette.info.main
  // 12.12.0 — TALKING. Its own color because it is a different decision: audio costs
  // twenty times more than text, so mixing it with `petición` left us unable to
  // answer the only question that matters here, "how much does it cost for people to talk to it?".
  const cVoz = theme.palette.success.main

  const b = budget.data
  const pct = Number(b?.pct ?? 0)
  const barColor = b?.over_budget ? 'error' : pct >= 80 ? 'warning' : 'primary'

  const statCard = (label: string, value: string, hint?: string, color?: string) => (
    <Grid item xs={6} md={3}>
      <Card variant="outlined">
        <CardContent sx={{ py: 1.5, '&:last-child': { pb: 1.5 } }}>
          <Typography variant="caption" color="text.secondary">{label}</Typography>
          <Typography variant="h6" sx={{ fontWeight: 700, color }}>{value}</Typography>
          {hint && (
            <Typography variant="caption" color="text.secondary" sx={{ display: 'block', lineHeight: 1.3 }}>
              {hint}
            </Typography>
          )}
        </CardContent>
      </Card>
    </Grid>
  )

  const credits = t('consumo.credits', 'créditos')
  const userLabel = (who: string) => {
    if (who === 'chat') return t('consumo.userChat', 'Chat (sin identificar)')
    if (who === 'canal') return t('consumo.userChannel', 'Canal (sin identificar)')
    if (who === 'otros') return t('consumo.userOther', 'Otros')
    return nameByKey[who.toLowerCase()] || nameByKey[who] || who
  }

  return (
    <Box>
      <PageHeader
        title={t('consumo.title', 'Consumo')}
        subtitle={t('consumo.subtitle', 'Créditos que consume este empleado: su presupuesto y el detalle por fechas, concepto y quién lo genera.')}
        icon={<PaidOutlinedIcon />}
      />

      {/* ── Budget gauge (rolling 30 days vs the deploy-time cap) ── */}
      {budget.isLoading ? (
        <Box sx={{ display: 'flex', justifyContent: 'center', py: 3 }}><CircularProgress size={24} /></Box>
      ) : budget.error || !b ? (
        <Alert severity="error">{t('consumo.loadError', 'No se pudo cargar el consumo.')}</Alert>
      ) : (
        <Card variant="outlined" sx={{ mb: 3 }}>
          <CardContent>
            <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 0.5 }}>
              {t('consumo.budgetTitle', 'Presupuesto (últimos {{days}} días)', { days: b.window_days })}
            </Typography>
            {b.unlimited ? (
              <Alert severity="info" sx={{ mt: 1 }}>{t('consumo.unlimited', 'Este empleado no tiene presupuesto definido (sin límite). Se define al desplegarlo.')}</Alert>
            ) : (
              <>
                {b.over_budget && (
                  <Alert severity="warning" sx={{ mt: 1, mb: 1 }}>{t('consumo.over', 'Presupuesto agotado — el empleado no actúa (ni autónomo, ni canales, ni chat) hasta que la ventana baje del límite o se amplíe el presupuesto al redesplegarlo.')}</Alert>
                )}
                <Grid container spacing={2} sx={{ mt: 0 }}>
                  {statCard(t('consumo.budget', 'Presupuesto'), `${fmtCr(b.budget_credits)} ${credits}`)}
                  {statCard(t('consumo.consumed', 'Consumido'), `${fmtCr(b.consumed_credits)} ${credits}`)}
                  {statCard(t('consumo.remaining', 'Restante'), `${fmtCr(b.remaining_credits)} ${credits}`, undefined, b.over_budget ? 'error.main' : undefined)}
                  {statCard(t('consumo.usedPct', 'Usado'), `${pct}%`, undefined, barColor === 'error' ? 'error.main' : undefined)}
                </Grid>
                <LinearProgress variant="determinate" value={Math.min(100, pct)} color={barColor as any} sx={{ height: 8, borderRadius: 1, mt: 2 }} />
              </>
            )}
          </CardContent>
        </Card>
      )}

      {/* ── Date range selector ── */}
      <Card variant="outlined" sx={{ mb: 3 }}>
        <CardContent>
          <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2} alignItems={{ sm: 'center' }} useFlexGap flexWrap="wrap">
            <ToggleButtonGroup
              size="small" exclusive value={preset}
              onChange={(_, v) => v && applyPreset(v)}
            >
              <ToggleButton value="7d" sx={{ textTransform: 'none' }}>{t('consumo.range7d', '7 días')}</ToggleButton>
              <ToggleButton value="30d" sx={{ textTransform: 'none' }}>{t('consumo.range30d', '30 días')}</ToggleButton>
              <ToggleButton value="90d" sx={{ textTransform: 'none' }}>{t('consumo.range90d', '90 días')}</ToggleButton>
            </ToggleButtonGroup>
            <Box sx={{ flex: 1 }} />
            <TextField
              size="small" type="date" label={t('consumo.from', 'Desde')} value={from}
              onChange={(e) => { setPreset('custom'); setFrom(e.target.value) }}
              InputLabelProps={{ shrink: true }}
            />
            <TextField
              size="small" type="date" label={t('consumo.to', 'Hasta')} value={to}
              onChange={(e) => { setPreset('custom'); setTo(e.target.value) }}
              InputLabelProps={{ shrink: true }}
            />
          </Stack>
        </CardContent>
      </Card>

      {cons.isLoading ? (
        <Box sx={{ display: 'flex', justifyContent: 'center', py: 3 }}><CircularProgress size={24} /></Box>
      ) : cons.error || !cons.data ? (
        <Alert severity="error">{t('consumo.loadError', 'No se pudo cargar el consumo.')}</Alert>
      ) : (
        <Stack spacing={3}>
          {/* Range totals + by-concept */}
          <Grid container spacing={2}>
            {statCard(t('consumo.rangeTotal', 'Total del rango'), `${fmtCr(cons.data.consumed_credits)} ${credits}`)}
            {statCard(t('consumo.autonomo', 'Autónomo'), `${fmtCr(cons.data.by_concept?.autonomo)} ${credits}`)}
            {statCard(t('consumo.peticion', 'Petición'), `${fmtCr(cons.data.by_concept?.peticion)} ${credits}`)}
            {statCard(t('consumo.ordenar', 'Ordenar lo aprendido'), `${fmtCr(cons.data.by_concept?.ordenar)} ${credits}`,
                      t('consumo.ordenarHint', 'Lo que cuesta que mantenga su memoria en orden, aparte de trabajar.'))}
            {statCard(t('consumo.voz', 'Hablar'), `${fmtCr(cons.data.by_concept?.voz)} ${credits}`,
                      t('consumo.vozHint', 'Conversaciones por voz. Se paga aparte porque el audio cuesta unas veinte veces más que el texto.'))}
          </Grid>

          {/* Temporal chart: stacked credits/day by concept */}
          <Card variant="outlined">
            <CardContent>
              <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1 }}>
                {t('consumo.overTime', 'Consumo por día (créditos)')}
              </Typography>
              <Box sx={{ width: '100%', height: 300 }}>
                <ResponsiveContainer>
                  <BarChart data={series} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" />
                    <XAxis dataKey="date" tickFormatter={shortDate} minTickGap={24} fontSize={11} />
                    <YAxis fontSize={11} width={40} />
                    <RTooltip
                      labelFormatter={(l: any) => shortDate(String(l))}
                      formatter={(value: any, name: string) => [
                        `${fmtCr(value)} ${credits}`,
                        name === 'autonomo' ? t('consumo.autonomo', 'Autónomo')
                          : name === 'ordenar' ? t('consumo.ordenar', 'Ordenar lo aprendido')
                          : name === 'voz' ? t('consumo.voz', 'Hablar')
                          : t('consumo.peticion', 'Petición'),
                      ]}
                    />
                    <Legend formatter={(v: string) => (
                      v === 'autonomo' ? t('consumo.autonomo', 'Autónomo')
                        : v === 'ordenar' ? t('consumo.ordenar', 'Ordenar lo aprendido')
                        : v === 'voz' ? t('consumo.voz', 'Hablar')
                        : t('consumo.peticion', 'Petición'))} />
                    <Bar dataKey="autonomo" stackId="c" fill={cAuto} name="autonomo" />
                    <Bar dataKey="peticion" stackId="c" fill={cPet} name="peticion" />
                    <Bar dataKey="ordenar" stackId="c" fill={cOrden} name="ordenar" />
                    <Bar dataKey="voz" stackId="c" fill={cVoz} name="voz" />
                  </BarChart>
                </ResponsiveContainer>
              </Box>
            </CardContent>
          </Card>

          {/* By user (petición attribution) */}
          <Card variant="outlined">
            <CardContent>
              <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>
                {t('consumo.byUserTitle', 'Petición — por usuario')}
              </Typography>
              <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 1 }}>
                {t('consumo.byUserHint', 'Quién generó el gasto de petición: los jefes que usan el chat y los remitentes de los canales.')}
              </Typography>
              {byUser.length === 0 ? (
                <Alert severity="info">{t('consumo.byUserEmpty', 'Sin consumo de petición en este rango.')}</Alert>
              ) : (
                <Table size="small">
                  <TableHead>
                    <TableRow>
                      <TableCell>{t('consumo.user', 'Usuario')}</TableCell>
                      {/* BOTH figures, not just the total: without separating writing from talking
                          you cannot know for whom voice pays off, which is the question
                          this column exists for. */}
                      <TableCell align="right">{t('consumo.escribiendo', 'escribiendo')}</TableCell>
                      <TableCell align="right">{t('consumo.hablando', 'hablando')}</TableCell>
                      <TableCell align="right">{t('consumo.credits', 'créditos')}</TableCell>
                      <TableCell align="right">{t('consumo.runs', 'ejecuciones')}</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {byUser.map((u: any) => (
                      <TableRow key={u.who}>
                        <TableCell>
                          <Chip size="small" variant="outlined" label={userLabel(u.who)} sx={{ maxWidth: 320 }} />
                        </TableCell>
                        <TableCell align="right" sx={{ color: 'text.secondary' }}>
                          {fmtCr(u.credits_peticion ?? u.credits)}
                        </TableCell>
                        <TableCell align="right"
                                   sx={{ color: (u.credits_voz ?? 0) > 0 ? 'success.main' : 'text.disabled' }}>
                          {fmtCr(u.credits_voz ?? 0)}
                        </TableCell>
                        <TableCell align="right" sx={{ fontWeight: 600 }}>{fmtCr(u.credits)}</TableCell>
                        <TableCell align="right">{u.runs}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </CardContent>
          </Card>
        </Stack>
      )}
    </Box>
  )
}
