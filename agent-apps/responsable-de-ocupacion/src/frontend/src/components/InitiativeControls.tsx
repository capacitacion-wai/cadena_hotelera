/**
 * v7.78.0 — the employee's INITIATIVE dials, centralised on the "Comportamiento"
 * screen (they used to live in Operaciones → "Rutina del empleado"). Each is a
 * self-contained control over ``/api/agent/settings`` (react-query key
 * ``agent-settings``): the master kill-switch for autonomous work, the execution
 * concurrency caps, the turn time limits (15.6.0), and self-scheduling. Extracted to a shared component so both
 * the (renamed) Comportamiento page and any admin surface can render the SAME
 * control without duplicating logic.
 */
import { useEffect, useState } from 'react';
import {
  Alert, Box, Card, CardContent, Stack, Switch, TextField, Typography,
} from '@mui/material';
import { useTranslation } from 'react-i18next';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { apiFetch } from '../services/apiClient';

function useSettings() {
  return useQuery({
    queryKey: ['agent-settings'],
    queryFn: () => apiFetch('/api/agent/settings').then(async (r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    }),
    staleTime: 10_000,
  });
}

function usePatchSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: Record<string, unknown>) => apiFetch('/api/agent/settings', {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    }).then(async (r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['agent-settings'] });
      // autonomy/triggers changes flip the health banner's triggers_off blocker.
      qc.invalidateQueries({ queryKey: ['agent-health'] });
      qc.invalidateQueries({ queryKey: ['capabilities'] });
    },
  });
}

/**
 * Master kill-switch for ALL autonomous work (the single place to pause it).
 *
 * v7.108.0 — it no longer paints the OPERATOR'S INTENT as if it were reality. The
 * switch being ON does not mean the employee is working: with every routine paused it
 * watches NOTHING, and a green "Ejecución autónoma activada" made the operator believe
 * he had an autonomous employee when he had a chatbot (INC-KOM-49). Three honest
 * states now, driven by the backend's ``autonomous_work_active`` (switch AND ≥1 armed
 * routine) + the routine counts:
 *   · ON  + routines armed → success, saying HOW MANY are running;
 *   · ON  + zero armed     → WARNING: "no vigila nada" + where to fix it;
 *   · OFF                  → warning: paused (unchanged).
 * Pausing routines is legitimate; announcing it as active autonomy is not.
 */
export function TriggersMasterSwitch() {
  const { t } = useTranslation();
  const q = useSettings();
  const mut = usePatchSettings();
  const active = !!q.data?.triggers_active;
  const armed: number = q.data?.automations_enabled ?? 0;
  const total: number = q.data?.automations_total ?? 0;
  // Fall back to `active` only while the counts are unknown (older backend), so the
  // component degrades to the previous behaviour instead of crying wolf.
  const working = q.data?.autonomous_work_active ?? active;
  const idle = active && !working;   // the lie we are killing: "on" but nothing armed
  return (
    <Alert severity={working ? 'success' : 'warning'} icon={false} sx={{ mb: 2 }}>
      <Stack direction="row" alignItems="center" spacing={1.5}>
        <Switch checked={active} disabled={q.isLoading || mut.isPending}
          onChange={(e) => mut.mutate({ triggers_active: e.target.checked })} />
        <Box sx={{ flex: 1, minWidth: 0 }}>
          <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>
            {working
              ? t('operations.triggers.masterOnActive', {
                  count: armed,
                  defaultValue: 'Ejecución autónoma activada · {{count}} rutina(s) activa(s)',
                })
              : idle
                ? t('operations.triggers.masterOnIdle', {
                    total,
                    defaultValue: 'Activada, pero SIN rutinas activas: el empleado no vigila nada ({{total}} en pausa)',
                  })
                : t('operations.triggers.masterOff', 'Ejecución autónoma en pausa')}
          </Typography>
          <Typography variant="caption" color="text.secondary">
            {idle
              ? t('operations.triggers.masterIdleHint',
                  'El interruptor está encendido, pero todas las rutinas de «Trabajo autónomo» están pausadas: el empleado NO se pondrá en marcha por su cuenta. Activa al menos una rutina abajo para que vigile de verdad.')
              : t('operations.triggers.masterHint',
                  'Interruptor general del trabajo autónomo: es el único sitio para pararlo todo. Cuando está en pausa, el empleado no se pone en marcha por su cuenta — se detienen las tareas programadas, las automatizaciones, el trabajo autónomo y las reacciones a eventos. El chat y las respuestas por los canales siguen funcionando con normalidad. Por defecto un empleado nuevo arranca en pausa (los niveles de autonomía más altos pueden arrancarlo activado).')}
          </Typography>
        </Box>
      </Stack>
    </Alert>
  );
}

/** Execution concurrency caps (total in-flight + max autonomous, reserving human capacity). */
export function ConcurrencyControl() {
  const { t } = useTranslation();
  const q = useSettings();
  const mut = usePatchSettings();
  const ceiling: number = q.data?.max_concurrent_ceiling ?? 8;
  const total: number = q.data?.max_concurrent_runs ?? 2;
  const auto: number = q.data?.max_autonomous_concurrent ?? 1;
  const inflight = q.data?.execution_inflight ?? { in_flight_total: 0, in_flight_autonomous: 0 };
  const [dt, setDt] = useState<string>('');
  const [da, setDa] = useState<string>('');
  useEffect(() => { setDt(String(total)); }, [total]);
  useEffect(() => { setDa(String(auto)); }, [auto]);
  const clamp = (n: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, n));
  const commitTotal = () => {
    if (dt.trim() === '') { setDt(String(total)); return; }
    const n = clamp(parseInt(dt, 10) || total, 1, ceiling);
    setDt(String(n));
    if (n !== total) {
      mut.mutate({ max_concurrent_runs: n });
      // v7.83.0 — keep autonomous ≤ total: lowering the total must not leave the
      // autonomous cap above it (negative human reserve).
      if (auto > n) { setDa(String(n)); mut.mutate({ max_autonomous_concurrent: n }); }
    }
  };
  const commitAuto = () => {
    if (da.trim() === '') { setDa(String(auto)); return; }
    const n = clamp(parseInt(da, 10) || auto, 1, total);
    setDa(String(n));
    if (n !== auto) mut.mutate({ max_autonomous_concurrent: n });
  };
  return (
    <Card variant="outlined" sx={{ mb: 2 }}>
      <CardContent>
        <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>
          {t('operations.concurrency.title', 'Concurrencia de ejecución')}
        </Typography>
        <Typography variant="caption" color="text.secondary">
          {t('operations.concurrency.hint',
            'Cuántas cosas hace el empleado a la vez. Cada ejecución consume recursos del pod, por eso hay un techo. Reserva capacidad para responder a personas dejando el máximo de tareas autónomas por debajo del total.')}
        </Typography>
        <Stack direction="row" spacing={2} sx={{ mt: 1.5, flexWrap: 'wrap' }} useFlexGap>
          <TextField type="number" size="small" sx={{ maxWidth: 300 }}
            label={t('operations.concurrency.total', 'Máx. ejecuciones a la vez')}
            value={dt} disabled={q.isLoading || mut.isPending}
            onChange={(e) => setDt(e.target.value)} onBlur={commitTotal}
            inputProps={{ min: 1, max: ceiling }}
            helperText={t('operations.concurrency.totalHelp', { max: ceiling, defaultValue: `Eventos que se ejecutan en paralelo (1–${ceiling}).` })} />
          <TextField type="number" size="small" sx={{ maxWidth: 300 }}
            label={t('operations.concurrency.auto', 'Máx. tareas autónomas a la vez')}
            value={da} disabled={q.isLoading || mut.isPending}
            onChange={(e) => setDa(e.target.value)} onBlur={commitAuto}
            inputProps={{ min: 1, max: total }}
            helperText={t('operations.concurrency.autoHelp', { reserved: Math.max(0, total - auto), defaultValue: `≤ total. Reservadas para personas: ${Math.max(0, total - auto)}.` })} />
        </Stack>
        <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1 }}>
          {t('operations.concurrency.inflight', {
            total: inflight.in_flight_total, max: total, auto: inflight.in_flight_autonomous, autoMax: auto,
            defaultValue: `En ejecución ahora: ${inflight.in_flight_total}/${total} (autónomas ${inflight.in_flight_autonomous}/${auto}).`,
          })}
        </Typography>
      </CardContent>
    </Card>
  );
}

/**
 * 15.6.0 — how long ONE chat reply and ONE background job may run, in minutes. Same path as
 * the concurrency (stored choice → manifest → deploy config) and just as live: the next
 * message already runs with the new value. Out-of-range input is clamped to the range the
 * backend reports and shown as such, so what you read is what applies.
 */
export function TimeLimitsControl() {
  const { t } = useTranslation();
  const q = useSettings();
  const mut = usePatchSettings();
  const lo: number = q.data?.time_limit_range?.min ?? 5;
  const hi: number = q.data?.time_limit_range?.max ?? 60;
  const chat: number = q.data?.chat_turn_max_minutes ?? 30;
  const auto: number = q.data?.autonomous_run_max_minutes ?? 15;
  const [dc, setDc] = useState<string>('');
  const [da, setDa] = useState<string>('');
  useEffect(() => { setDc(String(chat)); }, [chat]);
  useEffect(() => { setDa(String(auto)); }, [auto]);
  const fuera = (v: string) => {
    const n = parseInt(v, 10);
    return v.trim() !== '' && (Number.isNaN(n) || n < lo || n > hi);
  };
  const commit = (draft: string, current: number, key: string, set: (v: string) => void) => {
    if (draft.trim() === '') { set(String(current)); return; }
    const n = Math.max(lo, Math.min(hi, parseInt(draft, 10) || current));
    set(String(n));
    if (n !== current) mut.mutate({ [key]: n });
  };
  const rango = t('operations.timeLimits.range', { min: lo, max: hi, defaultValue: 'Entre {{min}} y {{max}} minutos.' });
  return (
    <Card variant="outlined" sx={{ mb: 2 }}>
      <CardContent>
        <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>
          {t('operations.timeLimits.title', 'Tiempo máximo de trabajo')}
        </Typography>
        <Typography variant="caption" color="text.secondary">
          {t('operations.timeLimits.hint',
            'Cuánto puede trabajar el empleado en una sola tarea antes de detenerla. Al llegar al máximo, la detiene y lo dice. Súbelo si hace consultas largas; un máximo alto también deja que una tarea atascada gaste más.')}
        </Typography>
        <Stack direction="row" spacing={2} sx={{ mt: 1.5, flexWrap: 'wrap' }} useFlexGap>
          <TextField type="number" size="small" sx={{ maxWidth: 300 }}
            label={t('operations.timeLimits.chat', 'Máx. minutos por respuesta en el chat')}
            value={dc} disabled={q.isLoading || mut.isPending}
            error={fuera(dc)}
            onChange={(e) => setDc(e.target.value)}
            onBlur={() => commit(dc, chat, 'chat_turn_max_minutes', setDc)}
            inputProps={{ min: lo, max: hi }}
            helperText={`${t('operations.timeLimits.chatHelp', 'Lo que espera una persona a la respuesta.')} ${rango}`} />
          <TextField type="number" size="small" sx={{ maxWidth: 300 }}
            label={t('operations.timeLimits.auto', 'Máx. minutos por tarea autónoma')}
            value={da} disabled={q.isLoading || mut.isPending}
            error={fuera(da)}
            onChange={(e) => setDa(e.target.value)}
            onBlur={() => commit(da, auto, 'autonomous_run_max_minutes', setDa)}
            inputProps={{ min: lo, max: hi }}
            helperText={`${t('operations.timeLimits.autoHelp', 'Rutinas, automatizaciones y respuestas por los canales.')} ${rango}`} />
        </Stack>
      </CardContent>
    </Card>
  );
}

/** Whether the employee may schedule its own future tasks (+ the open-task cap). */
export function SelfSchedulingControl() {
  const { t } = useTranslation();
  const q = useSettings();
  const mut = usePatchSettings();
  const selfSchedule = !!q.data?.self_schedule;
  const maxOpen: number = q.data?.max_open_tasks ?? 20;
  const [draftMax, setDraftMax] = useState<string>('');
  useEffect(() => { setDraftMax(String(maxOpen)); }, [maxOpen]);
  const commitMax = () => {
    if (draftMax.trim() === '') { setDraftMax(String(maxOpen)); return; }
    const n = Math.max(0, parseInt(draftMax, 10) || 0);
    if (n !== maxOpen) mut.mutate({ max_open_tasks: n });
  };
  return (
    <Card variant="outlined" sx={{ mb: 2 }}>
      <CardContent>
        <Stack direction="row" alignItems="center" spacing={1.5}>
          <Switch checked={selfSchedule} disabled={q.isLoading || mut.isPending}
            onChange={(e) => mut.mutate({ self_schedule: e.target.checked })} />
          <Box sx={{ flex: 1, minWidth: 0 }}>
            <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>
              {t('operations.scheduling.title', 'Auto-programación de tareas')}
            </Typography>
            <Typography variant="caption" color="text.secondary">
              {t('operations.scheduling.hint',
                'Si está activo, el empleado puede programarse tareas futuras (seguimientos, informes periódicos). Cada tarea futura puede generar coste, por eso hay un límite de tareas propias abiertas.')}
            </Typography>
          </Box>
        </Stack>
        <Box sx={{ mt: 1.5, pl: 5.5 }}>
          <TextField type="number" size="small"
            label={t('operations.scheduling.maxOpen', 'Máx. tareas propias abiertas (0 = sin límite)')}
            value={draftMax} disabled={!selfSchedule || q.isLoading || mut.isPending}
            onChange={(e) => setDraftMax(e.target.value)} onBlur={commitMax}
            inputProps={{ min: 0 }} sx={{ maxWidth: 320 }} />
        </Box>
      </CardContent>
    </Card>
  );
}
