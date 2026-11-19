/**
 * The SURFACE ASSISTANT (v7.37.0) — a deterministic, metadata-driven guide layer
 * shared by every business surface, so the operator always understands the
 * screen and each action explains EXACTLY what it will do before doing it.
 *
 *  - ACTIONS: per-action descriptor (title + plain-language "what happens" +
 *    whether it's DANGEROUS/irreversible). Keyed by a canonical action id (a
 *    lifecycle target state like ``resolved`` or a fixed verb like ``release``).
 *  - useActionConfirm(): wraps an action. For a dangerous/irreversible action it
 *    opens a confirm dialog ("this is what will happen" + Confirm/Cancel);
 *    a reversible one runs straight through.
 *  - SurfaceGuideButton: the "How does this screen work?" explainer. It opens
 *    a rich dialog: the screen's purpose + a graphical STATE MACHINE (states as
 *    coloured nodes, the lifecycle shape at a glance) + a card per action saying
 *    what it does and whether it can be undone. The diagram is canonical per
 *    archetype, or BUILT FROM the entity's own lifecycle for the generic board —
 *    so a new entity documents itself with no extra code.
 *
 * Text lives here as {es,en} pairs (not i18n keys) so a large block of
 * explanatory copy never churns the i18n parity gate.
 */
import { Fragment, useCallback, useMemo, useState } from 'react';
import {
  Box, Button, Chip, Dialog, DialogActions, DialogContent, DialogContentText,
  DialogTitle, Divider, IconButton, Stack, TextField, Tooltip, Typography,
} from '@mui/material';
import HelpOutlineIcon from '@mui/icons-material/HelpOutline';
import WarningAmberIcon from '@mui/icons-material/WarningAmber';
import ArrowForwardIcon from '@mui/icons-material/ArrowForward';
import LockIcon from '@mui/icons-material/Lock';
import UndoIcon from '@mui/icons-material/Undo';
import { useTranslation } from 'react-i18next';
import type { Archetype } from '../resolveArchetype';

type Loc = { es: string; en: string };
interface ActionInfo { title: Loc; body: Loc; danger: boolean }

const pick = (l: Loc | undefined, lang: string): string =>
  l ? (lang.startsWith('en') ? l.en : l.es) : '';

// Canonical action id → what it does. Lifecycle transitions use their TARGET
// state as the id (acknowledged / done / dismissed …); archetype verbs use a
// fixed id (to_task / release …).
const ACTIONS: Record<string, ActionInfo> = {
  // ── unified lifecycle (v7.40.0) — the SAME five actions for inbox (alerts),
  // advisory (recommendations) and ledger (decisions). One vocabulary, one model:
  // abierta → reconocida → en curso → hecha ; descartada (✗).
  acknowledged: {
    title: { es: 'Reconocer / aceptar', en: 'Acknowledge / accept' },
    body: { es: 'Marca esto como VISTO y en gestión (aceptado). Sigue en tu bandeja hasta que lo conviertas en encargo, lo marques hecho o lo descartes. No notifica a nadie. Reversible.', en: 'Marks this as SEEN and being handled (accepted). It stays in your queue until you turn it into a task, mark it done or dismiss it. Notifies no one. Reversible.' },
    danger: false,
  },
  to_task: {
    title: { es: 'Convertir en encargo', en: 'Turn into a task' },
    body: { es: 'Crea un ENCARGO que el empleado ejecuta AHORA y pasa esto a EN CURSO (enlazado «→ Encargo #X»). El empleado define de antemano cuál es el encargo (puede diferir del propio ítem); tú puedes AÑADIR indicaciones en el campo de abajo antes de confirmar. Cuando el empleado cierra el encargo, esto pasa a Completado con el resultado. Solo aparece cuando el ítem genera trabajo accionable. No se deshace el encargo lanzado.', en: 'Creates a TASK the employee runs NOW and moves this to IN PROGRESS (linked "→ Task #X"). The employee defines the task up front (it may differ from the item itself); you can ADD instructions in the field below before confirming. When the employee closes the task, this becomes Done with the outcome. It only appears when the item warrants actionable work. The launched task cannot be undone.' },
    danger: true,
  },
  done: {
    title: { es: 'Marcar hecho', en: 'Mark done' },
    body: { es: 'Marca esto como COMPLETADO. Sale del trabajo activo y queda en el historial. No notifica a nadie. Reversible (puedes reabrirlo).', en: 'Marks this as DONE. It leaves the active work and stays in history. Notifies no one. Reversible (you can reopen it).' },
    danger: false,
  },
  dismissed: {
    title: { es: 'Descartar', en: 'Dismiss' },
    body: { es: 'DESCARTA esto sin completarlo — se considera que no requería acción. Sale de la bandeja de abiertos. Reversible, pero se registra como descartado. En recomendaciones, el motivo que escribas se guarda como LÍMITE APRENDIDO para el empleado.', en: 'DISMISSES this without completing it — treated as "no action needed". Leaves the open queue. Reversible, but logged as dismissed. On recommendations, the reason you write is saved as a LEARNED BOUNDARY for the employee.' },
    danger: true,
  },
  open: {
    title: { es: 'Reabrir', en: 'Reopen' },
    body: { es: 'Vuelve a marcarlo como ABIERTO y regresa a la bandeja para gestionarlo.', en: 'Marks it OPEN again and returns it to the queue.' },
    danger: false,
  },
  // ── outbox (communications) — status is informative here, NOT a lifecycle
  release: {
    title: { es: 'Aprobar y enviar', en: 'Approve & send' },
    body: { es: 'Aprueba un BORRADOR y lo ENVÍA AHORA por su canal al destinatario. Sale hacia fuera y NO se puede deshacer. Revisa destinatario y contenido antes de confirmar.', en: 'Approves a DRAFT and SENDS it NOW through its channel to the recipient. It leaves the system and CANNOT be undone. Check recipient and content first.' },
    danger: true,
  },
  send: {
    title: { es: 'Enviar ahora', en: 'Send now' },
    body: { es: 'ENVÍA una comunicación pendiente (lista para envío) AHORA por su canal al destinatario. Sale hacia fuera y NO se puede deshacer.', en: 'SENDS a pending communication (ready to send) NOW through its channel to the recipient. It leaves the system and CANNOT be undone.' },
    danger: true,
  },
  cancel: {
    title: { es: 'Devolver a borrador', en: 'Return to draft' },
    body: { es: 'Devuelve el mensaje a BORRADOR — no se enviará. Puedes volver a aprobarlo más tarde.', en: 'Returns the message to DRAFT — it will not be sent. You can approve it again later.' },
    danger: false,
  },
  retry: {
    title: { es: 'Reintentar envío', en: 'Retry send' },
    body: { es: 'Vuelve a intentar ENVIAR un mensaje que falló. Sale hacia fuera al destinatario. No se puede deshacer.', en: 'Retries SENDING a failed message. It leaves to the recipient. Cannot be undone.' },
    danger: true,
  },
};

// The three "generation" archetypes (inbox / advisory / ledger) share the SAME
// unified lifecycle (v7.40.0), so they share the SAME screen-level flow copy —
// only the noun changes (alerta / recomendación / decisión = alert / recommendation / decision).
const UNIFIED_FLOW: Loc = {
  es: 'Todo lo que genero comparte el mismo ciclo: abierta → reconocida → en curso → hecha; o descartada. Recónocelo (lo estás gestionando), conviértelo en un ENCARGO (el empleado actúa ahora y, al cerrarlo, esto pasa a Hecho con el resultado), márcalo hecho o descártalo. Puedes filtrar por estado (pincha cada uno, con su número), por impacto, urgencia y confianza, y acotar por fecha; la lista se pagina. El número grande de cada tipo en el panel es lo PENDIENTE, no el total.',
  en: 'Everything I generate shares the same cycle: open → acknowledged → in progress → done; or dismissed. Acknowledge it (you are handling it), turn it into a TASK (the employee acts now and, on closing it, this becomes Done with the outcome), mark it done or dismiss it. Filter by state (click each, with its count), by impact, urgency and confidence, and narrow by date; the list is paginated. On the panel, the big number per type is what is PENDING, not the total.',
};

// Screen-level explanation per archetype.
const GUIDES: Partial<Record<Archetype, Loc>> = {
  inbox: {
    es: `Sala de alertas. El empleado registra aquí incidencias por gravedad. ${UNIFIED_FLOW.es}`,
    en: `Alert console. The employee logs incidents here by severity. ${UNIFIED_FLOW.en}`,
  },
  outbox: {
    es: 'Bandeja de salida. Mensajes que el empleado ha preparado. Nada sale sin tu visto bueno: revisa el destinatario y el contenido y envíalo. Verás el estado de entrega y, si algo falla, el motivo — y podrás reintentar.',
    en: 'Outbox. Messages the employee drafted. Nothing goes out without your OK: review the recipient and content and send it. You will see the delivery status and, on failure, the reason — and you can retry.',
  },
  advisory: {
    es: `Recomendaciones del empleado. Cada tarjeta propone algo con su motivo e impacto. ${UNIFIED_FLOW.es} Al descartar, el motivo se convierte en un límite aprendido.`,
    en: `Employee recommendations. Each card proposes something with its rationale and impact. ${UNIFIED_FLOW.en} When you dismiss one, the reason becomes a learned boundary.`,
  },
  ledger: {
    es: `Bitácora de decisiones. Registro de lo que el empleado consideró y por qué actuó (o no) — para transparencia. Cada decisión lleva una clasificación (Sin acción / Aplazada / Escalada) y un estado del mismo ciclo. ${UNIFIED_FLOW.es}`,
    en: `Decision log. A record of what the employee considered and why it did (or did not) act — for transparency. Each decision carries a classification (No action / Deferred / Escalated) and a status on the same cycle. ${UNIFIED_FLOW.en}`,
  },
  board: {
    es: 'Tablero por estado. El trabajo avanza entre columnas con «Mover». Cada transición te explica qué significa antes de aplicarla. El diagrama de abajo muestra los estados posibles y cómo se pasa de uno a otro.',
    en: 'Status board. Work advances between columns with "Move". Each transition explains what it means before applying it. The diagram below shows the possible states and how you move between them.',
  },
};

// Which actions to document per archetype (drives the action cards in the guide).
// inbox / advisory / ledger now expose the SAME unified action set.
const UNIFIED_ARCHETYPE_ACTIONS = ['acknowledged', 'to_task', 'done', 'dismissed'];
const ARCHETYPE_ACTIONS: Partial<Record<Archetype, string[]>> = {
  inbox: UNIFIED_ARCHETYPE_ACTIONS,
  advisory: UNIFIED_ARCHETYPE_ACTIONS,
  ledger: UNIFIED_ARCHETYPE_ACTIONS,
  outbox: ['release', 'send', 'cancel', 'retry'],
};

// ---- state machine (graphical) --------------------------------------------

type NodeRole = 'start' | 'mid' | 'good' | 'bad' | 'neutral';
interface SMNode { id: string; role: NodeRole; col: number; label?: Loc }
// `action` labels the edge with an ACTIONS entry (its title); `label` overrides
// with literal text (for platform/automatic transitions with no operator verb).
interface SMEdge { from: string; to: string; action?: string; label?: Loc }
interface SMSpec { nodes: SMNode[]; edges: SMEdge[] }

// v7.40.0 — ONE state machine shared by inbox / advisory / ledger: the unified
// lifecycle. abierta → reconocida → en curso → hecha ; descartada (✗).
const UNIFIED_SM: SMSpec = {
  nodes: [
    { id: 'open', role: 'start', col: 0 },
    { id: 'acknowledged', role: 'mid', col: 1 },
    { id: 'in_progress', role: 'mid', col: 2 },
    { id: 'done', role: 'good', col: 3 },
    { id: 'dismissed', role: 'bad', col: 3 },
  ],
  edges: [
    { from: 'open', to: 'acknowledged', action: 'acknowledged' },
    { from: 'open', to: 'in_progress', action: 'to_task' },
    { from: 'open', to: 'dismissed', action: 'dismissed' },
    { from: 'acknowledged', to: 'in_progress', action: 'to_task' },
    { from: 'acknowledged', to: 'done', action: 'done' },
    { from: 'acknowledged', to: 'dismissed', action: 'dismissed' },
    { from: 'in_progress', to: 'done', label: { es: 'El empleado cierra el encargo', en: 'Employee closes the task' } },
  ],
};

const STATE_MACHINES: Partial<Record<Archetype, SMSpec>> = {
  inbox: UNIFIED_SM,
  advisory: UNIFIED_SM,
  ledger: UNIFIED_SM,
  outbox: {
    nodes: [
      { id: 'draft', role: 'start', col: 0 },
      { id: 'ready', role: 'mid', col: 1 },
      { id: 'sent', role: 'good', col: 2 },
      { id: 'failed', role: 'bad', col: 2 },
    ],
    edges: [
      { from: 'draft', to: 'ready', action: 'release' },
      { from: 'ready', to: 'sent', label: { es: 'Entrega', en: 'Delivery' } },
      { from: 'ready', to: 'failed', label: { es: 'Fallo de entrega', en: 'Delivery failure' } },
      { from: 'failed', to: 'ready', action: 'retry' },
    ],
  },
};

const ROLE_BORDER: Record<NodeRole, string> = {
  start: 'grey.500', mid: 'info.main', good: 'success.main', bad: 'error.main', neutral: 'grey.500',
};
const ROLE_BG: Record<NodeRole, string> = {
  start: 'grey.100', mid: 'info.50', good: 'success.50', bad: 'error.50', neutral: 'grey.100',
};

/** Derive a state-machine spec from an entity's own lifecycle (for the generic
 * board). Starts = states with no incoming edge; columns = longest path from a
 * start; role inferred from the state name for terminal states. */
function buildSpecFromLifecycle(lc: any): SMSpec | null {
  const states: string[] = (lc?.states ?? []).map((s: any) => String(s));
  if (states.length < 2) return null;
  const transitions: Record<string, string[]> = lc?.transitions ?? {};
  const incoming = new Set<string>();
  for (const tos of Object.values(transitions)) for (const to of (tos ?? [])) incoming.add(String(to));
  const outgoing: Record<string, number> = {};
  for (const [f, tos] of Object.entries(transitions)) outgoing[f] = (tos ?? []).length;
  const col: Record<string, number> = {};
  const starts = states.filter((s) => !incoming.has(s));
  const queue = [...(starts.length ? starts : [states[0]])];
  for (const s of queue) col[s] = 0;
  let guard = 0;
  while (queue.length && guard++ < 1000) {
    const cur = queue.shift() as string;
    for (const to of (transitions[cur] ?? [])) {
      const c = (col[cur] ?? 0) + 1;
      if (col[String(to)] == null || c > col[String(to)]) { col[String(to)] = c; queue.push(String(to)); }
    }
  }
  const roleOf = (s: string): NodeRole => {
    if (!incoming.has(s)) return 'start';
    if (!(outgoing[s] > 0)) {
      if (/(done|resolved|closed|complete|accept|sent|approv|paid|won)/i.test(s)) return 'good';
      if (/(reject|dismiss|fail|cancel|error|lost)/i.test(s)) return 'bad';
      return 'neutral';
    }
    return 'mid';
  };
  const nodes: SMNode[] = states.map((s) => ({ id: s, role: roleOf(s), col: col[s] ?? 0 }));
  const edges: SMEdge[] = [];
  for (const [f, tos] of Object.entries(transitions)) for (const to of (tos ?? [])) edges.push({ from: f, to: String(to) });
  return { nodes, edges };
}

function StateMachineDiagram({ spec }: { spec: SMSpec }) {
  const { t, i18n } = useTranslation();
  const lang = i18n.language || 'es';
  const columns = useMemo(() => {
    const by: Record<number, SMNode[]> = {};
    for (const n of spec.nodes) (by[n.col] ??= []).push(n);
    return Object.keys(by).map(Number).sort((a, b) => a - b).map((c) => by[c]);
  }, [spec]);
  const label = (n: SMNode) => n.label ? pick(n.label, lang) : t(`states.${n.id}`, n.id);
  const edgeLabel = (e: SMEdge) =>
    e.label ? pick(e.label, lang) : (e.action && ACTIONS[e.action] ? pick(ACTIONS[e.action].title, lang) : '');
  return (
    <>
      {/* the shape: states as coloured nodes laid out in flow columns */}
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 0, overflowX: 'auto', py: 1.5 }}>
        {columns.map((nodes, ci) => (
          <Fragment key={ci}>
            <Stack spacing={1.25} justifyContent="center" sx={{ minWidth: 104 }}>
              {nodes.map((n) => (
                <Box key={n.id} sx={{
                  px: 1.5, py: 0.85, borderRadius: 1.5, border: 2,
                  borderColor: ROLE_BORDER[n.role], bgcolor: ROLE_BG[n.role],
                  textAlign: 'center', fontSize: 13, fontWeight: 600, whiteSpace: 'nowrap',
                }}>
                  {label(n)}
                </Box>
              ))}
            </Stack>
            {ci < columns.length - 1 && (
              <Box sx={{ px: 0.75, color: 'text.disabled', display: 'flex', alignItems: 'center' }}>
                <ArrowForwardIcon fontSize="small" />
              </Box>
            )}
          </Fragment>
        ))}
      </Box>
      {/* the transitions: exactly which action moves between which states (so a
          state with no outgoing edge reads as clearly terminal). */}
      {spec.edges.length > 0 && (
        <Stack spacing={0.5} sx={{ mt: 0.5 }}>
          {spec.edges.map((e, i) => {
            const lbl = edgeLabel(e);
            return (
              <Stack key={i} direction="row" spacing={0.75} alignItems="center" flexWrap="wrap" useFlexGap>
                <Chip size="small" variant="outlined" label={t(`states.${e.from}`, e.from)} />
                <ArrowForwardIcon sx={{ fontSize: 14, color: 'text.disabled' }} />
                <Chip size="small" variant="outlined" label={t(`states.${e.to}`, e.to)} />
                {lbl && <Typography variant="caption" color="text.secondary">· {lbl}</Typography>}
              </Stack>
            );
          })}
        </Stack>
      )}
    </>
  );
}

export function actionTip(actionId: string, lang: string): string {
  return pick(ACTIONS[actionId]?.body, lang);
}

/** Extra concrete detail to preview inside the confirm dialog — e.g. the exact
 * encargo (title + description) that a `to_task` will request, so the operator
 * sees what will be created before confirming. ``input`` (v7.44.0) adds a
 * free-text field whose value is passed to ``onOk`` — used so the operator can
 * COMPLEMENT the encargo with extra instructions when converting. */
export interface ConfirmPreview {
  heading?: string; title?: string; body?: string;
  input?: { label?: string; placeholder?: string };
}

// v7.40.0 — the ordered unified operator actions for a lifecycle row. `to_task`
// (convert to encargo) is offered while the row is still open/acknowledged and
// has not spawned one; the plain lifecycle transitions follow, EXCEPT
// ``in_progress`` (reached only via to_task, never a bare button). Ordered
// reconocer → convertir → hecho → descartar → reabrir so every surface reads the
// same.
const _ACTION_ORDER = ['acknowledged', 'to_task', 'done', 'dismissed', 'open'];
export function lifecycleActionsFor(
  status: string, lifecycle: any, hasEncargo: boolean, allowTask = false,
): string[] {
  const cur = String(status || '').toLowerCase();
  const nexts: string[] = lifecycle?.transitions?.[cur]
    ?? (lifecycle?.states ?? []).filter((s: string) => String(s).toLowerCase() !== cur);
  const ids = new Set<string>();
  for (const ns of nexts) {
    const s = String(ns).toLowerCase();
    if (s === 'in_progress') continue; // reached via to_task only
    ids.add(s);
  }
  // v7.44.0 — the «Convertir en encargo» button appears ONLY when the agent
  // marked the item as encargo-worthy (allow_task). Hidden by default.
  if (allowTask && !hasEncargo && (cur === 'open' || cur === 'acknowledged')) ids.add('to_task');
  return _ACTION_ORDER.filter((a) => ids.has(a));
}

/** The unified action buttons (v7.40.0) shared by inbox / advisory / ledger.
 * Renders «Reconocer/Aceptar», «Convertir en encargo», «Marcar hecho» and
 * «Descartar» / «Reabrir» from the row's OWN lifecycle, wiring each through the
 * confirm dialog. The parent owns the API calls (onTransition / onToTask) and
 * may intercept dismiss (onDismiss) to collect a learned-boundary reason. */
export function LifecycleActionBar({
  status, lifecycle, hasEncargo, allowTask = false, ackLabel, isBusy, confirm,
  onTransition, onToTask, onDismiss, toTaskPreview,
}: {
  status: string;
  lifecycle: any;
  hasEncargo: boolean;
  /** v7.44.0 — the agent flagged this item as encargo-worthy (metadata.task.allow). */
  allowTask?: boolean;
  ackLabel: string;
  isBusy: (id: string) => boolean;
  confirm: (id: string, onOk: (note?: string) => void, preview?: ConfirmPreview) => void;
  onTransition: (state: string) => void;
  onToTask: (note?: string) => void;
  onDismiss?: () => void;
  toTaskPreview?: ConfirmPreview;
}) {
  const { t, i18n } = useTranslation();
  const lang = i18n.language || 'es';
  const ids = lifecycleActionsFor(status, lifecycle, hasEncargo, allowTask);
  const cur = String(status || '').toLowerCase();
  // Actionable but the agent said it does NOT warrant an encargo → say so (v7.44.0).
  const noEncargo = !allowTask && !hasEncargo && (cur === 'open' || cur === 'acknowledged');
  if (!ids.length && !noEncargo) return null;
  // Uniform layout (v7.61.0): the encargo affordance — the «Convertir en encargo»
  // button OR the «Sin encargo asociado» note — always sits in the SAME slot
  // (right after Reconocer), and the column has a fixed width, so «Marcar hecho» /
  // «Descartar» align across cards regardless of whether the item allows an
  // encargo. Before, the note was appended at the bottom and the layout shifted.
  const slots: string[] = [...ids];
  if (noEncargo) {
    const at = slots.indexOf('acknowledged');
    slots.splice(at >= 0 ? at + 1 : slots.length, 0, '__no_encargo__');
  }
  return (
    <Stack spacing={0.5} sx={{ flexShrink: 0, alignItems: 'stretch', minWidth: 168 }}>
      {slots.map((id) => {
        if (id === '__no_encargo__') {
          return (
            <Typography key="__no_encargo__" variant="caption" color="text.disabled"
              sx={{ textAlign: 'center', py: 0.6, lineHeight: 1.2 }}>
              {t('surfaces.guide.noEncargo', 'Sin encargo asociado')}
            </Typography>
          );
        }
        const label = id === 'acknowledged' ? ackLabel : pick(ACTIONS[id]?.title, lang);
        const variant = id === 'done' ? 'contained' : id === 'acknowledged' ? 'outlined' : 'text';
        const color = id === 'dismissed' ? 'inherit' : undefined;
        const onClick = () => {
          // to_task collects an optional operator note (complements the encargo).
          if (id === 'to_task') {
            confirm('to_task', (note) => onToTask(note), {
              ...(toTaskPreview || {}),
              input: {
                label: t('surfaces.guide.encargoNote', 'Indicaciones adicionales (opcional)'),
                placeholder: t('surfaces.guide.encargoNotePh', 'Añade contexto o instrucciones para este encargo…'),
              },
            });
            return;
          }
          if (id === 'dismissed' && onDismiss) { onDismiss(); return; }
          confirm(id, () => onTransition(id));
        };
        return (
          <Tooltip key={id} title={actionTip(id, lang)} placement="left" disableInteractive>
            <span>
              <Button size="small" variant={variant as any} color={color as any}
                disabled={isBusy(id)} onClick={onClick}>
                {label}
              </Button>
            </span>
          </Tooltip>
        );
      })}
    </Stack>
  );
}

/** Wrap a surface's actions. `confirm(actionId, onOk, preview?)` shows the "what
 * happens" dialog for a DANGEROUS action (optionally with a concrete preview of
 * what it will create), or runs `onOk` straight through otherwise. */
export function useActionConfirm() {
  const { t, i18n } = useTranslation();
  const lang = i18n.language || 'es';
  const [pending, setPending] = useState<{ info: ActionInfo; onOk: (note?: string) => void; preview?: ConfirmPreview } | null>(null);
  const [note, setNote] = useState('');

  const confirm = useCallback((actionId: string, onOk: (note?: string) => void, preview?: ConfirmPreview) => {
    const info = ACTIONS[actionId];
    if (info?.danger) { setNote(''); setPending({ info, onOk, preview }); }
    else onOk();
  }, []);

  const dialog = (
    <Dialog open={!!pending} onClose={() => setPending(null)} maxWidth="xs" fullWidth>
      {pending && (
        <>
          <DialogTitle sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
            <WarningAmberIcon color="warning" fontSize="small" />
            {pick(pending.info.title, lang)}
          </DialogTitle>
          <DialogContent>
            <DialogContentText sx={{ whiteSpace: 'pre-wrap' }}>
              {pick(pending.info.body, lang)}
            </DialogContentText>
            {pending.preview && (pending.preview.title || pending.preview.body) && (
              <Box sx={{ mt: 1.5, p: 1.25, border: 1, borderColor: 'divider', borderRadius: 1.5, bgcolor: 'action.hover' }}>
                <Typography variant="caption" color="text.secondary" sx={{ fontWeight: 700, display: 'block' }}>
                  {pending.preview.heading || t('surfaces.guide.willCreate', 'Se creará este encargo:')}
                </Typography>
                {pending.preview.title && (
                  <Typography variant="body2" sx={{ fontWeight: 700, mt: 0.25 }}>{pending.preview.title}</Typography>
                )}
                {pending.preview.body && (
                  <Typography variant="body2" color="text.secondary"
                    sx={{ mt: 0.25, whiteSpace: 'pre-wrap', maxHeight: 180, overflow: 'auto' }}>
                    {pending.preview.body}
                  </Typography>
                )}
              </Box>
            )}
            {pending.preview?.input && (
              <TextField fullWidth multiline minRows={2} size="small" sx={{ mt: 1.5 }}
                label={pending.preview.input.label}
                placeholder={pending.preview.input.placeholder}
                value={note} onChange={(e) => setNote(e.target.value)} />
            )}
          </DialogContent>
          <DialogActions>
            <Button onClick={() => setPending(null)}>{t('common.cancel', 'Cancelar')}</Button>
            <Button variant="contained" onClick={() => { const ok = pending.onOk; const n = note; setPending(null); ok(n.trim() || undefined); }}>
              {t('common.confirm', 'Confirmar')}
            </Button>
          </DialogActions>
        </>
      )}
    </Dialog>
  );

  return { confirm, dialog };
}

/** The "How does this screen work?" explainer: purpose + graphical state
 * machine + per-action "what happens" cards. `lifecycle` (optional) lets the
 * generic board draw a diagram from the entity's own states. */
// v7.41.0 — the UNIVERSAL signal policy, explained the SAME way on every signal
// surface (inbox/advisory/ledger) so the operator understands WHY a card carries
// the severity/priority it does. Five separate, domain-agnostic axes.
const POLICY_AXES: { label: Loc; loc: Loc }[] = [
  { label: { es: 'Impacto', en: 'Impact' }, loc: { es: 'magnitud de la consecuencia (bajo → crítico).', en: 'magnitude of the consequence (low → critical).' } },
  { label: { es: 'Urgencia', en: 'Urgency' }, loc: { es: 'cuánto corre actuar (bajo → inmediato).', en: 'how soon action is needed (low → immediate).' } },
  { label: { es: 'Confianza', en: 'Confidence' }, loc: { es: 'cuán seguro está el empleado; si es baja, baja la intensidad.', en: 'how sure the employee is; low confidence lowers the intensity.' } },
  { label: { es: 'Riesgo', en: 'Risk' }, loc: { es: 'la Severidad (alertas) y la Prioridad (recomendaciones) NO se escriben a mano: se DERIVAN de impacto × urgencia.', en: 'Severity (alerts) and Priority (recommendations) are NOT hand-set: they are DERIVED from impact × urgency.' } },
  { label: { es: 'Aprobación', en: 'Approval' }, loc: { es: 'tu decisión: reconocer, convertir en encargo, marcar hecha o descartar (el ciclo de estados).', en: 'your call: acknowledge, turn into a task, mark done or dismiss (the status cycle).' } },
];
const POLICY_EXTRA: Loc = {
  es: 'Las alertas se DEDUPLICAN por clave/causa: si un problema reaparece, se actualiza la MISMA alerta (con «Visto N veces») en vez de duplicar. Las decisiones llevan un MOTIVO estructurado (por qué NO se actúa) y una referencia al hallazgo evaluado.',
  en: 'Alerts DEDUPLICATE by key/cause: a recurring problem updates the SAME alert ("Seen N times") instead of duplicating. Decisions carry a STRUCTURED reason code (why NOT acting) and a reference to the finding evaluated.',
};
const POLICY_ARCHETYPES = new Set<Archetype>(['inbox', 'advisory', 'ledger']);

export function SurfaceGuideButton({ archetype, lifecycle }: { archetype: Archetype; lifecycle?: any }) {
  const { t, i18n } = useTranslation();
  const lang = i18n.language || 'es';
  const [open, setOpen] = useState(false);
  const g = GUIDES[archetype];
  const spec = useMemo(
    () => STATE_MACHINES[archetype] ?? (archetype === 'board' ? buildSpecFromLifecycle(lifecycle) : null),
    [archetype, lifecycle],
  );
  const actions = ARCHETYPE_ACTIONS[archetype] ?? [];
  if (!g) return null;

  return (
    <>
      <Tooltip title={t('surfaces.guide.help', '¿Cómo funciona esta pantalla?') as string}>
        <IconButton size="small" onClick={() => setOpen(true)}><HelpOutlineIcon fontSize="small" /></IconButton>
      </Tooltip>
      <Dialog open={open} onClose={() => setOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
          <HelpOutlineIcon color="primary" fontSize="small" />
          {t('surfaces.guide.title', '¿Cómo funciona esta pantalla?')}
        </DialogTitle>
        <DialogContent>
          <Typography variant="body2" color="text.secondary" sx={{ whiteSpace: 'pre-wrap' }}>
            {pick(g, lang)}
          </Typography>

          {POLICY_ARCHETYPES.has(archetype) && (
            <Box sx={{ mt: 2 }}>
              <Divider sx={{ mb: 1.5 }} />
              <Typography variant="overline" color="text.secondary">
                {t('surfaces.guide.policy', 'Cómo decido qué genero (política común)')}
              </Typography>
              <Stack spacing={0.5} sx={{ mt: 0.5 }}>
                {POLICY_AXES.map((a) => (
                  <Typography key={a.label.en} variant="body2" color="text.secondary">
                    <b>{pick(a.label, lang)}:</b> {pick(a.loc, lang)}
                  </Typography>
                ))}
              </Stack>
              <Typography variant="body2" color="text.secondary" sx={{ mt: 0.75, whiteSpace: 'pre-wrap' }}>
                {pick(POLICY_EXTRA, lang)}
              </Typography>
            </Box>
          )}

          {spec && spec.nodes.length > 0 && (
            <Box sx={{ mt: 2 }}>
              <Divider sx={{ mb: 1.5 }} />
              <Typography variant="overline" color="text.secondary">
                {t('surfaces.guide.flow', 'Flujo de estados')}
              </Typography>
              <StateMachineDiagram spec={spec} />
            </Box>
          )}

          {actions.length > 0 && (
            <Box sx={{ mt: 1 }}>
              <Divider sx={{ mb: 1.5 }} />
              <Typography variant="overline" color="text.secondary">
                {t('surfaces.guide.actions', 'Qué hace cada acción')}
              </Typography>
              <Stack spacing={1} sx={{ mt: 0.5 }}>
                {actions.map((id) => {
                  const a = ACTIONS[id];
                  if (!a) return null;
                  return (
                    <Box key={id} sx={{ p: 1, border: 1, borderColor: 'divider', borderRadius: 1.5 }}>
                      <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.25 }}>
                        <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>{pick(a.title, lang)}</Typography>
                        <Chip size="small" variant="outlined"
                          color={a.danger ? 'warning' : 'success'}
                          icon={a.danger ? <LockIcon sx={{ fontSize: 14 }} /> : <UndoIcon sx={{ fontSize: 14 }} />}
                          label={a.danger ? t('surfaces.guide.irreversible', 'No se puede deshacer')
                                          : t('surfaces.guide.reversible', 'Reversible')} />
                      </Stack>
                      <Typography variant="body2" color="text.secondary">{pick(a.body, lang)}</Typography>
                    </Box>
                  );
                })}
              </Stack>
            </Box>
          )}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setOpen(false)}>{t('common.close', 'Cerrar')}</Button>
        </DialogActions>
      </Dialog>
    </>
  );
}
