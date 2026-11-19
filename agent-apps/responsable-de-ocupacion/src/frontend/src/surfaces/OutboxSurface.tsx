/**
 * `outbox` archetype — "Comunicaciones": the UNIFIED message log of the employee.
 *
 * v7.76.0 — was an OUTBOX that only showed the agent's `communications` staging
 * (drafts/ready/sent/failed), so it MISSED received messages and platform
 * notifications (e.g. an alert→WhatsApp). It now reads a unified FEED
 * (`/api/agent/comms/feed`) with one source PER LANE, so every message in/out
 * shows exactly once:
 *   Recibidos ← channel_messages(inbound) · Enviados ← channel_messages(notification)
 *   Pendientes / Fallidas ← communications (actionable) · Notas internas ← communications(internal)
 * Dates now show the TIME, and an in-app note reads as "Nota interna" (no
 * confusing "Para/Enviada"). Review-and-send actions still run on the underlying
 * `communications` rows.
 */
import { useEffect, useMemo, useState } from 'react';
import { useParams } from 'react-router-dom';
import {
  Alert, Avatar, Box, Button, Card, Chip, CircularProgress, Dialog, DialogActions,
  DialogContent, DialogTitle, Divider, List, ListItemButton, Snackbar, Stack,
  Tab, Tabs, Typography,
} from '@mui/material';
import EmailIcon from '@mui/icons-material/Email';
import WhatsAppIcon from '@mui/icons-material/WhatsApp';
import SmsIcon from '@mui/icons-material/Sms';
import ChatIcon from '@mui/icons-material/Chat';
import NotificationsIcon from '@mui/icons-material/Notifications';
import AttachFileIcon from '@mui/icons-material/AttachFile';
import SouthWestIcon from '@mui/icons-material/SouthWest';
import NorthEastIcon from '@mui/icons-material/NorthEast';
import { useTranslation } from 'react-i18next';
import { useQuery } from '@tanstack/react-query';
import { useManifest } from '../hooks/useManifest';
import { usePickLabel } from '../utils/dataLabels';
import { entityAction } from './common/api';
import { SurfaceShell, PersonChip, DateText, Kpi, Tone } from './common/primitives';
import {
  useAutomationProvenance,
  ProvenanceScopeAlert,
  AutomationOriginChip,
  AutomationProvenanceFilter,
} from './common/provenanceScope';
import { formatDateTime } from '../utils/datetime';
import { apiBase, apiFetch } from '../services/apiClient';
import { ACCENT_GOLD, PRIMARY, SECONDARY } from '../theme/theme';

const LANES = ['received', 'sent', 'pending', 'failed', 'internal'] as const;
type Lane = typeof LANES[number];
const PAGE_SIZE = 25;

const LANE_TONE: Record<Lane, Tone> = {
  received: 'success', sent: 'info', pending: 'warning', failed: 'error', internal: 'default',
};

// Channel-aware icon + human label + tint.
const CHANNEL_ICON: Record<string, JSX.Element> = {
  email: <EmailIcon />, whatsapp: <WhatsAppIcon />, sms: <SmsIcon />,
  slack: <ChatIcon />, internal: <NotificationsIcon />,
};
const CHANNEL_LABEL: Record<string, string> = {
  email: 'Email', whatsapp: 'WhatsApp', sms: 'SMS', slack: 'Slack', internal: 'App',
};
// color-fijo: WhatsApp and Slack carry THEIR brand colour (the viewer recognises it); the
// rest comes from the app theme.
const CHANNEL_COLOR: Record<string, string> = {
  email: PRIMARY, whatsapp: '#25d366', sms: SECONDARY, slack: '#611f69', internal: ACCENT_GOLD, // color-fijo: marcas ajenas
};
const chLower = (ch?: string) => String(ch ?? 'email').toLowerCase();
const channelIcon = (ch?: string) => CHANNEL_ICON[chLower(ch)] ?? <EmailIcon />;
const channelLabel = (ch?: string) => {
  const c = chLower(ch);
  return CHANNEL_LABEL[c] ?? (c ? c.charAt(0).toUpperCase() + c.slice(1) : 'Email');
};
const channelColor = (ch?: string) => CHANNEL_COLOR[chLower(ch)] ?? PRIMARY;

function ChannelAvatar({ channel, size = 36 }: { channel?: string; size?: number }) {
  const col = channelColor(channel);
  return (
    <Avatar variant="rounded" sx={{
      width: size, height: size, bgcolor: `${col}1A`, color: col,
      '& .MuiSvgIcon-root': { fontSize: size * 0.55 },
    }}>
      {channelIcon(channel)}
    </Avatar>
  );
}

interface FeedRow {
  id: number; lane: Lane; direction: 'in' | 'out' | 'internal'; channel: string;
  correspondent: string | null; subject: string | null; body: string;
  status: string | null; created_at: string | null; attachments: { filename?: string }[];
  delivery_id: string | null; error: string | null; actionable: boolean;
  created_by_automation?: { title?: string; via?: string } | null;
}

// The forward action(s) offered per actionable row, by its underlying status.
function rowActions(row: FeedRow, t: any): { action: string; label: string; primary: boolean }[] {
  if (row.lane === 'failed') return [{ action: 'retry', label: t('surfaces.outbox.doRetry', 'Reintentar envío'), primary: true }];
  if (row.lane === 'pending' && row.status === 'draft') return [{ action: 'release', label: t('surfaces.outbox.send', 'Aprobar y enviar'), primary: true }];
  if (row.lane === 'pending' && row.status === 'ready') return [
    { action: 'send', label: t('surfaces.outbox.sendNow', 'Enviar ahora'), primary: true },
    { action: 'cancel', label: t('surfaces.outbox.cancelSend', 'Devolver a borrador'), primary: false },
  ];
  return [];
}

export default function OutboxSurface() {
  const { slug } = useParams<{ slug: string }>();
  const { manifest } = useManifest();
  const pickLabel = usePickLabel();
  const { t } = useTranslation();
  const entity = useMemo(
    () => ((manifest as any)?.domain_entities ?? []).find((e: any) => e.slug === slug),
    [manifest, slug],
  );

  const [lane, setLane] = useState<Lane>('received');
  const [page, setPage] = useState(1);
  const [preview, setPreview] = useState<FeedRow | null>(null);
  const [snack, setSnack] = useState<{ msg: string; sev: 'success' | 'error' } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [landed, setLanded] = useState(false);
  const prov = useAutomationProvenance(slug, entity?.source, {
    producersUrl: `${apiBase}/agent/comms/producers`,
  });
  const provQs = useMemo(() => {
    const p = new URLSearchParams();
    if (prov.queryExtras.created_by_automation) {
      p.set('created_by_automation', prov.queryExtras.created_by_automation);
    }
    if (prov.queryExtras.only_open) p.set('only_open', '1');
    const qs = p.toString();
    return qs ? `&${qs}` : '';
  }, [prov.queryExtras.created_by_automation, prov.queryExtras.only_open]);

  const countsQ = useQuery({
    queryKey: ['comms-counts', provQs],
    queryFn: () => {
      const qs = provQs.replace(/^&/, '');
      return apiFetch(qs ? `/api/agent/comms/counts?${qs}` : '/api/agent/comms/counts').then((r) => r.json());
    },
  });
  const counts: Record<string, number> = countsQ.data?.counts ?? {};

  // Re-land when provenance scope changes (empty received/sent under filter).
  useEffect(() => { setLanded(false); setPage(1); setPreview(null); }, [prov.provAutomation]);

  // Land on the first lane that has something (received → sent → pending → failed).
  // With automation scope, prefer pending/failed/internal (the Communication lanes).
  useEffect(() => {
    if (landed || !countsQ.data) return;
    const order: Lane[] = prov.provAutomation
      ? ['pending', 'failed', 'internal', 'received', 'sent']
      : ['received', 'sent', 'pending', 'failed', 'internal'];
    const first = order.find((l) => (counts[l] ?? 0) > 0) ?? (prov.provAutomation ? 'pending' : 'received');
    setLane(first); setLanded(true);
  }, [countsQ.data, landed, counts, prov.provAutomation]);

  const feedQ = useQuery({
    queryKey: ['comms-feed', lane, page, provQs],
    queryFn: () => apiFetch(
      `/api/agent/comms/feed?lane=${lane}&page=${page}&page_size=${PAGE_SIZE}${provQs}`,
    ).then((r) => r.json()),
  });
  const rows: FeedRow[] = feedQ.data?.items ?? [];
  const total: number = feedQ.data?.total ?? 0;
  const provContext = feedQ.data?.filtered_by_automation ?? null;

  const refresh = () => { feedQ.refetch(); countsQ.refetch(); };
  const count = (l: string) => counts?.[l] ?? 0;

  const laneLabel = (l: Lane) => ({
    received: t('surfaces.outbox.laneReceived', 'Recibidos'),
    sent: t('surfaces.outbox.laneSent', 'Enviados'),
    pending: t('surfaces.outbox.lanePending', 'Pendientes'),
    failed: t('surfaces.outbox.laneFailed', 'Fallidas'),
    internal: t('surfaces.outbox.laneInternal', 'Notas internas'),
  }[l]);

  // v7.83.0 — a manual tab click counts as "landed", so the auto-land effect
  // can't yank the user away once counts resolve.
  const changeLane = (l: Lane) => { setLanded(true); setLane(l); setPage(1); setPreview(null); };

  // v7.83.0 — clamp the page if the lane's total shrank (after an action), so
  // the operator is never stranded on an empty page past the (hidden) pager.
  useEffect(() => {
    const pc = Math.max(1, Math.ceil(total / PAGE_SIZE));
    if (page > pc) setPage(pc);
  }, [total, page]);

  const doAction = async (row: FeedRow, action: string) => {
    if (!slug) return;
    setBusy(String(row.id));
    try {
      const res = await entityAction(slug, row.id, action, {});
      const st = String(res?.status ?? '');
      setSnack({
        msg: st === 'failed' ? (res?.delivery?.error || t('surfaces.outbox.sendFailed', 'No se pudo entregar'))
                             : t('surfaces.common.done', 'Hecho'),
        sev: st === 'failed' ? 'error' : 'success',
      });
      setPreview(null);
      refresh();
    } catch (e: any) {
      setSnack({ msg: e?.message ?? 'error', sev: 'error' });
    } finally { setBusy(null); }
  };

  const titleFor = (r: FeedRow): string => {
    const subj = String(r.subject ?? '').trim();
    if (subj) return subj.slice(0, 90);
    const body = String(r.body ?? '').trim();
    if (body) return body.slice(0, 90);
    return t('surfaces.outbox.noSubject', '(sin asunto)');
  };
  // "De" for received, "Para" for sent/pending; internal notes have no counterpart.
  const partyLabel = (r: FeedRow) => r.direction === 'in'
    ? t('surfaces.outbox.from', 'De') : t('surfaces.outbox.to', 'Para');

  if (!manifest) return <Box>{t('common.loading', 'Cargando…')}</Box>;

  const kpis: Kpi[] = [
    { label: laneLabel('received'), value: count('received'), tone: count('received') ? 'success' : 'default' },
    { label: laneLabel('sent'), value: count('sent'), tone: count('sent') ? 'info' : 'default' },
    { label: laneLabel('pending'), value: count('pending'), tone: count('pending') ? 'warning' : 'default' },
    { label: laneLabel('failed'), value: count('failed'), tone: count('failed') ? 'error' : 'default' },
  ];

  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const from = total === 0 ? 0 : (page - 1) * PAGE_SIZE + 1;
  const to = Math.min(page * PAGE_SIZE, total);
  const acts = preview ? rowActions(preview, t) : [];

  return (
    <SurfaceShell
      title={entity ? pickLabel(entity, entity.slug) : t('surfaces.outbox.title', 'Comunicaciones')}
      subtitle={t('surfaces.outbox.subtitleFeed', 'Todo lo que el empleado ha enviado y recibido por sus canales — y las notas internas que ha dejado.')}
      kpis={kpis}
      guide="outbox"
      actions={
        <AutomationProvenanceFilter
          value={prov.provAutomation}
          producers={prov.producers}
          onChange={prov.setProvenance}
        />
      }
    >
      <ProvenanceScopeAlert ctx={provContext} onClear={prov.clearProvenance} />
      <Tabs value={lane} onChange={(_, v) => changeLane(v)} variant="scrollable" scrollButtons="auto"
        sx={{ mb: 1.5, borderBottom: 1, borderColor: 'divider' }}>
        {LANES.map((l) => (
          <Tab key={l} value={l} label={
            <Stack direction="row" spacing={1} alignItems="center">
              <span>{laneLabel(l)}</span>
              <Chip size="small" color={count(l) ? LANE_TONE[l] : 'default'}
                variant={count(l) ? 'filled' : 'outlined'} label={count(l)} sx={{ height: 20 }} />
            </Stack>
          } />
        ))}
      </Tabs>

      {feedQ.isLoading && <Box sx={{ py: 4, textAlign: 'center' }}><CircularProgress size={26} /></Box>}
      {feedQ.isError && <Alert severity="error" sx={{ mb: 2 }}>{t('surfaces.outbox.loadError', 'No se pudieron cargar las comunicaciones.')}</Alert>}
      {!feedQ.isLoading && rows.length === 0 && (
        <Alert severity="info" variant="outlined">{t('surfaces.outbox.laneEmpty', 'Nada aquí.')}</Alert>
      )}

      {rows.length > 0 && (
        <Card variant="outlined" sx={{ borderRadius: 2 }}>
          <List disablePadding>
            {rows.map((row, i) => {
              const snippet = String(row.body ?? '').replace(/\s+/g, ' ').trim().slice(0, 130);
              return (
                <ListItemButton key={`${row.lane}-${row.id}`} onClick={() => setPreview(row)}
                  sx={{ borderTop: i ? 1 : 0, borderColor: 'divider', py: 1.25, gap: 1.5,
                    alignItems: 'flex-start', '&:hover': { bgcolor: 'action.hover' } }}>
                  <Box sx={{ pt: 0.25 }}><ChannelAvatar channel={row.channel} /></Box>
                  <Box sx={{ minWidth: 0, flex: 1 }}>
                    <Stack direction="row" justifyContent="space-between" alignItems="baseline" spacing={1}>
                      <Typography variant="subtitle2" noWrap sx={{ fontWeight: 700, minWidth: 0 }}>
                        {titleFor(row)}
                      </Typography>
                      <Box sx={{ whiteSpace: 'nowrap' }}>
                        <DateText value={row.created_at} />
                      </Box>
                    </Stack>
                    <Box sx={{ mt: 0.25 }}>
                      <AutomationOriginChip origin={row.created_by_automation} />
                    </Box>
                    {snippet && (
                      <Typography variant="body2" color="text.secondary" noWrap sx={{ mt: 0.25 }}>
                        {snippet}
                      </Typography>
                    )}
                    <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap sx={{ mt: 0.75 }}>
                      <Chip size="small" color={LANE_TONE[row.lane]} variant="outlined"
                        icon={row.direction === 'in' ? <SouthWestIcon /> : row.direction === 'out' ? <NorthEastIcon /> : undefined}
                        label={row.lane === 'internal' ? t('surfaces.outbox.internalNote', 'Nota interna')
                          : row.direction === 'in' ? t('surfaces.outbox.received', 'Recibido')
                          : t('surfaces.outbox.sent', 'Enviado')} />
                      {row.lane !== 'internal' && (
                        <>
                          <Typography variant="caption" color="text.secondary">{partyLabel(row)}:</Typography>
                          <PersonChip value={row.correspondent ?? ''} />
                        </>
                      )}
                      <Chip size="small" variant="outlined" label={channelLabel(row.channel)} />
                      {row.attachments.length > 0 && (
                        <Chip size="small" variant="outlined" icon={<AttachFileIcon />} label={String(row.attachments.length)} />
                      )}
                      {row.lane === 'failed' && <Chip size="small" color="error" variant="outlined" label={t('states.failed', 'fallida')} />}
                    </Stack>
                  </Box>
                </ListItemButton>
              );
            })}
          </List>
        </Card>
      )}

      {total > PAGE_SIZE && (
        <Stack direction="row" spacing={2} alignItems="center" justifyContent="flex-end" sx={{ mt: 1.5 }}>
          <Typography variant="caption" color="text.secondary">
            {t('surfaces.common.showing', { from, to, total, defaultValue: '{{from}}–{{to}} / {{total}}' })}
          </Typography>
          <Button size="small" disabled={page <= 1} onClick={() => setPage((p) => Math.max(1, p - 1))}>
            {t('surfaces.common.prev', 'Anterior')}
          </Button>
          <Button size="small" disabled={page >= pages} onClick={() => setPage((p) => Math.min(pages, p + 1))}>
            {t('surfaces.common.next', 'Siguiente')}
          </Button>
        </Stack>
      )}

      {/* Detail — read-only for received/sent/internal; review-and-send for staged. */}
      <Dialog open={!!preview} onClose={() => setPreview(null)} fullWidth maxWidth="sm">
        {preview && (
          <>
            <DialogTitle sx={{ pb: 0.5 }}>
              <Stack direction="row" spacing={1.5} alignItems="center">
                <ChannelAvatar channel={preview.channel} size={40} />
                <Box sx={{ minWidth: 0 }}>
                  <Typography variant="subtitle1" sx={{ fontWeight: 700, lineHeight: 1.2 }} noWrap>
                    {titleFor(preview)}
                  </Typography>
                  <Typography variant="caption" color="text.secondary">
                    {channelLabel(preview.channel)}
                  </Typography>
                </Box>
              </Stack>
            </DialogTitle>
            <DialogContent>
              <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap sx={{ mb: 1 }}>
                <Chip size="small" color={LANE_TONE[preview.lane]} variant="outlined"
                  label={preview.lane === 'internal' ? t('surfaces.outbox.internalNote', 'Nota interna')
                    : preview.direction === 'in' ? t('surfaces.outbox.received', 'Recibido')
                    : t('surfaces.outbox.sent', 'Enviado')} />
                {preview.lane !== 'internal' && (
                  <>
                    <Typography variant="caption" color="text.secondary">{partyLabel(preview)}:</Typography>
                    <PersonChip value={preview.correspondent ?? ''} size="medium" />
                  </>
                )}
                {preview.created_at && (
                  <Typography variant="caption" color="text.secondary">{formatDateTime(preview.created_at)}</Typography>
                )}
              </Stack>
              {preview.lane === 'internal' && (
                <Alert severity="info" variant="outlined" sx={{ mb: 1, py: 0.25 }}>
                  {t('surfaces.outbox.internalHint', 'Nota interna del empleado: queda registrada en la app, no se envió a ningún canal.')}
                </Alert>
              )}
              <Divider sx={{ my: 1 }} />
              <Typography variant="body2" sx={{ whiteSpace: 'pre-wrap' }}>
                {String(preview.body ?? '')}
              </Typography>
              {preview.attachments.length > 0 && (
                <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap sx={{ mt: 1.5 }}>
                  {preview.attachments.map((a, i) => (
                    <Chip key={i} size="small" icon={<AttachFileIcon />} variant="outlined"
                      label={a.filename ?? `adjunto ${i + 1}`} />
                  ))}
                </Stack>
              )}
              {preview.delivery_id && (
                <Typography variant="caption" color="text.secondary" display="block" sx={{ mt: 1 }}>
                  {t('surfaces.outbox.deliveryId', 'ID de entrega')}: {String(preview.delivery_id)}
                </Typography>
              )}
              {preview.error && <Alert severity="error" sx={{ mt: 1 }}>{String(preview.error)}</Alert>}
              {acts.length > 0 && (
                <Alert severity="warning" sx={{ mt: 2 }}>
                  {preview.lane === 'failed'
                    ? t('surfaces.outbox.retryWarn', 'Se reintentará el envío ahora, hacia el destinatario. No se puede deshacer.')
                    : t('surfaces.outbox.sendWarn', 'Al confirmar, el mensaje se envía AHORA por su canal al destinatario. No se puede deshacer.')}
                </Alert>
              )}
            </DialogContent>
            <DialogActions>
              <Button onClick={() => setPreview(null)}>{t('common.close', 'Cerrar')}</Button>
              {acts.map((a) => (
                <Button key={a.action} variant={a.primary ? 'contained' : 'text'}
                  color={a.primary ? 'warning' : 'inherit'} disabled={busy === String(preview.id)}
                  onClick={() => doAction(preview, a.action)}>
                  {a.label}
                </Button>
              ))}
            </DialogActions>
          </>
        )}
      </Dialog>

      <Snackbar open={!!snack} autoHideDuration={4000} onClose={() => setSnack(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}>
        {snack ? <Alert severity={snack.sev} onClose={() => setSnack(null)}>{snack.msg}</Alert> : undefined}
      </Snackbar>
    </SurfaceShell>
  );
}
