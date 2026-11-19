import { useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  IconButton,
  InputAdornment,
  Menu,
  MenuItem,
  Paper,
  Snackbar,
  Stack,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material';
import { DataGrid, GridColDef, GridRowSelectionModel, GridSortModel } from '@mui/x-data-grid';
import RefreshIcon from '@mui/icons-material/Refresh';
import SearchIcon from '@mui/icons-material/Search';
import StorageIcon from '@mui/icons-material/Storage';
import EditNoteIcon from '@mui/icons-material/EditNote';
import CheckCircleOutlineIcon from '@mui/icons-material/CheckCircleOutline';
import HighlightOffIcon from '@mui/icons-material/HighlightOff';
import ArrowDropDownIcon from '@mui/icons-material/ArrowDropDown';
import DoneAllIcon from '@mui/icons-material/DoneAll';
import SmartToyIcon from '@mui/icons-material/SmartToy';
import { useTranslation } from 'react-i18next';
import { PageHeader } from '../components/PageHeader';
import { useAuth } from '../auth/AuthContext';
import { apiBase } from '../services/apiClient';
import { useManifest } from '../hooks/useManifest';
import { usePickLabel } from '../utils/dataLabels';
import { formatDate, formatDateTime } from '../utils/datetime';

const STATUS_COLOR_MAP: Record<string, 'default' | 'success' | 'error' | 'warning' | 'info' | 'primary'> = {
  COMPLETED: 'success',
  PENDING:   'warning',
  CLAIMED:   'info',
  RUNNING:   'info',
  FAILED:    'error',
  RETRY_SCHEDULED: 'warning',
  CANCELLED: 'default',
  // Lower-case business statuses common in retail / banking demos.
  paid:      'success',
  shipped:   'success',
  delivered: 'success',
  cancelled: 'default',
  refunded:  'warning',
  pending:   'warning',
  draft:     'default',
  active:    'success',
  inactive:  'default',
  open:      'info',
  closed:    'default',
  ack:       'info',
  high:      'error',
  medium:    'warning',
  low:       'success',
  // Agent output tables (alerts / recommendations / decisions / comms).
  critical:     'error',
  warning:      'warning',
  info:         'info',
  urgent:       'error',
  acknowledged: 'warning',
  resolved:     'success',
  dismissed:    'default',
  accepted:     'success',
  rejected:     'default',
  done:         'success',
  ready:        'info',
  sent:         'success',
  failed:       'error',
  error:        'error',
  replied:      'success',
  skipped:      'default',
  filtered:     'default',
  no_action:    'default',
  deferred:     'warning',
  escalated:    'error',
};

// Columns that hold a categorical state and deserve a chip even when
// the manifest didn't declare ``format: status`` explicitly (the agent
// output tables — alerts, recommendations… — usually don't).
const CHIPPY_COLUMN_RE = /(^|_)(status|severity|priority|band|decision|state)($|_)/i;

// Width budget per column format. Giving every column the same
// ``flex: 1`` made narrow content (an integer id, a status chip)
// hog a quarter of the table while the long text column truncated —
// the table looked "indented" because the right-aligned id column
// rendered as a big empty leading band. Manifest-declared ``flex`` /
// ``min_width`` still win.
const SIZE_BY_FORMAT: Record<string, { flex?: number; width?: number; minWidth?: number }> = {
  integer:  { width: 90 },
  boolean:  { width: 90 },
  percent:  { flex: 0.5, minWidth: 100 },
  number:   { flex: 0.6, minWidth: 110 },
  currency: { flex: 0.6, minWidth: 120 },
  date:     { flex: 0.6, minWidth: 110 },
  // Wider so the tz abbreviation (e.g. "13:00 CEST") fits without
  // truncation now that timestamps carry their timezone label.
  datetime: { flex: 0.8, minWidth: 195 },
  status:   { flex: 0.5, minWidth: 120 },
  text:     { flex: 2,   minWidth: 200 },
};

// Title-case a raw enum code for filter options that have no states.*
// translation (e.g. domain categories). Status codes still resolve via
// the i18n states dictionary first.
// Accepts any value: filter options for read-only (project) entities can be
// numbers/booleans, and calling string methods on those throws
// "r.replace is not a function". Coerce to string first.
const humanise = (s: any): string => {
  const str = s === null || s === undefined ? '' : String(s);
  return !str ? '' : str.replace(/[-_]+/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
};

const statusColor = (value: string) =>
  STATUS_COLOR_MAP[value] ??
  STATUS_COLOR_MAP[value.toUpperCase()] ??
  STATUS_COLOR_MAP[value.toLowerCase()] ??
  'default';

const renderStatusChip = (raw: any, labelFor?: (v: string) => string) => {
  if (raw === null || raw === undefined || raw === '') return <span>—</span>;
  const value = String(raw);
  return (
    <Chip
      size="small"
      color={statusColor(value)}
      label={labelFor ? labelFor(value) : value}
      sx={{ fontWeight: 600, textTransform: 'uppercase', letterSpacing: 0.4 }}
    />
  );
};

// Lifecycle cell — an interactive status chip on entities whose
// manifest (or the built-in defaults for the canonical agent tables)
// declares a ``lifecycle``. Clicking opens the allowed transitions
// for the CURRENT state; choosing one PATCHes
// /api/domain/{slug}/{pk} and refreshes the list. A status column
// nobody can manage is decoration — this is where alert triage and
// recommendation accept/reject happen without leaving the table.
function LifecycleCell({
  slug, pk, value, lifecycle, token, onChanged, onError, labelFor,
}: {
  slug: string;
  pk: any;
  value: any;
  lifecycle: any;
  token: string | null;
  onChanged: () => void;
  onError: (msg: string) => void;
  labelFor: (v: string) => string;
}) {
  const { t } = useTranslation();
  const [anchor, setAnchor] = useState<null | HTMLElement>(null);
  const [busy, setBusy] = useState(false);
  // Picking a target state opens a small dialog so the operator can attach an
  // OPTIONAL comment to this change (recorded on the entity's comment thread,
  // visible in the detail). Blank comment = a plain transition.
  const [pending, setPending] = useState<string | null>(null);
  const [comment, setComment] = useState('');
  const current = String(value ?? '');
  const next: string[] = lifecycle?.transitions?.[current]
    ?? (lifecycle?.states ?? []).filter((s: string) => s !== current);

  const doTransition = async (state: string, note: string) => {
    setBusy(true);
    try {
      const r = await fetch(`${apiBase}/domain/${slug}/${encodeURIComponent(String(pk))}`, {
        method: 'PATCH',
        headers: {
          Authorization: `Bearer ${token ?? ''}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ status: state, comment: note.trim() || undefined }),
      });
      if (r.ok) { setPending(null); setComment(''); onChanged(); return; }
      // A rejected transition (bad target state, conflict, 401) used to do
      // NOTHING — the chip just reverted and the operator saw no reason why.
      if (r.status === 401) { onError(t('common.sessionExpired', 'Sesión caducada, vuelve a entrar')); return; }
      const text = await r.text().catch(() => '');
      onError(text || t('domain.actionFailed', 'No se pudo cambiar el estado'));
    } catch {
      onError(t('domain.actionFailed', 'No se pudo cambiar el estado'));
    } finally {
      setBusy(false);
    }
  };

  if (!current) return <span>—</span>;
  if (!next.length) {
    return renderStatusChip(current, labelFor);
  }
  return (
    <>
      <Chip
        size="small"
        color={statusColor(current)}
        label={labelFor(current)}
        disabled={busy}
        onClick={(e) => { e.stopPropagation(); setAnchor(e.currentTarget); }}
        onDelete={(e: any) => { e.stopPropagation?.(); setAnchor(e.currentTarget?.parentElement ?? null); }}
        deleteIcon={<ArrowDropDownIcon />}
        sx={{ fontWeight: 600, textTransform: 'uppercase', letterSpacing: 0.4 }}
      />
      <Menu
        anchorEl={anchor}
        open={!!anchor}
        onClose={() => setAnchor(null)}
        onClick={(e) => e.stopPropagation()}
      >
        {next.map((s) => (
          <MenuItem key={s} dense onClick={() => { setAnchor(null); setComment(''); setPending(s); }}>
            <Chip size="small" color={statusColor(s)} label={labelFor(s)} sx={{ mr: 1, pointerEvents: 'none' }} />
          </MenuItem>
        ))}
      </Menu>
      <Dialog open={!!pending} onClose={() => !busy && setPending(null)} maxWidth="xs" fullWidth onClick={(e) => e.stopPropagation()}>
        <DialogTitle>{t('lifecycle.markAs', { state: pending ? labelFor(pending) : '' })}</DialogTitle>
        <DialogContent>
          <TextField
            autoFocus
            fullWidth
            multiline
            minRows={2}
            maxRows={5}
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            placeholder={t('lifecycle.commentPlaceholder', 'Comentario para este cambio (opcional)')}
            sx={{ mt: 1 }}
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setPending(null)} disabled={busy}>{t('common.cancel', 'Cancelar')}</Button>
          <Button variant="contained" disabled={busy} onClick={() => pending && doTransition(pending, comment)}>
            {t('common.confirm', 'Confirmar')}
          </Button>
        </DialogActions>
      </Dialog>
    </>
  );
}

const renderBooleanChip = (raw: any) => {
  if (raw === null || raw === undefined) return <span>—</span>;
  const truthy = raw === true || raw === 1 || raw === '1' || String(raw).toLowerCase() === 'true';
  return (
    <Chip
      size="small"
      variant="outlined"
      color={truthy ? 'success' : 'default'}
      icon={truthy ? <CheckCircleOutlineIcon /> : <HighlightOffIcon />}
      label={truthy ? '✓' : '✕'}
      sx={{ minWidth: 64 }}
    />
  );
};

// Tenant currency from the manifest (set on window by Layout). EUR as
// back-compat default only.
const tenantCurrency = () =>
  (typeof window !== 'undefined' && (window as unknown as { __waiCurrency?: string }).__waiCurrency) || 'EUR';

const fmtCell = (v: any, format: string | undefined) => {
  if (v === null || v === undefined) return '—';
  switch (format) {
    case 'currency':
      return Number(v).toLocaleString(undefined, { style: 'currency', currency: tenantCurrency() });
    case 'percent':
      return `${(Number(v) * 100).toFixed(1)}%`;
    case 'integer':
      return Math.round(Number(v)).toLocaleString();
    case 'number':
      return Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 });
    case 'datetime':
      // Business-timezone render (utils/datetime) — never the raw
      // browser locale, so a London and a Madrid operator agree.
      return formatDateTime(v);
    case 'date':
      return formatDate(v);
    default:
      return String(v);
  }
};

export function DomainListPage() {
  const { slug } = useParams<{ slug: string }>();
  const { t } = useTranslation();
  const pickLabel = usePickLabel();
  const { token, logout } = useAuth();
  const { manifest } = useManifest();
  const navigate = useNavigate();

  const entity = useMemo(() => {
    const list = (manifest as any)?.domain_entities ?? [];
    return list.find((e: any) => e.slug === slug);
  }, [manifest, slug]);

  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(25);
  const [rows, setRows] = useState<any[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sortModel, setSortModel] = useState<GridSortModel>([]);
  const [q, setQ] = useState('');
  const [filters, setFilters] = useState<Record<string, string>>({});
  // Bumped after a lifecycle transition so the grid refetches and the
  // chip reflects the persisted state.
  const [refreshTick, setRefreshTick] = useState(0);
  // Bulk selection + actions (agent-activity entities with a lifecycle).
  const [selection, setSelection] = useState<GridRowSelectionModel>([]);
  const [bulkAnchor, setBulkAnchor] = useState<null | HTMLElement>(null);
  const [bulkBusy, setBulkBusy] = useState(false);
  const [bulkMsg, setBulkMsg] = useState<string | null>(null);
  // The bottom Snackbar is shared by bulk results AND lifecycle errors, so it
  // carries a severity instead of being hardcoded "success".
  const [snackSeverity, setSnackSeverity] = useState<'success' | 'error'>('success');
  // v7.142.0 — "show me what THIS automation left behind" (and, if asked, only what
  // is still open). It lives in the URL, not in local state, so the governance
  // panel's link is shareable and the back button undoes it. The backend returns
  // WHOSE output is being filtered, so there is no need to guess it here.
  const [searchParams, setSearchParams] = useSearchParams();
  const provAutomation = searchParams.get('created_by_automation') ?? '';
  const provOnlyOpen = searchParams.get('only_open') ?? '';
  const [provContext, setProvContext] = useState<any | null>(null);
  // v7.143.1 — the filter stops being a one-way link only: the operator can
  // pick the autonomous job from here. The options are the ones that HAVE written
  // to this entity (the backend resolves them), not the whole catalog: offering
  // an automation that never produced anything here only shows empty lists.
  const [producers, setProducers] = useState<{ id: number; title: string; count: number }[]>([]);

  // React Router reuses the same DomainList instance when navigating
  // between /domain/<a> and /domain/<b> (the route pattern is identical
  // — only the :slug param changes). Without an explicit reset, every
  // piece of local state — search, filter values, sort model, pagination,
  // even the in-flight rows — leaks into the next entity. That makes
  // filters from /domain/orders show up (and silently apply) on
  // /domain/customers, which then either filters by a column the new
  // entity doesn't have (backend ignores it) or just renders the wrong
  // chips. Wiping every per-entity field whenever the slug changes
  // restores per-entity isolation. The id-of-row keys come from the
  // entity's `filters[]` definition, so we never carry over filter ids
  // that the next entity doesn't even know about.
  useEffect(() => {
    setQ('');
    setFilters({});
    setSortModel([]);
    setPage(0);
    setRows([]);
    setTotal(0);
    setError(null);
    setSelection([]);
  }, [slug]);

  // Set of filter ids the CURRENT entity actually exposes. Used both
  // to render chips and to forward query params, so leftover values
  // from a previous entity (during the brief render between the
  // ``setFilters({})`` reset and the fetch effect) can never travel.
  const allowedFilterIds = useMemo(
    () => new Set<string>((entity?.filters ?? []).map((f: any) => f.id)),
    [entity],
  );

  useEffect(() => {
    if (!entity || (entity.source ?? 'project') !== 'local') { setProducers([]); return; }
    let alive = true;
    fetch(`${apiBase}/domain/${slug}/producers`, {
      headers: { Authorization: `Bearer ${token ?? ''}` },
    })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (alive && d) setProducers(d.items ?? []); })
      // The filter is an extra: if it can't be resolved, the list is still
      // the list. Never an on-screen error because of a dropdown.
      .catch(() => undefined);
    return () => { alive = false; };
  }, [entity, slug, token, refreshTick]);

  // Lives in the URL so the link is shareable and "back" undoes it.
  // Switching job does NOT carry over the previous one's "only open": it was
  // inherited from a link that said something else and would mislead about what is shown.
  const setProvenance = (id: string) => {
    const next = new URLSearchParams(searchParams);
    if (id) next.set('created_by_automation', id);
    else next.delete('created_by_automation');
    next.delete('only_open');
    setSearchParams(next, { replace: true });
    setPage(0);
  };

  useEffect(() => {
    if (!entity) return;
    const sort = sortModel[0]?.field ?? entity.default_sort?.column ?? entity.primary_key;
    const direction = sortModel[0]?.sort ?? entity.default_sort?.direction ?? 'desc';
    const params = new URLSearchParams({
      page: String(page + 1),
      page_size: String(pageSize),
      sort,
      direction,
    });
    if (q) params.set('q', q);
    Object.entries(filters).forEach(([k, v]) => {
      if (v && allowedFilterIds.has(k)) params.set(k, v);
    });
    if (provAutomation) {
      params.set('created_by_automation', provAutomation);
      if (provOnlyOpen) params.set('only_open', provOnlyOpen);
    }

    setLoading(true);
    setError(null);
    fetch(`${apiBase}/domain/${slug}?${params.toString()}`, {
      headers: { Authorization: `Bearer ${token ?? ''}` },
    })
      .then(async (r) => {
        if (r.status === 401) {
          logout();
          return null;
        }
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((data) => {
        if (!data) return;
        setRows(data.rows ?? []);
        setTotal(data.total ?? 0);
        setProvContext(data.filtered_by_automation ?? null);
      })
      .catch((exc) => setError(exc?.message ?? 'error'))
      .finally(() => setLoading(false));
  }, [entity, slug, page, pageSize, sortModel, q, filters, token, refreshTick,
      provAutomation, provOnlyOpen]);

  if (!manifest) {
    return <Box>{t('common.loading')}</Box>;
  }
  if (!entity) {
    return (
      <Alert severity="warning">{t('domain.unknownEntity', { slug })}</Alert>
    );
  }

  const columns: GridColDef[] = (entity.list_columns ?? []).map((c: any) => {
    const effFormat =
      c.format ?? (CHIPPY_COLUMN_RE.test(c.id) ? 'status' : 'text');
    const size = SIZE_BY_FORMAT[effFormat] ?? SIZE_BY_FORMAT.text;
    // Canonical agent-table columns get the localised business name
    // from i18n `fields.*`; domain columns keep the manifest label.
    const headerName = String(t(`fields.${c.id}`, {
      defaultValue: pickLabel(c, c.label ?? c.id),
    }));
    // NEVER NARROWER THAN ITS HEADER. The width came from the data format alone (90 px for an
    // integer), so «LEADS TOTALES» showed as «LEADS TO…» next to a half-empty text column
    // (04/10/2026). Uppercase 12 px bold ≈ 8 px a character, plus the sort icon and padding.
    const cabe = Math.ceil(headerName.length * 8) + 48;
    const base: GridColDef = {
      field: c.id,
      headerName,
      sortable: c.sortable !== false,
      ...(size.width && c.flex === undefined
        ? { width: Math.max(c.min_width ?? size.width, cabe) }
        : {
            flex: c.flex ?? size.flex ?? 1,
            minWidth: Math.max(c.min_width ?? size.minWidth ?? 140, cabe),
          }),
    };
    const stateLabel = (v: string) => t(`states.${v}`, { defaultValue: v });
    // The lifecycle field renders as an INTERACTIVE chip with the
    // allowed transitions; other status-like columns stay read-only.
    if (entity.lifecycle && c.id === entity.lifecycle.field) {
      return {
        ...base,
        renderCell: (p: any) => (
          <LifecycleCell
            slug={slug as string}
            pk={p.row?.[entity.primary_key] ?? p.id}
            value={p.value}
            lifecycle={entity.lifecycle}
            token={token}
            onChanged={() => setRefreshTick((n) => n + 1)}
            onError={(msg) => { setSnackSeverity('error'); setBulkMsg(msg); }}
            labelFor={stateLabel}
          />
        ),
      };
    }
    if (c.format === 'status' || (!c.format && CHIPPY_COLUMN_RE.test(c.id))) {
      return { ...base, renderCell: (p: any) => renderStatusChip(p.value, stateLabel) };
    }
    if (c.format === 'boolean') {
      return {
        ...base,
        align: 'center' as const,
        headerAlign: 'center' as const,
        renderCell: (p: any) => renderBooleanChip(p.value),
      };
    }
    if (c.format === 'currency' || c.format === 'percent' || c.format === 'integer' || c.format === 'number') {
      return {
        ...base,
        align: 'right' as const,
        headerAlign: 'right' as const,
        valueFormatter: (value: any) => fmtCell(value, c.format),
      };
    }
    if (c.format === 'datetime' || c.format === 'date') {
      return {
        ...base,
        valueFormatter: (value: any) => fmtCell(value, c.format),
      };
    }
    // Default: plain text, truncated by the grid — surface the full
    // value on hover so long descriptions stay inspectable in place.
    return {
      ...base,
      renderCell: (p: any) => {
        const text = fmtCell(p.value, c.format);
        return (
          <span title={text} style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>
            {text}
          </span>
        );
      },
    };
  });

  // "Did the employee do this on its own, and in which round?" — until now
  // you only knew by opening each record one by one. Added only when some
  // automation has written here: an empty column on a purely manual entity
  // would be noise on every row.
  if (producers.length > 0) {
    columns.push({
      field: '__origin',
      headerName: t('domain.origin.column', 'Origen'),
      sortable: false,
      width: 200,
      renderCell: (p: any) => {
        const origin = p.row?.created_by_automation;
        if (!origin) {
          return (
            <Typography variant="caption" color="text.secondary">
              {t('domain.origin.manual', 'Manual')}
            </Typography>
          );
        }
        const via = origin.via === 'event'
          ? t('domain.origin.viaEvent', 'reacción automática, sin consumo del modelo')
          : t('domain.origin.viaRun', 'el empleado lo decidió durante su ronda');
        return (
          <Tooltip title={`${t('domain.origin.chipHelp', 'Creado por el trabajo autónomo')}: ${origin.title} — ${via}`}>
            <Chip
              size="small"
              variant="outlined"
              color="secondary"
              icon={<SmartToyIcon />}
              label={origin.title}
              onClick={(e) => { e.stopPropagation(); setProvenance(String(origin.automation_id)); }}
              sx={{ maxWidth: '100%' }}
            />
          </Tooltip>
        );
      },
    });
  }

  const isLocal = (entity.source ?? 'project') === 'local';
  const accessKind = isLocal
    ? (entity.access ?? 'write')
    : (entity.access ?? 'read');
  const accessChip = (
    <Chip
      size="small"
      icon={accessKind === 'write' ? <EditNoteIcon /> : <StorageIcon />}
      color={accessKind === 'write' ? 'primary' : 'default'}
      variant="outlined"
      label={t(`domain.access.${accessKind}`)}
    />
  );

  // Bulk actions only apply to agent-activity entities that carry a
  // lifecycle (the only meaningful bulk op is a state transition —
  // triage many alerts, accept/reject many recommendations at once).
  const canBulk = isLocal && !!entity.lifecycle;
  const bulkStates: string[] = entity.lifecycle?.states ?? [];

  const applyBulk = async (state: string) => {
    setBulkAnchor(null);
    if (!selection.length) return;
    setBulkBusy(true);
    try {
      const r = await fetch(`${apiBase}/domain/${slug}/bulk`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${token ?? ''}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ ids: selection, status: state }),
      });
      if (r.status === 401) { logout(); return; }
      if (!r.ok) { setSnackSeverity('error'); setBulkMsg(t('bulk.failed')); return; }
      const data = await r.json();
      setSnackSeverity('success');
      setBulkMsg(
        t('bulk.result', {
          updated: data.updated_count ?? 0,
          skipped: data.skipped_count ?? 0,
        }),
      );
      setSelection([]);
      setRefreshTick((n) => n + 1);
    } catch {
      setSnackSeverity('error');
      setBulkMsg(t('bulk.failed'));
    } finally {
      setBulkBusy(false);
    }
  };

  const filterLabel = (f: any) => pickLabel(f, f.label ?? f.id);
  const activeFilters = Object.entries(filters).filter(
    ([k, v]) => v && allowedFilterIds.has(k),
  );
  const hasToolbar =
    !!entity.search_columns?.length ||
    (entity.filters ?? []).length > 0 ||
    activeFilters.length > 0 ||
    producers.length > 0;

  return (
    <Box>
      <PageHeader
        title={pickLabel(entity, entity.slug)}
        subtitle={
          <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap">
            {accessChip}
            {isLocal && (
              <Chip
                size="small"
                color="primary"
                variant="outlined"
                label={t('domain.localBadge')}
              />
            )}
            <Chip
              size="small"
              variant="outlined"
              label={t('domain.totalRows', { count: total })}
            />
          </Stack>
        }
        actions={
          <Tooltip title={t('domain.refresh') as string}>
            {/* Bump refreshTick (the fetch effect's dep) — writing the same
                page value was a no-op: React bailed and the list never
                reloaded. */}
            <IconButton onClick={() => setRefreshTick((n) => n + 1)}>
              <RefreshIcon />
            </IconButton>
          </Tooltip>
        }
      />

      {/* Scope inherited from the governance panel. Without this notice, "4 rows" reads
          as "there are only 4", which is a wrong operational conclusion. */}
      {provContext && (
        <Alert severity="info" sx={{ mb: 2 }}
          action={
            <Button size="small" onClick={() => navigate(`/domain/${slug}`)}>
              {t('domain.origin.clearFilter', 'Ver todo')}
            </Button>
          }>
          {provContext.only_open
            ? t('domain.origin.scopedOpen', 'Solo lo que dejó sin cerrar la automatización «{{title}}».', { title: provContext.title })
            : t('domain.origin.scoped', 'Solo lo que ha creado la automatización «{{title}}».', { title: provContext.title })}
        </Alert>
      )}

      {hasToolbar && (
        <Paper
          variant="outlined"
          sx={{
            mb: 2,
            p: 1.5,
            borderRadius: 2,
            backgroundColor: (theme) => theme.palette.background.paper,
          }}
        >
          <Stack direction="row" spacing={1.5} sx={{ flexWrap: 'wrap', alignItems: 'center' }}>
            {entity.search_columns?.length ? (
              <TextField
                size="small"
                placeholder={t('domain.search') as string}
                value={q}
                onChange={(e) => { setQ(e.target.value); setPage(0); }}
                sx={{ minWidth: 260, flex: '1 1 260px' }}
                InputProps={{
                  startAdornment: (
                    <InputAdornment position="start">
                      <SearchIcon fontSize="small" />
                    </InputAdornment>
                  ),
                }}
              />
            ) : null}
            {producers.length > 0 && (
              <TextField
                size="small"
                select
                label={t('domain.origin.filterLabel', 'Trabajo autónomo')}
                value={provAutomation}
                onChange={(e) => setProvenance(e.target.value)}
                sx={{ minWidth: 230 }}
              >
                <MenuItem value="">{t('domain.origin.filterAny', 'Cualquier origen')}</MenuItem>
                {producers.map((p) => (
                  <MenuItem key={p.id} value={String(p.id)}>
                    {p.title} ({p.count})
                  </MenuItem>
                ))}
              </TextField>
            )}
            {(entity.filters ?? []).map((f: any) => {
              const setVal = (v: string) => {
                setFilters((cur) => ({ ...cur, [f.id]: v }));
                setPage(0);
              };
              if (f.kind === 'enum') {
                return (
                  <TextField
                    key={f.id}
                    size="small"
                    select
                    label={filterLabel(f)}
                    value={filters[f.id] ?? ''}
                    onChange={(e) => setVal(e.target.value)}
                    sx={{ minWidth: 180 }}
                  >
                    <MenuItem value="">{t('domain.filterAny')}</MenuItem>
                    {(f.options ?? []).map((opt: string) => (
                      <MenuItem key={opt} value={opt}>
                        {t(`states.${opt}`, { defaultValue: humanise(opt) })}
                      </MenuItem>
                    ))}
                  </TextField>
                );
              }
              if (f.kind === 'date_from' || f.kind === 'date_to') {
                return (
                  <TextField
                    key={f.id}
                    size="small"
                    type="date"
                    label={filterLabel(f)}
                    value={filters[f.id] ?? ''}
                    onChange={(e) => setVal(e.target.value)}
                    InputLabelProps={{ shrink: true }}
                    sx={{ minWidth: 180 }}
                  />
                );
              }
              if (f.kind === 'number_min' || f.kind === 'number_max') {
                return (
                  <TextField
                    key={f.id}
                    size="small"
                    type="number"
                    label={filterLabel(f)}
                    value={filters[f.id] ?? ''}
                    onChange={(e) => setVal(e.target.value)}
                    sx={{ minWidth: 160 }}
                  />
                );
              }
              return (
                <TextField
                  key={f.id}
                  size="small"
                  label={filterLabel(f)}
                  value={filters[f.id] ?? ''}
                  onChange={(e) => setVal(e.target.value)}
                  sx={{ minWidth: 180 }}
                />
              );
            })}
            {activeFilters.length > 0 && (
              <Stack direction="row" spacing={0.5} flexWrap="wrap" sx={{ ml: 'auto' }}>
                {activeFilters.map(([k, v]) => {
                  const def = (entity.filters ?? []).find((f: any) => f.id === k);
                  return (
                    <Chip
                      key={k}
                      size="small"
                      color="primary"
                      variant="outlined"
                      label={`${def ? filterLabel(def) : k}: ${t(`states.${v}`, { defaultValue: v })}`}
                      onDelete={() => setFilters((cur) => ({ ...cur, [k]: '' }))}
                    />
                  );
                })}
              </Stack>
            )}
          </Stack>
        </Paper>
      )}

      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}

      {/* Bulk action bar — appears once the operator ticks rows. Lets
          them push a single lifecycle transition to the whole block
          (the backend validates each row and reports how many were
          updated vs skipped). */}
      {canBulk && selection.length > 0 && (
        <Paper
          variant="outlined"
          sx={{
            mb: 2,
            p: 1.25,
            borderRadius: 2,
            display: 'flex',
            alignItems: 'center',
            gap: 1.5,
            flexWrap: 'wrap',
            borderColor: 'primary.main',
            backgroundColor: (theme) => theme.palette.action.hover,
          }}
        >
          <Chip
            color="primary"
            label={t('bulk.selected', { count: selection.length })}
            sx={{ fontWeight: 600 }}
          />
          <Button
            size="small"
            variant="contained"
            startIcon={<DoneAllIcon />}
            endIcon={<ArrowDropDownIcon />}
            disabled={bulkBusy}
            onClick={(e) => setBulkAnchor(e.currentTarget)}
          >
            {t('bulk.markAs')}
          </Button>
          <Menu
            anchorEl={bulkAnchor}
            open={!!bulkAnchor}
            onClose={() => setBulkAnchor(null)}
          >
            {bulkStates.map((s) => (
              <MenuItem key={s} dense onClick={() => applyBulk(s)}>
                <Chip
                  size="small"
                  color={statusColor(s)}
                  label={t(`states.${s}`, { defaultValue: s })}
                  sx={{ mr: 1, pointerEvents: 'none' }}
                />
              </MenuItem>
            ))}
          </Menu>
          <Button
            size="small"
            color="inherit"
            disabled={bulkBusy}
            onClick={() => setSelection([])}
            sx={{ ml: 'auto' }}
          >
            {t('bulk.clear')}
          </Button>
        </Paper>
      )}

      {/* A3: DataGrid on tablet+, vertical card list on phones. The
          grid is unusable horizontally on narrow viewports — operators
          end up pinch-zooming to read a single column. */}
      <Card variant="outlined" sx={{ borderRadius: 2, overflow: 'hidden', display: { xs: 'none', md: 'block' } }}>
        <Box sx={{ height: 640 }}>
          <DataGrid
            rows={rows.map((r, i) => ({ ...r, id: r[entity.primary_key] ?? r.id ?? i }))}
            getRowId={(r) => r[entity.primary_key] ?? r.id}
            columns={columns}
            rowCount={total}
            paginationMode="server"
            sortingMode="server"
            loading={loading}
            paginationModel={{ page, pageSize }}
            onPaginationModelChange={(m) => { setPage(m.page); setPageSize(m.pageSize); }}
            sortModel={sortModel}
            onSortModelChange={setSortModel}
            pageSizeOptions={[10, 25, 50, 100]}
            disableRowSelectionOnClick
            checkboxSelection={canBulk}
            rowSelectionModel={selection}
            onRowSelectionModelChange={(m) => setSelection(m)}
            keepNonExistentRowsSelected
            density="comfortable"
            slotProps={{
              loadingOverlay: { variant: 'linear-progress' as const, noRowsVariant: 'skeleton' as const },
            }}
            sx={(theme) => ({
              border: '1px solid',
              borderColor: 'divider',
              borderRadius: 2,
              overflow: 'hidden',
              backgroundColor: 'background.paper',
              // Clean, light header (small-caps labels) instead of the
              // heavy solid-blue band — reads as more modern.
              '--DataGrid-containerBackground': theme.palette.grey[50],
              '& .MuiDataGrid-columnHeaders': {
                borderBottom: `1px solid ${theme.palette.divider}`,
              },
              '& .MuiDataGrid-columnHeaderTitle': {
                fontWeight: 700,
                fontSize: '0.7rem',
                textTransform: 'uppercase',
                letterSpacing: '0.06em',
                color: theme.palette.text.secondary,
              },
              '& .MuiDataGrid-sortIcon, & .MuiDataGrid-menuIconButton, & .MuiDataGrid-filterIcon': {
                color: theme.palette.text.secondary,
              },
              '& .MuiDataGrid-columnSeparator': { display: 'none' },
              '& .MuiDataGrid-row': { transition: 'background-color .12s ease' },
              '& .MuiDataGrid-row:hover': {
                cursor: 'pointer',
                backgroundColor: theme.palette.action.hover,
              },
              '& .MuiDataGrid-cell': {
                alignItems: 'center',
                borderColor: 'divider',
                fontSize: '0.82rem',
              },
              '& .MuiDataGrid-columnHeader:focus, & .MuiDataGrid-columnHeader:focus-within, & .MuiDataGrid-cell:focus, & .MuiDataGrid-cell:focus-within': {
                outline: 'none',
              },
              '& .MuiDataGrid-footerContainer': {
                borderTop: '1px solid',
                borderColor: 'divider',
              },
            })}
            onRowClick={(p) => {
              const pk = (p.row as any)?.[entity.primary_key] ?? p.id;
              if (pk === undefined || pk === null) return;
              navigate(`/domain/${slug}/${encodeURIComponent(String(pk))}`);
            }}
          />
        </Box>
      </Card>

      {/* A3: phone-friendly card list. Renders the first 3 list_columns
          per row so the operator can identify and tap an entity. */}
      <Stack spacing={1} sx={{ display: { xs: 'flex', md: 'none' } }}>
        {rows.map((r, i) => {
          const pk = r[entity.primary_key] ?? r.id ?? i;
          const cols3 = (entity.list_columns ?? []).slice(0, 3);
          return (
            <Card
              key={pk}
              variant="outlined"
              sx={{ cursor: 'pointer' }}
              onClick={() => navigate(`/domain/${slug}/${encodeURIComponent(String(pk))}`)}
            >
              <CardContent sx={{ p: 1.5, '&:last-child': { pb: 1.5 } }}>
                {cols3.map((c: any, idx: number) => (
                  <Box
                    key={c.id ?? idx}
                    sx={{
                      display: 'flex',
                      gap: 1,
                      fontSize: idx === 0 ? 13 : 12,
                      color: idx === 0 ? 'text.primary' : 'text.secondary',
                      fontWeight: idx === 0 ? 600 : 400,
                      mt: idx === 0 ? 0 : 0.25,
                    }}
                  >
                    <Box sx={{ minWidth: 88, color: 'text.secondary', fontSize: 11 }}>
                      {c.label_en || c.label || c.id}
                    </Box>
                    <Box sx={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {fmtCell(r[c.id], c.format)}
                    </Box>
                  </Box>
                ))}
                {r.created_by_automation && (
                  <Chip
                    size="small"
                    variant="outlined"
                    color="secondary"
                    icon={<SmartToyIcon />}
                    label={r.created_by_automation.title}
                    sx={{ mt: 0.75, maxWidth: '100%' }}
                  />
                )}
              </CardContent>
            </Card>
          );
        })}
      </Stack>

      <Snackbar
        open={!!bulkMsg}
        autoHideDuration={5000}
        onClose={() => setBulkMsg(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}
      >
        <Alert severity={snackSeverity} variant="filled" onClose={() => setBulkMsg(null)} sx={{ width: '100%' }}>
          {bulkMsg}
        </Alert>
      </Snackbar>
    </Box>
  );
}

export default DomainListPage;
