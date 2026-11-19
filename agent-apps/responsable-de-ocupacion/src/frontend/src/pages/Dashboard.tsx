import { useEffect, useMemo, useState } from 'react';
import {
  alpha,
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  CircularProgress,
  Collapse,
  Divider,
  Grid,
  Link,
  Stack,
  Tooltip,
  Typography,
} from '@mui/material';
import { Link as RouterLink, useNavigate } from 'react-router-dom';
import TrendingUpIcon from '@mui/icons-material/TrendingUp';
import TrendingDownIcon from '@mui/icons-material/TrendingDown';
import WarningAmberIcon from '@mui/icons-material/WarningAmber';
import LightbulbOutlinedIcon from '@mui/icons-material/LightbulbOutlined';
import GavelIcon from '@mui/icons-material/Gavel';
import CheckCircleOutlineIcon from '@mui/icons-material/CheckCircleOutline';
import NotificationsActiveOutlinedIcon from '@mui/icons-material/NotificationsActiveOutlined';
import ErrorOutlineIcon from '@mui/icons-material/ErrorOutline';
import ExpandMoreIcon from '@mui/icons-material/ExpandMore';
import ArrowForwardIcon from '@mui/icons-material/ArrowForward';
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip as RTooltip,
  XAxis,
  YAxis,
  Bar,
  BarChart,
} from 'recharts';
import { useTranslation } from 'react-i18next';
import { useQuery, useQueries } from '@tanstack/react-query';
import { PageHeader } from '../components/PageHeader';
import { SectionTitle } from '../components/SectionTitle';
import { useAuth } from '../auth/AuthContext';
import { apiBase } from '../services/apiClient';
import { usePickLabel } from '../utils/dataLabels';
import { useManifest } from '../hooks/useManifest';
import { apiFetch } from '../services/apiClient';
import { fetchRows } from '../surfaces/common/api';
import {
  EntityLink, UNIFIED_STATE_TONE, BAND_TONE, CONFIDENCE_TONE, Tone,
} from '../surfaces/common/primitives';
import { formatDateTime } from '../utils/datetime';

type Kpi = {
  id: string;
  label: string;
  label_en?: string;
  labels?: Record<string, string>;
  format: 'number' | 'integer' | 'percent' | 'currency';
  good_when: 'higher' | 'lower';
  target?: number | null;
  value: number | null;
  status: 'ok' | 'error';
  error?: string;
};

type Trend = {
  id: string;
  label: string;
  label_en?: string;
  labels?: Record<string, string>;
  kind: 'line' | 'bar';
  x: string;
  y: string;
  format: 'number' | 'integer' | 'percent' | 'currency';
  data: Record<string, any>[];
  status: 'ok' | 'error';
  error?: string;
};

// Tenant currency from the manifest (set on window by Layout). EUR as
// back-compat default only.
const tenantCurrency = () =>
  (typeof window !== 'undefined' && (window as unknown as { __waiCurrency?: string }).__waiCurrency) || 'EUR';

const fmt = (
  v: number | null | undefined,
  format: 'number' | 'integer' | 'percent' | 'currency' = 'number',
) => {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  switch (format) {
    case 'percent':
      return `${(v * 100).toFixed(1)}%`;
    case 'currency':
      return `${v.toLocaleString(undefined, { style: 'currency', currency: tenantCurrency() })}`;
    case 'integer':
      return Math.round(v).toLocaleString();
    default:
      return v.toLocaleString(undefined, { maximumFractionDigits: 2 });
  }
};

function kpiColor(kpi: Kpi): 'success' | 'warning' | 'error' | 'default' {
  if (kpi.status !== 'ok' || kpi.value === null || kpi.target === null || kpi.target === undefined) {
    return 'default';
  }
  const diff = kpi.value - (kpi.target as number);
  const ok = kpi.good_when === 'higher' ? diff >= 0 : diff <= 0;
  if (ok) return 'success';
  const pct = Math.abs(diff) / Math.max(1e-9, Math.abs(kpi.target as number));
  return pct < 0.1 ? 'warning' : 'error';
}

// ── "Lo que genero" overview (v7.45.0, redesigned v7.46.0) ──────────
// A clean per-entity summary of alerts / recommendations / decisions: a big
// total, a STATUS distribution bar (the lifecycle at a glance), tidy one-line
// facet rows (severity/impact/urgency/confidence/…), and the latest items.
// Exact counts come from /{slug}/summary; recent rows from a small fetch.
const SIGNAL_TABLES = ['alerts', 'recommendations', 'decisions'];
// Facets other than status, shown as compact rows (status gets the bar).
const FACET_ROWS = ['severity', 'priority', 'impact', 'urgency', 'confidence', 'decision', 'reason_code'];
const STATUS_ORDER = ['open', 'acknowledged', 'in_progress', 'done', 'dismissed'];
// Lifecycle states that still need a human's eyes (vs done/dismissed = resolved).
const PENDING_STATES = ['open', 'acknowledged', 'in_progress'];
// v7.85.0 — resolved is now EXPLICIT (done/dismissed) rather than `total − pending`,
// so an UNKNOWN/custom status is not silently counted as resolved (which greened the
// bar and could hide work).
const RESOLVED_STATES = ['done', 'dismissed'];
const BAND_ORDER = ['critical', 'urgent', 'immediate', 'high', 'medium', 'low', 'info'];
const DECISION_TONE: Record<string, Tone> = {
  no_action: 'default', deferred: 'warning', escalated: 'error', reviewed: 'info', executed: 'success',
};
// Each entity's tone comes from the THEME palette (before, fixed hex from another palette).
type TonoDeEntidad = 'error' | 'warning' | 'info' | null;
const ENTITY_ICON: Record<string, { icon: JSX.Element; tono: TonoDeEntidad }> = {
  alerts: { icon: <WarningAmberIcon />, tono: 'error' },
  recommendations: { icon: <LightbulbOutlinedIcon />, tono: 'warning' },
  decisions: { icon: <GavelIcon />, tono: 'info' },
};
const TONE_BG: Record<Tone, string> = {
  success: 'success.main', error: 'error.main', warning: 'warning.main',
  info: 'info.main', primary: 'primary.main', default: 'grey.400',
};

function facetTone(col: string, val: string): Tone {
  if (col === 'status') return UNIFIED_STATE_TONE[val] ?? 'default';
  if (col === 'confidence') return CONFIDENCE_TONE[val] ?? 'default';
  if (col === 'decision') return DECISION_TONE[val] ?? 'default';
  if (col === 'reason_code') return 'info';
  return BAND_TONE[val] ?? 'default'; // severity / priority / impact / urgency
}

// Order a facet's entries: status by lifecycle, intensity bands by severity,
// everything else by count desc.
function orderEntries(col: string, obj: Record<string, number>): [string, number][] {
  const ord = col === 'status' ? STATUS_ORDER
    : (['severity', 'priority', 'impact', 'urgency'].includes(col) ? BAND_ORDER : null);
  const entries = Object.entries(obj);
  if (!ord) return entries.sort((a, b) => b[1] - a[1]);
  const rank = (k: string) => { const i = ord.indexOf(k); return i < 0 ? 999 : i; };
  return entries.sort((a, b) => rank(a[0]) - rank(b[0]));
}

// One facet (status / severity / impact / …) as a single tidy line: a fixed
// label, a thin stacked distribution bar (hover for the exact split), and a
// caption of the dominant value. Uniform rows read cleanly instead of the old
// chip-soup that made the card a wall of text.
function FacetBar({ col, entries, label, valueLabel }: {
  col: string; entries: [string, number][]; label: string;
  valueLabel: (c: string, v: string) => string;
}) {
  const shown = entries.filter(([, n]) => n > 0);
  const total = shown.reduce((s, [, n]) => s + n, 0) || 1;
  return (
    <Stack direction="row" alignItems="center" spacing={1} sx={{ py: 0.4 }}>
      <Typography variant="caption" color="text.secondary" sx={{ width: 72, flexShrink: 0 }} noWrap>
        {label}
      </Typography>
      <Box sx={{ flex: 1, minWidth: 32, display: 'flex', height: 8, borderRadius: 4, overflow: 'hidden', bgcolor: 'grey.100' }}>
        {shown.map(([v, n]) => (
          <Tooltip key={v} title={`${valueLabel(col, v)}: ${n}`} arrow>
            <Box sx={{ width: `${(n / total) * 100}%`, bgcolor: TONE_BG[facetTone(col, v)] }} />
          </Tooltip>
        ))}
      </Box>
      <Typography variant="caption" color="text.secondary" sx={{ flexShrink: 0, maxWidth: 104 }} noWrap>
        {shown.length ? <>{valueLabel(col, shown[0][0])} <b>{shown[0][1]}</b></> : '—'}
      </Typography>
    </Stack>
  );
}

function Dot({ tone }: { tone: Tone }) {
  return <Box sx={{ width: 8, height: 8, borderRadius: '50%', bgcolor: TONE_BG[tone], flexShrink: 0 }} />;
}

// How many of a summary's items are still awaiting a human (open/ack/in_progress).
function pendingCount(by: Record<string, Record<string, number>>): number {
  const s = by?.status ?? {};
  return PENDING_STATES.reduce((n, k) => n + (Number(s[k]) || 0), 0);
}

// Explicitly resolved (done/dismissed) — NOT `total − pending`, so an unknown
// status doesn't masquerade as resolved.
function resolvedCount(by: Record<string, Record<string, number>>): number {
  const s = by?.status ?? {};
  return RESOLVED_STATES.reduce((n, k) => n + (Number(s[k]) || 0), 0);
}

// The one distribution a manager cares about: resuelto (verde) vs pendiente (ámbar).
function ProgressSplit({ resolved, pending }: { resolved: number; pending: number }) {
  const total = resolved + pending || 1;
  return (
    <Box sx={{ display: 'flex', height: 8, borderRadius: 4, overflow: 'hidden', bgcolor: 'grey.100' }}>
      <Box sx={{ width: `${(resolved / total) * 100}%`, bgcolor: 'success.main' }} />
      <Box sx={{ width: `${(pending / total) * 100}%`, bgcolor: 'warning.main' }} />
    </Box>
  );
}

function SignalCard({ entity, summary, summaryError }: {
  entity: any; summary: any; summaryError?: boolean;
}) {
  const { t } = useTranslation();
  const pickLabel = usePickLabel();
  const slug = entity.slug;
  const table = String(entity.table ?? '').toLowerCase();
  const recent = useQuery({
    queryKey: ['dash-recent', slug],
    queryFn: () => fetchRows(slug, { pageSize: 4, sort: 'created_at', direction: 'desc' }),
    staleTime: 60_000, retry: false,
  });
  const pk = entity.primary_key ?? 'id';
  const facetLabel = (col: string) => t(`dimensions.${col}`, t(`fields.${col}`, col));
  const valueLabel = (col: string, v: string) =>
    col === 'reason_code' ? t(`reasonCodes.${v}`, v) : t(`states.${v}`, v);
  const by: Record<string, Record<string, number>> = summary?.by ?? {};
  const total: number = summary?.total ?? 0;
  const pending = pendingCount(by);
  const resolved = resolvedCount(by);
  const pct = total ? Math.round((resolved / total) * 100) : 0;
  const rows: any[] = (recent.data?.rows ?? []).slice(0, 3);
  const meta = ENTITY_ICON[table] ?? { icon: <WarningAmberIcon />, tono: null };
  const facetCols = ['status', ...FACET_ROWS].filter((c) => by[c] && Object.keys(by[c]).length);
  const [showDetail, setShowDetail] = useState(false);

  return (
    <Card variant="outlined" sx={{ height: '100%', borderRadius: 2, display: 'flex', flexDirection: 'column' }}>
      <CardContent sx={{ p: 2.5, '&:last-child': { pb: 2.5 }, flex: 1, display: 'flex', flexDirection: 'column' }}>
        {/* Header: icon + name + big total */}
        <Stack direction="row" alignItems="center" spacing={1.5}>
          <Box sx={{
            width: 40, height: 40, borderRadius: 2, flexShrink: 0, display: 'flex',
            alignItems: 'center', justifyContent: 'center',
            bgcolor: (theme) => alpha(meta.tono ? theme.palette[meta.tono].main : theme.palette.text.secondary, 0.08),
            color: meta.tono ? `${meta.tono}.main` : 'text.secondary', '& .MuiSvgIcon-root': { fontSize: 22 },
          }}>{meta.icon}</Box>
          <Box sx={{ flex: 1, minWidth: 0 }}>
            <Typography variant="subtitle1" sx={{ fontWeight: 700, lineHeight: 1.2 }} noWrap>
              {pickLabel(entity, slug)}
            </Typography>
            <Typography variant="caption" color="text.secondary">
              {t('dashboard.signals.total', { count: total, defaultValue: `${total} en total` })}
            </Typography>
          </Box>
          {/* The prominent number is what needs FOLLOW-UP (pending/open), NOT the
              cumulative total — a business owner tracks the backlog, and the
              already-resolved ones don't need attention. Total stays as context. */}
          {/* v7.85.0 — on a failed summary the counts are 0 by coercion; show an
              UNKNOWN "—" (neutral) instead of a green 0/"Al día" that would falsely
              read as healthy. */}
          <Box sx={{ textAlign: 'right', flexShrink: 0 }}>
            <Typography variant="h4" sx={{
              fontWeight: 800, lineHeight: 1,
              color: summaryError ? 'text.disabled' : (pending > 0 ? 'warning.main' : 'success.main'),
            }}>{summaryError ? '—' : pending}</Typography>
            <Typography variant="caption" sx={{
              fontWeight: 600,
              color: summaryError ? 'text.secondary' : (pending > 0 ? 'warning.main' : 'success.main'),
            }}>
              {summaryError
                ? t('dashboard.signals.cardUnknown', 'sin datos')
                : (pending > 0 ? t('dashboard.signals.pendingLabel', 'pendientes') : t('dashboard.signals.cardClear', 'Al día'))}
            </Typography>
          </Box>
        </Stack>

        {summaryError && <Alert severity="warning" sx={{ mt: 1.5 }}>{t('dashboard.signals.loadError', 'No se pudo cargar el resumen.')}</Alert>}

        {/* Progress — resueltas vs pendientes (the business signal). */}
        {!summaryError && total > 0 && (
          <Box sx={{ mt: 2 }}>
            <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 0.75 }}>
              {t('dashboard.signals.resolvedOfTotal', {
                pct, resolved, total,
                defaultValue: `${pct}% resuelto · ${resolved} de ${total}`,
              })}
            </Typography>
            <ProgressSplit resolved={resolved} pending={pending} />
          </Box>
        )}

        {/* Recent items — title ellipsised so it never wraps per-word. */}
        {rows.length > 0 && (
          <Box sx={{ mt: 2 }}>
            <Typography variant="overline" color="text.secondary">
              {t('dashboard.signals.recent', 'Últimos')}
            </Typography>
            <Stack spacing={0.75} sx={{ mt: 0.5 }}>
              {rows.map((row) => {
                const st = String(row.status ?? '').toLowerCase();
                return (
                  <Stack key={String(row[pk])} direction="row" spacing={1} alignItems="center" sx={{ minWidth: 0 }}>
                    {st && <Tooltip title={valueLabel('status', st)} arrow><span style={{ display: 'flex' }}><Dot tone={UNIFIED_STATE_TONE[st] ?? 'default'} /></span></Tooltip>}
                    <Box sx={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      <EntityLink kind={slug} id={row[pk]}
                        label={String(row.title || row.reason_md || `#${row[pk]}`).replace(/\s+/g, ' ').slice(0, 80)} />
                    </Box>
                    <Typography variant="caption" color="text.secondary" sx={{ flexShrink: 0 }}>
                      {row.created_at ? formatDateTime(row.created_at, { year: undefined, timeZoneName: undefined, day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : ''}
                    </Typography>
                  </Stack>
                );
              })}
            </Stack>
          </Box>
        )}

        {/* Footer: detail-on-demand + go to the full list. Pushed to the bottom
            so cards of different heights align their footers. */}
        <Box sx={{ flexGrow: 1 }} />
        <Divider sx={{ my: 1.5 }} />
        <Stack direction="row" justifyContent="space-between" alignItems="center">
          {facetCols.length > 0 ? (
            <Button size="small" onClick={() => setShowDetail((v) => !v)}
              sx={{ textTransform: 'none', minWidth: 0, px: 0.5, color: 'text.secondary' }}
              startIcon={<ExpandMoreIcon sx={{ transform: showDetail ? 'rotate(180deg)' : 'none', transition: 'transform .15s' }} />}>
              {showDetail ? t('dashboard.signals.hideBreakdown', 'Ocultar') : t('dashboard.signals.breakdown', 'Desglose')}
            </Button>
          ) : <span />}
          <Link component={RouterLink} to={`/domain/${slug}`} variant="caption"
            sx={{ display: 'inline-flex', alignItems: 'center', gap: 0.25, fontWeight: 600 }}>
            {t('dashboard.signals.viewAll', 'Ver todas')} <ArrowForwardIcon fontSize="inherit" />
          </Link>
        </Stack>
        <Collapse in={showDetail} unmountOnExit>
          <Box sx={{ mt: 1 }}>
            {facetCols.map((col) => (
              <FacetBar key={col} col={col} label={facetLabel(col)} valueLabel={valueLabel}
                entries={orderEntries(col, by[col])} />
            ))}
          </Box>
        </Collapse>
      </CardContent>
    </Card>
  );
}

// The business headline: WHAT NEEDS A HUMAN right now, aggregated across the
// three signal types, with one-click access to each backlog. Green "todo al día"
// when nothing is pending.
function AttentionHero({ cards, onOpen, loading, error }: {
  cards: { slug: string; name: string; pending: number; total: number }[];
  onOpen: (slug: string) => void;
  loading?: boolean;
  error?: boolean;
}) {
  const { t } = useTranslation();
  const totalPending = cards.reduce((n, c) => n + c.pending, 0);
  // While the summaries are still loading, don't flash a premature "todo al día".
  if (loading) {
    return (
      <Card variant="outlined" sx={{ borderRadius: 2, mb: 2 }}>
        <CardContent sx={{ py: 2, '&:last-child': { pb: 2 }, display: 'flex', alignItems: 'center', gap: 2 }}>
          <CircularProgress size={22} />
          <Typography variant="body2" color="text.secondary">
            {t('dashboard.signals.loading', 'Cargando resumen…')}
          </Typography>
        </CardContent>
      </Card>
    );
  }
  // v7.85.0 — if a summary FAILED to load, do NOT paint the green "todo al día":
  // a failed fetch coerces its counts to 0, which would otherwise masquerade as a
  // healthy all-clear. Show a neutral "couldn't load" state so the operator knows
  // the status is UNKNOWN, not clear.
  if (error) {
    return (
      <Card variant="outlined" sx={{
        borderRadius: 2, mb: 2, borderLeft: '4px solid', borderLeftColor: 'text.disabled',
      }}>
        <CardContent sx={{ py: 2, '&:last-child': { pb: 2 }, display: 'flex', alignItems: 'center', gap: 2 }}>
          <Box sx={{
            width: 48, height: 48, borderRadius: 2, flexShrink: 0, display: 'flex',
            alignItems: 'center', justifyContent: 'center',
            bgcolor: 'action.disabledBackground', color: 'text.secondary',
            '& .MuiSvgIcon-root': { fontSize: 26 },
          }}>
            <ErrorOutlineIcon />
          </Box>
          <Box sx={{ minWidth: 0 }}>
            <Typography variant="h6" sx={{ fontWeight: 700, lineHeight: 1.15 }}>
              {t('dashboard.signals.loadErrorTitle', 'No se pudo cargar el estado del empleado')}
            </Typography>
            <Typography variant="body2" color="text.secondary">
              {t('dashboard.signals.loadErrorHint', 'Reintenta en unos segundos. No significa que esté todo al día.')}
            </Typography>
          </Box>
      </CardContent>
    </Card>
  );
  }
  const clear = totalPending === 0;
  const tone: 'success' | 'warning' = clear ? 'success' : 'warning';
  return (
    <Card variant="outlined" sx={{
      borderRadius: 2, mb: 2, borderLeft: '4px solid', borderLeftColor: `${tone}.main`,
      bgcolor: (th) => alpha(th.palette[tone].main, 0.05),
    }}>
      <CardContent sx={{ py: 2, '&:last-child': { pb: 2 } }}>
        <Stack direction="row" spacing={2} alignItems="center" flexWrap="wrap" useFlexGap>
          <Box sx={{
            width: 48, height: 48, borderRadius: 2, flexShrink: 0, display: 'flex',
            alignItems: 'center', justifyContent: 'center',
            bgcolor: `${tone}.main`, color: '#fff', '& .MuiSvgIcon-root': { fontSize: 26 },
          }}>
            {clear ? <CheckCircleOutlineIcon /> : <NotificationsActiveOutlinedIcon />}
          </Box>
          <Box sx={{ minWidth: 0 }}>
            <Typography variant="h5" sx={{ fontWeight: 800, lineHeight: 1.1, color: `${tone}.main` }}>
              {clear ? t('dashboard.signals.allClear', 'Todo al día') : totalPending}
            </Typography>
            <Typography variant="body2" color="text.secondary">
              {clear
                ? t('dashboard.signals.allClearHint', 'El empleado no tiene nada pendiente de tu revisión.')
                : t('dashboard.signals.needsAttention', 'Necesita tu atención')}
            </Typography>
          </Box>
          <Box sx={{ flex: 1 }} />
          {!clear && (
            <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
              {cards.filter((c) => c.pending > 0).map((c) => (
                <Chip key={c.slug} clickable color="warning" variant="outlined"
                  onClick={() => onOpen(c.slug)} label={`${c.name}: ${c.pending}`}
                  sx={{ fontWeight: 600 }} />
              ))}
            </Stack>
          )}
        </Stack>
      </CardContent>
    </Card>
  );
}

function SignalsOverview() {
  const { t } = useTranslation();
  const pickLabel = usePickLabel();
  const navigate = useNavigate();
  const { manifest } = useManifest();
  const all: any[] = (manifest as any)?.domain_entities ?? [];
  // Exactly ONE card per canonical table, in a fixed order — deterministic and
  // dedup-proof (no ghost/duplicate cards from a repeated manifest entry).
  const entities = SIGNAL_TABLES
    .map((tbl) => all.find((e: any) => String(e?.table ?? '').toLowerCase() === tbl
      && (e?.source ?? 'local') === 'local' && e?.slug))
    .filter(Boolean);
  // Fetch the 3 summaries ONCE here (shared react-query cache with the cards) so
  // the "needs attention" hero can aggregate without a second round-trip.
  const summaries = useQueries({
    queries: entities.map((e: any) => ({
      queryKey: ['dash-summary', e.slug],
      queryFn: () => apiFetch(`/api/domain/${e.slug}/summary`).then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json();
      }),
      staleTime: 60_000, retry: false,
    })),
  });
  if (entities.length === 0) return null;
  const cards = entities.map((e: any, i: number) => ({
    slug: e.slug,
    name: pickLabel(e, e.slug),
    total: summaries[i]?.data?.total ?? 0,
    pending: pendingCount(summaries[i]?.data?.by ?? {}),
  }));
  const heroLoading = summaries.some((s) => !s.data && s.isLoading);
  // A settled failure must NOT read as "todo al día": surface it at the hero.
  const heroError = summaries.some((s) => s.isError) && !heroLoading;
  return (
    <Box sx={{ mb: 3 }}>
      <SectionTitle>{t('dashboard.signals.title', 'Lo que genera el empleado')}</SectionTitle>
      <AttentionHero cards={cards} onOpen={(slug) => navigate(`/domain/${slug}`)} loading={heroLoading} error={heroError} />
      <Grid container spacing={2}>
        {entities.map((e: any, i: number) => (
          <Grid item xs={12} md={4} key={e.slug}>
            <SignalCard entity={e} summary={summaries[i]?.data}
              summaryError={!!summaries[i]?.isError} />
          </Grid>
        ))}
      </Grid>
    </Box>
  );
}

export function DashboardPage() {
  const { t } = useTranslation();
  const pickLabel = usePickLabel();
  const { token, logout } = useAuth();
  const [kpis, setKpis] = useState<Kpi[] | null>(null);
  const [trends, setTrends] = useState<Trend[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // U2: dashboard auto-refresh every 60 s while the tab is visible.
  // ``softRefresh`` skips the loading spinner so the operator sees a
  // smooth update instead of a flicker.
  useEffect(() => {
    let cancelled = false;
    const run = async (softRefresh = false) => {
      try {
        if (!softRefresh) setLoading(true);
        const resp = await fetch(`${apiBase}/dashboard/summary`, {
          headers: { Authorization: `Bearer ${token ?? ''}` },
        });
        if (resp.status === 401) {
          logout();
          return;
        }
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        if (cancelled) return;
        setKpis(data.kpis ?? []);
        setTrends(data.trends ?? []);
        setError(null);
      } catch (exc: any) {
        if (!cancelled) setError(exc?.message ?? 'error');
      } finally {
        if (!cancelled && !softRefresh) setLoading(false);
      }
    };
    run();

    let timer: number | undefined;
    const startTimer = () => {
      timer = window.setInterval(() => {
        if (document.visibilityState === 'visible') {
          run(true);
        }
      }, 60_000);
    };
    const onVisibility = () => {
      if (document.visibilityState === 'visible') {
        run(true);
      }
    };
    startTimer();
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisibility);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  const empty = useMemo(
    () => !loading && (kpis?.length ?? 0) === 0 && (trends?.length ?? 0) === 0,
    [loading, kpis, trends],
  );

  return (
    <Box>
      <PageHeader
        title={t('dashboard.title')}
        subtitle={t('dashboard.subtitle')}
      />

      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}

      {loading && (
        <Stack alignItems="center" sx={{ py: 6 }}>
          <CircularProgress />
        </Stack>
      )}

      {empty && !error && (
        <Alert severity="info">
          {t('dashboard.empty')}
        </Alert>
      )}

      {(kpis?.length ?? 0) > 0 && (
        <>
          <SectionTitle>{t('dashboard.kpis')}</SectionTitle>
          <Grid container spacing={2} sx={{ mb: 3 }}>
            {kpis!.map((k) => {
              const color = kpiColor(k);
              return (
                <Grid item xs={12} sm={6} md={3} key={k.id}>
                  <Card variant="outlined" sx={{ height: '100%' }}>
                    <CardContent sx={{ minWidth: 0 }}>
                      {/* A6: tooltip with the full label so a truncated
                          KPI name (e.g. "Customer Acquisition Cost
                          EMEA YoY") is still discoverable. */}
                      <Tooltip title={pickLabel(k, k.id)} placement="top" arrow>
                        <Typography
                          variant="overline"
                          color="text.secondary"
                          sx={{
                            display: 'block',
                            whiteSpace: 'nowrap',
                            overflow: 'hidden',
                            textOverflow: 'ellipsis',
                          }}
                        >
                          {pickLabel(k, k.id)}
                        </Typography>
                      </Tooltip>
                      <Stack
                        direction="row"
                        alignItems="center"
                        spacing={1}
                        useFlexGap
                        flexWrap="wrap"
                        sx={{ minWidth: 0 }}
                      >
                        <Typography
                          variant="h4"
                          sx={{
                            fontSize: { xs: '1.5rem', md: '1.75rem' },
                            lineHeight: 1.2,
                            wordBreak: 'break-word',
                            overflowWrap: 'anywhere',
                          }}
                        >
                          {fmt(k.value, k.format)}
                        </Typography>
                        {k.target !== null && k.target !== undefined && (
                          <Chip
                            size="small"
                            color={color}
                            label={`${t('dashboard.target')}: ${fmt(k.target, k.format)}`}
                            icon={k.good_when === 'higher' ? <TrendingUpIcon /> : <TrendingDownIcon />}
                            sx={{ maxWidth: '100%' }}
                          />
                        )}
                      </Stack>
                      {k.status === 'error' && (
                        // The database's raw message is for whoever fixes it, not the face of
                        // the panel (it showed «(sqlite3.OperationalError) no such column…»).
                        <Tooltip title={k.error || ''} arrow>
                          <Typography variant="caption" color="error">
                            {t('dashboard.indicatorFailed', 'No se pudo calcular: su consulta tiene un fallo. Pídele al agente de código que la revise.')}
                          </Typography>
                        </Tooltip>
                      )}
                    </CardContent>
                  </Card>
                </Grid>
              );
            })}
          </Grid>
        </>
      )}

      {!loading && <SignalsOverview />}

      {(trends?.length ?? 0) > 0 && (
        <>
          <SectionTitle>{t('dashboard.trends')}</SectionTitle>
          <Grid container spacing={2}>
            {trends!.map((tr) => (
              <Grid item xs={12} md={6} key={tr.id}>
                <Card variant="outlined">
                  <CardContent>
                    <Typography variant="subtitle1" gutterBottom>
                      {pickLabel(tr, tr.id)}
                    </Typography>
                    {tr.status === 'error' ? (
                      <Tooltip title={tr.error || ''} arrow>
                        <Alert severity="warning">
                          {t('dashboard.trendFailed', 'No se pudo dibujar: su consulta tiene un fallo. Pídele al agente de código que la revise.')}
                        </Alert>
                      </Tooltip>
                    ) : (
                      <Box sx={{ width: '100%', height: 240 }}>
                        <ResponsiveContainer>
                          {tr.kind === 'bar' ? (
                            <BarChart data={tr.data}>
                              <CartesianGrid strokeDasharray="3 3" />
                              <XAxis dataKey={tr.x} />
                              <YAxis />
                              <RTooltip />
                              <Bar dataKey={tr.y} />
                            </BarChart>
                          ) : (
                            <LineChart data={tr.data}>
                              <CartesianGrid strokeDasharray="3 3" />
                              <XAxis dataKey={tr.x} />
                              <YAxis />
                              <RTooltip />
                              <Line type="monotone" dataKey={tr.y} dot={false} />
                            </LineChart>
                          )}
                        </ResponsiveContainer>
                      </Box>
                    )}
                  </CardContent>
                </Card>
              </Grid>
            ))}
          </Grid>
        </>
      )}
    </Box>
  );
}

export default DashboardPage;
