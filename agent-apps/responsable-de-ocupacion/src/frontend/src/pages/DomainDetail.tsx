import { ReactNode, useEffect, useMemo, useState } from 'react';
import { Link as RouterLink, useNavigate, useParams } from 'react-router-dom';
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  Collapse,
  Grid,
  IconButton,
  Link,
  Stack,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material';
import { DataGrid, GridColDef } from '@mui/x-data-grid';
import ArrowBackIcon from '@mui/icons-material/ArrowBack';
import ExpandMoreIcon from '@mui/icons-material/ExpandMore';
import ContentCopyIcon from '@mui/icons-material/ContentCopy';
import AutoModeIcon from '@mui/icons-material/AutoMode';
import { useTranslation } from 'react-i18next';
import { PageHeader } from '../components/PageHeader';
import { SectionTitle } from '../components/SectionTitle';
import { useAuth } from '../auth/AuthContext';
import { apiBase } from '../services/apiClient';
import { useManifest } from '../hooks/useManifest';
import { formatDateTime } from '../utils/datetime';
import { EntityLink } from '../surfaces/common/primitives';
import { LifecycleActionBar, useActionConfirm } from '../surfaces/common/guide';
import { transition, entityAction } from '../surfaces/common/api';

const fmtCell = (v: any): string => {
  if (v === null || v === undefined) return '—';
  if (typeof v === 'boolean') return v ? '✓' : '✗';
  if (typeof v === 'object') {
    try { return JSON.stringify(v); } catch { return String(v); }
  }
  // ISO date heuristic → render in the BUSINESS timezone (utils/datetime),
  // never the raw browser locale, so operators in different countries
  // see the same wall clock the agent's schedules use.
  if (typeof v === 'string' && /^\d{4}-\d{2}-\d{2}[T ]/.test(v)) {
    return formatDateTime(v);
  }
  return String(v);
};

// P4-6: read the tenant's preferred currency from the manifest
// (``agent_app.locale.currency``). Defaults to EUR for back-compat.
// Set once at module load via the global ``window.__waiCurrency`` —
// the value is stable for the lifetime of the SPA.
const _tenantCurrency = (): string => {
  if (typeof window === 'undefined') return 'EUR';
  const w = window as unknown as { __waiCurrency?: string };
  return w.__waiCurrency || 'EUR';
};

const fmtCurrency = (v: any) => {
  if (v === null || v === undefined || v === '') return '—';
  const n = Number(v);
  if (Number.isNaN(n)) return String(v);
  return n.toLocaleString(undefined, { style: 'currency', currency: _tenantCurrency() });
};

export function DomainDetailPage() {
  const { slug, id } = useParams<{ slug: string; id: string }>();
  const { t } = useTranslation();
  const { token, logout } = useAuth();
  const { manifest } = useManifest();
  const navigate = useNavigate();
  const [data, setData] = useState<any | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Bumped after a lifecycle transition so the detail refetches.
  const [refreshTick, setRefreshTick] = useState(0);

  const entity = useMemo(
    () => (manifest as any)?.domain_entities?.find((e: any) => e.slug === slug),
    [manifest, slug],
  );

  useEffect(() => {
    if (!slug || !id || id === 'undefined') {
      setError(`pk inválido: ${id}`);
      return;
    }
    let cancelled = false;
    setError(null);
    setData(null);
    const run = async () => {
      try {
        const r = await fetch(`${apiBase}/domain/${slug}/${encodeURIComponent(id)}`, {
          headers: { Authorization: `Bearer ${token ?? ''}` },
        });
        if (r.status === 401) { logout(); return; }
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const json = await r.json();
        if (!cancelled) setData(json);
      } catch (exc: any) {
        if (!cancelled) setError(exc?.message ?? 'error');
      }
    };
    run();
    return () => { cancelled = true; };
  }, [slug, id, token, refreshTick]);

  return (
    <Box>
      <PageHeader
        title={entity?.label ?? slug ?? ''}
        subtitle={`${t('domain.detail')} · #${id}`}
        actions={
          <Button
            startIcon={<ArrowBackIcon />}
            onClick={() => navigate(`/domain/${slug}`)}
          >
            {t('common.back')}
          </Button>
        }
      />
      {error && <Alert severity="error">{error}</Alert>}
      {!data && !error && (
        <Typography color="text.secondary">{t('common.loading')}</Typography>
      )}
      {data && (
        <Stack spacing={3}>
          {/* v7.140.0 — who created it: the reverse stamp of autonomous-work
              traceability. Without it, a list of alerts cannot tell which are
              the employee's work and which a person added. */}
          {data.created_by_automation && (
            <Alert severity="info" icon={<AutoModeIcon fontSize="small" />}
              sx={{ '& .MuiAlert-message': { width: '100%' } }}>
              <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
                <Typography variant="body2">
                  {t('domain.origin.byAutomation', 'Lo creó la automatización «{{title}}»', {
                    title: data.created_by_automation.title,
                  })}
                </Typography>
                {data.created_by_automation.via === 'event' && (
                  <Chip size="small" variant="outlined"
                    label={t('domain.origin.deterministic', 'sin coste de modelo')} />
                )}
                <Link component={RouterLink} variant="body2"
                  to="/trabajo-autonomo">
                  {t('domain.origin.open', 'Ver la automatización →')}
                </Link>
              </Stack>
            </Alert>
          )}
          {entity?.lifecycle && data.record && (
            isUnifiedLifecycle(entity.lifecycle) ? (
              <UnifiedLifecycleActions
                slug={slug as string}
                pk={id as string}
                record={data.record}
                lifecycle={entity.lifecycle}
                onChanged={() => setRefreshTick((n) => n + 1)}
              />
            ) : (
              <LifecycleBar
                slug={slug as string}
                pk={id as string}
                record={data.record}
                lifecycle={entity.lifecycle}
                onChanged={() => setRefreshTick((n) => n + 1)}
              />
            )
          )}
          {data.record && <SignalSections record={data.record} />}
          {(data.blocks ?? []).map((block: any, i: number) => (
            <Card key={i} variant="outlined">
              <CardContent>
                <SectionTitle>{block.label ?? entity?.label ?? slug}</SectionTitle>
                {block.kind === 'fields' && (
                  <FieldsBlock fields={block.fields ?? []} />
                )}
                {block.kind === 'table' && (
                  <RelatedTable
                    rows={block.rows ?? []}
                    columns={block.columns ?? null}
                  />
                )}
              </CardContent>
            </Card>
          ))}
          {(data.blocks ?? []).length === 0 && (
            <Alert severity="info">{t('domain.detailEmpty')}</Alert>
          )}
          {data.record && <RelatedLinks record={data.record} />}
          {/* Comment thread — available for any entity row; most useful on
              lifecycle entities (alerts, recommendations) where transitions
              annotate WHY. Refetches on refreshTick so a transition-with-comment
              shows immediately. */}
          {slug && id && id !== 'undefined' && (
            <CommentThread slug={slug} pk={id} refreshTick={refreshTick} />
          )}
        </Stack>
      )}
    </Box>
  );
}

// Parse a metadata_json bag (string or object) → plain object. Never throws.
function parseMeta(v: any): any {
  if (!v) return {};
  if (typeof v === 'object') return v;
  try { return JSON.parse(v) || {}; } catch { return {}; }
}

// True for the unified signal lifecycle (alerts/recommendations/decisions), so
// the detail can offer the SAME action set as the surfaces.
function isUnifiedLifecycle(lc: any): boolean {
  const states = (lc?.states ?? []).map((s: any) => String(s).toLowerCase());
  return states.includes('acknowledged') && states.includes('dismissed');
}

// ── Outcome + associated encargo (surfaced, not buried in JSON) ──────
// The employee's RESULT (metadata.outcome.result_md) and the associated encargo
// (metadata.task) are the most business-relevant part of a signal detail — they
// used to be hidden inside the raw metadata_json dump. v7.46.0.
function SignalSections({ record }: { record: any }) {
  const { t } = useTranslation();
  const meta = parseMeta(record.metadata_json);
  const task = meta.task || null;
  const outcome = meta.outcome || null;
  const encId = meta.encargo_id;
  if (!outcome?.result_md && !(task && (task.title || task.description))) return null;
  return (
    <>
      {outcome?.result_md && (
        <Card variant="outlined" sx={{ borderLeft: '4px solid', borderLeftColor: 'success.main' }}>
          <CardContent>
            <SectionTitle>{t('domain.signal.result', 'Resultado')}</SectionTitle>
            <Typography variant="body1" sx={{ whiteSpace: 'pre-wrap' }}>{String(outcome.result_md)}</Typography>
            <Stack direction="row" spacing={2} flexWrap="wrap" useFlexGap sx={{ mt: 1 }}>
              {outcome.completed_at && (
                <Typography variant="caption" color="text.secondary">
                  {t('domain.signal.completedAt', 'Completado')}: {formatDateTime(outcome.completed_at)}
                </Typography>
              )}
              {encId && <EntityLink kind="scheduled_items" id={encId}
                label={t('domain.related.encargo', { id: encId, defaultValue: `→ Encargo #${encId}` })} />}
              {outcome.last_run_id && <EntityLink runId={outcome.last_run_id} />}
            </Stack>
          </CardContent>
        </Card>
      )}
      {task && (task.title || task.description) && (
        <Card variant="outlined">
          <CardContent>
            <SectionTitle>{t('domain.signal.task', 'Encargo asociado')}</SectionTitle>
            {task.title && <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>{String(task.title)}</Typography>}
            {task.description && (
              <Typography variant="body2" color="text.secondary" sx={{ whiteSpace: 'pre-wrap', mt: 0.5 }}>
                {String(task.description)}
              </Typography>
            )}
            {!encId && task.allow && (
              <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.75 }}>
                {t('domain.signal.taskHint', 'Aún no lanzado — puedes convertirlo en encargo desde las acciones de arriba.')}
              </Typography>
            )}
          </CardContent>
        </Card>
      )}
    </>
  );
}

// ── Unified lifecycle actions (same as the surfaces) ────────────────
// Reconocer/Aceptar · Convertir en encargo (con nota) · Marcar hecha · Descartar
// · Reabrir — wired to the transition / to_task endpoints. v7.46.0.
function UnifiedLifecycleActions({
  slug, pk, record, lifecycle, onChanged,
}: { slug: string; pk: string; record: any; lifecycle: any; onChanged: () => void }) {
  const { t } = useTranslation();
  const { confirm, dialog } = useActionConfirm();
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const meta = parseMeta(record.metadata_json);
  const field = lifecycle.field ?? 'status';
  const status = String(record[field] ?? '').toLowerCase();
  const encId = meta.encargo_id;
  const task = meta.task || {};
  const run = async (key: string, fn: () => Promise<any>) => {
    setBusy(key); setErr(null);
    try { await fn(); onChanged(); }
    catch (e: any) { setErr(e?.message ?? String(e)); }
    finally { setBusy(null); }
  };
  return (
    <Card variant="outlined" sx={{ borderLeft: '4px solid', borderLeftColor: 'primary.main' }}>
      <CardContent sx={{ py: 1.5, '&:last-child': { pb: 1.5 } }}>
        <Stack direction="row" spacing={1.5} alignItems="center" flexWrap="wrap" useFlexGap>
          <Typography variant="body2" color="text.secondary" sx={{ fontWeight: 600 }}>{t('lifecycle.state', 'Estado')}</Typography>
          <Chip size="small" color={CHIP_COLOR[status] ?? 'default'} label={t(`states.${status}`, status)}
            sx={{ fontWeight: 700, textTransform: 'uppercase', letterSpacing: 0.4 }} />
          <Box sx={{ flex: 1 }} />
          <LifecycleActionBar
            status={status} lifecycle={lifecycle} hasEncargo={!!encId} allowTask={!!task.allow}
            ackLabel={t('surfaces.inbox.ack', 'Reconocer')}
            isBusy={(id) => busy === id} confirm={confirm}
            onTransition={(state) => run(state, () => transition(slug, pk, state))}
            onToTask={(note) => run('to_task', () => entityAction(slug, pk, 'to_task', note ? { note } : {}))}
            toTaskPreview={{
              title: `Encargo: ${String(task.title || record.title || `#${pk}`)}`,
              body: String(task.description || record.rationale_md || record.description || ''),
            }}
          />
        </Stack>
        {err && <Alert severity="error" sx={{ mt: 1, py: 0.25 }}>{err}</Alert>}
      </CardContent>
      {dialog}
    </Card>
  );
}

// ── Related links ───────────────────────────────────────────────────
// The detail page's "everything this row connects to" section: turns the raw
// trace columns (metadata.encargo_id, ref_kind/ref_id, source_event_id,
// created_by_run_id, target_entity_*) into REAL navigable links, so an operator
// can jump from an alert to its encargo, back to the run, out to the affected
// entity — the round-trips the flat field list left as dead ids (v7.42.0).
function RelatedLinks({ record }: { record: any }) {
  const { t } = useTranslation();
  let meta: any = record.metadata_json;
  if (typeof meta === 'string') { try { meta = JSON.parse(meta); } catch { meta = {}; } }
  meta = meta && typeof meta === 'object' ? meta : {};
  const from = meta.from || {};

  const links: ReactNode[] = [];
  if (from.table && from.pk != null && from.pk !== '') {
    links.push(<EntityLink key="from" kind={String(from.table)} id={from.pk}
      label={t('domain.related.from', { kind: String(from.table), id: from.pk, defaultValue: `← Origen: ${from.table} #${from.pk}` })} />);
  }
  if (record.ref_kind && record.ref_id != null && record.ref_id !== '') {
    links.push(<EntityLink key="ref" kind={String(record.ref_kind)} id={record.ref_id}
      label={t('domain.related.ref', { kind: String(record.ref_kind), id: record.ref_id, defaultValue: `↳ ${record.ref_kind} #${record.ref_id}` })} />);
  }
  if (meta.encargo_id) {
    links.push(<EntityLink key="enc" kind="scheduled_items" id={meta.encargo_id}
      label={t('domain.related.encargo', { id: meta.encargo_id, defaultValue: `→ Encargo #${meta.encargo_id}` })} />);
  }
  if (record.target_entity_kind && record.target_entity_id != null && record.target_entity_id !== '') {
    links.push(<EntityLink key="target" kind={String(record.target_entity_kind)} id={record.target_entity_id} />);
  }
  if (record.source_event_id) links.push(<EntityLink key="evt" eventId={record.source_event_id} />);
  if (record.created_by_run_id) links.push(<EntityLink key="run" runId={record.created_by_run_id} />);

  if (links.length === 0) return null;
  return (
    <Card variant="outlined">
      <CardContent>
        <SectionTitle>{t('domain.related.title', 'Relacionado')}</SectionTitle>
        <Stack direction="row" spacing={2} flexWrap="wrap" useFlexGap sx={{ mt: 0.5 }}>
          {links}
        </Stack>
      </CardContent>
    </Card>
  );
}

// ── Lifecycle bar ───────────────────────────────────────────────────
// Operator-managed state transitions for local entities that declare
// (or inherit) a ``lifecycle``: current state as a coloured chip plus
// one button per allowed next state. PATCH /api/domain/{slug}/{pk}
// persists the transition and stamps who/when on the audit columns.
function LifecycleBar({
  slug, pk, record, lifecycle, onChanged,
}: {
  slug: string;
  pk: string;
  record: any;
  lifecycle: any;
  onChanged: () => void;
}) {
  const { t } = useTranslation();
  const { token } = useAuth();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Optional note attached to THIS state change — recorded on the comment
  // thread with the from→to states (the WHY behind the transition).
  const [comment, setComment] = useState('');

  const field: string = lifecycle.field ?? 'status';
  const current = String(record?.[field] ?? '');
  const next: string[] = lifecycle?.transitions?.[current]
    ?? (lifecycle?.states ?? []).filter((s: string) => s !== current);
  const stateLabel = (v: string) => t(`states.${v}`, { defaultValue: v });
  const chipColor = (v: string) =>
    (CHIP_COLOR[v.toLowerCase()] ?? 'default') as any;

  const doTransition = async (state: string) => {
    setBusy(true);
    setError(null);
    try {
      const r = await fetch(`${apiBase}/domain/${slug}/${encodeURIComponent(pk)}`, {
        method: 'PATCH',
        headers: {
          Authorization: `Bearer ${token ?? ''}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ status: state, comment: comment.trim() || undefined }),
      });
      if (!r.ok) {
        const detail = await r.json().catch(() => ({}));
        throw new Error(typeof detail?.detail === 'string' ? detail.detail : `HTTP ${r.status}`);
      }
      setComment('');
      onChanged();
    } catch (exc: any) {
      setError(exc?.message ?? String(exc));
    } finally {
      setBusy(false);
    }
  };

  if (!current) return null;
  return (
    <Card variant="outlined" sx={{ borderLeft: '4px solid', borderLeftColor: 'primary.main' }}>
      <CardContent sx={{ py: 1.5, '&:last-child': { pb: 1.5 } }}>
        <Stack direction="row" spacing={1.5} alignItems="center" flexWrap="wrap" useFlexGap>
          <Typography variant="body2" color="text.secondary" sx={{ fontWeight: 600 }}>
            {t('lifecycle.state')}
          </Typography>
          <Chip
            size="small"
            color={chipColor(current)}
            label={stateLabel(current)}
            sx={{ fontWeight: 700, textTransform: 'uppercase', letterSpacing: 0.4 }}
          />
          <Box sx={{ flex: 1 }} />
          {next.length === 0 && (
            <Typography variant="caption" color="text.secondary">
              {t('lifecycle.noTransitions')}
            </Typography>
          )}
          {next.map((s) => (
            <Button
              key={s}
              size="small"
              variant="outlined"
              disabled={busy}
              onClick={() => doTransition(s)}
              sx={{ textTransform: 'none' }}
            >
              {t('lifecycle.markAs', { state: stateLabel(s) })}
            </Button>
          ))}
        </Stack>
        {next.length > 0 && (
          <TextField
            size="small"
            fullWidth
            multiline
            maxRows={3}
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            placeholder={t('lifecycle.commentPlaceholder', 'Comentario para este cambio (opcional)')}
            sx={{ mt: 1 }}
          />
        )}
        {error && <Alert severity="error" sx={{ mt: 1, py: 0.25 }}>{t('lifecycle.error')}: {error}</Alert>}
      </CardContent>
    </Card>
  );
}

// ── Comment thread ──────────────────────────────────────────────────
// Per-row note thread (entity_comments). Shows every comment oldest-first,
// with a from→to badge when the note accompanied a state change, plus an input
// to add a free-standing comment. Refetches when ``refreshTick`` bumps (so a
// transition-with-comment from the LifecycleBar shows up immediately).
function CommentThread({
  slug, pk, refreshTick,
}: {
  slug: string;
  pk: string;
  refreshTick: number;
}) {
  const { t } = useTranslation();
  const { token } = useAuth();
  const [items, setItems] = useState<any[]>([]);
  const [body, setBody] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const stateLabel = (v: string) => t(`states.${v}`, { defaultValue: v });

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch(
          `${apiBase}/domain/${slug}/${encodeURIComponent(pk)}/comments`,
          { headers: { Authorization: `Bearer ${token ?? ''}` } },
        );
        if (!r.ok) return;
        const j = await r.json();
        if (!cancelled) setItems(Array.isArray(j.items) ? j.items : []);
      } catch { /* read-only; a transient failure just shows the last list */ }
    })();
    return () => { cancelled = true; };
  }, [slug, pk, token, tick, refreshTick]);

  const add = async () => {
    const text = body.trim();
    if (!text) return;
    setBusy(true);
    setError(null);
    try {
      const r = await fetch(
        `${apiBase}/domain/${slug}/${encodeURIComponent(pk)}/comments`,
        {
          method: 'POST',
          headers: { Authorization: `Bearer ${token ?? ''}`, 'Content-Type': 'application/json' },
          body: JSON.stringify({ body: text }),
        },
      );
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setBody('');
      setTick((n) => n + 1);
    } catch (exc: any) {
      setError(exc?.message ?? String(exc));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card variant="outlined">
      <CardContent>
        <SectionTitle>{t('domain.comments.title', 'Comentarios')}</SectionTitle>
        {items.length === 0 && (
          <Typography variant="body2" color="text.secondary">
            {t('domain.comments.empty', 'Sin comentarios todavía.')}
          </Typography>
        )}
        <Stack spacing={1.5} sx={{ mt: items.length ? 1 : 0 }}>
          {items.map((c) => (
            <Box key={c.id} sx={{ borderLeft: '3px solid', borderLeftColor: 'divider', pl: 1.5 }}>
              <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
                <Typography variant="caption" sx={{ fontWeight: 600 }}>
                  {c.author || t('domain.comments.system', 'sistema')}
                </Typography>
                <Typography variant="caption" color="text.secondary">
                  {formatDateTime(c.created_at)}
                </Typography>
                {c.to_status && (
                  <Chip
                    size="small"
                    variant="outlined"
                    label={`${stateLabel(String(c.from_status || ''))} → ${stateLabel(String(c.to_status))}`}
                  />
                )}
              </Stack>
              <Typography variant="body2" sx={{ whiteSpace: 'pre-wrap', mt: 0.25 }}>{c.body}</Typography>
            </Box>
          ))}
        </Stack>
        {error && <Alert severity="error" sx={{ mt: 1 }} onClose={() => setError(null)}>{error}</Alert>}
        <Stack direction="row" spacing={1} alignItems="flex-start" sx={{ mt: 1.5 }}>
          <TextField
            size="small"
            fullWidth
            multiline
            maxRows={4}
            value={body}
            onChange={(e) => setBody(e.target.value)}
            placeholder={t('domain.comments.placeholder', 'Escribe un comentario…')}
          />
          <Button
            variant="contained"
            disabled={busy || !body.trim()}
            onClick={add}
            sx={{ textTransform: 'none', whiteSpace: 'nowrap' }}
          >
            {t('domain.comments.add', 'Comentar')}
          </Button>
        </Stack>
      </CardContent>
    </Card>
  );
}

// ── Field classification ────────────────────────────────────────────
// The old design rendered every field as an identical grey card in a
// 3-column grid: an id, a 4-line description and a JSON blob all got
// the same 1/3-width box, which made the page unreadable. We now
// classify each field by key + value shape and render accordingly:
//   headline — title-like field, rendered big at the top
//   chips    — status / severity / priority / decision → coloured chip
//   text     — long prose (description, rationale, reason) full-width
//   json     — *_json / object values → collapsible formatted block
//   meta     — ids, idempotency keys, run ids → collapsed footer
//   plain    — everything else → compact definition list

type FieldDef = { label: string; key?: string; value: any };

const CHIP_COLOR: Record<string, 'default' | 'success' | 'error' | 'warning' | 'info' | 'primary'> = {
  // lifecycle
  completed: 'success', failed: 'error', running: 'info', pending: 'warning',
  claimed: 'info', retry_scheduled: 'warning', cancelled: 'default',
  // unified lifecycle (v7.40.0)
  in_progress: 'info',
  // business statuses
  open: 'info', acknowledged: 'warning', ack: 'warning', resolved: 'success',
  dismissed: 'default', accepted: 'success', rejected: 'default', done: 'success',
  draft: 'default', ready: 'info', sent: 'success', replied: 'success',
  skipped: 'default', filtered: 'default', error: 'error', active: 'success',
  inactive: 'default', closed: 'default',
  // severity / priority / bands + policy axes (v7.41.0)
  critical: 'error', high: 'error', urgent: 'error', immediate: 'error',
  medium: 'warning', warning: 'warning', low: 'success', info: 'info',
  // decisions
  no_action: 'default', deferred: 'warning', escalated: 'error',
};

// Confidence is a caution axis (low = warn), so it does NOT follow the intensity
// palette above — mapped separately.
const CONFIDENCE_COLOR: Record<string, 'success' | 'info' | 'warning'> = {
  high: 'success', medium: 'info', low: 'warning',
};

// Keys that render as a coloured CHIP (v7.41.0 adds the separated policy axes +
// the structured decision reason). ``reason_code`` is matched on its own because
// its inner underscore breaks the word-boundary form.
// EXACT enum columns only — matching a substring made ``expected_impact`` (a
// free-text business headline) render as an uppercase chip. v7.46.0.
const CHIP_KEY_RE = /^(status|severity|priority|band|decision|state|impact|urgency|confidence)$/i;
const REASON_CODE_KEY_RE = /^reason_code$/i;
const META_KEY_RE = /(^id$|_id$|^idempotency|_key$|^created_by|^resolved_by|^decided_by|^delivery_id$)/i;
const TITLE_KEY_RE = /^(title|name|subject|metric)$/i;
const LONG_TEXT_KEYS = /(description|rationale|reason|body|explanation|message|notes?)(_md)?$/i;
const DATE_KEY_RE = /(_at|_date|^date)$/i;

function isJsonish(f: FieldDef): boolean {
  if (typeof f.value === 'object' && f.value !== null) return true;
  if (typeof f.value === 'string' && /_json$/i.test(f.key ?? '')) return true;
  if (typeof f.value === 'string') {
    const s = f.value.trim();
    if ((s.startsWith('{') && s.endsWith('}')) || (s.startsWith('[') && s.endsWith(']'))) {
      try { JSON.parse(s); return true; } catch { return false; }
    }
  }
  return false;
}

function parseJsonish(v: any): any {
  if (typeof v === 'object') return v;
  try { return JSON.parse(String(v)); } catch { return v; }
}

function classify(f: FieldDef): 'headline' | 'chip' | 'text' | 'json' | 'meta' | 'date' | 'plain' {
  const key = (f.key ?? f.label ?? '').toLowerCase();
  if (TITLE_KEY_RE.test(key)) return 'headline';
  // Chips are for SHORT enum values only — never a long free-text field.
  if ((CHIP_KEY_RE.test(key) || REASON_CODE_KEY_RE.test(key))
      && typeof f.value === 'string' && f.value.length <= 40) return 'chip';
  if (isJsonish(f)) return 'json';
  if (META_KEY_RE.test(key)) return 'meta';
  if (DATE_KEY_RE.test(key)) return 'date';
  if (LONG_TEXT_KEYS.test(key) || (typeof f.value === 'string' && f.value.length > 140)) return 'text';
  return 'plain';
}

function chipFor(label: string, value: any, withLabel = true, display?: string,
                 colorOverride?: 'default' | 'success' | 'error' | 'warning' | 'info' | 'primary') {
  const v = String(value ?? '').toLowerCase();
  const color = colorOverride ?? CHIP_COLOR[v] ?? 'default';
  const shown = display ?? String(value);
  return (
    <Tooltip key={label} title={label}>
      <Chip
        size="small"
        color={color}
        label={withLabel ? `${label}: ${shown}` : shown}
        sx={{ fontWeight: 600, textTransform: 'uppercase', letterSpacing: 0.3 }}
      />
    </Tooltip>
  );
}

function FieldsBlock({ fields: rawFields }: { fields: FieldDef[] }) {
  const { t } = useTranslation();
  const [metaOpen, setMetaOpen] = useState(false);

  if (!rawFields || rawFields.length === 0) {
    return <Typography color="text.secondary">—</Typography>;
  }

  // Business names first: the backend sends humanised COLUMN names
  // ("Target Entity Kind"); the i18n `fields.*` dictionary maps the
  // canonical agent-table columns to operator language ("Entidad
  // afectada"). Unknown keys keep the backend label.
  const fields = rawFields.map((f) => ({
    ...f,
    label: f.key ? t(`fields.${f.key}`, { defaultValue: f.label }) : f.label,
  }));

  const headline: FieldDef[] = [];
  const chips: FieldDef[] = [];
  const texts: FieldDef[] = [];
  const jsons: FieldDef[] = [];
  const metas: FieldDef[] = [];
  const plains: FieldDef[] = [];
  const dates: FieldDef[] = [];
  for (const f of fields) {
    const empty = f.value === null || f.value === undefined || f.value === '';
    const kind = classify(f);
    // Empty values: keep them only in the metadata footer, drop the
    // noise everywhere else ("Resolved At —" cards add nothing).
    if (empty && kind !== 'meta') continue;
    switch (kind) {
      case 'headline': headline.push(f); break;
      case 'chip': chips.push(f); break;
      case 'text': texts.push(f); break;
      case 'json': jsons.push(f); break;
      case 'meta': metas.push(f); break;
      case 'date': dates.push(f); break;
      default: plains.push(f);
    }
  }

  return (
    <Stack spacing={2}>
      {/* Header: title + state chips + dates, the things an operator
          scans for first. */}
      {(headline.length > 0 || chips.length > 0 || dates.length > 0) && (
        <Box>
          {headline.map((f) => (
            <Typography key={f.key ?? f.label} variant="h6" sx={{ fontWeight: 700, lineHeight: 1.3 }}>
              {String(f.value)}
            </Typography>
          ))}
          {(chips.length > 0 || dates.length > 0) && (
            <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap sx={{ mt: headline.length ? 1 : 0 }}>
              {chips.map((f) => {
                const key = (f.key ?? '').toLowerCase();
                const v = String(f.value ?? '').toLowerCase();
                // reason_code → reasonCodes.* label + info tone (matches the Ledger);
                // confidence → its caution palette; everything else → states.* + intensity.
                if (REASON_CODE_KEY_RE.test(key)) {
                  return chipFor(f.label, f.value, true, t(`reasonCodes.${v}`, { defaultValue: String(f.value) }), 'info');
                }
                const display = t(`states.${v}`, { defaultValue: String(f.value) });
                const override = key === 'confidence' ? CONFIDENCE_COLOR[v] : undefined;
                return chipFor(f.label, f.value, true, display, override);
              })}
              {dates.map((f) => (
                <Chip
                  key={f.key ?? f.label}
                  size="small"
                  variant="outlined"
                  label={`${f.label}: ${fmtCell(f.value)}`}
                />
              ))}
            </Stack>
          )}
        </Box>
      )}

      {/* Long prose, full width and readable. */}
      {texts.map((f) => (
        <Box key={f.key ?? f.label}>
          <Typography variant="overline" color="text.secondary" sx={{ letterSpacing: 0.6 }}>
            {f.label}
          </Typography>
          <Typography variant="body1" sx={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
            {String(f.value)}
          </Typography>
        </Box>
      ))}

      {/* Short scalar fields: dense two-column definition list. */}
      {plains.length > 0 && (
        <Grid container spacing={0} sx={{ border: '1px solid', borderColor: 'divider', borderRadius: 1, overflow: 'hidden' }}>
          {plains.map((f, idx) => (
            <Grid
              item
              xs={12}
              sm={6}
              key={f.key ?? f.label}
              sx={{
                display: 'flex',
                gap: 1,
                px: 1.5,
                py: 0.75,
                bgcolor: idx % 2 === 0 ? 'transparent' : 'action.hover',
                borderBottom: '1px solid',
                borderColor: 'divider',
                '&:last-of-type': { borderBottom: 0 },
              }}
            >
              <Typography variant="body2" color="text.secondary" sx={{ minWidth: 160, flexShrink: 0 }}>
                {f.label}
              </Typography>
              <Typography variant="body2" sx={{ fontWeight: 500, wordBreak: 'break-word' }}>
                {typeof f.value === 'boolean' ? (f.value ? '✓' : '✗') : fmtCell(f.value)}
              </Typography>
            </Grid>
          ))}
        </Grid>
      )}

      {/* JSON evidence / metadata: formatted, full width, monospace. The
          PLATFORM bag on metadata_json (task / outcome / encargo_id / …) is
          rendered as its own readable sections above, so strip it here — the
          block then shows only the entity's own business data (or hides). */}
      {jsons.map((f) => {
        let val = parseJsonish(f.value);
        if ((f.key ?? '').toLowerCase() === 'metadata_json' && val && typeof val === 'object' && !Array.isArray(val)) {
          const { task, outcome, encargo_id, severity_override, priority_override, from, ...rest } = val;
          val = rest;
        }
        return <JsonBlock key={f.key ?? f.label} label={f.label} value={val} />;
      })}

      {/* Audit metadata (ids, keys): collapsed by default. */}
      {metas.length > 0 && (
        <Box>
          <Button
            size="small"
            color="inherit"
            onClick={() => setMetaOpen((v) => !v)}
            endIcon={
              <ExpandMoreIcon
                sx={{ transform: metaOpen ? 'rotate(180deg)' : 'none', transition: '0.15s' }}
              />
            }
            sx={{ color: 'text.secondary', textTransform: 'none' }}
          >
            {t('domain.metadata', { defaultValue: 'Metadatos' })} ({metas.length})
          </Button>
          <Collapse in={metaOpen}>
            <Box sx={{ mt: 1, p: 1.5, borderRadius: 1, bgcolor: 'grey.50', border: '1px solid', borderColor: 'divider' }}>
              {metas.map((f) => (
                <Box key={f.key ?? f.label} sx={{ display: 'flex', gap: 1, py: 0.25, alignItems: 'baseline' }}>
                  <Typography variant="caption" color="text.secondary" sx={{ minWidth: 160, flexShrink: 0 }}>
                    {f.label}
                  </Typography>
                  <Typography variant="caption" sx={{ fontFamily: 'monospace', wordBreak: 'break-all' }}>
                    {f.value === null || f.value === undefined || f.value === '' ? '—' : String(f.value)}
                  </Typography>
                </Box>
              ))}
            </Box>
          </Collapse>
        </Box>
      )}
    </Stack>
  );
}

// Render a *_json field for a NON-technical operator: flat objects
// become a readable two-column list (humanised keys, localised
// values); the raw JSON stays available behind a small toggle for
// whoever needs to copy it. Showing a black terminal block as the
// primary view was exactly the "everything is too technical" complaint.
function JsonBlock({ label, value }: { label: string; value: any }) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  const [rawOpen, setRawOpen] = useState(false);

  const pretty = useMemo(() => {
    try { return JSON.stringify(value, null, 2); } catch { return String(value); }
  }, [value]);
  const isEmpty = pretty === '{}' || pretty === '[]' || pretty === 'null';
  if (isEmpty) return null;

  const fmtScalar = (v: any): string => {
    if (v === null || v === undefined || v === '') return '—';
    if (typeof v === 'boolean') return v ? '✓' : '✗';
    if (typeof v === 'number') return v.toLocaleString(undefined, { maximumFractionDigits: 2 });
    return String(v);
  };
  const isScalar = (v: any) => v === null || ['string', 'number', 'boolean'].includes(typeof v);

  // "Friendly" only when the object is flat enough to read as rows:
  // scalar values or short arrays of scalars.
  const entries: Array<[string, any]> | null = useMemo(() => {
    if (typeof value !== 'object' || value === null || Array.isArray(value)) return null;
    const es = Object.entries(value);
    if (es.length === 0) return null;
    const ok = es.every(([, v]) =>
      isScalar(v) || (Array.isArray(v) && v.length <= 12 && v.every(isScalar)));
    return ok ? es : null;
  }, [value]);

  return (
    <Box>
      <Stack direction="row" alignItems="center" spacing={0.5}>
        <Typography variant="overline" color="text.secondary" sx={{ letterSpacing: 0.6 }}>
          {label}
        </Typography>
        <Tooltip title={copied ? '✓' : 'Copy JSON'}>
          <IconButton
            size="small"
            onClick={() => {
              navigator.clipboard?.writeText(pretty).then(() => {
                setCopied(true);
                setTimeout(() => setCopied(false), 1200);
              });
            }}
          >
            <ContentCopyIcon sx={{ fontSize: 14 }} />
          </IconButton>
        </Tooltip>
      </Stack>

      {entries ? (
        <>
          <Grid container spacing={0} sx={{ border: '1px solid', borderColor: 'divider', borderRadius: 1, overflow: 'hidden' }}>
            {entries.map(([k, v], idx) => (
              <Grid
                item
                xs={12}
                sm={6}
                key={k}
                sx={{
                  display: 'flex',
                  gap: 1,
                  px: 1.5,
                  py: 0.75,
                  bgcolor: idx % 2 === 0 ? 'transparent' : 'action.hover',
                }}
              >
                <Typography variant="body2" color="text.secondary" sx={{ minWidth: 150, flexShrink: 0 }}>
                  {t(`fields.${k}`, { defaultValue: humanise(k) })}
                </Typography>
                {Array.isArray(v) ? (
                  <Stack direction="row" spacing={0.5} flexWrap="wrap" useFlexGap>
                    {v.map((item, i) => (
                      <Chip key={i} size="small" variant="outlined" label={fmtScalar(item)} />
                    ))}
                  </Stack>
                ) : (
                  <Typography variant="body2" sx={{ fontWeight: 500, wordBreak: 'break-word' }}>
                    {fmtScalar(v)}
                  </Typography>
                )}
              </Grid>
            ))}
          </Grid>
          <Button
            size="small"
            color="inherit"
            onClick={() => setRawOpen((o) => !o)}
            endIcon={
              <ExpandMoreIcon
                sx={{ transform: rawOpen ? 'rotate(180deg)' : 'none', transition: '0.15s', fontSize: 16 }}
              />
            }
            sx={{ mt: 0.5, color: 'text.secondary', textTransform: 'none', fontSize: 12 }}
          >
            JSON
          </Button>
          <Collapse in={rawOpen}>
            <JsonPre pretty={pretty} />
          </Collapse>
        </>
      ) : (
        <JsonPre pretty={pretty} />
      )}
    </Box>
  );
}

function JsonPre({ pretty }: { pretty: string }) {
  return (
    <Box
      component="pre"
      sx={{
        m: 0,
        p: 1.5,
        borderRadius: 1,
        bgcolor: 'grey.50',
        border: '1px solid',
        borderColor: 'divider',
        color: 'text.primary',
        fontSize: 12.5,
        lineHeight: 1.6,
        overflow: 'auto',
        maxHeight: 320,
        fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
      }}
    >
      {pretty}
    </Box>
  );
}

function RelatedTable({ rows, columns }: { rows: any[]; columns: string[] | null }) {
  const { t } = useTranslation();
  const cols = useMemo(() => {
    const keys = columns && columns.length > 0
      ? columns
      : Object.keys(rows?.[0] ?? {});
    const out: GridColDef[] = keys.map((k) => ({
      field: k,
      headerName: t(`fields.${k}`, { defaultValue: humanise(k) }),
      flex: 1,
      sortable: true,
      valueFormatter: (value: any) => {
        if (k.endsWith('_amount') || k === 'amount' || k === 'price' || k === 'total' || k.endsWith('_eur')) {
          return fmtCurrency(value);
        }
        return fmtCell(value);
      },
    }));
    return out;
  }, [rows, columns, t]);

  if (!rows || rows.length === 0) {
    return <Typography color="text.secondary">—</Typography>;
  }

  // P4-2: phone-friendly card list at xs (same pattern as
  // DomainList / Runs / Events). DataGrid stays the default for
  // tablet+; the cards show the first 3 columns per row.
  const cols3 = cols.slice(0, 3);
  return (
    <Box sx={{ width: '100%' }}>
      <Box sx={{ display: { xs: 'none', md: 'block' } }}>
        <DataGrid
          rows={rows.map((r, i) => ({ id: r.id ?? i, ...r }))}
          columns={cols}
          autoHeight
          density="standard"
          columnHeaderHeight={44}
          disableRowSelectionOnClick
          pageSizeOptions={[10, 25, 50]}
          initialState={{ pagination: { paginationModel: { pageSize: 10, page: 0 } } }}
          sx={(theme) => ({
            border: '1px solid',
            borderColor: 'divider',
            borderRadius: 2,
            overflow: 'hidden',
            backgroundColor: 'background.paper',
            // Clean, light header instead of the heavy solid-blue band:
            // a subtle tinted strip with small-caps labels reads as more
            // modern and far less "técnico".
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
            '& .MuiDataGrid-cell': {
              borderColor: theme.palette.divider,
              fontSize: '0.82rem',
              color: theme.palette.text.primary,
            },
            '& .MuiDataGrid-row': { transition: 'background-color .12s ease' },
            '& .MuiDataGrid-row:hover': {
              backgroundColor: theme.palette.action.hover,
            },
            '& .MuiDataGrid-footerContainer': {
              borderTop: `1px solid ${theme.palette.divider}`,
            },
            '& .MuiDataGrid-columnHeader:focus, & .MuiDataGrid-columnHeader:focus-within, & .MuiDataGrid-cell:focus, & .MuiDataGrid-cell:focus-within': {
              outline: 'none',
            },
          })}
        />
      </Box>
      <Stack spacing={1} sx={{ display: { xs: 'flex', md: 'none' } }}>
        {rows.map((r, i) => (
          <Card key={r.id ?? i} variant="outlined">
            <CardContent sx={{ p: 1.5, '&:last-child': { pb: 1.5 } }}>
              {cols3.map((c) => (
                <Box
                  key={c.field}
                  sx={{
                    display: 'flex', gap: 1, fontSize: 12, mb: 0.25,
                    color: 'text.secondary',
                  }}
                >
                  <Box sx={{ minWidth: 88, fontSize: 11 }}>{c.headerName}</Box>
                  <Box sx={{ flex: 1, color: 'text.primary', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {fmtCell(r[c.field])}
                  </Box>
                </Box>
              ))}
            </CardContent>
          </Card>
        ))}
      </Stack>
    </Box>
  );
}

function humanise(s: string): string {
  if (!s) return '';
  return s
    .replace(/[-_]+/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

export default DomainDetailPage;
