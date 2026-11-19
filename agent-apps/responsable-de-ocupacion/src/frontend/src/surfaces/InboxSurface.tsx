/**
 * `inbox` archetype — the triage console for ALERTS (and any alert-shaped
 * entity: a `severity` + the unified open→acknowledged→in_progress→done
 * lifecycle). It replaces the CRUD grid with an operations inbox: severity
 * lanes, aging, real links to the affected entity/event, and the unified triage
 * verbs (reconocer / convertir en encargo / marcar hecha / descartar) inline.
 * v7.40.0 — an alert no longer escalates into a recommendation; it goes STRAIGHT
 * to an encargo (to_task), sharing the exact lifecycle of recommendations and
 * decisions.
 */
import { useMemo, useState } from 'react';
import { useParams } from 'react-router-dom';
import {
  Alert, Box, Card, CardContent, Chip, CircularProgress, Divider,
  Snackbar, Stack, Tooltip, Typography,
} from '@mui/material';
import { useTranslation } from 'react-i18next';
import { useManifest } from '../hooks/useManifest';
import { usePickLabel } from '../utils/dataLabels';
import { transition, entityAction } from './common/api';
import {
  SurfaceShell, useEntityRows, EntityLink, TitleLink, AgingChip, DateText, Kpi, Tone,
  useRowSelection, useBulkDelete, useBulkStatus, useDateRange, DateRangeControls,
  SelectBox, SelectionBar, SelectAllControl,
  SelectFilter, StatusFilterChips, buildStatusOptions, UNIFIED_STATE_TONE,
  DimensionChips, OccurrencesChip, usePagedItems, Pager, dimensionFilterOptions,
  OPEN_FILTER,
} from './common/primitives';
import {
  useAutomationProvenance,
  ProvenanceScopeAlert,
  AutomationOriginChip,
  AutomationProvenanceFilter,
} from './common/provenanceScope';
import { useSuppressPrompt } from '../components/SuppressionsDialog';
import { useActionConfirm, LifecycleActionBar } from './common/guide';
import { uiConfig } from './resolveArchetype';

const SEVERITY_ORDER = ['critical', 'high', 'medium', 'low', 'info'];
const SEVERITY_TONE: Record<string, Tone> = {
  critical: 'error', high: 'error', medium: 'warning', low: 'success', info: 'info',
};
// Non-terminal states: still on the operator's plate (drives aging + the KPI).
const OPEN_STATES = new Set(['open', 'acknowledged', 'in_progress', 'new', 'pending']);

/** Parse a metadata_json bag (string or object) into a plain object. */
const parseMeta = (v: any): Record<string, any> => {
  if (!v) return {};
  if (typeof v === 'object') return v;
  try { return JSON.parse(v) || {}; } catch { return {}; }
};

export default function InboxSurface() {
  const { slug } = useParams<{ slug: string }>();
  const { manifest } = useManifest();
  const pickLabel = usePickLabel();
  const { t } = useTranslation();
  const { confirm, dialog } = useActionConfirm();
  const entity = useMemo(
    () => ((manifest as any)?.domain_entities ?? []).find((e: any) => e.slug === slug),
    [manifest, slug],
  );
  const cfg = uiConfig(entity);
  // Filters: status (chips) + severity + impact + urgency + confidence + date.
  const [statusFilter, setStatusFilter] = useState<string>(OPEN_FILTER);
  const suppress = useSuppressPrompt('alerts');
  const [sevFilter, setSevFilter] = useState<string>('');
  const [impactFilter, setImpactFilter] = useState<string>('');
  const [urgencyFilter, setUrgencyFilter] = useState<string>('');
  const [confFilter, setConfFilter] = useState<string>('');
  const [snack, setSnack] = useState<{ msg: string; sev: 'success' | 'error' } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const sel = useRowSelection();
  const dateR = useDateRange();
  const prov = useAutomationProvenance(slug, entity?.source);
  const entityQuery = useMemo(() => ({
    sort: 'created_at' as const,
    direction: 'desc' as const,
    pageSize: 200,
    ...prov.queryExtras,
  }), [prov.queryExtras.created_by_automation, prov.queryExtras.only_open]);

  const { rows, loading, error, refresh, provContext } = useEntityRows(
    slug, entityQuery, [],
  );

  const lifecycle = entity?.lifecycle;
  const statusField = lifecycle?.field ?? 'status';
  const severityField = cfg.status_field && cfg.status_field !== statusField
    ? cfg.status_field
    : (entity?.list_columns ?? []).map((c: any) => c.id).find((id: string) => /severity/i.test(id)) ?? 'severity';
  const headline = cfg.headline ?? 'title';
  const bodyField = cfg.body_field ?? ['description', 'summary', 'detail'].find((f) =>
    (entity?.list_columns ?? []).some((c: any) => c.id === f)) ?? 'description';
  const targetKindF = cfg.target?.kind_field ?? 'target_entity_kind';
  const targetIdF = cfg.target?.id_field ?? 'target_entity_id';

  const del = useBulkDelete(slug, () => { refresh(); sel.clear(); }, setSnack);
  const bulk = useBulkStatus(slug, () => { refresh(); sel.clear(); }, setSnack);
  // Bulk lifecycle targets (in_progress is only reached via to_task).
  const BULK_STATES = ['acknowledged', 'done', 'dismissed', 'open'];
  const bulkStatusActions = BULK_STATES.map((s) => ({ value: s, label: t(`states.${s}`, s), tone: UNIFIED_STATE_TONE[s] }));

  const isOpen = (r: any) => OPEN_STATES.has(String(r[statusField] ?? '').toLowerCase());
  const statusOf = (r: any) => String(r[statusField] ?? '').toLowerCase();
  const visible = rows.filter((r) =>
    (statusFilter === OPEN_FILTER ? isOpen(r) : (!statusFilter || statusOf(r) === statusFilter))
    && (!sevFilter || String(r[severityField] ?? '').toLowerCase() === sevFilter)
    && (!impactFilter || String(r.impact ?? '').toLowerCase() === impactFilter)
    && (!urgencyFilter || String(r.urgency ?? '').toLowerCase() === urgencyFilter)
    && (!confFilter || String(r.confidence ?? '').toLowerCase() === confFilter)
    && dateR.inRange(r.created_at));
  // Ordered status chips (state · count) from the entity's own lifecycle.
  const statusOptions = buildStatusOptions(rows, statusField, lifecycle, (s) => t(`states.${s}`, s));

  // Paginate the filtered set, THEN group the current page into severity lanes.
  const paged = usePagedItems(visible, 25);
  const lanes = useMemo(() => {
    const by: Record<string, any[]> = {};
    for (const r of paged.pageItems) {
      const sev = String(r[severityField] ?? 'info').toLowerCase();
      (by[sev] ??= []).push(r);
    }
    const ordered = SEVERITY_ORDER.filter((s) => by[s]?.length);
    const extras = Object.keys(by).filter((s) => !SEVERITY_ORDER.includes(s));
    return [...ordered, ...extras].map((sev) => ({ sev, items: by[sev] }));
  }, [paged.pageItems, severityField]);

  const openCriticals = rows.filter((r) => isOpen(r) &&
    ['critical', 'high'].includes(String(r[severityField] ?? '').toLowerCase())).length;

  // The per-state counts live in the status chips now; the KPI shows only the
  // cross-cut highlight (open × critical/high) that the chips don't.
  const kpis: Kpi[] = [
    { label: t('surfaces.inbox.kpiCritical', 'críticas abiertas'), value: openCriticals, tone: openCriticals ? 'error' : 'success' },
  ];

  const pk = entity?.primary_key ?? 'id';

  const doTransition = async (row: any, next: string) => {
    if (!slug) return;
    setBusy(`${row[pk]}:${next}`);
    try {
      await transition(slug, row[pk], next);
      // v7.72.0 — closing a signal (done/dismissed) offers to silence it in place,
      // so the operator never has to hunt for it later in a combo.
      if (next === 'done' || next === 'dismissed') suppress.offer(row[pk], String(row[headline] ?? ''));
      else setSnack({ msg: t('surfaces.common.done', 'Hecho'), sev: 'success' });
      refresh();
    } catch (e: any) {
      setSnack({ msg: e?.message ?? 'error', sev: 'error' });
    } finally { setBusy(null); }
  };

  const doToTask = async (row: any, note?: string) => {
    if (!slug) return;
    setBusy(`${row[pk]}:to_task`);
    try {
      await entityAction(slug, row[pk], 'to_task', note ? { note } : {});
      setSnack({ msg: t('surfaces.inbox.converted', 'Convertida en encargo'), sev: 'success' });
      refresh();
    } catch (e: any) {
      setSnack({ msg: e?.message ?? 'error', sev: 'error' });
    } finally { setBusy(null); }
  };

  if (!manifest) return <Box>{t('common.loading', 'Cargando…')}</Box>;
  if (!entity) return <Alert severity="warning">{t('domain.unknownEntity', { slug })}</Alert>;

  return (
    <SurfaceShell
      title={pickLabel(entity, entity.slug)}
      subtitle={t('surfaces.inbox.subtitle', 'Alertas por severidad — reconoce, conviértelas en encargo, resuélvelas o descártalas.')}
      kpis={kpis}
      guide="inbox"
      notifyTable="alerts"
      suppressTable="alerts"
      actions={
        <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap alignItems="center">
          <StatusFilterChips value={statusFilter} onChange={setStatusFilter} options={statusOptions} />
          <SelectFilter label={t('dimensions.severity', 'Severidad')} value={sevFilter} onChange={setSevFilter}
            options={[
              { value: '', label: t('surfaces.common.all', 'Todas') },
              ...SEVERITY_ORDER.map((s) => ({ value: s, label: t(`states.${s}`, s) })),
            ]} />
          <SelectFilter label={t('dimensions.impact', 'Impacto')} value={impactFilter} onChange={setImpactFilter}
            options={dimensionFilterOptions(rows, 'impact', t)} />
          <SelectFilter label={t('dimensions.urgency', 'Urgencia')} value={urgencyFilter} onChange={setUrgencyFilter}
            options={dimensionFilterOptions(rows, 'urgency', t)} />
          <SelectFilter label={t('dimensions.confidence', 'Confianza')} value={confFilter} onChange={setConfFilter}
            options={dimensionFilterOptions(rows, 'confidence', t)} />
          <DateRangeControls r={dateR} />
          <AutomationProvenanceFilter
            value={prov.provAutomation}
            producers={prov.producers}
            onChange={prov.setProvenance}
          />
        </Stack>
      }
    >
      <ProvenanceScopeAlert ctx={provContext} onClear={prov.clearProvenance} />
      {loading && <Box sx={{ py: 4, textAlign: 'center' }}><CircularProgress size={26} /></Box>}
      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
      {!loading && visible.length === 0 && (
        <Alert severity="success" variant="outlined">
          {t('surfaces.inbox.emptyFiltered', 'Sin alertas que coincidan con los filtros.')}
        </Alert>
      )}

      {paged.pageItems.length > 0 && (
        <Box sx={{ mb: 1 }}>
          <SelectAllControl ids={paged.pageItems.map((r) => r[pk])} sel={sel} />
        </Box>
      )}
      <SelectionBar count={sel.count} busy={del.busy || bulk.busy}
        onDelete={() => del.request(sel.ids)} onClear={sel.clear}
        statusActions={bulkStatusActions} onStatus={(s) => bulk.apply(sel.ids, s)} />

      <Stack spacing={2.5}>
        {lanes.map(({ sev, items }) => (
          <Box key={sev}>
            <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
              <Tooltip title={t('dimensions.derivedHint', 'Derivada de impacto × urgencia (baja si la confianza es baja)')} disableInteractive>
                <Chip size="small" color={SEVERITY_TONE[sev] ?? 'default'}
                  label={`${t(`states.${sev}`, sev)} · ${items.length}`} />
              </Tooltip>
            </Stack>
            <Stack spacing={1}>
              {items.map((row) => {
                const current = String(row[statusField] ?? '').toLowerCase();
                const open = isOpen(row);
                const meta = parseMeta(row.metadata_json);
                const encId = meta.encargo_id;
                const task = meta.task || {};
                return (
                  <Card key={String(row[pk])} variant="outlined"
                    sx={{ borderLeft: 4, borderLeftColor: `${SEVERITY_TONE[sev] ?? 'grey'}.main` }}>
                    <CardContent sx={{ py: 1.5, '&:last-child': { pb: 1.5 } }}>
                      <Stack direction="row" alignItems="flex-start" spacing={1.5}>
                        <SelectBox checked={sel.isSelected(row[pk])} onToggle={() => sel.toggle(row[pk])} />
                        <Box sx={{ flex: 1, minWidth: 0 }}>
                          <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>
                            <TitleLink slug={slug} id={row[pk]}>{String(row[headline] ?? `#${row[pk]}`)}</TitleLink>
                          </Typography>
                          {row[bodyField] && (
                            <Typography variant="body2" color="text.secondary"
                              sx={{ mt: 0.25, display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden' }}>
                              {String(row[bodyField])}
                            </Typography>
                          )}
                          <Stack direction="row" spacing={1.5} alignItems="center" flexWrap="wrap" useFlexGap sx={{ mt: 0.75 }}>
                            <Chip size="small" variant="outlined" color={UNIFIED_STATE_TONE[current] ?? 'default'}
                              label={t(`states.${current}`, current)} />
                            <DimensionChips impact={row.impact} urgency={row.urgency} confidence={row.confidence} />
                            <OccurrencesChip count={row.occurrences} />
                            <DateText value={row.created_at} prefix={t('surfaces.common.created', 'Creada')} />
                            <AutomationOriginChip origin={row.created_by_automation} />
                            <AgingChip createdAt={row.created_at} open={open} />
                            {encId && (
                              <EntityLink kind="scheduled_items" id={encId}
                                label={t('surfaces.inbox.encargoLink', { id: encId, defaultValue: `→ Encargo #${encId}` })} />
                            )}
                            {row[targetKindF] && (
                              <EntityLink kind={row[targetKindF]} id={row[targetIdF]} />
                            )}
                            {row.source_event_id && <EntityLink eventId={row.source_event_id} />}
                            {row.created_by_run_id && <EntityLink runId={row.created_by_run_id} />}
                          </Stack>
                        </Box>
                        <LifecycleActionBar
                          status={current}
                          lifecycle={lifecycle}
                          hasEncargo={!!encId}
                          ackLabel={t('surfaces.inbox.ack', 'Reconocer')}
                          allowTask={!!task.allow}
                          isBusy={(id) => busy === `${row[pk]}:${id}`}
                          confirm={confirm}
                          onTransition={(state) => doTransition(row, state)}
                          onToTask={(note) => doToTask(row, note)}
                          toTaskPreview={{
                            title: `Encargo: ${String(task.title || row[headline] || `#${row[pk]}`)}`,
                            body: String(task.description || row[bodyField] || ''),
                          }}
                        />
                      </Stack>
                    </CardContent>
                  </Card>
                );
              })}
            </Stack>
            <Divider sx={{ mt: 1.5 }} />
          </Box>
        ))}
      </Stack>

      <Pager page={paged.page} pageCount={paged.pageCount} total={paged.total}
        from={paged.from} to={paged.to} onPage={(p) => { paged.setPage(p); sel.clear(); }} />

      {dialog}
      {del.dialog}
      {suppress.node}
      <Snackbar open={!!snack} autoHideDuration={3000} onClose={() => setSnack(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}>
        {snack ? <Alert severity={snack.sev} onClose={() => setSnack(null)}>{snack.msg}</Alert> : undefined}
      </Snackbar>
    </SurfaceShell>
  );
}
