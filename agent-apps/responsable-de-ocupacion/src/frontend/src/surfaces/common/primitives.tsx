/**
 * Cross-archetype building blocks every business surface reuses. Keeping them
 * here (not per-surface) is what makes the surfaces feel like ONE product and
 * gives new entities the same business affordances for free:
 *   - SurfaceShell  — page chrome + a KPI strip header.
 *   - KpiStrip      — the header stat chips (open criticals, pending, …).
 *   - EntityLink    — target_entity / source_event / run → REAL hyperlinks,
 *                     never a raw id (the "UI assist, not raw IDs" rule).
 *   - PersonChip    — a recipient/owner as a DIRECTORY user (avatar+name),
 *                     never a raw email/phone.
 *   - AgingChip     — created_at → age/SLA colour for still-open items.
 */
import { ReactNode, useEffect, useMemo, useState } from 'react';
import { Link as RouterLink } from 'react-router-dom';
import {
  Avatar, Box, Button, Checkbox, Chip, Dialog, DialogActions, DialogContent,
  DialogContentText, DialogTitle, Link, Menu, MenuItem, Stack, TextField, Tooltip, Typography,
} from '@mui/material';
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline';
import NotificationsActiveOutlinedIcon from '@mui/icons-material/NotificationsActiveOutlined';
import NotificationsOffOutlinedIcon from '@mui/icons-material/NotificationsOffOutlined';
import { useTranslation } from 'react-i18next';
import PageHeader from '../../components/PageHeader';
import NotifyRulesDialog from '../../components/NotifyRulesDialog';
import SuppressionsDialog from '../../components/SuppressionsDialog';
import { useManifest } from '../../hooks/useManifest';
import { useUsersDirectory } from '../../components/UserPicker';
import { formatRelative, formatDateTime } from '../../utils/datetime';
import { fetchRows, deleteRows, bulkStatus, EntityQuery, FetchRowsResult } from './api';
import type { ProvenanceContext } from './provenanceScope';
import { SurfaceGuideButton } from './guide';
import type { Archetype } from '../resolveArchetype';

// ---- data hook ------------------------------------------------------------

/** List rows for an entity with the grid contract. Refetches when `deps` or the
 * returned `refresh()` changes. Total-agnostic surfaces pass a big pageSize. */
export function useEntityRows(slug: string | undefined, query: EntityQuery, deps: any[] = []) {
  const [rows, setRows] = useState<any[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [provContext, setProvContext] = useState<ProvenanceContext | null>(null);
  const [tick, setTick] = useState(0);
  const refresh = () => setTick((t) => t + 1);
  useEffect(() => {
    if (!slug) return;
    let alive = true;
    setLoading(true);
    setError(null);
    fetchRows(slug, query)
      .then(({ rows, total, filtered_by_automation }: FetchRowsResult) => {
        if (alive) {
          setRows(rows);
          setTotal(total);
          setProvContext(filtered_by_automation ?? null);
        }
      })
      .catch((e) => { if (alive) setError(e?.message ?? 'error'); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug, tick, JSON.stringify(query), ...deps]);
  return { rows, total, loading, error, refresh, provContext };
}

/** Client-side pagination over an ALREADY-FILTERED array (status/date/search all
 * happen client-side on the fetched page, so we slice the visible set — the same
 * approach as the Actividad feed). Auto-clamps the page when the set shrinks. */
export function usePagedItems<T>(items: T[], perPage = 25) {
  const [page, setPage] = useState(1);
  const pageCount = Math.max(1, Math.ceil(items.length / perPage));
  const cur = Math.min(page, pageCount);
  useEffect(() => { if (page !== cur) setPage(cur); }, [cur, page]);
  const start = (cur - 1) * perPage;
  const pageItems = items.slice(start, start + perPage);
  return {
    page: cur, setPage, pageItems, pageCount, perPage,
    total: items.length,
    from: items.length ? start + 1 : 0,
    to: start + pageItems.length,
  };
}

/** Prev/Next pager with an "X–Y de N" caption. Renders nothing for a single page. */
export function Pager({ page, pageCount, total, from, to, onPage }: {
  page: number; pageCount: number; total: number; from: number; to: number;
  onPage: (p: number) => void;
}) {
  const { t } = useTranslation();
  if (total === 0 || pageCount <= 1) return null;
  return (
    <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mt: 2, flexWrap: 'wrap', gap: 1 }}>
      <Typography variant="caption" color="text.secondary">
        {t('surfaces.common.showing', { from, to, total, defaultValue: `Mostrando ${from}–${to} de ${total}` })}
      </Typography>
      <Stack direction="row" spacing={1} alignItems="center">
        <Button size="small" variant="outlined" disabled={page <= 1} onClick={() => onPage(page - 1)}>
          {t('surfaces.common.prev', 'Anterior')}
        </Button>
        <Typography variant="caption" color="text.secondary">{page} / {pageCount}</Typography>
        <Button size="small" variant="outlined" disabled={page >= pageCount} onClick={() => onPage(page + 1)}>
          {t('surfaces.common.next', 'Siguiente')}
        </Button>
      </Stack>
    </Stack>
  );
}

// ---- shell + KPIs ---------------------------------------------------------

export type Tone = 'default' | 'success' | 'error' | 'warning' | 'info' | 'primary';

export interface Kpi { label: string; value: ReactNode; tone?: Tone }

export function KpiStrip({ items }: { items: Kpi[] }) {
  if (!items.length) return null;
  return (
    <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap sx={{ mb: 2 }}>
      {items.map((k, i) => (
        <Chip
          key={i}
          color={k.tone ?? 'default'}
          variant={k.tone && k.tone !== 'default' ? 'filled' : 'outlined'}
          label={<span><b>{k.value}</b>&nbsp;{k.label}</span>}
          sx={{ borderRadius: 1.5, fontSize: 13 }}
        />
      ))}
    </Stack>
  );
}

export function SurfaceShell({
  title, subtitle, kpis, actions, guide, guideLifecycle, notifyTable, suppressTable, children,
}: {
  title: string;
  subtitle?: ReactNode;
  kpis?: Kpi[];
  actions?: ReactNode;
  /** Archetype → renders the "How does this screen work?" explainer in the
   * header, so every surface teaches itself without per-surface wiring. */
  guide?: Archetype;
  /** The entity lifecycle (states+transitions), so the generic board can draw
   * its state-machine diagram from the entity's own shape. */
  guideLifecycle?: any;
  /** v7.62.0 — when set (alerts/recommendations/decisions), the header gains a
   * "Reglas de notificación" button that opens the rules manager for that table. */
  notifyTable?: string;
  /** v7.64.0 — when set (alerts/recommendations/decisions), the header gains a
   * "Silencios" button that opens the no-repetir manager for that table. */
  suppressTable?: string;
  children: ReactNode;
}) {
  const { t } = useTranslation();
  const [rulesOpen, setRulesOpen] = useState(false);
  const [supprOpen, setSupprOpen] = useState(false);
  // Only the small "How does it work?" button rides in the header's actions slot
  // (which is flexShrink:0 and does NOT wrap). The FILTER TOOLBAR — chips +
  // several selects + the date range — is rendered as its OWN full-width row
  // below the header so it WRAPS instead of pushing the page wide (the horizontal
  // overflow that used to appear on the Alertas/Recomendaciones/Decisiones
  // screens once each gained impact/urgency/confidence/reason filters).
  return (
    <Box sx={{ minWidth: 0 }}>
      <PageHeader
        title={title}
        subtitle={subtitle}
        actions={
          <Stack direction="row" spacing={1} alignItems="center">
            {suppressTable && (
              <Button size="small" variant="outlined" startIcon={<NotificationsOffOutlinedIcon />}
                onClick={() => setSupprOpen(true)} sx={{ textTransform: 'none' }}>
                {t('suppressions.button', 'Silencios')}
              </Button>
            )}
            {notifyTable && (
              <Button size="small" variant="outlined" startIcon={<NotificationsActiveOutlinedIcon />}
                onClick={() => setRulesOpen(true)} sx={{ textTransform: 'none' }}>
                {t('notifyRules.button', 'Reglas de notificación')}
              </Button>
            )}
            {guide && <SurfaceGuideButton archetype={guide} lifecycle={guideLifecycle} />}
          </Stack>
        }
      />
      {notifyTable && rulesOpen && (
        <NotifyRulesDialog open table={notifyTable} onClose={() => setRulesOpen(false)} />
      )}
      {suppressTable && supprOpen && (
        <SuppressionsDialog open table={suppressTable} onClose={() => setSupprOpen(false)} />
      )}
      {actions && (
        // NB: no `overflowX: hidden` here — setting only one overflow axis makes
        // the other compute to `auto`, which clipped the SELECT labels floating
        // above their inputs. The toolbar wraps (flexWrap) so it never overflows.
        <Box sx={{ mb: 2, maxWidth: '100%', pt: 0.5 }}>
          {actions}
        </Box>
      )}
      <Box sx={{ minWidth: 0 }}>
        {kpis && kpis.length > 0 && <KpiStrip items={kpis} />}
        {children}
      </Box>
    </Box>
  );
}

// ---- entity links (id → real navigation) ----------------------------------

/** Resolve where a `target_entity_kind` points: a domain entity slug/table, or
 * nowhere (then we render plain text, not a dead link). */
function useDomainSlugResolver() {
  const { manifest } = useManifest();
  const known = useMemo(() => {
    const m = new Map<string, string>();
    for (const e of ((manifest as any)?.domain_entities ?? [])) {
      if (e?.slug) m.set(String(e.slug).toLowerCase(), e.slug);
      if (e?.table) m.set(String(e.table).toLowerCase(), e.slug ?? e.table);
    }
    return m;
  }, [manifest]);
  return (kind?: string | null): string | null =>
    kind ? (known.get(String(kind).toLowerCase()) ?? null) : null;
}

export function EntityLink({
  kind, id, eventId, runId, label,
}: {
  kind?: string | null;
  id?: string | number | null;
  eventId?: string | number | null;
  runId?: string | number | null;
  label?: ReactNode;
}) {
  const resolveSlug = useDomainSlugResolver();
  let to: string | null = null;
  let text: ReactNode = label;
  if (runId != null && runId !== '') { to = `/runs/${runId}`; text = label ?? `run #${runId}`; }
  else if (eventId != null && eventId !== '') { to = `/events/${eventId}`; text = label ?? `evento #${eventId}`; }
  else if (kind && /(^|_)(scheduled_item|encargo)/i.test(String(kind)) && id != null && id !== '') {
    // Encargos (scheduled_items) are NOT a browsable domain entity, but they DO
    // have a dedicated detail page — link straight to it so «→ Encargo #X» opens
    // the encargo itself (with its outcome + a link back to the source signal).
    to = `/encargos/${encodeURIComponent(String(id))}`; text = label ?? `encargo #${id}`;
  }
  else if (kind && /(^|_)approvals?$/i.test(String(kind)) && id != null && id !== '') {
    // Approvals likewise have their own detail page (not a domain entity).
    to = `/approvals/${encodeURIComponent(String(id))}`; text = label ?? `aprobación #${id}`;
  }
  else if (kind && id != null && id !== '') {
    const slug = resolveSlug(kind);
    text = label ?? `${kind} #${id}`;
    to = slug ? `/domain/${slug}/${encodeURIComponent(String(id))}` : null;
  }
  if (text == null) return null;
  if (!to) return <Typography component="span" variant="body2" color="text.secondary">{text}</Typography>;
  return (
    <Link component={RouterLink} to={to} underline="hover" variant="body2" sx={{ fontWeight: 500 }}>
      {text}
    </Link>
  );
}

/** Wrap a card's headline so it links to the entity's DETAIL page
 * (`/domain/{slug}/{id}`) — the list → detail affordance. Falls back to plain
 * children when there's no slug/id. */
export function TitleLink({ slug, id, children }: { slug?: string; id: any; children: ReactNode }) {
  if (!slug || id == null || id === '') return <>{children}</>;
  return (
    <Link component={RouterLink} to={`/domain/${slug}/${encodeURIComponent(String(id))}`}
      underline="hover" color="inherit" sx={{ textDecorationColor: 'inherit' }}>
      {children}
    </Link>
  );
}

/** A compact "Ver detalle" link to the entity's detail page — for cards without
 * a prominent headline (e.g. the decision log). */
export function DetailLink({ slug, id }: { slug?: string; id: any }) {
  const { t } = useTranslation();
  if (!slug || id == null || id === '') return null;
  return (
    <Link component={RouterLink} to={`/domain/${slug}/${encodeURIComponent(String(id))}`}
      underline="hover" variant="caption" sx={{ fontWeight: 500 }}>
      {t('surfaces.common.detail', 'Ver detalle')}
    </Link>
  );
}

// ---- people (directory user, never a raw handle) --------------------------

const initials = (name: string): string =>
  name.split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0]?.toUpperCase() ?? '').join('') || '·';

/** Render a recipient/owner value as its DIRECTORY user (avatar + name), with
 * the raw handle only in the tooltip. Falls back to the raw value when the
 * handle isn't in the directory (e.g. an external address). */
export function PersonChip({ value, size = 'small' }: { value?: string | null; size?: 'small' | 'medium' }) {
  const { data } = useUsersDirectory();
  const users = (data?.items ?? []) as any[];
  if (!value) return null;
  const v = String(value).trim().toLowerCase();
  const u = users.find((x) =>
    String(x.email ?? '').toLowerCase() === v ||
    (x.phones ?? []).some((p: any) => String(p).toLowerCase() === v));
  // v7.82.0 — fall back to the ACTUAL value, not the email: for a WhatsApp/SMS
  // the value is the PHONE, and showing the person's email instead was wrong
  // (a WhatsApp must read as its phone when the person has no display name).
  const name = u ? (u.display_name || u.alias || value) : value;
  const px = size === 'small' ? 22 : 28;
  return (
    <Tooltip title={u?.email || String(value)}>
      <Chip
        size={size}
        variant="outlined"
        avatar={<Avatar sx={{ width: px, height: px, fontSize: px * 0.42 }}>{initials(String(name))}</Avatar>}
        label={name}
        sx={{ maxWidth: 260 }}
      />
    </Tooltip>
  );
}

// ---- aging / SLA ----------------------------------------------------------

/** The item's ABSOLUTE date+time (effective timezone, tz-labelled), with the
 * relative age in a tooltip. Shown on EVERY surface with the SAME format so
 * alertas / recomendaciones / decisiones read homogeneously — it used to show
 * the date only, so surfaces drifted (the ledger printed its own datetime
 * while inbox/advisory hid the time; v7.89.3 unifies on this primitive). */
export function DateText({ value, prefix }: { value?: string | null; prefix?: string }) {
  if (!value) return null;
  return (
    <Tooltip title={formatRelative(value)}>
      <Typography component="span" variant="caption" color="text.secondary">
        {prefix ? `${prefix} ` : ''}{formatDateTime(value)}
      </Typography>
    </Tooltip>
  );
}

/** Age of a still-open item as a coloured chip. Muted once `open` is false. */
export function AgingChip({ createdAt, open = true }: { createdAt?: string | null; open?: boolean }) {
  if (!createdAt) return null;
  const rel = formatRelative(createdAt);
  let tone: Tone = 'default';
  if (open) {
    const ageH = (Date.now() - new Date(createdAt).getTime()) / 3_600_000;
    tone = ageH >= 72 ? 'error' : ageH >= 24 ? 'warning' : 'default';
  }
  return (
    <Chip
      size="small"
      variant={tone === 'default' ? 'outlined' : 'filled'}
      color={tone}
      label={rel}
      sx={{ fontSize: 12 }}
    />
  );
}

// ---- selection + bulk delete ----------------------------------------------

/** Track a set of selected row ids for bulk actions. Stable string keys. */
export function useRowSelection() {
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const toggle = (id: any) => setSelected((s) => {
    const n = new Set(s); const k = String(id);
    if (n.has(k)) n.delete(k); else n.add(k);
    return n;
  });
  const clear = () => setSelected(new Set());
  const isSelected = (id: any) => selected.has(String(id));
  // Replace the whole selection with exactly these ids (for "select all on screen").
  const selectAll = (ids: (string | number)[]) => setSelected(new Set(ids.map(String)));
  return { toggle, clear, isSelected, selectAll, count: selected.size, ids: [...selected] };
}

/** A per-card selection checkbox (compact, stops row-click propagation). */
export function SelectBox({ checked, onToggle }: { checked: boolean; onToggle: () => void }) {
  return (
    <Checkbox size="small" checked={checked} onClick={(e) => e.stopPropagation()}
      onChange={onToggle} sx={{ p: 0.5, mt: -0.25 }} />
  );
}

/** A "select all on screen" checkbox (indeterminate when only some are picked).
 * ``ids`` are the currently-visible row pks. */
export function SelectAllControl({ ids, sel }: {
  ids: (string | number)[];
  sel: ReturnType<typeof useRowSelection>;
}) {
  const { t } = useTranslation();
  if (ids.length === 0) return null;
  const allChecked = ids.every((id) => sel.isSelected(id));
  const some = !allChecked && ids.some((id) => sel.isSelected(id));
  return (
    <Stack direction="row" spacing={0.5} alignItems="center">
      <Checkbox size="small" checked={allChecked} indeterminate={some} sx={{ p: 0.5 }}
        onChange={() => (allChecked ? sel.clear() : sel.selectAll(ids))} />
      <Typography variant="body2" color="text.secondary">
        {t('surfaces.common.selectAll', { count: ids.length, defaultValue: `Seleccionar todo (${ids.length})` })}
      </Typography>
    </Stack>
  );
}

/** The bulk-action bar shown while rows are selected: "N seleccionados" + an
 * optional "change status" menu (bulk lifecycle transition) + Eliminar/Cancelar. */
export function SelectionBar({
  count, onDelete, onClear, busy, statusActions, onStatus,
}: {
  count: number; onDelete: () => void; onClear: () => void; busy?: boolean;
  /** Lifecycle states offered for a bulk change (label + value). */
  statusActions?: { value: string; label: string; tone?: Tone }[];
  onStatus?: (value: string) => void;
}) {
  const { t } = useTranslation();
  const [anchor, setAnchor] = useState<null | HTMLElement>(null);
  if (count <= 0) return null;
  return (
    <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap
      sx={{ mb: 1.5, py: 0.75, px: 1.25, borderRadius: 1.5, bgcolor: 'action.hover', border: 1, borderColor: 'divider' }}>
      <Typography variant="body2" sx={{ fontWeight: 600, flex: 1 }}>
        {t('surfaces.common.selected', { count, defaultValue: '{{count}} seleccionados' })}
      </Typography>
      {statusActions && statusActions.length > 0 && onStatus && (
        <>
          <Button size="small" variant="outlined" disabled={busy} onClick={(e) => setAnchor(e.currentTarget)}>
            {t('surfaces.common.bulkStatus', 'Cambiar estado')}
          </Button>
          <Menu anchorEl={anchor} open={!!anchor} onClose={() => setAnchor(null)}>
            {statusActions.map((s) => (
              <MenuItem key={s.value} onClick={() => { setAnchor(null); onStatus(s.value); }}>
                <Chip size="small" color={s.tone ?? 'default'} variant="outlined" label={s.label} sx={{ mr: 1 }} />
                {t('surfaces.common.markAll', { label: s.label, defaultValue: `Marcar como ${s.label}` })}
              </MenuItem>
            ))}
          </Menu>
        </>
      )}
      <Button size="small" color="inherit" onClick={onClear} disabled={busy}>{t('common.cancel', 'Cancelar')}</Button>
      <Button size="small" color="error" variant="contained" startIcon={<DeleteOutlineIcon />} disabled={busy} onClick={onDelete}>
        {t('surfaces.common.delete', 'Eliminar')}
      </Button>
    </Stack>
  );
}

/** Bulk lifecycle transition: `apply(ids, status)` calls the /{slug}/bulk
 * endpoint, reports updated/skipped, and refreshes. Mirrors useBulkDelete. */
export function useBulkStatus(
  slug: string | undefined,
  onDone: () => void,
  notify: (s: { msg: string; sev: 'success' | 'error' }) => void,
) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  const apply = async (ids: (string | number)[], status: string) => {
    if (!slug || !ids.length) return;
    setBusy(true);
    try {
      const r = await bulkStatus(slug, ids, status);
      const upd = r.updated_count ?? (r.updated?.length ?? 0);
      const skp = r.skipped_count ?? (r.skipped?.length ?? 0);
      notify({
        msg: skp
          ? t('surfaces.common.bulkDoneSkipped', { updated: upd, skipped: skp, defaultValue: `${upd} actualizados · ${skp} omitidos` })
          : t('surfaces.common.bulkDone', { count: upd, defaultValue: `${upd} actualizados` }),
        sev: 'success',
      });
      onDone();
    } catch (e: any) {
      notify({ msg: e?.message ?? 'error', sev: 'error' });
    } finally { setBusy(false); }
  };
  return { apply, busy };
}

/** Encapsulates delete: a confirm dialog + the API call. `request(ids)` opens
 * the confirm; on confirm it deletes, calls `onDone` (refresh + clear) and
 * reports via `notify`. Render `dialog` once. */
export function useBulkDelete(
  slug: string | undefined,
  onDone: () => void,
  notify: (s: { msg: string; sev: 'success' | 'error' }) => void,
) {
  const { t } = useTranslation();
  const [pending, setPending] = useState<string[] | null>(null);
  const [busy, setBusy] = useState(false);
  const request = (ids: (string | number)[]) => { if (ids.length) setPending(ids.map(String)); };
  const doDelete = async () => {
    if (!slug || !pending) return;
    setBusy(true);
    try {
      const r = await deleteRows(slug, pending);
      notify({
        msg: r.skipped
          ? t('surfaces.common.deletedSkipped', { deleted: r.deleted, skipped: r.skipped, defaultValue: '{{deleted}} eliminados · {{skipped}} omitidos (solo borradores)' })
          : t('surfaces.common.deleted', { count: r.deleted, defaultValue: '{{count}} eliminados' }),
        sev: 'success',
      });
      setPending(null);
      onDone();
    } catch (e: any) {
      notify({ msg: e?.message ?? 'error', sev: 'error' });
    } finally { setBusy(false); }
  };
  const dialog = (
    <Dialog open={!!pending} onClose={() => { if (!busy) setPending(null); }} maxWidth="xs" fullWidth>
      <DialogTitle sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
        <DeleteOutlineIcon color="error" fontSize="small" />
        {t('surfaces.common.deleteTitle', 'Eliminar')}
      </DialogTitle>
      <DialogContent>
        <DialogContentText>
          {t('surfaces.common.deleteConfirm', { count: pending?.length ?? 0, defaultValue: '¿Eliminar {{count}} elemento(s)? No se puede deshacer.' })}
        </DialogContentText>
      </DialogContent>
      <DialogActions>
        <Button onClick={() => setPending(null)} disabled={busy}>{t('common.cancel', 'Cancelar')}</Button>
        <Button color="error" variant="contained" onClick={doDelete} disabled={busy}>{t('surfaces.common.delete', 'Eliminar')}</Button>
      </DialogActions>
    </Dialog>
  );
  return { request, dialog, busy };
}

// ---- filtering (date range + select) --------------------------------------

/** Client-side date-range filter state (over the loaded rows). `inRange(iso)`
 * tests a row's created_at against the [from, to] calendar days (inclusive). */
export function useDateRange() {
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const inRange = (iso?: string | null): boolean => {
    if (!from && !to) return true;
    if (!iso) return true;
    const d = String(iso).slice(0, 10); // YYYY-MM-DD
    if (from && d < from) return false;
    if (to && d > to) return false;
    return true;
  };
  return { from, setFrom, to, setTo, inRange, active: !!(from || to), clear: () => { setFrom(''); setTo(''); } };
}

export function DateRangeControls({ r }: { r: ReturnType<typeof useDateRange> }) {
  const { t } = useTranslation();
  return (
    <>
      <TextField size="small" type="date" label={t('surfaces.common.from', 'Desde')}
        InputLabelProps={{ shrink: true }} value={r.from}
        onChange={(e) => r.setFrom(e.target.value)} sx={{ width: 160 }} />
      <TextField size="small" type="date" label={t('surfaces.common.to', 'Hasta')}
        InputLabelProps={{ shrink: true }} value={r.to}
        onChange={(e) => r.setTo(e.target.value)} sx={{ width: 160 }} />
    </>
  );
}

/** A compact labelled dropdown filter (e.g. Severidad). ``displayEmpty`` makes
 * the empty-value option (e.g. "Todas") actually render as the selected label
 * instead of a blank box. */
export function SelectFilter({
  label, value, onChange, options,
}: {
  label: string; value: string; onChange: (v: string) => void;
  options: { value: string; label: string }[];
}) {
  return (
    <TextField select size="small" label={label} value={value}
      onChange={(e) => onChange(e.target.value)} InputLabelProps={{ shrink: true }}
      SelectProps={{ displayEmpty: true }} sx={{ minWidth: 150 }}>
      {options.map((o) => <MenuItem key={o.value} value={o.value}>{o.label}</MenuItem>)}
    </TextField>
  );
}

/** The CANONICAL status filter for every "Lo que genero" surface: a row of
 * clickable chips, each showing the state + its count, plus an "all" chip with
 * the total. Homogenises the filter across inbox/advisory/ledger. `value=''` =
 * all. */
// v7.40.0 — the ONE operator lifecycle shared by alerts, recommendations and
// decisions, ordered by logical progression (menor → mayor) so the status chips
// and the state diagram read left→right the same way everywhere.
export const UNIFIED_STATES = ['open', 'acknowledged', 'in_progress', 'done', 'dismissed'];
export const UNIFIED_STATE_TONE: Record<string, Tone> = {
  open: 'warning', acknowledged: 'info', in_progress: 'info', done: 'success', dismissed: 'default',
};

// v7.72.0 — the NON-terminal states ("abiertas/pendientes": still need attention).
// The surfaces default to showing these so the operator lands on the work queue,
// not on a wall that includes everything already closed. Legacy synonyms included.
export const OPEN_STATES = new Set(['open', 'acknowledged', 'in_progress', 'new', 'pending']);
export const OPEN_FILTER = '__open__';
export const isOpenState = (s: any) => OPEN_STATES.has(String(s ?? '').toLowerCase());

/** Build the ordered status-chip options (state · count) for a lifecycle entity:
 * the entity's own lifecycle states when present (already in logical order),
 * else the unified default. Counts come from the loaded rows. */
export function buildStatusOptions(
  rows: any[], statusField: string, lifecycle: any,
  label: (state: string) => string,
): StatusOption[] {
  const states: string[] = (lifecycle?.states?.length ? lifecycle.states : UNIFIED_STATES)
    .map((s: any) => String(s).toLowerCase());
  const ordered = UNIFIED_STATES.filter((s) => states.includes(s))
    .concat(states.filter((s) => !UNIFIED_STATES.includes(s)));
  return ordered.map((s) => ({
    value: s,
    label: label(s),
    count: rows.filter((r) => String(r[statusField] ?? '').toLowerCase() === s).length,
    tone: UNIFIED_STATE_TONE[s] ?? 'default',
  }));
}

// Intensity-band order shared by impact / urgency / confidence filters.
const _BAND_ORDER = ['critical', 'urgent', 'immediate', 'high', 'medium', 'low', 'info'];

/** Options for a dimension (impact/urgency/confidence) SelectFilter, built from
 * the VALUES actually present in the rows (so no empty choices), ordered by
 * intensity, with a leading "Todas". v7.61.0. */
export function dimensionFilterOptions(
  rows: any[], field: string, t: (k: string, d?: any) => string,
): { value: string; label: string }[] {
  const present = new Set<string>();
  for (const r of rows) {
    const v = String(r?.[field] ?? '').toLowerCase();
    if (v) present.add(v);
  }
  const sorted = [...present].sort((a, b) => {
    const ia = _BAND_ORDER.indexOf(a), ib = _BAND_ORDER.indexOf(b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
  });
  return [
    { value: '', label: t('surfaces.common.all', 'Todas') },
    ...sorted.map((v) => ({ value: v, label: t(`states.${v}`, v) })),
  ];
}

// v7.41.0 — the SEPARATED policy axes (impact/urgency/confidence). Tones follow
// intensity so a card reads at a glance; confidence is a caution axis (low = warn).
export const BAND_TONE: Record<string, Tone> = {
  critical: 'error', immediate: 'error', urgent: 'error',
  high: 'warning', medium: 'info', low: 'default', info: 'default',
};
export const CONFIDENCE_TONE: Record<string, Tone> = {
  high: 'success', medium: 'info', low: 'warning',
};

/** The three separated policy axes as compact labelled chips. Shows only the
 * axes present on the row. ``severity``/``priority`` are DERIVED from these, so
 * they are rendered by {@link DerivedBandChip} with a "derivada" tooltip. */
export function DimensionChips({ impact, urgency, confidence }: {
  impact?: string | null; urgency?: string | null; confidence?: string | null;
}) {
  const { t } = useTranslation();
  const items: { k: string; v: string; tone: Tone }[] = [];
  if (impact) items.push({ k: t('dimensions.impact', 'Impacto'), v: String(impact), tone: BAND_TONE[String(impact)] ?? 'default' });
  if (urgency) items.push({ k: t('dimensions.urgency', 'Urgencia'), v: String(urgency), tone: BAND_TONE[String(urgency)] ?? 'default' });
  if (confidence) items.push({ k: t('dimensions.confidence', 'Confianza'), v: String(confidence), tone: CONFIDENCE_TONE[String(confidence)] ?? 'default' });
  if (!items.length) return null;
  return (
    <>
      {items.map((it) => (
        <Tooltip key={it.k} title={t(`dimensions.${it.k}`, it.k)} disableInteractive>
          <Chip size="small" variant="outlined" color={it.tone}
            label={`${it.k}: ${t(`states.${it.v}`, it.v)}`} />
        </Tooltip>
      ))}
    </>
  );
}

/** The DERIVED risk band (alert ``severity`` / recommendation ``priority``) with
 * a tooltip that makes the policy explicit: it comes from impact × urgency. */
export function DerivedBandChip({ value, kind }: { value?: string | null; kind: 'severity' | 'priority' }) {
  const { t } = useTranslation();
  if (!value) return null;
  const label = kind === 'severity' ? t('dimensions.severity', 'Severidad') : t('dimensions.priority', 'Prioridad');
  return (
    <Tooltip title={t('dimensions.derivedHint', 'Derivada de impacto × urgencia (baja si la confianza es baja)')} disableInteractive>
      <Chip size="small" color={BAND_TONE[String(value)] ?? 'default'}
        label={`${label}: ${t(`states.${value}`, String(value))}`} />
    </Tooltip>
  );
}

/** "Visto N veces" badge for a deduplicated alert (occurrences > 1). */
export function OccurrencesChip({ count }: { count?: number | null }) {
  const { t } = useTranslation();
  if (!count || count <= 1) return null;
  return <Chip size="small" variant="outlined" color="warning"
    label={t('dimensions.occurrences', { count, defaultValue: `Visto ${count} veces` })} />;
}

export interface StatusOption { value: string; label: string; count: number; tone?: Tone }
export function StatusFilterChips({
  value, onChange, options, allLabel,
}: {
  value: string; onChange: (v: string) => void; options: StatusOption[]; allLabel?: string;
}) {
  const { t } = useTranslation();
  const total = options.reduce((s, o) => s + (o.count || 0), 0);
  // v7.72.0 — a leading "Abiertas" (pending) aggregate: the default landing filter,
  // summing every non-terminal state so the operator sees the work queue first.
  const openCount = options.filter((o) => OPEN_STATES.has(o.value)).reduce((s, o) => s + (o.count || 0), 0);
  return (
    <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap alignItems="center">
      <Chip size="small" label={`${t('surfaces.common.openFilter', 'Abiertas')} · ${openCount}`}
        color={value === OPEN_FILTER ? 'primary' : 'warning'} variant={value === OPEN_FILTER ? 'filled' : 'outlined'}
        onClick={() => onChange(OPEN_FILTER)} />
      <Chip size="small" label={`${allLabel ?? t('surfaces.common.all', 'Todas')} · ${total}`}
        color={value === '' ? 'primary' : 'default'} variant={value === '' ? 'filled' : 'outlined'}
        onClick={() => onChange('')} />
      {options.map((o) => (
        <Chip key={o.value} size="small" label={`${o.label} · ${o.count}`}
          color={value === o.value ? 'primary' : (o.tone ?? 'default')}
          variant={value === o.value ? 'filled' : 'outlined'}
          onClick={() => onChange(o.value)} />
      ))}
    </Stack>
  );
}
