/**
 * Notify-rules manager (v7.62.0) — the operator UI for `SignalNotifyRule`, opened
 * from the alerts / recommendations / decisions screens. A rule = "when a NEW
 * <signal> meets these intensity thresholds, message this directory user via this
 * channel". Reads are open; create/toggle/delete are admin-only (the backend
 * enforces it too). Uses NAME pickers (channel/user), never raw ids.
 */
import { useState } from 'react';
import {
  Alert, Box, Button, Chip, Dialog, DialogActions, DialogContent, DialogTitle,
  Divider, FormControl, IconButton, InputLabel, MenuItem, Select, Stack, Switch,
  TextField, Tooltip, Typography,
} from '@mui/material';
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline';
import AddIcon from '@mui/icons-material/Add';
import { useTranslation } from 'react-i18next';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { apiFetch } from '../services/apiClient';

// Bands per axis (ordered ascending). Decisions only carry confidence.
const BANDS: Record<string, string[]> = {
  impact_min: ['low', 'medium', 'high', 'critical'],
  urgency_min: ['low', 'medium', 'high', 'immediate'],
  confidence_min: ['low', 'medium', 'high'],
};

interface Rule {
  id: number; signal_table: string; name: string | null;
  conditions: { impact_min?: string; urgency_min?: string; confidence_min?: string; match?: string };
  channel_id: number; channel_name: string | null;
  recipient_user: string; recipient_name: string | null;
  enabled: boolean; last_fired_at: string | null;
}
interface Options {
  channels: { id: number; label: string; kind: string; status: string }[];
  users: { email: string; display_name: string }[];
}

export default function NotifyRulesDialog({ open, table, onClose }: {
  open: boolean; table: string; onClose: () => void;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const axes = table === 'decisions' ? ['confidence_min'] : ['impact_min', 'urgency_min', 'confidence_min'];

  const me = useQuery({
    queryKey: ['users-me'],
    queryFn: () => apiFetch('/api/agent/users/me').then((r) => (r.ok ? r.json() : { is_admin: false })),
    staleTime: 60_000,
  });
  const isAdmin = me.data?.is_admin === true;

  const rulesQ = useQuery({
    queryKey: ['notify-rules', table],
    queryFn: () => apiFetch(`/api/agent/notify-rules?table=${table}`).then((r) => r.json()),
    enabled: open,
  });
  const optsQ = useQuery({
    queryKey: ['notify-rules-options'],
    queryFn: () => apiFetch('/api/agent/notify-rules/options').then((r) => r.json() as Promise<Options>),
    enabled: open,
  });
  const rules: Rule[] = rulesQ.data?.items ?? [];
  const opts: Options = optsQ.data ?? { channels: [], users: [] };

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
    onSuccess: () => qc.invalidateQueries({ queryKey: ['notify-rules', table] }),
    onError: (e: any) => setErr(String(e?.message ?? e)),
  });

  // ── New-rule form ──────────────────────────────────────────────────
  const [f, setF] = useState<{ name: string; cond: Record<string, string>; match: string; channel_id: string; recipient_user: string }>(
    { name: '', cond: {}, match: 'all', channel_id: '', recipient_user: '' },
  );
  const resetForm = () => setF({ name: '', cond: {}, match: 'all', channel_id: '', recipient_user: '' });
  const hasThreshold = axes.some((a) => f.cond[a]);
  const canCreateRule = isAdmin && hasThreshold && f.channel_id && f.recipient_user;
  const submit = () => {
    setErr(null);
    const conditions: any = { match: f.match };
    for (const a of axes) if (f.cond[a]) conditions[a] = f.cond[a];
    mut.mutate({
      path: '/api/agent/notify-rules', method: 'POST',
      body: { signal_table: table, name: f.name.trim() || undefined, conditions, channel_id: Number(f.channel_id), recipient_user: f.recipient_user, enabled: true },
    }, { onSuccess: () => { qc.invalidateQueries({ queryKey: ['notify-rules', table] }); resetForm(); } });
  };

  const axisLabel = (a: string) => t(`notifyRules.axis.${a}`, a.replace('_min', ''));
  const condSummary = (c: Rule['conditions']) => {
    const parts = axes.filter((a) => (c as any)[a]).map((a) => `${axisLabel(a)} ≥ ${t(`states.${(c as any)[a]}`, (c as any)[a])}`);
    if (!parts.length) return '—';
    const sep = c.match === 'any' ? ` ${t('notifyRules.or', 'o')} ` : ` ${t('notifyRules.and', 'y')} `;
    return parts.join(sep);
  };

  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="md">
      <DialogTitle>{t('notifyRules.title', 'Reglas de notificación')}</DialogTitle>
      <DialogContent>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          {t('notifyRules.intro', 'Cuando el empleado cree un elemento nuevo que cumpla estos umbrales, avisará a la persona elegida por el canal elegido.')}
        </Typography>
        {err && <Alert severity="error" sx={{ mb: 1 }} onClose={() => setErr(null)}>{err}</Alert>}

        {/* Existing rules */}
        {rules.length > 0 ? (
          <Stack spacing={1} sx={{ mb: 2 }}>
            {rules.map((r) => (
              <Box key={r.id} sx={{ p: 1.25, border: '1px solid', borderColor: 'divider', borderRadius: 1.5, opacity: r.enabled ? 1 : 0.6 }}>
                <Stack direction="row" alignItems="center" spacing={1} flexWrap="wrap" useFlexGap>
                  <Box sx={{ flex: 1, minWidth: 0 }}>
                    {r.name && <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>{r.name}</Typography>}
                    <Typography variant="body2">
                      {condSummary(r.conditions)}
                    </Typography>
                    <Typography variant="caption" color="text.secondary">
                      → {r.recipient_name || r.recipient_user} · {r.channel_name || `#${r.channel_id}`}
                      {r.last_fired_at ? ` · ${t('notifyRules.lastFired', 'último aviso')}` : ''}
                    </Typography>
                  </Box>
                  <Tooltip title={r.enabled ? t('notifyRules.disable', 'Desactivar') : t('notifyRules.enable', 'Activar')}>
                    <span><Switch size="small" checked={r.enabled} disabled={!isAdmin || mut.isPending}
                      onChange={(e) => mut.mutate({ path: `/api/agent/notify-rules/${r.id}`, method: 'PATCH', body: { enabled: e.target.checked } })} /></span>
                  </Tooltip>
                  <Tooltip title={t('common.delete', 'Eliminar') as string}>
                    <span><IconButton size="small" color="error" disabled={!isAdmin || mut.isPending}
                      onClick={() => mut.mutate({ path: `/api/agent/notify-rules/${r.id}`, method: 'DELETE' })}>
                      <DeleteOutlineIcon fontSize="small" /></IconButton></span>
                  </Tooltip>
                </Stack>
              </Box>
            ))}
          </Stack>
        ) : (
          <Alert severity="info" sx={{ mb: 2 }}>{t('notifyRules.empty', 'Aún no hay reglas. Crea una abajo.')}</Alert>
        )}

        {isAdmin && (
          <>
            <Divider sx={{ my: 1.5 }}><Typography variant="overline" color="text.secondary">{t('notifyRules.newRule', 'Nueva regla')}</Typography></Divider>
            <Stack spacing={1.5}>
              <TextField size="small" label={t('notifyRules.name', 'Nombre (opcional)')} value={f.name}
                onChange={(e) => setF({ ...f, name: e.target.value })} fullWidth />
              <Stack direction="row" spacing={1.5} flexWrap="wrap" useFlexGap alignItems="center">
                {axes.map((a) => (
                  <FormControl size="small" key={a} sx={{ minWidth: 160 }}>
                    <InputLabel>{axisLabel(a)}</InputLabel>
                    <Select label={axisLabel(a)} value={f.cond[a] ?? ''}
                      onChange={(e) => setF({ ...f, cond: { ...f.cond, [a]: String(e.target.value) } })}>
                      <MenuItem value="">{t('notifyRules.any', '(cualquiera)')}</MenuItem>
                      {BANDS[a].map((b) => (
                        <MenuItem key={b} value={b}>{t(`states.${b}`, b)} {t('notifyRules.orMore', 'o más')}</MenuItem>
                      ))}
                    </Select>
                  </FormControl>
                ))}
                {axes.length > 1 && (
                  <FormControl size="small" sx={{ minWidth: 150 }}>
                    <InputLabel>{t('notifyRules.match', 'Coincidencia')}</InputLabel>
                    <Select label={t('notifyRules.match', 'Coincidencia')} value={f.match}
                      onChange={(e) => setF({ ...f, match: String(e.target.value) })}>
                      <MenuItem value="all">{t('notifyRules.matchAll', 'Todas (Y)')}</MenuItem>
                      <MenuItem value="any">{t('notifyRules.matchAny', 'Alguna (O)')}</MenuItem>
                    </Select>
                  </FormControl>
                )}
              </Stack>
              <Stack direction="row" spacing={1.5} flexWrap="wrap" useFlexGap>
                <FormControl size="small" sx={{ minWidth: 200 }}>
                  <InputLabel>{t('notifyRules.channel', 'Canal')}</InputLabel>
                  <Select label={t('notifyRules.channel', 'Canal')} value={f.channel_id}
                    onChange={(e) => setF({ ...f, channel_id: String(e.target.value) })}>
                    {opts.channels.map((c) => (
                      <MenuItem key={c.id} value={String(c.id)}>{c.label} ({c.kind})</MenuItem>
                    ))}
                  </Select>
                </FormControl>
                <FormControl size="small" sx={{ minWidth: 220 }}>
                  <InputLabel>{t('notifyRules.recipient', 'Avisar a')}</InputLabel>
                  <Select label={t('notifyRules.recipient', 'Avisar a')} value={f.recipient_user}
                    onChange={(e) => setF({ ...f, recipient_user: String(e.target.value) })}>
                    {opts.users.map((u) => (
                      <MenuItem key={u.email} value={u.email}>{u.display_name}</MenuItem>
                    ))}
                  </Select>
                </FormControl>
                <Button variant="contained" startIcon={<AddIcon />} disabled={!canCreateRule || mut.isPending} onClick={submit}>
                  {t('notifyRules.add', 'Añadir regla')}
                </Button>
              </Stack>
              {opts.channels.length === 0 && (
                <Alert severity="warning">{t('notifyRules.noChannels', 'No hay canales configurados. Crea uno en Operaciones › Canales para poder avisar.')}</Alert>
              )}
            </Stack>
          </>
        )}
        {!isAdmin && rules.length === 0 && (
          <Typography variant="caption" color="text.secondary">
            {t('notifyRules.memberHint', 'Solo un administrador puede crear reglas de notificación.')}
          </Typography>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>{t('common.close', 'Cerrar')}</Button>
      </DialogActions>
    </Dialog>
  );
}
