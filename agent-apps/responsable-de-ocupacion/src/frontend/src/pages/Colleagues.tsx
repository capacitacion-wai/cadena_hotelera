import { useMemo, useState } from 'react';
import {
  Alert, Box, Button, Chip, CircularProgress, Dialog, DialogActions, DialogContent,
  DialogContentText, DialogTitle, Divider, Paper, Stack, Tab, Tabs, TextField, Typography,
} from '@mui/material';
import ArrowForwardIcon from '@mui/icons-material/ArrowForward';
import CheckCircleIcon from '@mui/icons-material/CheckCircle';
import BlockIcon from '@mui/icons-material/Block';
import PauseCircleOutlineIcon from '@mui/icons-material/PauseCircleOutline';
import PlayCircleOutlineIcon from '@mui/icons-material/PlayCircleOutline';
import LinkOffIcon from '@mui/icons-material/LinkOff';
import GroupsIcon from '@mui/icons-material/Groups';
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline';
import ExpandMoreIcon from '@mui/icons-material/ExpandMore';
import PersonIcon from '@mui/icons-material/Person';
import { useTranslation } from 'react-i18next';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { apiFetch } from '../services/apiClient';

/**
 * The `Colegas` (Colleagues) and `Quién me habla` (Who talks to me) tabs — governance of THIS employee's collaborations.
 *
 * It lives here, next to where Kommo is connected and the write policy is set, because a
 * colleague is just one more capability of the employee. The platform network shows the full
 * map and does the same; they are two doors to the same model, not two models.
 *
 * The tab that really matters is the second one: **who talks to me**. That is where the
 * person responsible for this employee accepts —or not— that another one consults it. Nobody hooks
 * into an employee without its owner knowing.
 */

interface Relacion {
  id: number;
  caller_app_id: number;
  caller_name: string;
  callee_app_id: number;
  callee_name: string;
  modes: string[];
  status: string;
  status_label: string;
  purpose: string;
  requested_by: string;
  decided_by: string | null;
  status_reason: string | null;
  activity: {
    calls_today: number;
    cost_today_usd: number;
    failures_today: number;
    // ANSWERED and OPEN go next to the total because they are different things: "7 calls"
    // with a single run on the callee is not a contradiction — maybe five never
    // arrived (denied, loop cut) and another one was left unclosed.
    answered_today?: number;
    open_calls?: number;
    last_status: string | null;
  } | null;
}

/** A concrete request that travelled through the collaboration, with its answer. */
interface Peticion {
  id: number;
  on_behalf_of: string;
  mode: string;
  status: string;
  reason: string | null;
  question: string | null;
  answer: string | null;
  cost_usd: number | null;
  duration_ms: number | null;
  created_at: string | null;
  finished_at: string | null;
}

/** The requests of ONE collaboration. Fetched only when it is opened: almost always one
 *  is looked at, and fetching them all at once would be one query per relationship for nothing. */
function Peticiones({ id }: { id: number }) {
  const { t } = useTranslation();
  const { data, isLoading, isError } = useQuery({
    queryKey: ['colleague-calls', id],
    queryFn: async () => {
      const r = await apiFetch(`/mcp/colleague/relations/${id}/calls?limit=50`);
      if (!r.ok) throw new Error(String(r.status));
      return ((await r.json())?.calls || []) as Peticion[];
    },
  });
  if (isLoading) return <CircularProgress size={16} sx={{ my: 1 }} />;
  if (isError) {
    return <Alert severity="warning" sx={{ my: 1 }}>
      {t('colleagues.callsError', 'No se han podido leer las peticiones ahora mismo.')}
    </Alert>;
  }
  const calls = data || [];
  if (!calls.length) {
    return <Typography variant="body2" color="text.secondary" sx={{ py: 1 }}>
      {t('colleagues.callsEmpty', 'Todavía no ha viajado ninguna petición por aquí.')}
    </Typography>;
  }
  const etiqueta = (st: string) => ({
    ok: t('colleagues.callOk', 'Atendida'),
    running: t('colleagues.callRunning', 'Sin cerrar'),
    denied: t('colleagues.callDenied', 'Denegada'),
    error: t('colleagues.callError', 'Con error'),
    capped: t('colleagues.callCapped', 'Cortada por un tope'),
    loop_cut: t('colleagues.callLoop', 'Cortada por bucle'),
  } as Record<string, string>)[st] || st;
  return (
    <Box sx={{ mt: 1, pl: 1.5, borderLeft: '2px solid', borderColor: 'divider' }}>
      {calls.map((c) => (
        <Box key={c.id} sx={{ py: 0.9, borderBottom: '1px solid', borderColor: 'divider' }}>
          <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
            <Chip size="small" color={c.status === 'ok' ? 'success' : c.status === 'running' ? 'info' : 'error'}
              label={etiqueta(c.status)} />
            <Typography variant="caption" color="text.secondary">
              {c.created_at ? new Date(c.created_at).toLocaleString() : ''}
            </Typography>
            {/* On behalf of WHOM: the collaboration authorizes the employee, but what I
                show or touch depends on the PERSON behind the call. */}
            <Chip size="small" variant="outlined" icon={<PersonIcon />} label={c.on_behalf_of} />
            <Chip size="small" variant="outlined" label={c.mode} />
            {c.duration_ms != null && (
              <Typography variant="caption" color="text.secondary">
                {(c.duration_ms / 1000).toFixed(1)}s
              </Typography>
            )}
            {c.cost_usd ? (
              <Typography variant="caption" color="text.secondary">${c.cost_usd.toFixed(4)}</Typography>
            ) : null}
          </Stack>
          {c.question && (
            <Typography variant="body2" sx={{ mt: 0.3 }}>
              <strong>{t('colleagues.asked', 'Pidió')}:</strong> {c.question.slice(0, 400)}
              {c.question.length > 400 ? '…' : ''}
            </Typography>
          )}
          {c.answer && (
            <Typography variant="body2" color="text.secondary">
              <strong>{t('colleagues.answered', 'Respuesta')}:</strong> {c.answer.slice(0, 400)}
              {c.answer.length > 400 ? '…' : ''}
            </Typography>
          )}
          {c.reason && c.status !== 'ok' && (
            <Typography variant="caption" color="error.main" display="block">{c.reason}</Typography>
          )}
        </Box>
      ))}
    </Box>
  );
}

const color = (s: string) =>
  s === 'active' ? 'success' : s === 'pending' ? 'info' : s === 'paused' ? 'warning' : 'error';

export default function Colleagues() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [tab, setTab] = useState(0);
  const [denegando, setDenegando] = useState<Relacion | null>(null);
  const [motivo, setMotivo] = useState('');
  const [abierta, setAbierta] = useState<number | null>(null);

  const direccion = tab === 0 ? 'outgoing' : 'incoming';
  const { data, isLoading, isError } = useQuery({
    queryKey: ['colleagues', direccion],
    queryFn: async () => {
      const r = await apiFetch(`/mcp/colleague/relations?direction=${direccion}`);
      if (!r.ok) throw new Error(String(r.status));
      return (await r.json()) as Relacion[];
    },
    refetchInterval: 20_000,
  });

  const refrescar = () => qc.invalidateQueries({ queryKey: ['colleagues'] });

  const decidir = useMutation({
    mutationFn: async (p: { id: number; accion: string; reason?: string }) => {
      const r = await apiFetch(`/mcp/colleague/relations/${p.id}/${p.accion}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reason: p.reason || '' }),
      });
      if (!r.ok) throw new Error(String(r.status));
      return r.json();
    },
    onSuccess: () => { setDenegando(null); setMotivo(''); refrescar(); },
  });

  const borrar = useMutation({
    mutationFn: async (id: number) => {
      const r = await apiFetch(`/mcp/colleague/relations/${id}`, { method: 'DELETE' });
      if (!r.ok) throw new Error(String(r.status));
      return r.json();
    },
    onSuccess: refrescar,
  });

  const relaciones = useMemo(() => (data || []), [data]);

  return (
    <Box sx={{ p: 3 }}>
      <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
        <GroupsIcon color="primary" />
        <Typography variant="h5" fontWeight={600}>
          {t('colleagues.title', 'Compañeros')}
        </Typography>
      </Stack>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        {t('colleagues.subtitle', 'Con qué otros empleados trabajo y quién trabaja conmigo. Cuando un compañero me consulta, aplico MIS permisos a la persona que pregunta — no a él.')}
      </Typography>

      <Tabs value={tab} onChange={(_, v) => setTab(v)} sx={{ mb: 2 }}>
        <Tab label={t('colleagues.outgoing', 'Con quién hablo')} />
        <Tab label={t('colleagues.incoming', 'Quién me habla')} />
      </Tabs>

      {isLoading && <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}><CircularProgress /></Box>}
      {isError && (
        <Alert severity="error">
          {t('colleagues.loadError', 'No se han podido leer las colaboraciones. Puede que la plataforma no esté disponible ahora mismo.')}
        </Alert>
      )}

      {!isLoading && !isError && relaciones.length === 0 && (
        <Paper variant="outlined" sx={{ p: 4, textAlign: 'center' }}>
          <Typography color="text.secondary">
            {tab === 0
              ? t('colleagues.emptyOut', 'Todavía no consulto a ningún compañero. Las colaboraciones se piden desde la red de empleados de la plataforma.')
              : t('colleagues.emptyIn', 'Ningún compañero me consulta todavía.')}
          </Typography>
        </Paper>
      )}

      <Stack spacing={1.5}>
        {relaciones.map((r) => {
          const esEntrante = tab === 1;
          return (
            <Paper key={r.id} variant="outlined" sx={{ p: 2 }}>
              <Stack direction="row" spacing={1.5} alignItems="center" flexWrap="wrap" useFlexGap>
                <Typography fontWeight={600}>{r.caller_name}</Typography>
                <ArrowForwardIcon fontSize="small" color="action" />
                <Typography fontWeight={600}>{r.callee_name}</Typography>
                {r.modes.map((m) => <Chip key={m} size="small" variant="outlined" label={m} />)}
                <Chip size="small" color={color(r.status) as any} label={r.status_label || r.status} />
                <Box sx={{ flex: 1 }} />
                {/* Accept/deny only makes sense for INCOMING ones: it is this employee's
                    decision about who may talk to it. */}
                {esEntrante && r.status === 'pending' && (
                  <>
                    <Button size="small" startIcon={<CheckCircleIcon />}
                      onClick={() => decidir.mutate({ id: r.id, accion: 'accept' })}>
                      {t('colleagues.accept', 'Aceptar')}
                    </Button>
                    <Button size="small" color="error" startIcon={<BlockIcon />}
                      onClick={() => { setDenegando(r); setMotivo(''); }}>
                      {t('colleagues.deny', 'Denegar')}
                    </Button>
                  </>
                )}
                {r.status === 'active' && (
                  <Button size="small" startIcon={<PauseCircleOutlineIcon />}
                    onClick={() => decidir.mutate({ id: r.id, accion: 'pause' })}>
                    {t('colleagues.pause', 'Pausar')}
                  </Button>
                )}
                {r.status === 'paused' && (
                  <Button size="small" startIcon={<PlayCircleOutlineIcon />}
                    onClick={() => decidir.mutate({ id: r.id, accion: 'resume' })}>
                    {t('colleagues.resume', 'Reanudar')}
                  </Button>
                )}
                {['active', 'paused', 'pending'].includes(r.status) && (
                  <Button size="small" color="error" startIcon={<LinkOffIcon />}
                    onClick={() => decidir.mutate({ id: r.id, accion: 'revoke' })}>
                    {t('colleagues.revoke', 'Revocar')}
                  </Button>
                )}
                {/* FINISHED ones can be thrown away. A revoked one does not come back, and piled up
                    they hide the three that matter. Live ones cannot: that is what revoke is for,
                    which records the why. */}
                {['revoked', 'denied'].includes(r.status) && (
                  <Button size="small" color="error" startIcon={<DeleteOutlineIcon />}
                    disabled={borrar.isPending}
                    onClick={() => borrar.mutate(r.id)}>
                    {t('colleagues.delete', 'Borrar')}
                  </Button>
                )}
              </Stack>

              <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
                {r.purpose}
              </Typography>
              <Typography variant="caption" color="text.secondary">
                {t('colleagues.requestedBy', 'Pedida por {{quien}}', { quien: r.requested_by })}
                {r.decided_by ? ` · ${t('colleagues.decidedBy', 'decidida por {{quien}}', { quien: r.decided_by })}` : ''}
              </Typography>

              {r.activity && (
                <>
                  <Divider sx={{ my: 1 }} />
                  <Stack direction="row" spacing={2} flexWrap="wrap" useFlexGap>
                    <Typography variant="caption" color="text.secondary">
                      {t('colleagues.callsToday', '{{n}} peticiones hoy', { n: r.activity.calls_today })}
                    </Typography>
                    {/* ANSWERED apart from the total: adding them into one figure made "7
                        calls" and a single run on the callee look like a product bug
                        when it was the correct reading of what happened. */}
                    <Typography variant="caption" color="text.secondary">
                      {t('colleagues.answeredToday', '{{n}} atendidas', { n: r.activity.answered_today ?? 0 })}
                    </Typography>
                    {(r.activity.open_calls ?? 0) > 0 && (
                      <Typography variant="caption" color="warning.main">
                        {t('colleagues.openCalls', '{{n}} sin cerrar', { n: r.activity.open_calls })}
                      </Typography>
                    )}
                    <Typography variant="caption" color="text.secondary">
                      {t('colleagues.costToday', '{{c}} $ hoy', { c: (r.activity.cost_today_usd || 0).toFixed(4) })}
                    </Typography>
                    {r.activity.failures_today > 0 && (
                      <Typography variant="caption" color="warning.main" fontWeight={600}>
                        {t('colleagues.failures', '{{n}} sin éxito', { n: r.activity.failures_today })}
                      </Typography>
                    )}
                  </Stack>
                </>
              )}

              <Button size="small" sx={{ mt: 0.5 }}
                startIcon={<ExpandMoreIcon sx={{
                  transform: abierta === r.id ? 'rotate(180deg)' : 'none',
                  transition: 'transform .2s' }} />}
                onClick={() => setAbierta(abierta === r.id ? null : r.id)}>
                {abierta === r.id
                  ? t('colleagues.hideCalls', 'Ocultar peticiones')
                  : t('colleagues.seeCalls', 'Ver peticiones')}
              </Button>
              {abierta === r.id && <Peticiones id={r.id} />}
            </Paper>
          );
        })}
      </Stack>

      <Dialog open={!!denegando} onClose={() => setDenegando(null)} maxWidth="sm" fullWidth>
        <DialogTitle>{t('colleagues.denyTitle', 'Denegar la colaboración')}</DialogTitle>
        <DialogContent>
          <DialogContentText sx={{ mb: 2 }}>
            {t('colleagues.denyHelp', 'Di por qué: lo va a leer el responsable del otro empleado, y sin motivo tendrá que preguntártelo.')}
          </DialogContentText>
          <TextField autoFocus fullWidth multiline minRows={2} size="small"
            value={motivo} onChange={(e) => setMotivo(e.target.value)} />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDenegando(null)}>{t('common.cancel', 'Cancelar')}</Button>
          <Button color="error" variant="contained" disabled={!motivo.trim()}
            onClick={() => decidir.mutate({ id: Number(denegando?.id), accion: 'deny', reason: motivo.trim() })}>
            {t('colleagues.deny', 'Denegar')}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
