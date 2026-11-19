/**
 * Suppressions manager (v7.64.0, reworked v7.72.0, v7.75.0) — "Silencios / No
 * repetir" for the alerts / recommendations / decisions screens. A suppression =
 * "do NOT regenerate this recurring signal, forever or until a date", enforced
 * deterministically at signal-creation time (backend).
 *
 * v7.72.0 — silences are created IN CONTEXT, at the moment you close a signal.
 * v7.75.0 — the in-context offer is a CLEAR MODAL DIALOG (was a too-subtle
 *   Snackbar + a floating Menu that overlapped the page and rendered badly), and
 *   the manager lets you OPEN each silence to see its FULL DETAIL and EDIT it
 *   (change duration, edit the reason, enable/disable, remove) — not just toggle.
 * v7.140.0 — three additions that turn this from a list of rules into something
 *   auditable: (1) a silence can be CONDITIONAL ("solo si …") and is shown as
 *   such, (2) the employee can PROPOSE one and it silences nothing until an admin
 *   accepts it here, and (3) a «Qué no te avisó» (What it did not alert you
 *   about) tab lists the actual silenced events — because a rule you can read is not the same as an effect you can see.
 */
import { useState } from 'react';
import {
  Accordion, AccordionDetails, AccordionSummary, Alert, Box, Button, Chip, Dialog,
  DialogActions, DialogContent, DialogTitle, Snackbar, Stack, Switch, Tab, Tabs,
  TextField, ToggleButton, ToggleButtonGroup, Tooltip, Typography,
} from '@mui/material';
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline';
import ExpandMoreIcon from '@mui/icons-material/ExpandMore';
import NotificationsOffOutlinedIcon from '@mui/icons-material/NotificationsOffOutlined';
import PersonOutlineIcon from '@mui/icons-material/PersonOutline';
import SmartToyOutlinedIcon from '@mui/icons-material/SmartToyOutlined';
import RuleFolderOutlinedIcon from '@mui/icons-material/RuleFolderOutlined';
import HistoryToggleOffIcon from '@mui/icons-material/HistoryToggleOff';
import { useTranslation } from 'react-i18next';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { apiFetch } from '../services/apiClient';
import LearnedFromChip, { type LearnedFrom } from './LearnedFromChip';

interface Rule {
  id: number; signal_table: string; muted: string; match_key: string;
  muted_until: string | null; forever: boolean; active: boolean;
  reason: string | null; created_by: string | null; by_agent: boolean;
  source_signal_id: number | null; suppress_count: number;
  last_suppressed_at: string | null; enabled: boolean; created_at: string | null;
  // v7.140.0
  review_state?: string; proposed?: boolean;
  subject_pattern?: string | null;
  condition?: Record<string, any> | null; conditional?: boolean;
  severity_max?: string | null; expires_at?: string | null;
  learned_from?: LearnedFrom | null;
}

interface SilenceEvent {
  id: number; suppression_id: number | null; silence: string | null;
  signal_table: string; label: string | null; severity: string | null;
  effect_applied: string; reason: string | null; occurred_at: string | null;
  run_id: string | null; silence_still_active: boolean;
  learned_from?: LearnedFrom | null;
}

/** Human sentence for a stored condition (mirrors ``signal_conditions._eval``).
 *  The operator has to be able to READ the rule without knowing the JSON shape;
 *  otherwise "solo si" is a black box and nobody dares to keep it. */
const WEEKDAY_NAMES = ['', 'lunes', 'martes', 'miércoles', 'jueves', 'viernes', 'sábado', 'domingo'];

export function describeCondition(c?: Record<string, any> | null): string {
  if (!c || typeof c !== 'object') return '';
  const kind = String(c.kind || 'always').toLowerCase();
  if (kind === 'always') return 'siempre';
  if (kind === 'weekday') {
    const days = (c.days || []).map((d: any) => WEEKDAY_NAMES[Number(d)] || d);
    return days.length ? `los ${days.join(' y ')}` : 'día de la semana (sin días)';
  }
  if (kind === 'time_window') return `entre las ${c.from} y las ${c.to}`;
  if (kind === 'field') {
    const OPS: Record<string, string> = {
      eq: 'es', '=': 'es', neq: 'no es', '!=': 'no es', contains: 'contiene',
      gt: '>', '>': '>', lt: '<', '<': '<', gte: '≥', '>=': '≥', lte: '≤', '<=': '≤',
    };
    return `${c.field} ${OPS[String(c.op || 'eq')] ?? c.op} «${c.value}»`;
  }
  if (kind === 'all' || kind === 'any') {
    const parts = (c.of || []).map((n: any) => describeCondition(n)).filter(Boolean);
    return parts.join(kind === 'all' ? ' y ' : ' o ');
  }
  return `condición desconocida (${kind})`;
}

/** "no avisar" vs "esperar y volver a mirar" — the two effects a condition has. */
function effectLabel(c?: Record<string, any> | null): string {
  const eff = String((c || {}).effect || 'skip').toLowerCase();
  return eff === 'defer'
    ? `esperar ${(c as any)?.defer_minutes ?? 60} min y volver a mirar`
    : 'no avisar';
}

type Choice = '7' | '30' | 'forever';

/**
 * In-context "🔕 Don't repeat it" prompt shown right after a signal is closed
 * (done/dismissed). Returns ``offer(signalId, label?)`` to trigger it and a
 * ``node`` to render once per surface. A modal dialog — deliberate + well
 * rendered. Best-effort: a failed create never blocks the close.
 */
export function useSuppressPrompt(table: string) {
  const { t } = useTranslation();
  const [signalId, setSignalId] = useState<number | null>(null);
  const [label, setLabel] = useState<string>('');
  const [choice, setChoice] = useState<Choice>('30');
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);

  const offer = (id: number, signalLabel?: string) => {
    setSignalId(id); setLabel(signalLabel || ''); setChoice('30'); setBusy(false);
  };
  const close = () => { setSignalId(null); setBusy(false); };

  const confirm = async () => {
    if (!signalId) return;
    const opts = choice === 'forever' ? { forever: true } : { until_days: Number(choice) };
    setBusy(true);
    try {
      await apiFetch('/api/agent/suppressions', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ signal_table: table, from_signal_id: signalId, ...opts }),
      });
      close(); setDone(true);
    } catch { setBusy(false); }
  };

  const node = (
    <>
      <Dialog open={!!signalId} onClose={close} maxWidth="xs" fullWidth>
        <DialogTitle sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
          <NotificationsOffOutlinedIcon fontSize="small" color="action" />
          {t('suppressions.offerTitle', '¿Que no se repita?')}
        </DialogTitle>
        <DialogContent>
          <Typography variant="body2" sx={{ mb: label ? 1.5 : 2 }}>
            {t('suppressions.offerBody', 'Has cerrado esta señal. Si quieres, el empleado dejará de volver a generar avisos como este mientras se mantenga la misma situación.')}
          </Typography>
          {label && (
            <Box sx={{ bgcolor: 'action.hover', borderRadius: 1, px: 1.25, py: 1, mb: 2 }}>
              <Typography variant="body2" sx={{ fontWeight: 600, wordBreak: 'break-word' }}>«{label}»</Typography>
            </Box>
          )}
          <Typography variant="caption" sx={{ fontWeight: 700, display: 'block', mb: 0.75 }}>
            {t('suppressions.pickDuration', 'Durante cuánto tiempo')}
          </Typography>
          <ToggleButtonGroup size="small" exclusive fullWidth value={choice}
            onChange={(_, v) => { if (v) setChoice(v as Choice); }}>
            <ToggleButton value="7" sx={{ textTransform: 'none' }}>{t('suppressions.days7', '7 días')}</ToggleButton>
            <ToggleButton value="30" sx={{ textTransform: 'none' }}>{t('suppressions.days30', '30 días')}</ToggleButton>
            <ToggleButton value="forever" sx={{ textTransform: 'none' }}>{t('suppressions.foreverCap', 'Para siempre')}</ToggleButton>
          </ToggleButtonGroup>
          <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1.5 }}>
            {t('suppressions.reversibleHint', 'Queda registrado y podrás cambiarlo o quitarlo cuando quieras en «Silencios».')}
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button onClick={close} disabled={busy} sx={{ textTransform: 'none' }}>
            {t('suppressions.noThanks', 'No, gracias')}
          </Button>
          <Button variant="contained" startIcon={<NotificationsOffOutlinedIcon />} disabled={busy}
            onClick={confirm} sx={{ textTransform: 'none' }}>
            {t('suppressions.silence', 'Silenciar')}
          </Button>
        </DialogActions>
      </Dialog>
      <Snackbar open={done} autoHideDuration={4000} onClose={() => setDone(false)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}>
        <Alert severity="success" variant="filled" onClose={() => setDone(false)}>
          {t('suppressions.createdMsg', 'Silenciada — no se regenerará. Puedes cambiarlo o quitarlo en «Silencios».')}
        </Alert>
      </Snackbar>
    </>
  );
  return { offer, node };
}

// ── Manager: one expandable row per silence, with full detail + edit ──────
function RuleRow({ r, mut, busy, t }: { r: Rule; mut: any; busy: boolean; t: any }) {
  const [reason, setReason] = useState(r.reason ?? '');
  const dim = !(r.enabled && r.active);
  const patch = (body: any) => mut.mutate({ path: `/api/agent/suppressions/${r.id}`, method: 'PATCH', body });

  const untilText = r.forever
    ? t('suppressions.foreverCap', 'Para siempre')
    : t('suppressions.until', 'hasta {{date}}', { date: r.muted_until ? new Date(r.muted_until).toLocaleDateString() : '—' });
  const who = r.by_agent ? t('suppressions.agent', 'empleado') : (r.created_by || t('suppressions.user', 'persona'));

  return (
    <Accordion variant="outlined" disableGutters
      sx={{ opacity: dim ? 0.6 : 1, borderRadius: 1.5, '&:before': { display: 'none' } }}>
      <AccordionSummary expandIcon={<ExpandMoreIcon />}>
        <Stack direction="row" alignItems="center" spacing={1} sx={{ flex: 1, minWidth: 0, pr: 1 }}>
          <Box sx={{ flex: 1, minWidth: 0 }}>
            <Typography variant="subtitle2" sx={{ fontWeight: 700, wordBreak: 'break-word' }}>{r.muted}</Typography>
            <Stack direction="row" spacing={0.75} alignItems="center" flexWrap="wrap" useFlexGap sx={{ mt: 0.25 }}>
              <Chip size="small" variant="outlined" color={r.active ? 'default' : 'warning'}
                label={r.active ? untilText : t('suppressions.expired', 'expirado')} />
              <Chip size="small" variant="outlined"
                icon={r.by_agent ? <SmartToyOutlinedIcon /> : <PersonOutlineIcon />} label={who} />
              {r.suppress_count > 0 && (
                <Chip size="small" color="info" variant="outlined"
                  label={t('suppressions.blocked', 'ha evitado {{count}} repeticiones', { count: r.suppress_count })} />
              )}
              {r.conditional && (
                <Tooltip title={`${describeCondition(r.condition)} → ${effectLabel(r.condition)}`}>
                  <Chip size="small" color="warning" variant="outlined"
                    icon={<RuleFolderOutlinedIcon />}
                    label={t('suppressions.conditionalChip', 'solo si…')} />
                </Tooltip>
              )}
              {r.severity_max && (
                <Tooltip title={t('suppressions.capTip', 'Nunca silencia señales por encima de esta gravedad') as string}>
                  <Chip size="small" variant="outlined"
                    label={t('suppressions.capChip', 'tope {{sev}}', { sev: r.severity_max })} />
                </Tooltip>
              )}
              <LearnedFromChip origin={r.learned_from} />
            </Stack>
          </Box>
          <Tooltip title={(r.enabled ? t('suppressions.disable', 'Desactivar') : t('suppressions.enable', 'Activar')) as string}>
            <Box component="span" onClick={(e) => e.stopPropagation()}>
              <Switch size="small" checked={r.enabled} disabled={busy}
                onChange={(e) => patch({ enabled: e.target.checked })} />
            </Box>
          </Tooltip>
        </Stack>
      </AccordionSummary>
      <AccordionDetails sx={{ pt: 0 }}>
        <Stack spacing={1.75}>
          {r.conditional && (
            <Alert severity="warning" icon={<RuleFolderOutlinedIcon fontSize="small" />}>
              <Typography variant="body2" sx={{ fontWeight: 700 }}>
                {t('suppressions.condTitle', 'Silencio condicional')}
              </Typography>
              <Typography variant="body2">
                {t('suppressions.condBody', 'Solo si {{cond}} → {{effect}}', {
                  cond: describeCondition(r.condition), effect: effectLabel(r.condition),
                })}
              </Typography>
              {r.subject_pattern && (
                <Typography variant="caption" color="text.secondary" display="block" sx={{ mt: 0.5 }}>
                  {t('suppressions.patternLabel', 'Alcance')}: «{r.subject_pattern}»
                </Typography>
              )}
            </Alert>
          )}
          <Box>
            <Typography variant="caption" sx={{ fontWeight: 700, display: 'block', mb: 0.5 }}>
              {t('suppressions.changeDuration', 'Cambiar duración (se recuenta desde hoy)')}
            </Typography>
            <ToggleButtonGroup size="small" exclusive value={r.forever ? 'forever' : null}
              onChange={(_, v) => { if (!v) return; v === 'forever' ? patch({ forever: true }) : patch({ until_days: Number(v) }); }}>
              <ToggleButton value="7" sx={{ textTransform: 'none' }}>{t('suppressions.days7', '7 días')}</ToggleButton>
              <ToggleButton value="30" sx={{ textTransform: 'none' }}>{t('suppressions.days30', '30 días')}</ToggleButton>
              <ToggleButton value="90" sx={{ textTransform: 'none' }}>{t('suppressions.days90', '90 días')}</ToggleButton>
              <ToggleButton value="forever" sx={{ textTransform: 'none' }}>{t('suppressions.foreverCap', 'Para siempre')}</ToggleButton>
            </ToggleButtonGroup>
          </Box>

          <Box>
            <Typography variant="caption" sx={{ fontWeight: 700, display: 'block', mb: 0.5 }}>
              {t('suppressions.reasonLabel', 'Motivo')}
            </Typography>
            <Stack direction="row" spacing={1} alignItems="flex-start">
              <TextField size="small" fullWidth multiline maxRows={3} value={reason}
                onChange={(e) => setReason(e.target.value)}
                placeholder={t('suppressions.reasonPh', 'Por qué se silenció…') as string} />
              <Button size="small" variant="outlined" sx={{ textTransform: 'none', mt: 0.25 }}
                disabled={busy || reason === (r.reason ?? '')} onClick={() => patch({ reason })}>
                {t('suppressions.save', 'Guardar')}
              </Button>
            </Stack>
          </Box>

          <Box sx={{ bgcolor: 'action.hover', borderRadius: 1, p: 1 }}>
            <Typography variant="caption" color="text.secondary" display="block">
              {t('suppressions.createdBy', 'Creado por {{who}}', { who })}
              {r.created_at ? ` · ${new Date(r.created_at).toLocaleString()}` : ''}
            </Typography>
            {r.last_suppressed_at && (
              <Typography variant="caption" color="text.secondary" display="block">
                {t('suppressions.lastBlocked', 'Último bloqueo: {{date}}', { date: new Date(r.last_suppressed_at).toLocaleString() })}
              </Typography>
            )}
            <Typography variant="caption" color="text.secondary" display="block" sx={{ wordBreak: 'break-all' }}>
              {t('suppressions.scope', 'Ámbito')}: {r.match_key}
            </Typography>
          </Box>

          <Box sx={{ display: 'flex', justifyContent: 'flex-end' }}>
            <Button size="small" color="error" startIcon={<DeleteOutlineIcon />} disabled={busy}
              sx={{ textTransform: 'none' }}
              onClick={() => mut.mutate({ path: `/api/agent/suppressions/${r.id}`, method: 'DELETE' })}>
              {t('suppressions.remove', 'Quitar silencio')}
            </Button>
          </Box>
        </Stack>
      </AccordionDetails>
    </Accordion>
  );
}

/** «Qué no te avisó» — the non-action log. A silence's counter tells you HOW MANY
 *  times it acted; this tells you WHICH signals it swallowed, when and why. Any
 *  system that can decide not to speak has to be auditable by its silences, not
 *  only by its rules. */
function SilenceLog({ open }: { open: boolean }) {
  const { t } = useTranslation();
  const q = useQuery({
    queryKey: ['suppression-silences'],
    queryFn: async () => {
      const r = await apiFetch('/api/agent/suppressions/silences?limit=100');
      if (!r.ok) return { items: [] };
      return r.json() as Promise<{ items: SilenceEvent[] }>;
    },
    enabled: open,
    retry: false,
  });
  const items = q.data?.items ?? [];

  if (!items.length) {
    return (
      <Alert severity="info">
        {t('suppressions.silencesEmpty', 'Todavía no hay silencios aplicados: ninguna señal se ha dejado de generar por una regla.')}
      </Alert>
    );
  }
  return (
    <Stack spacing={1}>
      <Typography variant="body2" color="text.secondary">
        {t('suppressions.silencesIntro', 'Cada línea es un aviso que el empleado NO te dio, y por qué. Sirve para auditar por efectos: si algo importante falta, aquí se ve qué regla lo tapó.')}
      </Typography>
      {items.map((s) => (
        <Box key={s.id} sx={{ border: 1, borderColor: 'divider', borderRadius: 1.5, p: 1.25 }}>
          <Stack direction="row" spacing={1} alignItems="flex-start">
            <Box sx={{ flex: 1, minWidth: 0 }}>
              <Typography variant="subtitle2" sx={{ fontWeight: 700, wordBreak: 'break-word' }}>
                {s.label || t('suppressions.unnamedSignal', '(señal sin título)')}
              </Typography>
              <Typography variant="caption" color="text.secondary" display="block">
                {s.occurred_at ? new Date(s.occurred_at).toLocaleString() : '—'}
                {s.silence ? ` · ${t('suppressions.byRule', 'por «{{rule}}»', { rule: s.silence })}` : ''}
                {s.reason ? ` · ${s.reason}` : ''}
              </Typography>
            </Box>
            <Stack direction="row" spacing={0.5} alignItems="center" flexWrap="wrap" useFlexGap>
              <Chip size="small" variant="outlined"
                color={s.effect_applied === 'deferred' ? 'info' : 'default'}
                label={s.effect_applied === 'deferred'
                  ? t('suppressions.effDeferred', 'esperó y reevaluó')
                  : t('suppressions.effSkip', 'no avisó')} />
              {s.severity && <Chip size="small" variant="outlined" label={s.severity} />}
              {!s.silence_still_active && (
                <Chip size="small" color="warning" variant="outlined"
                  label={t('suppressions.ruleGone', 'la regla ya no está activa')} />
              )}
              <LearnedFromChip origin={s.learned_from} />
            </Stack>
          </Stack>
        </Box>
      ))}
    </Stack>
  );
}

export default function SuppressionsDialog({ open, table, onClose }: {
  open: boolean; table: string; onClose: () => void;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [tab, setTab] = useState(0);

  const rulesQ = useQuery({
    queryKey: ['suppressions', table],
    queryFn: () => apiFetch(`/api/agent/suppressions?table=${table}`).then((r) => r.json()),
    enabled: open,
  });
  const all: Rule[] = rulesQ.data?.items ?? [];
  // A PROPOSED silence silences nothing (the gate checks ``review_state``), so it
  // must never sit in the same list as the ones in force: the operator has to be
  // able to tell "this no longer alerts you" from "this is asking you for it".
  const rules = all.filter((r) => !r.proposed);
  const proposals = all.filter((r) => r.proposed);

  const [err, setErr] = useState<string | null>(null);
  const call = async (path: string, method: string, body?: any) => {
    const r = await apiFetch(path, {
      method, headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!r.ok) { const b = await r.json().catch(() => null); throw new Error(b?.detail || `HTTP ${r.status}`); }
    return r.json();
  };
  const mut = useMutation({
    mutationFn: (v: { path: string; method: string; body?: any }) => call(v.path, v.method, v.body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['suppressions', table] });
      qc.invalidateQueries({ queryKey: ['self-proposals-badge'] });
      qc.invalidateQueries({ queryKey: ['suppression-silences'] });
    },
    onError: (e: any) => setErr(String(e?.message ?? e)),
  });

  // Dry run of a proposal: the only honest way to approve a conditional rule is
  // to see what it WOULD have silenced over real history.
  const [preview, setPreview] = useState<Record<number, any>>({});
  const runPreview = async (r: Rule) => {
    try {
      const res = await call('/api/agent/suppressions/preview', 'POST', {
        signal_table: r.signal_table, match_key: r.match_key,
        match_label: r.muted, subject_pattern: r.subject_pattern,
        condition: r.condition, severity_max: r.severity_max,
      });
      setPreview((p) => ({ ...p, [r.id]: res }));
    } catch (e: any) { setErr(String(e?.message ?? e)); }
  };

  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="md">
      <DialogTitle sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
        <NotificationsOffOutlinedIcon fontSize="small" />
        {t('suppressions.title', 'Silencios (no repetir)')}
      </DialogTitle>
      <DialogContent>
        <Tabs value={tab} onChange={(_, v) => setTab(v)} variant="scrollable" allowScrollButtonsMobile
          sx={{ mb: 2, borderBottom: 1, borderColor: 'divider' }}>
          <Tab sx={{ textTransform: 'none' }} label={t('suppressions.tabRules', 'Silencios activos')} />
          <Tab sx={{ textTransform: 'none' }} label={
            <Stack direction="row" spacing={0.75} alignItems="center">
              <span>{t('suppressions.tabProposals', 'Te lo está pidiendo')}</span>
              {proposals.length > 0 && (
                <Chip size="small" color="primary" label={proposals.length}
                  sx={{ height: 18, '& .MuiChip-label': { px: 0.6, fontSize: 11, fontWeight: 700 } }} />
              )}
            </Stack>
          } />
          <Tab sx={{ textTransform: 'none' }} icon={<HistoryToggleOffIcon fontSize="small" />}
            iconPosition="start" label={t('suppressions.tabSilences', 'Qué no te avisó')} />
        </Tabs>

        {err && <Alert severity="error" sx={{ mb: 1 }} onClose={() => setErr(null)}>{err}</Alert>}

        {tab === 0 && (<>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            {t('suppressions.introManage', 'Estas son las señales silenciadas: el empleado no las volverá a generar aunque se cumplan sus condiciones. Abre cada una para ver el detalle o editarla (duración, motivo). Para silenciar una nueva, márcala como hecha o descártala y pulsa «Que no se repita».')}
          </Typography>
          {rules.length > 0 ? (
            <Stack spacing={1}>
              {rules.map((r) => <RuleRow key={r.id} r={r} mut={mut} busy={mut.isPending} t={t} />)}
            </Stack>
          ) : (
            <Alert severity="info">{t('suppressions.emptyManage', 'No hay señales silenciadas. Para silenciar una, márcala como hecha o descártala en la lista y pulsa «Que no se repita».')}</Alert>
          )}
        </>)}

        {tab === 1 && (<>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            {t('suppressions.introProposals', 'El empleado ha aprendido algo y te pide permiso para dejar de avisarte. Mientras no lo aceptes NO silencia nada: seguirás recibiendo los avisos. Pulsa «Probar» para ver a qué señales reales habría afectado antes de decidir.')}
          </Typography>
          {proposals.length > 0 ? (
            <Stack spacing={1}>
              {proposals.map((r) => (
                <Box key={r.id} sx={{ border: 1, borderColor: 'primary.main', borderRadius: 1.5, p: 1.5 }}>
                  <Typography variant="subtitle2" sx={{ fontWeight: 700, wordBreak: 'break-word' }}>{r.muted}</Typography>
                  {r.reason && (
                    <Typography variant="body2" color="text.secondary" sx={{ mt: 0.25 }}>{r.reason}</Typography>
                  )}
                  <Stack direction="row" spacing={0.75} alignItems="center" flexWrap="wrap" useFlexGap sx={{ mt: 0.75 }}>
                    {r.conditional && (
                      <Chip size="small" color="warning" variant="outlined" icon={<RuleFolderOutlinedIcon />}
                        label={`${t('suppressions.onlyIf', 'solo si')} ${describeCondition(r.condition)}`}
                        sx={{ maxWidth: 420 }} />
                    )}
                    {r.severity_max && (
                      <Chip size="small" variant="outlined"
                        label={t('suppressions.capChip', 'tope {{sev}}', { sev: r.severity_max })} />
                    )}
                    <LearnedFromChip origin={r.learned_from} />
                  </Stack>
                  {preview[r.id] && (
                    <Alert severity={preview[r.id]?.would_affect ? 'warning' : 'success'} sx={{ mt: 1 }}>
                      <Typography variant="body2">{preview[r.id]?.summary ?? preview[r.id]?.error}</Typography>
                      {(preview[r.id]?.hits ?? []).slice(0, 8).map((h: any) => (
                        <Typography key={h.id} variant="caption" display="block" color="text.secondary">
                          · {h.label} {h.severity ? `(${h.severity})` : ''} — {h.reason}
                        </Typography>
                      ))}
                    </Alert>
                  )}
                  <Stack direction="row" spacing={1} justifyContent="flex-end" sx={{ mt: 1 }}>
                    <Button size="small" sx={{ textTransform: 'none' }} disabled={mut.isPending}
                      onClick={() => runPreview(r)}>
                      {t('suppressions.tryIt', 'Probar')}
                    </Button>
                    <Button size="small" color="error" sx={{ textTransform: 'none' }} disabled={mut.isPending}
                      onClick={() => mut.mutate({ path: `/api/agent/suppressions/${r.id}/reject`, method: 'POST', body: {} })}>
                      {t('suppressions.reject', 'No, sigue avisándome')}
                    </Button>
                    <Button size="small" variant="contained" sx={{ textTransform: 'none' }} disabled={mut.isPending}
                      onClick={() => mut.mutate({ path: `/api/agent/suppressions/${r.id}/accept`, method: 'POST', body: { where: 'queue' } })}>
                      {t('suppressions.accept', 'Aceptar')}
                    </Button>
                  </Stack>
                </Box>
              ))}
            </Stack>
          ) : (
            <Alert severity="info">{t('suppressions.emptyProposals', 'El empleado no te está pidiendo ningún silencio.')}</Alert>
          )}
        </>)}

        {tab === 2 && <SilenceLog open={open && tab === 2} />}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} sx={{ textTransform: 'none' }}>{t('common.close', 'Cerrar')}</Button>
      </DialogActions>
    </Dialog>
  );
}
