/**
 * Channels — list page.
 *
 * Lists every channel (`/api/channels`) with quick filters and the
 * Add-channel wizard. Detail view lives at /channels/:id.
 *
 * Channels are a BUILT-IN capability of every employee (7.91.0): always
 * enabled, always configured HERE after deploy (mailboxes, numbers,
 * credentials) — never in the manifest nor on the deploy form. The
 * `enabled` flag from the meta endpoint stays in the payload for
 * backwards compatibility and is now always true.
 */
import { useEffect, useRef, useState } from 'react';
import {
  Alert, Box, Button, Card, CardContent, Chip, Dialog, Divider, IconButton,
  MenuItem, Select, Stack, Switch, TextField, Tooltip, Typography,
} from '@mui/material';
import AddIcon from '@mui/icons-material/Add';
import RefreshIcon from '@mui/icons-material/Refresh';
import EmailIcon from '@mui/icons-material/AlternateEmail';
import OpenInNewIcon from '@mui/icons-material/OpenInNew';
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline';
import { Link as RouterLink, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

import PageHeader from '../components/PageHeader';
import EmptyState from '../components/EmptyState';
import StatusChip from '../components/StatusChip';
import { channelsApi, type Channel, type ChannelCapabilities } from '../services/channelsApi';
import { extractApiError } from '../utils/apiError';
import { formatDateTime } from '../utils/datetime';
import AddChannelDialog from './channels/AddChannelDialog';

export default function ChannelsPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();

  const [channels, setChannels] = useState<Channel[]>([]);
  const [capabilities, setCapabilities] = useState<ChannelCapabilities | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showAdd, setShowAdd] = useState(false);
  const [statusFilter, setStatusFilter] = useState<string>('');
  const [pendingDelete, setPendingDelete] = useState<Channel | null>(null);

  // Drop responses that arrive after a newer reload (filter change) or
  // after the page unmounted.
  const reloadSeq = useRef(0);

  const reload = async () => {
    const seq = ++reloadSeq.current;
    setLoading(true);
    setError(null);
    try {
      const [caps, list] = await Promise.all([
        channelsApi.capabilities(),
        channelsApi.list(statusFilter ? { status: statusFilter } : undefined),
      ]);
      if (seq !== reloadSeq.current) return;
      setCapabilities(caps);
      setChannels(list);
    } catch (exc: unknown) {
      if (seq !== reloadSeq.current) return;
      setError(`${t('channels.errors.load')}: ${extractApiError(exc)}`);
    } finally {
      if (seq === reloadSeq.current) setLoading(false);
    }
  };

  useEffect(() => {
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [statusFilter]);

  useEffect(() => () => {
    reloadSeq.current += 1;
  }, []);

  // Business-timezone render (utils/datetime), not the raw browser locale.
  const fmt = (iso: string | null) => formatDateTime(iso);

  const handleDelete = async () => {
    if (!pendingDelete) return;
    try {
      await channelsApi.delete(pendingDelete.id);
      setPendingDelete(null);
      reload();
    } catch (exc: unknown) {
      setError(`${t('channels.errors.delete')}: ${extractApiError(exc)}`);
    }
  };

  const handleToggleActive = async (c: Channel) => {
    try {
      await channelsApi.patch(c.id, { is_active: !c.is_active });
      reload();
    } catch (exc: unknown) {
      setError(`${t('channels.errors.patch')}: ${extractApiError(exc)}`);
    }
  };

  const anyAuthAvailable = !!capabilities && (
    capabilities.oauth_available ||
    capabilities.imap_available ||
    Boolean(capabilities.whatsapp_available) ||
    Boolean(capabilities.slack_available)
  );
  const canAdd = !!capabilities?.enabled && anyAuthAvailable;

  return (
    <Box sx={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
      <PageHeader
        title={t('channels.title')}
        subtitle={t('channels.subtitle')}
        icon={<EmailIcon />}
        actions={
          <Stack direction="row" spacing={1}>
            <Tooltip title={t('common.refresh') as string}>
              <IconButton onClick={reload}><RefreshIcon /></IconButton>
            </Tooltip>
            <Button
              variant="contained"
              startIcon={<AddIcon />}
              onClick={() => setShowAdd(true)}
              disabled={!canAdd}
            >
              {t('channels.add')}
            </Button>
          </Stack>
        }
      />

      {!capabilities?.enabled && (
        <Alert severity="info" sx={{ mb: 2 }}>
          {t('channels.disabled')}
        </Alert>
      )}
      {capabilities?.enabled && !anyAuthAvailable && (
        <Alert severity="warning" sx={{ mb: 2 }}>
          {t('channels.noAuth')}
        </Alert>
      )}
      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}

      <Card sx={{ mb: 2 }}>
        <CardContent sx={{ display: 'flex', gap: 2, alignItems: 'center', flexWrap: 'wrap' }}>
          <Typography variant="body2" color="text.secondary">{t('channels.filters.status')}:</Typography>
          <Select
            size="small"
            value={statusFilter}
            displayEmpty
            onChange={(e) => setStatusFilter(String(e.target.value))}
            sx={{ minWidth: 180 }}
          >
            <MenuItem value="">{t('common.all')}</MenuItem>
            <MenuItem value="active">{t('channels.status.active')}</MenuItem>
            <MenuItem value="pending">{t('channels.status.pending')}</MenuItem>
            <MenuItem value="unauthorized">{t('channels.status.unauthorized')}</MenuItem>
            <MenuItem value="error">{t('channels.status.error')}</MenuItem>
          </Select>
          <Box sx={{ flex: 1 }} />
          <Typography variant="caption" color="text.secondary">
            {t('channels.count', { count: channels.length })}
          </Typography>
        </CardContent>
      </Card>

      {!loading && channels.length === 0 && (
        <EmptyState
          title={t('channels.empty.title')}
          body={t('channels.empty.description')}
        />
      )}

      <Stack spacing={1.5} sx={{ flex: 1, overflow: 'auto', minHeight: 0 }}>
        {channels.map((c) => (
          <Card key={c.id} variant="outlined">
            <CardContent>
              <Stack direction={{ xs: 'column', md: 'row' }} spacing={2} alignItems="flex-start">
                <Box sx={{ flex: 1, minWidth: 0 }}>
                  <Stack direction="row" spacing={1.5} alignItems="center" sx={{ mb: 0.5 }}>
                    <Typography variant="h6" sx={{ fontWeight: 700 }}>
                      {c.display_name}
                    </Typography>
                    {/* A deactivated channel (is_active=0) is shown as 'Desactivado' (Deactivated),
                        not with its status (which could stay at 'active' and was confusing). */}
                    {c.is_active
                      ? <StatusChip status={c.status} />
                      : <Chip size="small" label={t('channels.inactive', 'Desactivado')} variant="outlined" />}
                    <Chip
                      label={t(`channels.kind.${c.kind}`)}
                      size="small"
                      color={c.kind === 'whatsapp' ? 'success' : c.kind === 'slack' ? 'secondary' : 'default'}
                      variant="outlined"
                    />
                    <Chip
                      label={t(`channels.auth.${c.auth_method}`)}
                      size="small"
                      variant="outlined"
                    />
                  </Stack>
                  <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
                    {c.mailbox_address}
                    {c.composio_toolkit ? ` · ${c.composio_toolkit}` : ''}
                  </Typography>
                  <Stack direction="row" spacing={3} flexWrap="wrap">
                    <Typography variant="caption" color="text.secondary">
                      {t('channels.cols.received24h')}: <b>{c.counters_24h?.received ?? c.messages_received}</b>
                    </Typography>
                    <Typography variant="caption" color="text.secondary">
                      {t('channels.cols.replied24h')}: <b>{c.counters_24h?.replied ?? c.messages_replied}</b>
                    </Typography>
                    <Typography variant="caption" color="text.secondary">
                      {t('channels.cols.poll')}: <b>{c.poll_interval_sec}s</b>
                    </Typography>
                    <Typography variant="caption" color="text.secondary">
                      {t('channels.cols.lastActivity')}: <b>{fmt(c.last_message_at)}</b>
                    </Typography>
                  </Stack>
                  {c.status_reason && (
                    <Alert severity={c.status === 'unauthorized' ? 'warning' : 'error'} sx={{ mt: 1 }}>
                      {c.status_reason}
                    </Alert>
                  )}
                </Box>
                <Stack direction="row" spacing={1} alignItems="center">
                  <Tooltip title={c.is_active ? t('channels.toggleOff') as string : t('channels.toggleOn') as string}>
                    <Switch
                      checked={c.is_active}
                      onChange={() => handleToggleActive(c)}
                      size="small"
                    />
                  </Tooltip>
                  <Button
                    component={RouterLink}
                    to={`/channels/${c.id}`}
                    endIcon={<OpenInNewIcon />}
                    size="small"
                  >
                    {t('channels.open')}
                  </Button>
                  <Tooltip title={t('channels.delete') as string}>
                    <IconButton size="small" onClick={() => setPendingDelete(c)}>
                      <DeleteOutlineIcon fontSize="small" />
                    </IconButton>
                  </Tooltip>
                </Stack>
              </Stack>
            </CardContent>
          </Card>
        ))}
      </Stack>

      <AddChannelDialog
        open={showAdd}
        capabilities={capabilities}
        onClose={() => setShowAdd(false)}
        onCreated={(ch: Channel) => {
          setShowAdd(false);
          reload();
          // Navigate to detail so the operator sees the wizard's
          // confirmation page (and any reauth prompt if OAuth is still
          // pending). The list refresh already includes the new row.
          navigate(`/channels/${ch.id}`);
        }}
      />

      <Dialog open={!!pendingDelete} onClose={() => setPendingDelete(null)}>
        <Box sx={{ p: 3, minWidth: 360 }}>
          <Typography variant="h6" sx={{ mb: 1 }}>
            {t('channels.deleteConfirm.title')}
          </Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            {t('channels.deleteConfirm.body', { name: pendingDelete?.display_name })}
          </Typography>
          <Divider sx={{ mb: 2 }} />
          <Stack direction="row" spacing={1} justifyContent="flex-end">
            <Button onClick={() => setPendingDelete(null)}>{t('common.cancel')}</Button>
            <Button color="error" variant="contained" onClick={handleDelete}>
              {t('channels.delete')}
            </Button>
          </Stack>
        </Box>
      </Dialog>
    </Box>
  );
}
