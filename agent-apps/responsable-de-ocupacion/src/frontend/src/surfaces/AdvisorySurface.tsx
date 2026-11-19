/**
 * `advisory` archetype — the decision-support board for RECOMMENDATIONS (and any
 * suggestion-shaped entity: a rationale + a priority + a lifecycle). Cards lead
 * with the business rationale and the expected impact as a headline metric; the
 * operator ACTS on them with the SAME unified verbs as alerts and decisions
 * (v7.40.0): acknowledge/accept, turn into a task, mark done or dismiss
 * (the dismissal reason becomes a learned boundary).
 */
import { useMemo, useState } from 'react';
import { useParams } from 'react-router-dom';
import {
  Alert, Box, Button, Card, CardContent, Chip, CircularProgress, Dialog,
  DialogActions, DialogContent, DialogTitle, Snackbar, Stack, TextField, Typography,
} from '@mui/material';
import TrendingUpIcon from '@mui/icons-material/TrendingUp';
import { useTranslation } from 'react-i18next';
import { useManifest } from '../hooks/useManifest';
import { usePickLabel } from '../utils/dataLabels';
import { transition, entityAction } from './common/api';
import {
  SurfaceShell, useEntityRows, EntityLink, TitleLink, DateText, AgingChip, Kpi, Tone,
  useRowSelection, useBulkDelete, useBulkStatus, useDateRange, DateRangeControls,
  SelectBox, SelectionBar, SelectAllControl,
  StatusFilterChips, buildStatusOptions, UNIFIED_STATE_TONE, SelectFilter,
  DimensionChips, DerivedBandChip, usePagedItems, Pager, dimensionFilterOptions,
  OPEN_FILTER, isOpenState,
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

const PRIORITY_ORDER = ['urgent', 'high', 'medium', 'low'];
const PRIORITY_TONE: Record<string, Tone> = {
  urgent: 'error', high: 'warning', medium: 'info', low: 'default',
};
// Non-terminal states (still on the operator's plate) — drives the KPI.
const OPEN_STATES = new Set(['open', 'acknowledged', 'in_progress', 'new', 'pending']);

/** Parse a metadata_json bag (string or object) into a plain object. */
const parseMeta = (v: any): Record<string, any> => {
  if (!v) return {};
  if (typeof v === 'object') return v;
  try { return JSON.parse(v) || {}; } catch { return {}; }
};

export default function AdvisorySurface() {
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
  const pk = entity?.primary_key ?? 'id';
  const lifecycle = entity?.lifecycle;
  const statusField = lifecycle?.field ?? cfg.status_field ?? 'status';
  const headline = cfg.headline ?? 'title';
  const rationaleField = cfg.body_field ?? ['rationale_md', 'rationale', 'reason_md'].find((f) =>
    (entity?.list_columns ?? []).some((c: any) => c.id === f)) ?? 'rationale_md';
  const priorityField = 'priority';
  const impactField = 'expected_impact';
  const targetKindF = cfg.target?.kind_field ?? 'target_entity_kind';
  const targetIdF = cfg.target?.id_field ?? 'target_entity_id';

  const [lane, setLane] = useState<string>(OPEN_FILTER);
  const suppress = useSuppressPrompt('recommendations');
  const [impactFilter, setImpactFilter] = useState<string>('');
  const [urgencyFilter, setUrgencyFilter] = useState<string>('');
  const [confFilter, setConfFilter] = useState<string>('');
  const [snack, setSnack] = useState<{ msg: string; sev: 'success' | 'error' } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [dismiss, setDismiss] = useState<{ row: any; reason: string } | null>(null);
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
  const del = useBulkDelete(slug, () => { refresh(); sel.clear(); }, setSnack);
  const bulk = useBulkStatus(slug, () => { refresh(); sel.clear(); }, setSnack);
  const bulkStatusActions = ['acknowledged', 'done', 'dismissed', 'open']
    .map((s) => ({ value: s, label: t(`states.${s}`, s), tone: UNIFIED_STATE_TONE[s] }));

  const laneOf = (r: any) => String(r[statusField] ?? 'open').toLowerCase();
  const visible = useMemo(() => {
    const list = rows.filter((r) => (lane === OPEN_FILTER ? isOpenState(laneOf(r)) : (!lane || laneOf(r) === lane))
      && (!impactFilter || String(r.impact ?? '').toLowerCase() === impactFilter)
      && (!urgencyFilter || String(r.urgency ?? '').toLowerCase() === urgencyFilter)
      && (!confFilter || String(r.confidence ?? '').toLowerCase() === confFilter)
      && dateR.inRange(r.created_at));
    return [...list].sort((a, b) =>
      PRIORITY_ORDER.indexOf(String(a[priorityField])) - PRIORITY_ORDER.indexOf(String(b[priorityField])));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows, lane, impactFilter, urgencyFilter, confFilter, dateR.from, dateR.to]);

  const paged = usePagedItems(visible, 25);
  const statusOptions = buildStatusOptions(rows, statusField, lifecycle, (s) => t(`states.${s}`, s));
  const openUrgents = rows.filter((r) => OPEN_STATES.has(laneOf(r)) &&
    ['urgent', 'high'].includes(String(r[priorityField] ?? '').toLowerCase())).length;
  // Per-state counts live in the status chips; the KPI shows only the cross-cut
  // highlight (open × urgent/high), parallel to the Inbox.
  const kpis: Kpi[] = [
    { label: t('surfaces.advisory.kpiUrgent', 'urgentes abiertas'), value: openUrgents, tone: openUrgents ? 'error' : 'success' },
  ];

  const run = async (row: any, key: string, fn: () => Promise<any>, okMsg: string) => {
    setBusy(`${row[pk]}:${key}`);
    try {
      await fn();
      // v7.72.0 — closing a signal offers to silence it in place (no combo later).
      if (key === 'done' || key === 'dismissed') suppress.offer(row[pk], String(row[headline] ?? ''));
      else setSnack({ msg: okMsg, sev: 'success' });
      refresh();
    }
    catch (e: any) { setSnack({ msg: e?.message ?? 'error', sev: 'error' }); }
    finally { setBusy(null); }
  };

  const doTransition = (row: any, state: string) =>
    run(row, state, () => transition(slug!, row[pk], state), t('surfaces.common.done', 'Hecho'));
  const doToTask = (row: any, note?: string) =>
    run(row, 'to_task', () => entityAction(slug!, row[pk], 'to_task', note ? { note } : {}), t('surfaces.advisory.converted', 'Convertida en encargo'));
  const doDismiss = async () => {
    if (!dismiss) return;
    const { row, reason } = dismiss;
    setDismiss(null);
    // Dismiss is a lifecycle transition; the reason rides along as the transition
    // comment (recorded on the entity's WHY-log thread → learned boundary).
    await run(row, 'dismissed', () => transition(slug!, row[pk], 'dismissed', reason), t('surfaces.advisory.dismissed', 'Descartada'));
  };

  if (!manifest) return <Box>{t('common.loading', 'Cargando…')}</Box>;
  if (!entity) return <Alert severity="warning">{t('domain.unknownEntity', { slug })}</Alert>;

  return (
    <SurfaceShell
      title={pickLabel(entity, entity.slug)}
      subtitle={t('surfaces.advisory.subtitle', 'Sugerencias del empleado — acéptalas, conviértelas en trabajo o descártalas.')}
      kpis={kpis}
      guide="advisory"
      notifyTable="recommendations"
      suppressTable="recommendations"
      actions={
        <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap alignItems="center">
          <StatusFilterChips value={lane} onChange={setLane} options={statusOptions} />
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
        <Alert severity="info" variant="outlined">{t('surfaces.advisory.empty', 'No hay recomendaciones aquí.')}</Alert>
      )}

      {paged.pageItems.length > 0 && (
        <Box sx={{ mb: 1 }}>
          <SelectAllControl ids={paged.pageItems.map((r) => r[pk])} sel={sel} />
        </Box>
      )}
      <SelectionBar count={sel.count} busy={del.busy || bulk.busy}
        onDelete={() => del.request(sel.ids)} onClear={sel.clear}
        statusActions={bulkStatusActions} onStatus={(s) => bulk.apply(sel.ids, s)} />

      <Stack spacing={1.25}>
        {paged.pageItems.map((row) => {
          const prio = String(row[priorityField] ?? 'medium').toLowerCase();
          const st = laneOf(row);
          const meta = parseMeta(row.metadata_json);
          const encId = meta.encargo_id;
          const task = meta.task || {};
          const outcome = meta.outcome || null;
          return (
            <Card key={String(row[pk])} variant="outlined"
              sx={{ borderLeft: 4, borderLeftColor: `${PRIORITY_TONE[prio] ?? 'grey'}.main` }}>
              <CardContent sx={{ py: 1.5, '&:last-child': { pb: 1.5 } }}>
                <Stack direction="row" alignItems="flex-start" spacing={1.5}>
                  <SelectBox checked={sel.isSelected(row[pk])} onToggle={() => sel.toggle(row[pk])} />
                  <Box sx={{ flex: 1, minWidth: 0 }}>
                    <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
                      <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>
                        <TitleLink slug={slug} id={row[pk]}>{String(row[headline] ?? `#${row[pk]}`)}</TitleLink>
                      </Typography>
                      <Chip size="small" variant="outlined" color={UNIFIED_STATE_TONE[st] ?? 'default'} label={t(`states.${st}`, st)} />
                      <DerivedBandChip value={prio} kind="priority" />
                      <DimensionChips impact={row.impact} urgency={row.urgency} confidence={row.confidence} />
                      {row[impactField] && (
                        <Chip size="small" color="success" icon={<TrendingUpIcon />} label={String(row[impactField])} />
                      )}
                    </Stack>
                    {row[rationaleField] && (
                      <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5, whiteSpace: 'pre-wrap' }}>
                        {String(row[rationaleField])}
                      </Typography>
                    )}
                    {outcome?.result_md && (
                      <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5, whiteSpace: 'pre-wrap' }}>
                        <b>{t('surfaces.advisory.outcome', 'Resultado')}:</b> {String(outcome.result_md).slice(0, 300)}
                      </Typography>
                    )}
                    <Stack direction="row" spacing={1.5} alignItems="center" flexWrap="wrap" useFlexGap sx={{ mt: 0.75 }}>
                      <DateText value={row.created_at} prefix={t('surfaces.common.created', 'Creada')} />
                      <AutomationOriginChip origin={row.created_by_automation} />
                      {/* Same aging signal the inbox shows: a recommendation
                          awaiting a decision ages exactly like an open alert. */}
                      <AgingChip createdAt={row.created_at} open={isOpenState(st)} />
                      {encId && (
                        <EntityLink kind="scheduled_items" id={encId}
                          label={t('surfaces.advisory.encargoLink', { id: encId, defaultValue: `→ Encargo #${encId}` })} />
                      )}
                      {row[targetKindF] && <EntityLink kind={row[targetKindF]} id={row[targetIdF]} />}
                    </Stack>
                  </Box>
                  <LifecycleActionBar
                    status={st}
                    lifecycle={lifecycle}
                    hasEncargo={!!encId}
                    ackLabel={t('surfaces.advisory.accept', 'Aceptar')}
                    allowTask={!!task.allow}
                    isBusy={(id) => busy === `${row[pk]}:${id}`}
                    confirm={confirm}
                    onTransition={(state) => doTransition(row, state)}
                    onToTask={(note) => doToTask(row, note)}
                    onDismiss={() => setDismiss({ row, reason: '' })}
                    toTaskPreview={{
                      title: `Encargo: ${String(task.title || row[headline] || `#${row[pk]}`)}`,
                      body: String(task.description || row[rationaleField] || ''),
                    }}
                  />
                </Stack>
              </CardContent>
            </Card>
          );
        })}
      </Stack>

      <Pager page={paged.page} pageCount={paged.pageCount} total={paged.total}
        from={paged.from} to={paged.to} onPage={(p) => { paged.setPage(p); sel.clear(); }} />

      <Dialog open={!!dismiss} onClose={() => setDismiss(null)} fullWidth maxWidth="sm">
        <DialogTitle>{t('surfaces.advisory.dismissTitle', 'Descartar recomendación')}</DialogTitle>
        <DialogContent>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
            {t('surfaces.advisory.dismissHint', 'El motivo se guarda como límite aprendido para el empleado.')}
          </Typography>
          <TextField autoFocus fullWidth multiline minRows={2} value={dismiss?.reason ?? ''}
            placeholder={t('surfaces.advisory.dismissPlaceholder', 'Motivo del descarte…')}
            onChange={(e) => setDismiss((r) => (r ? { ...r, reason: e.target.value } : r))} />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDismiss(null)}>{t('common.cancel', 'Cancelar')}</Button>
          <Button variant="contained" color="error" onClick={doDismiss}>
            {t('surfaces.advisory.dismiss', 'Descartar')}
          </Button>
        </DialogActions>
      </Dialog>

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
