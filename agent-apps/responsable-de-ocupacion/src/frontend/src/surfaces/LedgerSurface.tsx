/**
 * `ledger` archetype — the DECISION LOG. Each decision now carries TWO axes
 * (v7.40.0): a CLASSIFICATION (`decision`: sin acción / aplazada / escalada —
 * what the employee concluded) and a STATUS on the SAME unified lifecycle as
 * alerts and recommendations (abierta → reconocida → en curso → hecha;
 * descartada). It renders as an auditable timeline — the reason is the
 * protagonist — with the unified triage verbs inline (reconocer / convertir en
 * encargo / marcar hecha / descartar).
 */
import { useMemo, useState } from 'react';
import { useParams } from 'react-router-dom';
import {
  Alert, Box, Card, CardContent, Chip, CircularProgress,
  Snackbar, Stack, Typography,
} from '@mui/material';
import { useTranslation } from 'react-i18next';
import { useManifest } from '../hooks/useManifest';
import { usePickLabel } from '../utils/dataLabels';
import { transition, entityAction } from './common/api';
import {
  SurfaceShell, useEntityRows, EntityLink, DetailLink, DateText, Kpi, Tone,
  useRowSelection, useBulkDelete, useBulkStatus, useDateRange, DateRangeControls,
  SelectBox, SelectionBar, SelectAllControl,
  SelectFilter, StatusFilterChips, buildStatusOptions, UNIFIED_STATE_TONE, DimensionChips,
  usePagedItems, Pager, dimensionFilterOptions,   OPEN_FILTER, isOpenState,
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

// Classification tone (the "Tipo" of decision — separate from the lifecycle).
const DECISION_TONE: Record<string, Tone> = {
  no_action: 'default', deferred: 'warning', escalated: 'error',
  reviewed: 'info', executed: 'success',
};

/** Parse a metadata_json bag (string or object) into a plain object. */
const parseMeta = (v: any): Record<string, any> => {
  if (!v) return {};
  if (typeof v === 'object') return v;
  try { return JSON.parse(v) || {}; } catch { return {}; }
};

export default function LedgerSurface() {
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
  const statusField = lifecycle?.field ?? 'status';
  // The classification axis ("Tipo"): decision / kind column.
  const typeField = cfg.status_field && cfg.status_field !== statusField ? cfg.status_field : 'decision';
  const reasonField = cfg.body_field ?? ['reason_md', 'reason', 'rationale_md', 'rationale']
    .find((f) => (entity?.list_columns ?? []).some((c: any) => c.id === f)) ?? 'reason_md';

  const [statusFilter, setStatusFilter] = useState<string>(OPEN_FILTER);
  const suppress = useSuppressPrompt('decisions');
  const [typeFilter, setTypeFilter] = useState<string>('');
  const [reasonFilter, setReasonFilter] = useState<string>('');
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
  const del = useBulkDelete(slug, () => { refresh(); sel.clear(); }, setSnack);
  const bulk = useBulkStatus(slug, () => { refresh(); sel.clear(); }, setSnack);
  const bulkStatusActions = ['acknowledged', 'done', 'dismissed', 'open']
    .map((s) => ({ value: s, label: t(`states.${s}`, s), tone: UNIFIED_STATE_TONE[s] }));

  const statusOf = (r: any) => String(r[statusField] ?? '').toLowerCase();
  const typeOf = (r: any) => String(r[typeField] ?? '').toLowerCase();
  const reasonOf = (r: any) => String(r.reason_code ?? '').toLowerCase();

  const types = useMemo(() => {
    const s = new Set<string>();
    for (const r of rows) if (r[typeField]) s.add(typeOf(r));
    return Array.from(s);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows, typeField]);
  const reasons = useMemo(() => {
    const s = new Set<string>();
    for (const r of rows) if (r.reason_code) s.add(reasonOf(r));
    return Array.from(s);
  }, [rows]);

  const visible = rows.filter((r) =>
    (statusFilter === OPEN_FILTER ? isOpenState(statusOf(r)) : (!statusFilter || statusOf(r) === statusFilter))
    && (!typeFilter || typeOf(r) === typeFilter)
    && (!reasonFilter || reasonOf(r) === reasonFilter)
    && (!confFilter || String(r.confidence ?? '').toLowerCase() === confFilter)
    && dateR.inRange(r.created_at));

  const paged = usePagedItems(visible, 25);
  const statusOptions = buildStatusOptions(rows, statusField, lifecycle, (s) => t(`states.${s}`, s));
  // The per-state counts live in the status chips; no separate KPI strip needed.
  const kpis: Kpi[] = [];

  const run = async (row: any, key: string, fn: () => Promise<any>, okMsg: string) => {
    setBusy(`${row[pk]}:${key}`);
    try {
      await fn();
      if (key === 'done' || key === 'dismissed') suppress.offer(row[pk], String(row[reasonField] ?? '').split('\n')[0]);
      else setSnack({ msg: okMsg, sev: 'success' });
      refresh();
    }
    catch (e: any) { setSnack({ msg: e?.message ?? 'error', sev: 'error' }); }
    finally { setBusy(null); }
  };
  const doTransition = (row: any, state: string) =>
    run(row, state, () => transition(slug!, row[pk], state), t('surfaces.common.done', 'Hecho'));
  const doToTask = (row: any, note?: string) =>
    run(row, 'to_task', () => entityAction(slug!, row[pk], 'to_task', note ? { note } : {}), t('surfaces.ledger.converted', 'Convertida en encargo'));

  if (!manifest) return <Box>{t('common.loading', 'Cargando…')}</Box>;
  if (!entity) return <Alert severity="warning">{t('domain.unknownEntity', { slug })}</Alert>;

  return (
    <SurfaceShell
      title={pickLabel(entity, entity.slug)}
      subtitle={t('surfaces.ledger.subtitle', 'Registro de lo que el empleado consideró y por qué actuó (o no).')}
      kpis={kpis}
      guide="ledger"
      notifyTable="decisions"
      suppressTable="decisions"
      actions={
        <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap alignItems="center">
          <StatusFilterChips value={statusFilter} onChange={setStatusFilter} options={statusOptions} />
          {types.length > 0 && (
            <SelectFilter label={t('surfaces.ledger.type', 'Tipo')} value={typeFilter} onChange={setTypeFilter}
              options={[
                { value: '', label: t('surfaces.common.all', 'Todas') },
                ...types.map((tp) => ({ value: tp, label: t(`states.${tp}`, tp) })),
              ]} />
          )}
          {reasons.length > 0 && (
            <SelectFilter label={t('surfaces.ledger.reason', 'Motivo')} value={reasonFilter} onChange={setReasonFilter}
              options={[
                { value: '', label: t('surfaces.common.all', 'Todas') },
                ...reasons.map((rc) => ({ value: rc, label: t(`reasonCodes.${rc}`, rc) })),
              ]} />
          )}
          {rows.some((r) => r.confidence) && (
            <SelectFilter label={t('dimensions.confidence', 'Confianza')} value={confFilter} onChange={setConfFilter}
              options={dimensionFilterOptions(rows, 'confidence', t)} />
          )}
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
        <Alert severity="info" variant="outlined">
          {t('surfaces.ledger.empty', 'Aún no hay decisiones registradas.')}
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

      <Stack spacing={1.25}>
        {paged.pageItems.map((row) => {
          const tp = typeOf(row);
          const st = statusOf(row);
          const meta = parseMeta(row.metadata_json);
          const encId = meta.encargo_id;
          const task = meta.task || {};
          const reason = String(row[reasonField] ?? '');
          const encTitle = `Encargo: ${String(task.title || reason.split('\n')[0] || `#${row[pk]}`).slice(0, 120)}`;
          return (
            <Card key={String(row[pk])} variant="outlined">
              <CardContent sx={{ py: 1.5, '&:last-child': { pb: 1.5 } }}>
                <Stack direction="row" alignItems="flex-start" spacing={1.5}>
                  <SelectBox checked={sel.isSelected(row[pk])} onToggle={() => sel.toggle(row[pk])} />
                  <Box sx={{ flex: 1, minWidth: 0 }}>
                    <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap sx={{ mb: 0.5 }}>
                      <DateText value={row.created_at} prefix={t('surfaces.common.created', 'Creada')} />
                      <AutomationOriginChip origin={row.created_by_automation} />
                      {st && (
                        <Chip size="small" variant="outlined" color={UNIFIED_STATE_TONE[st] ?? 'default'} label={t(`states.${st}`, st)} />
                      )}
                      {tp && (
                        <Chip size="small" color={DECISION_TONE[tp] ?? 'default'} label={t(`states.${tp}`, tp)} />
                      )}
                      {row.reason_code && (
                        <Chip size="small" variant="outlined" color="info"
                          label={t(`reasonCodes.${row.reason_code}`, String(row.reason_code))} />
                      )}
                      <DimensionChips confidence={row.confidence} />
                    </Stack>
                    {row[reasonField] && (
                      <Typography variant="body2" sx={{ whiteSpace: 'pre-wrap' }}>
                        {String(row[reasonField])}
                      </Typography>
                    )}
                    <Stack direction="row" spacing={1.5} alignItems="center" flexWrap="wrap" useFlexGap sx={{ mt: 0.75 }}>
                      <DetailLink slug={slug} id={row[pk]} />
                      {row.ref_kind && row.ref_id && (
                        <EntityLink kind={String(row.ref_kind)} id={row.ref_id}
                          label={t('surfaces.ledger.refLink', { kind: String(row.ref_kind), id: row.ref_id, defaultValue: `↳ ${row.ref_kind} #${row.ref_id}` })} />
                      )}
                      {encId && (
                        <EntityLink kind="scheduled_items" id={encId}
                          label={t('surfaces.ledger.encargoLink', { id: encId, defaultValue: `→ Encargo #${encId}` })} />
                      )}
                      {row.source_event_id && <EntityLink eventId={row.source_event_id} />}
                      {row.created_by_run_id && <EntityLink runId={row.created_by_run_id} />}
                    </Stack>
                  </Box>
                  <LifecycleActionBar
                    status={st}
                    lifecycle={lifecycle}
                    hasEncargo={!!encId}
                    ackLabel={t('surfaces.ledger.ack', 'Reconocer')}
                    allowTask={!!task.allow}
                    isBusy={(id) => busy === `${row[pk]}:${id}`}
                    confirm={confirm}
                    onTransition={(state) => doTransition(row, state)}
                    onToTask={(note) => doToTask(row, note)}
                    toTaskPreview={{ title: encTitle, body: String(task.description || reason) }}
                  />
                </Stack>
              </CardContent>
            </Card>
          );
        })}
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
