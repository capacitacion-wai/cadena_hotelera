/**
 * Channel detail page — settings, activity, reauth.
 *
 * Tabs (mirrors the wai-agent-app-builder UX for runs / events):
 *   * Resumen     — header counters, last activity, status_reason
 *   * Mensajes    — paginated channel_messages with links to /runs/:id
 *   * Configuración — editable filter + reply config (PATCH)
 *   * Autenticación — reauth button (OAuth) or password reset (IMAP)
 */
import { useEffect, useRef, useState } from 'react';
import {
  Alert, Box, Button, Card, CardContent, Chip, CircularProgress,
  Divider, FormControlLabel, IconButton, MenuItem, Radio, RadioGroup,
  Select, Stack, Tab, Tabs, TextField, Tooltip, Typography,
} from '@mui/material';
import RefreshIcon from '@mui/icons-material/Refresh';
import VpnKeyIcon from '@mui/icons-material/VpnKey';
import PlayCircleOutlineIcon from '@mui/icons-material/PlayCircleOutline';
import { useNavigate, useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

import PageHeader from '../components/PageHeader';
import StatusChip from '../components/StatusChip';
import UserPicker from '../components/UserPicker';
import EmptyState from '../components/EmptyState';
import useConfirm, { usePrompt } from '../components/ConfirmDialog';
import {
  Channel,
  ChannelMessage,
  channelsApi,
  TestChannelResponse,
} from '../services/channelsApi';
import { extractApiError } from '../utils/apiError';
import { formatDateTime } from '../utils/datetime';
import { parseDomains, parseNumbers } from '../utils/channelValidation';

type TabKey = 'overview' | 'messages' | 'config' | 'auth';

/** Parse a comma/space-separated list of Slack ids (users U…/W… or
 *  channels C…/G…) into a clean, upper-cased, deduped array. */
function parseSlackIds(raw: string): string[] {
  return Array.from(new Set(
    (raw || '')
      .split(/[\s,;]+/)
      .map((s) => s.trim().toUpperCase())
      .filter(Boolean),
  ));
}

export default function ChannelDetailPage() {
  const { t } = useTranslation();
  const { prompt: promptAccount, dialog: promptDialog } = usePrompt();
  const navigate = useNavigate();
  const params = useParams();
  const id = Number(params.id);

  const [tab, setTab] = useState<TabKey>('overview');
  const [channel, setChannel] = useState<Channel | null>(null);
  const [messages, setMessages] = useState<ChannelMessage[]>([]);
  const [messagesTotal, setMessagesTotal] = useState(0);
  const [messagesLoading, setMessagesLoading] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<TestChannelResponse | null>(null);
  const [messageStatusFilter, setMessageStatusFilter] = useState('');

  // Sequence guards: a slow response for channel A must never land on
  // the screen after the operator navigated to channel B (or away).
  const reloadSeq = useRef(0);
  const messagesSeq = useRef(0);
  const testSeq = useRef(0);

  useEffect(() => {
    // Full reset when navigating between channel ids — otherwise the
    // previous channel's tab, messages and test banner leak into the
    // new one for a flash (or permanently when a fetch fails).
    setTab('overview');
    setChannel(null);
    setMessages([]);
    setMessagesTotal(0);
    setMessageStatusFilter('');
    setTestResult(null);
    setError(null);
    // Invalidate an in-flight test from the previous channel so its result
    // can't land on this one's banner.
    testSeq.current += 1;
    if (!id) return;
    void reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  useEffect(() => {
    if (tab !== 'messages' || !id) return;
    void loadMessages();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, messageStatusFilter, id]);

  // Invalidate in-flight requests on unmount.
  useEffect(() => () => {
    reloadSeq.current += 1;
    messagesSeq.current += 1;
  }, []);

  // Listen for the OAuth popup notification (postMessage from the
  // auth-service /composio/callback page) so we refresh as soon as the
  // operator finishes consent.
  useEffect(() => {
    function onMessage(ev: MessageEvent) {
      if (!ev?.data || typeof ev.data !== 'object') return;
      if ((ev.data as { type?: string }).type === 'wai_mcp_oauth_success') {
        void reload();
      }
    }
    window.addEventListener('message', onMessage);
    return () => window.removeEventListener('message', onMessage);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const reload = async () => {
    const seq = ++reloadSeq.current;
    setLoading(true);
    setError(null);
    try {
      const c = await channelsApi.get(id);
      if (seq !== reloadSeq.current) return;
      setChannel(c);
    } catch (exc: unknown) {
      if (seq !== reloadSeq.current) return;
      setError(extractApiError(exc));
    } finally {
      if (seq === reloadSeq.current) setLoading(false);
    }
  };

  const loadMessages = async () => {
    const seq = ++messagesSeq.current;
    setMessagesLoading(true);
    try {
      const r = await channelsApi.messages(id, {
        status: messageStatusFilter || undefined,
        limit: 50,
      });
      if (seq !== messagesSeq.current) return;
      setMessages(r.rows);
      setMessagesTotal(r.total);
    } catch (exc: unknown) {
      if (seq !== messagesSeq.current) return;
      setError(extractApiError(exc));
    } finally {
      if (seq === messagesSeq.current) setMessagesLoading(false);
    }
  };

  const handleTest = async () => {
    // Sequence guard (like reload/loadMessages): testing channel A then
    // navigating to channel B must not display A's pass/fail banner on B.
    const seq = ++testSeq.current;
    setTesting(true);
    setTestResult(null);
    try {
      const r = await channelsApi.test(id);
      if (seq !== testSeq.current) return;
      setTestResult(r);
      // A passing test may self-heal a pending/unauthorized channel
      // (backend flips it to active) — refresh the status chip.
      if (r.ok) void reload();
    } catch (exc: unknown) {
      if (seq !== testSeq.current) return;
      setTestResult({ ok: false, message: extractApiError(exc) });
    } finally {
      if (seq === testSeq.current) setTesting(false);
    }
  };

  const handleReauth = (opts?: { changeAccount?: boolean }) => {
    if (!channel) return;
    const doAuth = async (account?: string) => {
      try {
        const r = await channelsApi.authorize(channel.id, {
          return_origin: window.location.origin,
          ...(opts?.changeAccount ? { change_account: true } : {}),
          ...(account ? { composio_user_id: account } : {}),
        });
        if (r.authorize_url) {
          const w = 600;
          const h = 700;
          const left = window.screenX + Math.max(0, (window.outerWidth - w) / 2);
          const top = window.screenY + Math.max(0, (window.outerHeight - h) / 2);
          window.open(r.authorize_url, 'wai-channel-oauth-reauth',
            `width=${w},height=${h},left=${left},top=${top}`);
        } else if (r.reauthorized) {
          await reload();
        }
      } catch (exc: unknown) {
        setError(extractApiError(exc));
      }
    };
    if (opts?.changeAccount) {
      // Optional new account email; blank ⇒ keep the same entity and pick the
      // account in the provider's sign-in window. Cancelling aborts.
      const current = channel.composio_user_id || channel.mailbox_address || '';
      promptAccount(
        { message: t('channels.changeAccountPrompt'), defaultValue: current },
        (answer) => void doAuth(answer.trim().toLowerCase() || undefined),
      );
    } else {
      void doAuth(undefined);
    }
  };

  // Business-timezone render (utils/datetime), not the raw browser locale.
  const fmt = (iso: string | null) => formatDateTime(iso);

  if (loading && !channel) {
    return (
      <Box sx={{ display: 'flex', justifyContent: 'center', mt: 6 }}>
        <CircularProgress />
      </Box>
    );
  }
  if (!channel) {
    return <Alert severity="error">{error || t('common.notFound')}</Alert>;
  }

  return (
    <Box sx={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
      <PageHeader
        title={channel.display_name}
        subtitle={
          <Stack direction="row" spacing={1} alignItems="center" component="span">
            <span>{channel.mailbox_address}</span>
            <span>·</span>
            <Chip label={t(`channels.auth.${channel.auth_method}`)} size="small" variant="outlined" />
            <StatusChip status={channel.status} />
          </Stack>
        }
        breadcrumbs={[
          { label: t('channels.title'), to: '/channels' },
          { label: channel.display_name },
        ]}
        actions={
          <Stack direction="row" spacing={1}>
            <Tooltip title={t('common.refresh') as string}>
              <IconButton onClick={reload}><RefreshIcon /></IconButton>
            </Tooltip>
            <Button
              variant="outlined"
              startIcon={<PlayCircleOutlineIcon />}
              onClick={handleTest}
              disabled={testing}
            >
              {testing ? t('common.loading') : t('channels.test')}
            </Button>
            {channel.status === 'unauthorized' && (
              <Button
                variant="contained"
                color="warning"
                startIcon={<VpnKeyIcon />}
                onClick={() => handleReauth()}
              >
                {t('channels.reauth')}
              </Button>
            )}
          </Stack>
        }
      />

      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
      {testResult && (
        <Alert severity={testResult.ok ? 'success' : 'error'} sx={{ mb: 2 }}>
          {testResult.message}
        </Alert>
      )}

      <Tabs value={tab} onChange={(_, v) => setTab(v)} sx={{ mb: 2, flexShrink: 0 }}>
        <Tab label={t('channels.tabs.overview')} value="overview" />
        <Tab label={t('channels.tabs.messages')} value="messages" />
        <Tab label={t('channels.tabs.config')} value="config" />
        <Tab label={t('channels.tabs.auth')} value="auth" />
      </Tabs>

      {/* Scrollable panel: the page lives inside a fixed-height main
          (calc(100vh - 64px)); without an own scroll area the tab
          content overflowed the viewport and could not be scrolled. */}
      <Box sx={{ flex: 1, minHeight: 0, overflowY: 'auto', pb: 2, pr: 0.5 }}>
        {tab === 'overview' && (
          <OverviewTab channel={channel} fmt={fmt} />
        )}
        {tab === 'messages' && (
          <MessagesTab
            messages={messages}
            total={messagesTotal}
            loading={messagesLoading}
            statusFilter={messageStatusFilter}
            setStatusFilter={setMessageStatusFilter}
            navigate={navigate}
            fmt={fmt}
            t={t}
          />
        )}
        {tab === 'config' && (
          // key resets the form state when navigating between channels —
          // without it the previous channel's edits survive in the inputs.
          <ConfigTab key={channel.id} channel={channel} onSaved={() => void reload()} />
        )}
        {tab === 'auth' && (
          <AuthTab
            key={channel.id}
            channel={channel}
            onSaved={() => void reload()}
          />
        )}
      </Box>
      {promptDialog}
    </Box>
  );
}


function OverviewTab({ channel, fmt }: { channel: Channel; fmt: (s: string | null) => string }) {
  const { t } = useTranslation();
  const isWhatsApp = channel.kind === 'whatsapp';
  const isSlack = channel.kind === 'slack';
  return (
    <Stack spacing={2}>
      <Card><CardContent>
        <Stack direction="row" spacing={4} flexWrap="wrap">
          <KpiBlock label={t('channels.cols.received24h')} value={channel.counters_24h?.received ?? channel.messages_received} />
          <KpiBlock label={t('channels.cols.replied24h')} value={channel.counters_24h?.replied ?? channel.messages_replied} />
          <KpiBlock label={t('channels.cols.totalReceived')} value={channel.messages_received} />
          <KpiBlock label={t('channels.cols.totalReplied')} value={channel.messages_replied} />
          <KpiBlock label={t('channels.cols.errors')} value={channel.errors_count} alert={channel.errors_count > 0} />
        </Stack>
      </CardContent></Card>

      <Card><CardContent>
        <Typography variant="subtitle1" sx={{ mb: 1, fontWeight: 700 }}>{t('channels.tabs.overview')}</Typography>
        <Stack spacing={1}>
          <DetailRow label={t('channels.cols.mailbox')} value={channel.mailbox_address} />
          <DetailRow label={t('channels.cols.kind')} value={t(`channels.kind.${channel.kind}`) as string} />
          <DetailRow label={t('channels.cols.auth')} value={t(`channels.auth.${channel.auth_method}`) as string} />
          {channel.composio_toolkit && (
            <DetailRow label={t('channels.cols.toolkit')} value={channel.composio_toolkit} />
          )}
          <DetailRow label={t('channels.cols.replyMode')} value={t(`channels.replyMode.${channel.reply_mode}`) as string} />
          <DetailRow label={t('channels.cols.poll')} value={`${channel.poll_interval_sec}s`} />
          <DetailRow
            label={t('channels.cols.processSince')}
            value={channel.process_since
              ? formatDateTime(channel.process_since)
              : (t('channels.cols.processSinceAll') as string)}
          />
          <DetailRow label={t('channels.cols.cursor')} value={channel.cursor || '—'} />
          <DetailRow label={t('channels.cols.lastPoll')} value={fmt(channel.last_polled_at)} />
          <DetailRow label={t('channels.cols.lastActivity')} value={fmt(channel.last_message_at)} />
          {!isSlack && (
            <DetailRow
              label={t('channels.allow.people', 'Personas que pueden escribir (directorio)')}
              value={(channel.filter_config?.allow_directory_users || []).join(', ') || '—'}
            />
          )}
          {isWhatsApp ? (
            <DetailRow
              label={t('channels.cols.allowedNumbers')}
              value={(channel.filter_config?.from_numbers_allow || []).join(', ') || '—'}
            />
          ) : isSlack ? (
            <DetailRow
              label={t('channels.cols.allowedUsers')}
              value={(channel.filter_config?.from_users_allow || []).join(', ')
                || (t('channels.wizard.fields.slackUsersAll') as string)}
            />
          ) : (
            <>
              <DetailRow
                label={t('channels.cols.allowedSenders')}
                value={(channel.filter_config?.from_domain_allow || []).join(', ') || '—'}
              />
              {channel.filter_config?.subject_regex && (
                <DetailRow label={t('channels.cols.subjectRegex')} value={channel.filter_config.subject_regex} />
              )}
            </>
          )}
        </Stack>
      </CardContent></Card>
    </Stack>
  );
}


function MessagesTab({
  messages, total, loading, statusFilter, setStatusFilter, navigate, fmt, t,
}: {
  messages: ChannelMessage[];
  total: number;
  loading: boolean;
  statusFilter: string;
  setStatusFilter: (s: string) => void;
  navigate: (path: string) => void;
  fmt: (s: string | null) => string;
  t: (k: string, opts?: Record<string, unknown>) => string;
}) {
  return (
    <Stack spacing={1.5}>
      <Stack direction="row" spacing={2} alignItems="center" sx={{ mb: 1 }}>
        <Typography variant="body2" color="text.secondary">
          {t('channels.cols.total')}: <b>{total}</b>
        </Typography>
        <Select
          size="small"
          value={statusFilter}
          displayEmpty
          onChange={(e) => setStatusFilter(String(e.target.value))}
          sx={{ minWidth: 180 }}
        >
          <MenuItem value="">{t('common.all')}</MenuItem>
          <MenuItem value="pending">{t('channels.messageStatus.pending')}</MenuItem>
          <MenuItem value="filtered">{t('channels.messageStatus.filtered')}</MenuItem>
          <MenuItem value="processing">{t('channels.messageStatus.processing')}</MenuItem>
          <MenuItem value="replied">{t('channels.messageStatus.replied')}</MenuItem>
          <MenuItem value="skipped">{t('channels.messageStatus.skipped')}</MenuItem>
          <MenuItem value="error">{t('channels.messageStatus.error')}</MenuItem>
        </Select>
        {loading && <CircularProgress size={18} />}
      </Stack>
      {!loading && messages.length === 0 && (
        <EmptyState
          title={t('channels.messages.empty.title')}
          body={t('channels.messages.empty.description')}
        />
      )}
      {messages.map((m) => (
        <MessageThreadCard key={m.id} m={m} navigate={navigate} fmt={fmt} t={t} />
      ))}
    </Stack>
  );
}


function MessageThreadCard({ m, navigate, fmt, t }: {
  m: ChannelMessage;
  navigate: (path: string) => void;
  fmt: (s: string | null) => string;
  t: (k: string, opts?: Record<string, unknown>) => string;
}) {
  const [expanded, setExpanded] = useState(false);
  const hasReply = Boolean(m.reply_body);
  const isNotification = m.direction === 'notification';
  const deliveryOk = m.status === 'replied';
  const deliveryFailed = m.status === 'error';
  const outboundTitle = isNotification
    ? (deliveryOk
      ? t('channels.messages.notificationSent')
      : deliveryFailed
        ? t('channels.messages.notificationFailed')
        : t('channels.messages.notificationPreview'))
    : t('channels.messages.replyTitle');
  return (
    <Card variant="outlined">
      <CardContent
        onClick={() => setExpanded((v) => !v)}
        sx={{ cursor: 'pointer', '&:last-child': { pb: 2 } }}
      >
        <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 0.5 }}>
          {isNotification && (
            <Chip
              size="small"
              color="info"
              variant="outlined"
              label={t('channels.messages.notification')}
            />
          )}
          <Typography variant="body2" sx={{ fontWeight: 700, flex: 1 }} noWrap>
            {m.subject || m.reply_subject || t('channels.messages.noSubject')}
          </Typography>
          <StatusChip status={m.status} />
          <Typography variant="caption" color="text.secondary">{fmt(m.received_at)}</Typography>
        </Stack>
        <Typography variant="caption" color="text.secondary">
          {isNotification
            ? `${t('channels.messages.notifyTo')}: ${m.sender || '—'}`
            : `${t('channels.cols.from')}: ${m.sender || '—'}`}
        </Typography>

        {m.error && (
          <Alert severity="error" sx={{ mt: 1 }}>
            <Typography variant="caption" sx={{ fontWeight: 700, display: 'block', mb: 0.5 }}>
              {t('channels.messages.deliveryError')}
            </Typography>
            {m.error}
          </Alert>
        )}

        {/* Inbound body — excerpt collapsed, full when expanded.
            Notification rows carry their content on the reply_* fields. */}
        {!isNotification && (
          <Typography
            variant="body2"
            color="text.secondary"
            sx={{ mt: 1, whiteSpace: expanded ? 'pre-wrap' : 'normal' }}
          >
            {expanded ? (m.body_excerpt || '') : (m.body_excerpt?.slice(0, 200) || '')}
          </Typography>
        )}

        {/* Outbound reply — the full thread, not just the inbound half */}
        {hasReply && (
          <Box
            sx={{
              mt: 1.5, p: 1.5, borderRadius: 1,
              bgcolor: 'action.hover',
              borderLeft: '3px solid',
              borderLeftColor: deliveryOk
                ? 'success.main'
                : deliveryFailed
                  ? 'error.main'
                  : 'warning.main',
            }}
          >
            <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 0.5 }}>
              <Typography variant="caption" sx={{ fontWeight: 700 }}>
                {outboundTitle}
              </Typography>
              {m.replied_at && (
                <Typography variant="caption" color="text.secondary">
                  {fmt(m.replied_at)}
                </Typography>
              )}
            </Stack>
            {m.reply_subject && (
              <Typography variant="caption" color="text.secondary" sx={{ display: 'block' }}>
                {t('channels.messages.replySubject')}: {m.reply_subject}
              </Typography>
            )}
            <Typography
              variant="body2"
              sx={{ mt: 0.5, whiteSpace: expanded ? 'pre-wrap' : 'normal' }}
            >
              {expanded ? (m.reply_body || '') : (m.reply_body?.slice(0, 200) || '')}
            </Typography>
            {(m.reply_attachments || []).length > 0 && (
              <Stack direction="row" spacing={1} sx={{ mt: 1 }} flexWrap="wrap">
                {(m.reply_attachments || []).map((a, i) => (
                  <Chip
                    key={`${a.filename}-${i}`}
                    size="small"
                    variant="outlined"
                    label={a.filename}
                    component={a.url ? 'a' : 'div'}
                    href={a.url || undefined}
                    target="_blank"
                    clickable={Boolean(a.url)}
                    onClick={(e: { stopPropagation: () => void }) => e.stopPropagation()}
                  />
                ))}
              </Stack>
            )}
          </Box>
        )}

        <Stack direction="row" spacing={1} sx={{ mt: 1 }}>
          {m.run_id && (
            <Button
              size="small"
              onClick={(e) => { e.stopPropagation(); navigate(`/runs/${m.run_id}`); }}
            >
              {t('channels.messages.viewRun', { id: m.run_id })}
            </Button>
          )}
          <Button size="small" color="inherit" onClick={(e) => { e.stopPropagation(); setExpanded((v) => !v); }}>
            {expanded ? t('channels.messages.collapse') : t('channels.messages.expand')}
          </Button>
        </Stack>
      </CardContent>
    </Card>
  );
}


function ConfigTab({ channel, onSaved }: { channel: Channel; onSaved: () => void }) {
  const { t } = useTranslation();
  const isWhatsApp = channel.kind === 'whatsapp';
  const isSlack = channel.kind === 'slack';
  const [displayName, setDisplayName] = useState(channel.display_name);
  // Kept as text so the operator can clear the field while typing —
  // validated (and NaN-rejected) on save instead of silently coerced.
  const [poll, setPoll] = useState(String(channel.poll_interval_sec));
  const [sinceMode, setSinceMode] = useState<'all' | 'date'>(channel.process_since ? 'date' : 'all');
  const [sinceDate, setSinceDate] = useState(toLocalInput(channel.process_since));
  const [fromDomains, setFromDomains] = useState((channel.filter_config?.from_domain_allow || []).join(', '));
  const [fromNumbers, setFromNumbers] = useState((channel.filter_config?.from_numbers_allow || []).join(', '));
  const [fromUsers, setFromUsers] = useState((channel.filter_config?.from_users_allow || []).join(', '));
  // People from the directory allowed to write (v7.24.0) — canonical emails.
  const [allowPeople, setAllowPeople] = useState<string[]>(channel.filter_config?.allow_directory_users || []);
  const [subjectRegex, setSubjectRegex] = useState(channel.filter_config?.subject_regex || '');
  const [replyMode, setReplyMode] = useState(channel.reply_mode);
  const [signature, setSignature] = useState(channel.reply_config?.signature || '');
  // Slack keeps a raw-id CSV; email/whatsapp pick PEOPLE from the directory.
  const [notifyTo, setNotifyTo] = useState((channel.notify_config?.notify_to || []).join(', '));
  const [notifyPeople, setNotifyPeople] = useState<string[]>(channel.notify_config?.notify_to || []);
  const [notifyPolicy, setNotifyPolicy] = useState(
    channel.notify_config?.policy
      || ((channel.notify_config?.notify_to || []).length ? 'agent' : 'never'),
  );
  const [notifyMax, setNotifyMax] = useState(String(channel.notify_config?.max_per_day ?? 0));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSave = async () => {
    setError(null);
    const pollSec = Number(poll);
    if (!Number.isFinite(pollSec) || pollSec <= 0) {
      setError(t('channels.errors.invalidPoll'));
      return;
    }
    let processSince: string | null = null;
    if (sinceMode === 'date') {
      const d = new Date(sinceDate);
      if (Number.isNaN(d.getTime())) {
        setError(t('channels.errors.invalidSince'));
        return;
      }
      processSince = d.toISOString();
    }
    setSaving(true);
    try {
      await channelsApi.patch(channel.id, {
        display_name: displayName,
        poll_interval_sec: pollSec,
        process_since: processSince,
        filter_config: (isWhatsApp
          ? {
              from_numbers_allow: parseNumbers(fromNumbers),
              allow_directory_users: allowPeople,
            }
          : isSlack
            ? { from_users_allow: parseSlackIds(fromUsers) }
            : {
                from_domain_allow: parseDomains(fromDomains),
                allow_directory_users: allowPeople,
                subject_regex: subjectRegex || null,
              }) as unknown as Channel['filter_config'],
        reply_mode: replyMode,
        reply_config: {
          ...channel.reply_config,
          signature: signature || null,
        } as unknown as Channel['reply_config'],
        notify_config: {
          // email/whatsapp: PEOPLE picked from the directory (whatsapp stores
          // the person's phone, email their email); slack: raw ids.
          notify_to: isSlack ? parseSlackIds(notifyTo) : notifyPeople,
          policy: notifyPolicy,
          max_per_day: Math.max(0, Number(notifyMax) || 0),
        } as unknown as Channel['notify_config'],
      });
      onSaved();
    } catch (exc: unknown) {
      setError(extractApiError(exc));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card><CardContent>
      <Stack spacing={2}>
        <TextField
          label={t('channels.wizard.fields.displayName')}
          value={displayName}
          onChange={(e) => setDisplayName(e.target.value)}
          fullWidth
        />
        {isWhatsApp ? (
          <>
            <UserPicker kind="email" multiple
              label={t('channels.allow.people', 'Personas que pueden escribir (directorio)')}
              helperText={t('channels.allow.peopleHelpWhatsapp', 'Se reconocen por sus teléfonos de la ficha de Usuarios; si alguien no tiene ficha con email, usa las reglas de números.')}
              value={allowPeople}
              onChange={setAllowPeople}
            />
            <TextField
              label={t('channels.wizard.fields.fromNumbers')}
              value={fromNumbers}
              onChange={(e) => setFromNumbers(e.target.value)}
              fullWidth
              helperText={t('channels.wizard.fields.fromNumbersHelp')}
            />
          </>
        ) : isSlack ? (
          <TextField
            label={t('channels.wizard.fields.slackUsers')}
            value={fromUsers}
            onChange={(e) => setFromUsers(e.target.value)}
            fullWidth
            helperText={t('channels.wizard.fields.slackUsersHelp')}
          />
        ) : (
          <>
            <UserPicker kind="email" multiple
              label={t('channels.allow.people', 'Personas que pueden escribir (directorio)')}
              helperText={t('channels.allow.peopleHelpEmail', 'Además de las personas elegidas, las reglas de dominio admiten remitentes de forma global.')}
              value={allowPeople}
              onChange={setAllowPeople}
            />
            <TextField
              label={t('channels.wizard.fields.fromDomains')}
              value={fromDomains}
              onChange={(e) => setFromDomains(e.target.value)}
              fullWidth
              helperText={t('channels.wizard.fields.fromDomainsHelp')}
            />
            <TextField
              label={t('channels.wizard.fields.subjectRegex')}
              value={subjectRegex}
              onChange={(e) => setSubjectRegex(e.target.value)}
              fullWidth
            />
          </>
        )}
        <Box sx={{ display: 'flex', gap: 2 }}>
          <TextField
            label={t('channels.wizard.fields.pollInterval')}
            type="number"
            value={poll}
            onChange={(e) => setPoll(e.target.value)}
            sx={{ flex: 1 }}
          />
          <Select
            value={replyMode}
            onChange={(e) => setReplyMode(e.target.value as 'auto' | 'never')}
            sx={{ flex: 1 }}
          >
            <MenuItem value="auto">{t('channels.replyMode.auto')}</MenuItem>
            <MenuItem value="never">{t('channels.replyMode.never')}</MenuItem>
          </Select>
        </Box>
        {!isSlack && (
          <TextField
            label={t('channels.wizard.fields.signature')}
            value={signature}
            onChange={(e) => setSignature(e.target.value)}
            fullWidth
            multiline
            minRows={2}
          />
        )}

        <Box>
          <Typography variant="subtitle2" sx={{ mb: 0.5 }}>
            {t('channels.wizard.fields.processSince')}
          </Typography>
          <Typography variant="caption" color="text.secondary" display="block" sx={{ mb: 1 }}>
            {t('channels.wizard.fields.processSinceHelp')}
          </Typography>
          <RadioGroup
            value={sinceMode}
            onChange={(e) => setSinceMode(e.target.value as 'all' | 'date')}
          >
            <FormControlLabel
              value="all"
              control={<Radio />}
              label={t('channels.wizard.fields.processSinceAll')}
            />
            <FormControlLabel
              value="date"
              control={<Radio />}
              label={t('channels.wizard.fields.processSinceDate')}
            />
            {sinceMode === 'date' && (
              <TextField
                type="datetime-local"
                size="small"
                value={sinceDate}
                onChange={(e) => setSinceDate(e.target.value)}
                sx={{ ml: 4, mb: 1, maxWidth: 280 }}
                InputLabelProps={{ shrink: true }}
              />
            )}
          </RadioGroup>
        </Box>

        <Divider sx={{ my: 1 }} />
        <Box>
          <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>
            {t('channels.notify.title')}
          </Typography>
          <Typography variant="caption" color="text.secondary">
            {t('channels.notify.description')}
          </Typography>
        </Box>
        {isSlack ? (
          <TextField
            label={t('channels.notify.recipients')}
            value={notifyTo}
            onChange={(e) => setNotifyTo(e.target.value)}
            fullWidth
            helperText={t('channels.notify.recipientsHelpSlack')}
          />
        ) : (
          <UserPicker kind={isWhatsApp ? 'whatsapp' : 'email'} multiple
            label={t('channels.notify.recipients')}
            helperText={
              isWhatsApp
                ? t('channels.notify.recipientsHelpWhatsapp')
                : t('channels.notify.recipientsHelpEmail')
            }
            value={notifyPeople}
            onChange={setNotifyPeople}
          />
        )}
        <Box sx={{ display: 'flex', gap: 2 }}>
          <Select
            value={notifyPolicy}
            onChange={(e) => setNotifyPolicy(e.target.value as typeof notifyPolicy)}
            sx={{ flex: 1 }}
          >
            <MenuItem value="agent">{t('channels.notify.policy.agent')}</MenuItem>
            <MenuItem value="critical_only">{t('channels.notify.policy.criticalOnly')}</MenuItem>
            <MenuItem value="never">{t('channels.notify.policy.never')}</MenuItem>
          </Select>
          <TextField
            label={t('channels.notify.maxPerDay')}
            type="number"
            value={notifyMax}
            onChange={(e) => setNotifyMax(e.target.value)}
            sx={{ flex: 1 }}
            helperText={t('channels.notify.maxPerDayHelp')}
          />
        </Box>

        {error && <Alert severity="error">{error}</Alert>}
        <Stack direction="row" justifyContent="flex-end">
          <Button variant="contained" onClick={handleSave} disabled={saving}>
            {saving ? t('common.loading') : t('common.save')}
          </Button>
        </Stack>
      </Stack>
    </CardContent></Card>
  );
}


function AuthTab({
  channel, onSaved,
}: {
  channel: Channel;
  onSaved: () => void;
}) {
  const { t } = useTranslation();
  if (channel.auth_method === 'oauth_composio') {
    // LEGACY: the Composio/OAuth email adapter was retired — email channels are
    // IMAP/SMTP only. Such a channel can no longer be created and cannot be
    // polled; show a read-only notice instead of a (dead) reauth flow.
    return (
      <Card><CardContent>
        <Typography variant="subtitle1" sx={{ mb: 1, fontWeight: 700 }}>
          {t('channels.auth.oauth_composio')}
        </Typography>
        <Alert severity="warning">
          {t('channels.tabs.authOAuthRetired', 'Este canal usaba email por OAuth/Composio, que ya no está soportado (el email es solo IMAP/SMTP). Elimínalo y crea uno nuevo por IMAP + contraseña de aplicación.')}
        </Alert>
      </CardContent></Card>
    );
  }
  if (channel.auth_method === 'twilio') {
    // WhatsApp — let the operator rotate the Twilio Auth Token.
    return <TwilioAuthCard channel={channel} onSaved={onSaved} />;
  }
  if (channel.auth_method === 'bot_token') {
    // Slack — let the operator rotate the bot token (xoxb-…).
    return <SlackAuthCard channel={channel} onSaved={onSaved} />;
  }
  // IMAP — let the operator update the App Password.
  return <ImapAuthCard channel={channel} onSaved={onSaved} />;
}


function SlackAuthCard({ channel, onSaved }: { channel: Channel; onSaved: () => void }) {
  const { t } = useTranslation();
  const [botToken, setBotToken] = useState('');
  const [saving, setSaving] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const [ok, setOk] = useState<boolean | null>(null);

  const handleSave = async () => {
    if (!botToken) return;
    setSaving(true);
    setResult(null);
    setOk(null);
    try {
      await channelsApi.authorize(channel.id, { bot_token: botToken });
      setOk(true);
      setResult(t('channels.tabs.authSlackSuccess') as string);
      setBotToken('');
      onSaved();
    } catch (exc: unknown) {
      setOk(false);
      setResult(extractApiError(exc));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card><CardContent>
      <Typography variant="subtitle1" sx={{ mb: 1, fontWeight: 700 }}>
        {t('channels.auth.bot_token')}
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        {t('channels.tabs.authSlackHint')}
      </Typography>
      <Stack spacing={2}>
        <TextField
          label={t('channels.wizard.fields.slackBotToken')}
          type="password"
          value={botToken}
          onChange={(e) => setBotToken(e.target.value.trim())}
          placeholder="xoxb-..."
          fullWidth
        />
        {result && <Alert severity={ok ? 'success' : 'error'}>{result}</Alert>}
        <Stack direction="row" justifyContent="flex-end">
          <Button variant="contained" onClick={handleSave} disabled={saving || !botToken}>
            {saving ? t('common.loading') : t('channels.tabs.authSlackSave')}
          </Button>
        </Stack>
      </Stack>
    </CardContent></Card>
  );
}


function TwilioAuthCard({ channel, onSaved }: { channel: Channel; onSaved: () => void }) {
  const { t } = useTranslation();
  const [authToken, setAuthToken] = useState('');
  const [saving, setSaving] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const [ok, setOk] = useState<boolean | null>(null);

  const handleSave = async () => {
    if (!authToken) return;
    setSaving(true);
    setResult(null);
    setOk(null);
    try {
      await channelsApi.authorize(channel.id, { auth_token: authToken });
      setOk(true);
      setResult(t('channels.tabs.authTwilioSuccess') as string);
      setAuthToken('');
      onSaved();
    } catch (exc: unknown) {
      setOk(false);
      setResult(extractApiError(exc));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card><CardContent>
      <Typography variant="subtitle1" sx={{ mb: 1, fontWeight: 700 }}>
        {t('channels.auth.twilio')}
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        {t('channels.tabs.authTwilioHint')}
      </Typography>
      <Alert severity="info" sx={{ mb: 2 }}>
        <Typography variant="caption" component="div" sx={{ fontWeight: 700, mb: 0.5 }}>
          {t('channels.tabs.twilioSetupTitle', 'Cómo configurarlo en Twilio (resumen)')}
        </Typography>
        <Typography variant="caption" component="div" sx={{ whiteSpace: 'pre-line' }}>
          {t('channels.tabs.twilioSetupSteps',
            '1) Crea cuenta en twilio.com y copia Account SID + Auth Token (panel de inicio).\n' +
            '2) Para PROBAR ya: Messaging → Try it out → Sandbox; envía el «join <palabra>» desde tu móvil al número del sandbox. Usa ese número como tu número de WhatsApp.\n' +
            '3) Para PRODUCCIÓN: Messaging → Senders → WhatsApp senders → Create new sender (Embedded Signup con Meta: Business Manager, WABA, alta y verificación del número).\n' +
            '4) ¿No tienes número? Cómpralo en Phone Numbers → Buy a number (SMS+Voz) y lee el código de verificación en Monitor → Logs → Messaging (no necesitas teléfono ni SIM).\n' +
            '5) No configures webhook: el agente sondea los mensajes por su cuenta.')}
        </Typography>
      </Alert>
      <Stack spacing={2}>
        <TextField
          label={t('channels.wizard.fields.waAuthToken')}
          type="password"
          value={authToken}
          onChange={(e) => setAuthToken(e.target.value)}
          fullWidth
        />
        {result && <Alert severity={ok ? 'success' : 'error'}>{result}</Alert>}
        <Stack direction="row" justifyContent="flex-end">
          <Button variant="contained" onClick={handleSave} disabled={saving || !authToken}>
            {saving ? t('common.loading') : t('channels.tabs.authTwilioSave')}
          </Button>
        </Stack>
      </Stack>
    </CardContent></Card>
  );
}


function ImapAuthCard({ channel, onSaved }: { channel: Channel; onSaved: () => void }) {
  const { t } = useTranslation();
  const [password, setPassword] = useState('');
  const [saving, setSaving] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const [ok, setOk] = useState<boolean | null>(null);

  const handleSave = async () => {
    if (!password) return;
    setSaving(true);
    setResult(null);
    setOk(null);
    try {
      const r = await channelsApi.authorize(channel.id, { password });
      setOk(true);
      setResult(t('channels.tabs.authImapSuccess') as string);
      setPassword('');
      onSaved();
      void r;
    } catch (exc: unknown) {
      setOk(false);
      setResult(extractApiError(exc));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card><CardContent>
      <Typography variant="subtitle1" sx={{ mb: 1, fontWeight: 700 }}>
        {t('channels.auth.imap_smtp')}
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        {t('channels.tabs.authImapHint')}
      </Typography>
      <Stack spacing={2}>
        <TextField
          label={t('channels.wizard.fields.password')}
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          fullWidth
        />
        {result && <Alert severity={ok ? 'success' : 'error'}>{result}</Alert>}
        <Stack direction="row" justifyContent="flex-end">
          <Button variant="contained" onClick={handleSave} disabled={saving || !password}>
            {saving ? t('common.loading') : t('channels.tabs.authImapSave')}
          </Button>
        </Stack>
      </Stack>
    </CardContent></Card>
  );
}


function KpiBlock({ label, value, alert }: { label: string; value: number; alert?: boolean }) {
  return (
    <Box>
      <Typography variant="caption" color="text.secondary">{label}</Typography>
      <Typography variant="h5" sx={{ fontWeight: 700, color: alert ? 'error.main' : 'text.primary' }}>
        {value}
      </Typography>
    </Box>
  );
}


function DetailRow({ label, value }: { label: string; value: string }) {
  return (
    <Box sx={{ display: 'grid', gridTemplateColumns: '180px 1fr', gap: 1, alignItems: 'baseline' }}>
      <Typography variant="caption" color="text.secondary">{label}</Typography>
      <Typography variant="body2">{value}</Typography>
    </Box>
  );
}


/** ISO datetime → datetime-local input value (local tz). Falls back to
 *  the current time when the channel has no floor set yet. */
function toLocalInput(iso: string | null): string {
  const d = iso ? new Date(iso) : new Date();
  const base = Number.isNaN(d.getTime()) ? new Date() : d;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${base.getFullYear()}-${pad(base.getMonth() + 1)}-${pad(base.getDate())}T${pad(base.getHours())}:${pad(base.getMinutes())}`;
}
