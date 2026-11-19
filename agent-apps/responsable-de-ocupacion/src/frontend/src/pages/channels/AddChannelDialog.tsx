/**
 * Add Channel wizard.
 *
 * 4 steps:
 *   1. Choose `auth_method` (email IMAP+App Password / WhatsApp Twilio /
 *      Slack bot token). Email is IMAP/SMTP only — the Composio/OAuth email
 *      adapter was retired.
 *   2. Credentials (IMAP email + password / Twilio number + SID + token /
 *      Slack bot token + channel id).
 *   3. Filters + reply config + polling cadence.
 *   4. Confirm — the backend tests the connection before persisting.
 */
import { useEffect, useMemo, useState } from 'react';
import {
  Alert, Box, Button, Chip, Dialog, DialogActions, DialogContent, DialogTitle,
  FormControl, FormControlLabel, InputAdornment, InputLabel, MenuItem, Radio,
  RadioGroup, Select, Stack, Step, StepLabel, Stepper, Switch, TextField,
  Typography,
} from '@mui/material';
import VisibilityOffIcon from '@mui/icons-material/VisibilityOff';
import VisibilityIcon from '@mui/icons-material/Visibility';
import { useTranslation } from 'react-i18next';
import ChannelSetupHelp, { SetupKind } from './ChannelSetupHelp';

import { useAuth } from '../../auth/AuthContext';
import {
  Channel,
  ChannelAuthMethod,
  ChannelCapabilities,
  channelsApi,
  CreateChannelRequest,
} from '../../services/channelsApi';
import { extractApiError } from '../../utils/apiError';
import { isE164, parseDomains, parseNumbers } from '../../utils/channelValidation';
import UserPicker from '../../components/UserPicker';

interface Props {
  open: boolean;
  capabilities: ChannelCapabilities | null;
  onClose: () => void;
  onCreated: (channel: Channel) => void;
}

interface FormState {
  display_name: string;
  // IMAP path
  email_provider: string;      // gmail|outlook|yahoo|icloud|zoho|other
  imap_email: string;
  imap_password: string;
  imap_host: string;
  imap_port: number;
  imap_use_ssl: boolean;
  smtp_host: string;
  smtp_port: number;
  smtp_use_tls: boolean;
  // WhatsApp (Twilio) path
  wa_number: string;           // E.164 business number
  wa_account_sid: string;
  wa_auth_token: string;
  // Slack (bot token) path
  slack_bot_token: string;     // xoxb-…
  slack_channel_id: string;    // C…/G…
  slack_users: string;         // comma-separated optional user-id whitelist
  // Telegram (bot token) path
  tg_bot_token: string;        // "123456789:ABC-…" from @BotFather
  // common
  poll_interval_sec: number;
  // "process messages since" floor
  process_since_mode: 'new' | 'date' | 'all';
  process_since_date: string;  // datetime-local value when mode === 'date'
  from_domains: string;        // comma-separated input (email) — global rules
  from_numbers: string;        // comma-separated input (whatsapp, E.164) — global rules
  allow_people: string[];      // directory people (canonical emails, UserPicker)
  subject_regex: string;
  reply_mode: 'auto' | 'never';
  reply_signature: string;
}

const STEPS = ['auth', 'credentials', 'behavior', 'confirm'] as const;

// Email provider presets — pick one and the IMAP/SMTP coordinates fill in
// automatically (works even for corporate domains on Google Workspace /
// Microsoft 365 where domain-based detection can't tell). `other` = manual.
interface ProviderPreset {
  imap_host: string;
  imap_port: number;
  imap_use_ssl: boolean;
  smtp_host: string;
  smtp_port: number;
  smtp_use_tls: boolean;
}
const EMAIL_PROVIDERS: Record<string, ProviderPreset | null> = {
  gmail:   { imap_host: 'imap.gmail.com',        imap_port: 993, imap_use_ssl: true, smtp_host: 'smtp.gmail.com',        smtp_port: 587, smtp_use_tls: true },
  outlook: { imap_host: 'outlook.office365.com', imap_port: 993, imap_use_ssl: true, smtp_host: 'smtp.office365.com',    smtp_port: 587, smtp_use_tls: true },
  yahoo:   { imap_host: 'imap.mail.yahoo.com',   imap_port: 993, imap_use_ssl: true, smtp_host: 'smtp.mail.yahoo.com',   smtp_port: 587, smtp_use_tls: true },
  icloud:  { imap_host: 'imap.mail.me.com',      imap_port: 993, imap_use_ssl: true, smtp_host: 'smtp.mail.me.com',      smtp_port: 587, smtp_use_tls: true },
  zoho:    { imap_host: 'imap.zoho.com',         imap_port: 993, imap_use_ssl: true, smtp_host: 'smtp.zoho.com',         smtp_port: 587, smtp_use_tls: true },
  other:   null,
};
const EMAIL_PROVIDER_KEYS = ['gmail', 'outlook', 'yahoo', 'icloud', 'zoho', 'other'] as const;

/** Apply a provider preset to the form (autofill IMAP/SMTP coordinates). */
function withProvider(form: FormState, provider: string): FormState {
  const p = EMAIL_PROVIDERS[provider];
  if (!p) {
    // "Other" — clear preset hosts so the operator enters them manually.
    return {
      ...form,
      email_provider: provider,
      imap_host: '',
      imap_port: 993,
      imap_use_ssl: true,
      smtp_host: '',
      smtp_port: 587,
      smtp_use_tls: true,
    };
  }
  return {
    ...form,
    email_provider: provider,
    imap_host: p.imap_host,
    imap_port: p.imap_port,
    imap_use_ssl: p.imap_use_ssl,
    smtp_host: p.smtp_host,
    smtp_port: p.smtp_port,
    smtp_use_tls: p.smtp_use_tls,
  };
}

export default function AddChannelDialog({ open, capabilities, onClose, onCreated }: Props) {
  const { t } = useTranslation();
  const { user } = useAuth();
  const [step, setStep] = useState(0);
  const [helpOpen, setHelpOpen] = useState(false);
  const [authMethod, setAuthMethod] = useState<ChannelAuthMethod>('imap_smtp');
  const [form, setForm] = useState<FormState>(defaultForm(user?.email || ''));
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showPassword, setShowPassword] = useState(false);

  useEffect(() => {
    if (open) {
      setStep(0);
      setError(null);
      setShowPassword(false);
      setForm(defaultForm(user?.email || ''));
      const allowed = capabilities?.allowed_auth_methods || [];
      // Email = IMAP + App Password (OAuth/Composio for email is no longer
      // offered). Fall back to the other channel kinds when email is off.
      const def: ChannelAuthMethod = allowed.includes('imap_smtp')
        ? 'imap_smtp'
        : allowed.includes('twilio')
          ? 'twilio'
          : allowed.includes('bot_token')
            ? 'bot_token'
            : 'imap_smtp';
      setAuthMethod(def);
    }
  }, [open, capabilities, user?.email]);

  // NOTE: no early return may appear above the hooks below (React
  // #310 — "rendered more hooks than during the previous render").
  // `capabilities` is null while the parent page is still fetching;
  // derive everything defensively and bail out just before the JSX.
  const emailAllowed = (capabilities?.allowed_kinds || []).includes('email');
  const waAllowed = (capabilities?.allowed_kinds || []).includes('whatsapp');
  const slackAllowed = (capabilities?.allowed_kinds || []).includes('slack');
  const imapOk = Boolean(capabilities?.imap_available) && emailAllowed;
  const waOk = Boolean(capabilities?.whatsapp_available) && waAllowed &&
    (capabilities?.allowed_auth_methods || []).includes('twilio');
  const slackOk = Boolean(capabilities?.slack_available) && slackAllowed &&
    (capabilities?.allowed_auth_methods || []).includes('bot_token');
  const tgAllowed = (capabilities?.allowed_kinds || []).includes('telegram');
  const tgOk = Boolean(capabilities?.telegram_available) && tgAllowed &&
    (capabilities?.allowed_auth_methods || []).includes('telegram_bot');
  const isWhatsApp = authMethod === 'twilio';
  const isSlack = authMethod === 'bot_token';
  const isTelegram = authMethod === 'telegram_bot';
  const TG_TOKEN_RE = /^\d{5,}:[A-Za-z0-9_-]{30,}$/;

  const next = () => setStep((s) => Math.min(s + 1, STEPS.length - 1));
  const back = () => setStep((s) => Math.max(s - 1, 0));

  const handleSubmit = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const filter_config = isWhatsApp
        ? {
            from_numbers_allow: parseNumbers(form.from_numbers),
            allow_directory_users: form.allow_people,
          }
        : isSlack
          ? { from_users_allow: parseSlackUsers(form.slack_users) }
          : isTelegram
            ? {
                // Trust = directory people who have a Telegram @username on their
                // ficha (resolved via email_for_telegram). Usernames live on the
                // person, not here — so the wizard only picks the people.
                allow_directory_users: form.allow_people,
              }
            : {
                from_domain_allow: parseDomains(form.from_domains),
                allow_directory_users: form.allow_people,
                subject_regex: form.subject_regex || null,
              };
      const reply_config = {
        signature: form.reply_signature || null,
        auto_mark_read: true,
      };
      const process_since = resolveProcessSince(form.process_since_mode, form.process_since_date);
      let body: CreateChannelRequest;
      if (authMethod === 'twilio') {
        body = {
          display_name: form.display_name,
          kind: 'whatsapp',
          auth_method: 'twilio',
          whatsapp_number: form.wa_number.trim(),
          account_sid: form.wa_account_sid.trim(),
          auth_token: form.wa_auth_token,
          poll_interval_sec: form.poll_interval_sec,
          process_since,
          filter_config,
          reply_mode: form.reply_mode,
          reply_config,
        };
      } else if (authMethod === 'bot_token') {
        body = {
          display_name: form.display_name,
          kind: 'slack',
          auth_method: 'bot_token',
          bot_token: form.slack_bot_token.trim(),
          slack_channel_id: form.slack_channel_id.trim(),
          poll_interval_sec: form.poll_interval_sec,
          process_since,
          filter_config,
          reply_mode: form.reply_mode,
          reply_config,
        };
      } else if (authMethod === 'telegram_bot') {
        body = {
          display_name: form.display_name,
          kind: 'telegram',
          auth_method: 'telegram_bot',
          bot_token: form.tg_bot_token.trim(),
          poll_interval_sec: form.poll_interval_sec,
          process_since,
          filter_config,
          reply_mode: form.reply_mode,
          reply_config,
        };
      } else {
        body = {
          display_name: form.display_name,
          kind: 'email',
          auth_method: 'imap_smtp',
          email: form.imap_email,
          password: form.imap_password,
          imap_host: form.imap_host || undefined,
          imap_port: form.imap_port || undefined,
          imap_use_ssl: form.imap_use_ssl,
          smtp_host: form.smtp_host || undefined,
          smtp_port: form.smtp_port || undefined,
          smtp_use_tls: form.smtp_use_tls,
          poll_interval_sec: form.poll_interval_sec,
          process_since,
          filter_config,
          reply_mode: form.reply_mode,
          reply_config,
        };
      }
      const res = await channelsApi.create(body);
      onCreated(res.channel);
    } catch (exc: unknown) {
      setError(extractApiError(exc));
    } finally {
      setSubmitting(false);
    }
  };

  const canContinue = useMemo(() => {
    if (step === 0) {
      return (authMethod === 'imap_smtp' && imapOk) ||
             (authMethod === 'twilio' && waOk) ||
             (authMethod === 'bot_token' && slackOk) ||
             (authMethod === 'telegram_bot' && tgOk);
    }
    if (step === 1) {
      if (authMethod === 'twilio') {
        return (
          isE164(form.wa_number) &&
          form.wa_account_sid.trim().length > 0 &&
          form.wa_auth_token.length > 0 &&
          form.display_name.trim().length > 0
        );
      }
      if (authMethod === 'bot_token') {
        return (
          form.slack_bot_token.trim().startsWith('xoxb-') &&
          /^[CGD][A-Z0-9]{6,}$/.test(form.slack_channel_id.trim()) &&
          form.display_name.trim().length > 0
        );
      }
      if (authMethod === 'telegram_bot') {
        return (
          TG_TOKEN_RE.test(form.tg_bot_token.trim()) &&
          form.display_name.trim().length > 0
        );
      }
      return (
        form.imap_email.includes('@') &&
        form.imap_password.length > 0 &&
        form.display_name.trim().length > 0 &&
        (form.email_provider !== 'other' ||
          (form.imap_host.trim().length > 0 && form.smtp_host.trim().length > 0))
      );
    }
    if (step === 2) {
      if (form.process_since_mode === 'date' &&
          Number.isNaN(new Date(form.process_since_date).getTime())) {
        return false;
      }
      if (isSlack) return true; // the watched channel is the trust boundary
      // Telegram: a bot has no membership boundary → require directory people
      // (those carry the @telegram handle used to resolve the sender).
      if (isTelegram) return form.allow_people.length > 0;
      // Whitelist required: global rules AND/OR directory people (v7.24.0).
      if (form.allow_people.length > 0) return true;
      return isWhatsApp
        ? form.from_numbers.trim().length > 0
        : form.from_domains.trim().length > 0;
    }
    return true;
  }, [step, authMethod, imapOk, waOk, slackOk, tgOk, isWhatsApp, isSlack, isTelegram, form]);

  // Early return AFTER every hook so the hook count stays stable
  // between the "capabilities still loading" and "loaded" renders.
  if (!capabilities) return null;

  return (
    <Dialog open={open} onClose={submitting ? undefined : onClose} maxWidth="md" fullWidth>
      <DialogTitle sx={{ pb: 1 }}>
        {t('channels.wizard.title')}
        <Typography variant="caption" color="text.secondary" display="block">
          {t('channels.wizard.subtitle')}
        </Typography>
      </DialogTitle>
      <DialogContent>
        <Stepper activeStep={step} sx={{ my: 2 }}>
          {STEPS.map((s) => (
            <Step key={s}>
              <StepLabel>{t(`channels.wizard.steps.${s}`)}</StepLabel>
            </Step>
          ))}
        </Stepper>

        {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}

        {step === 0 && (
          <Stack spacing={2}>
            <Typography variant="body2" color="text.secondary">
              {t('channels.wizard.auth.explainer')}
            </Typography>
            <RadioGroup value={authMethod} onChange={(e) => setAuthMethod(e.target.value as ChannelAuthMethod)}>
              <Box sx={{ display: 'grid', gridTemplateColumns: { md: '1fr 1fr' }, gap: 2 }}>
                <AuthOptionCard
                  value="imap_smtp"
                  selected={authMethod === 'imap_smtp'}
                  disabled={!imapOk}
                  title={t('channels.auth.imap_smtp')}
                  pros={[
                    t('channels.wizard.auth.imap.pros1'),
                    t('channels.wizard.auth.imap.pros2'),
                  ]}
                  cons={[
                    t('channels.wizard.auth.imap.cons1'),
                    t('channels.wizard.auth.imap.cons2'),
                  ]}
                  disabledMessage={!imapOk ? (t('channels.wizard.auth.imap.disabled') as string) : undefined}
                />
                <AuthOptionCard
                  value="twilio"
                  selected={authMethod === 'twilio'}
                  disabled={!waOk}
                  title={t('channels.auth.twilio')}
                  pros={[
                    t('channels.wizard.auth.twilio.pros1'),
                    t('channels.wizard.auth.twilio.pros2'),
                  ]}
                  cons={[t('channels.wizard.auth.twilio.cons1')]}
                  disabledMessage={!waOk ? (t('channels.wizard.auth.twilio.disabled') as string) : undefined}
                />
                <AuthOptionCard
                  value="bot_token"
                  selected={authMethod === 'bot_token'}
                  disabled={!slackOk}
                  title={t('channels.auth.bot_token')}
                  pros={[
                    t('channels.wizard.auth.slack.pros1'),
                    t('channels.wizard.auth.slack.pros2'),
                    t('channels.wizard.auth.slack.pros3'),
                  ]}
                  cons={[t('channels.wizard.auth.slack.cons1')]}
                  disabledMessage={!slackOk ? (t('channels.wizard.auth.slack.disabled') as string) : undefined}
                />
                <AuthOptionCard
                  value="telegram_bot"
                  selected={authMethod === 'telegram_bot'}
                  disabled={!tgOk}
                  title={t('channels.auth.telegram_bot', 'Telegram (bot)') as string}
                  pros={[
                    t('channels.wizard.auth.telegram.pros1', 'Recibe mensajes, notas de voz (transcritas) y ficheros') as string,
                    t('channels.wizard.auth.telegram.pros2', 'Solo necesitas un bot de @BotFather (un token)') as string,
                    t('channels.wizard.auth.telegram.pros3', 'Sin webhooks ni configuración pública') as string,
                  ]}
                  cons={[t('channels.wizard.auth.telegram.cons1', 'El usuario debe escribir primero al bot') as string]}
                  disabledMessage={!tgOk ? (t('channels.wizard.auth.telegram.disabled', 'No disponible (falta CHANNEL_ENCRYPTION_KEY o no está habilitado)') as string) : undefined}
                />
              </Box>
            </RadioGroup>
          </Stack>
        )}

        {step === 1 && (
          <Stack spacing={2}>
            {/* v7.156.0 — whoever fills this in usually doesn't know where a token, a SID
                or an IMAP host comes from: the wizard asked for them without saying how
                to get them. The guide covers BOTH halves (the external system and WAI)
                and warns about the conditions that catch people out later. */}
            <Alert
              severity="info"
              action={
                <Button size="small" onClick={() => setHelpOpen(true)} sx={{ textTransform: 'none' }}>
                  {t('channels.help.open', 'Ver guía paso a paso')}
                </Button>
              }
            >
              {t('channels.help.teaser', '¿No sabes de dónde sacar estos datos? Te lo explicamos paso a paso, sin tecnicismos.')}
            </Alert>
            <TextField
              label={t('channels.wizard.fields.displayName')}
              value={form.display_name}
              onChange={(e) => setForm({ ...form, display_name: e.target.value })}
              fullWidth
              required
              helperText={t('channels.wizard.fields.displayNameHelp')}
            />
            {authMethod === 'twilio' ? (
              <>
                <TextField
                  label={t('channels.wizard.fields.waNumber')}
                  value={form.wa_number}
                  onChange={(e) => setForm({ ...form, wa_number: e.target.value.trim() })}
                  fullWidth
                  required
                  placeholder="+34600111222"
                  helperText={t('channels.wizard.fields.waNumberHelp')}
                />
                <TextField
                  label={t('channels.wizard.fields.waAccountSid')}
                  value={form.wa_account_sid}
                  onChange={(e) => setForm({ ...form, wa_account_sid: e.target.value.trim() })}
                  fullWidth
                  required
                  placeholder="AC..."
                />
                <TextField
                  label={t('channels.wizard.fields.waAuthToken')}
                  type={showPassword ? 'text' : 'password'}
                  value={form.wa_auth_token}
                  onChange={(e) => setForm({ ...form, wa_auth_token: e.target.value })}
                  fullWidth
                  required
                  helperText={t('channels.wizard.fields.waAuthTokenHelp')}
                  InputProps={{
                    endAdornment: (
                      <InputAdornment position="end">
                        <Button
                          size="small"
                          startIcon={showPassword ? <VisibilityOffIcon /> : <VisibilityIcon />}
                          onClick={() => setShowPassword((s) => !s)}
                        >
                          {showPassword ? t('channels.wizard.fields.hide') : t('channels.wizard.fields.show')}
                        </Button>
                      </InputAdornment>
                    ),
                  }}
                />
                <Alert severity="warning">{t('channels.wizard.auth.twilio.tokenHint')}</Alert>
              </>
            ) : authMethod === 'bot_token' ? (
              <>
                <TextField
                  label={t('channels.wizard.fields.slackBotToken')}
                  type={showPassword ? 'text' : 'password'}
                  value={form.slack_bot_token}
                  onChange={(e) => setForm({ ...form, slack_bot_token: e.target.value.trim() })}
                  fullWidth
                  required
                  placeholder="xoxb-..."
                  helperText={t('channels.wizard.fields.slackBotTokenHelp')}
                  InputProps={{
                    endAdornment: (
                      <InputAdornment position="end">
                        <Button
                          size="small"
                          startIcon={showPassword ? <VisibilityOffIcon /> : <VisibilityIcon />}
                          onClick={() => setShowPassword((s) => !s)}
                        >
                          {showPassword ? t('channels.wizard.fields.hide') : t('channels.wizard.fields.show')}
                        </Button>
                      </InputAdornment>
                    ),
                  }}
                />
                <TextField
                  label={t('channels.wizard.fields.slackChannelId')}
                  value={form.slack_channel_id}
                  onChange={(e) => setForm({ ...form, slack_channel_id: e.target.value.trim() })}
                  fullWidth
                  required
                  placeholder="C0123ABCD"
                  helperText={t('channels.wizard.fields.slackChannelIdHelp')}
                />
                <Alert severity="info">{t('channels.wizard.auth.slack.setup')}</Alert>
              </>
            ) : authMethod === 'telegram_bot' ? (
              <>
                <TextField
                  label={t('channels.wizard.fields.tgBotToken', 'Token del bot (@BotFather)') as string}
                  type={showPassword ? 'text' : 'password'}
                  value={form.tg_bot_token}
                  onChange={(e) => setForm({ ...form, tg_bot_token: e.target.value.trim() })}
                  fullWidth
                  required
                  placeholder="123456789:ABCdef..."
                  helperText={t('channels.wizard.fields.tgBotTokenHelp', 'Créalo en Telegram con @BotFather (/newbot) y pega aquí el token.') as string}
                  InputProps={{
                    endAdornment: (
                      <InputAdornment position="end">
                        <Button
                          size="small"
                          startIcon={showPassword ? <VisibilityOffIcon /> : <VisibilityIcon />}
                          onClick={() => setShowPassword((s) => !s)}
                        >
                          {showPassword ? t('channels.wizard.fields.hide') : t('channels.wizard.fields.show')}
                        </Button>
                      </InputAdornment>
                    ),
                  }}
                />
                <Alert severity="info">
                  {t('channels.wizard.auth.telegram.setup', 'Los usuarios deben ESCRIBIR primero al bot para poder recibir respuestas. Autorízalos dando de alta su @usuario de Telegram en la ficha de la persona (sección Usuarios).') as string}
                </Alert>
              </>
            ) : (
              <>
                <FormControl fullWidth>
                  <InputLabel>{t('channels.wizard.fields.provider')}</InputLabel>
                  <Select
                    value={form.email_provider}
                    label={t('channels.wizard.fields.provider')}
                    onChange={(e) => setForm(withProvider(form, String(e.target.value)))}
                  >
                    {EMAIL_PROVIDER_KEYS.map((p) => (
                      <MenuItem key={p} value={p}>{t(`channels.providers.${p}`)}</MenuItem>
                    ))}
                  </Select>
                </FormControl>
                <TextField
                  label={t('channels.wizard.fields.email')}
                  type="email"
                  value={form.imap_email}
                  onChange={(e) => setForm({ ...form, imap_email: e.target.value.trim().toLowerCase() })}
                  fullWidth
                  required
                />
                <TextField
                  label={t('channels.wizard.fields.password')}
                  type={showPassword ? 'text' : 'password'}
                  value={form.imap_password}
                  onChange={(e) => setForm({ ...form, imap_password: e.target.value })}
                  fullWidth
                  required
                  helperText={t(`channels.wizard.fields.passwordHelp.${form.email_provider}`)}
                  InputProps={{
                    endAdornment: (
                      <InputAdornment position="end">
                        <Button
                          size="small"
                          startIcon={showPassword ? <VisibilityOffIcon /> : <VisibilityIcon />}
                          onClick={() => setShowPassword((s) => !s)}
                        >
                          {showPassword ? t('channels.wizard.fields.hide') : t('channels.wizard.fields.show')}
                        </Button>
                      </InputAdornment>
                    ),
                  }}
                />
                {form.email_provider === 'other' ? (
                  <ImapAdvancedFields form={form} setForm={setForm} />
                ) : (
                  <Alert severity="success" icon={false}>
                    {t('channels.wizard.fields.serversAuto', {
                      imap: `${form.imap_host}:${form.imap_port}`,
                      smtp: `${form.smtp_host}:${form.smtp_port}`,
                    })}
                  </Alert>
                )}
                <Alert severity="warning">
                  {t(`channels.wizard.fields.passwordHelp.${form.email_provider}`)}
                </Alert>
              </>
            )}
          </Stack>
        )}

        {step === 2 && (
          <Stack spacing={2}>
            <Typography variant="subtitle1">{t('channels.wizard.behavior.title')}</Typography>
            {isWhatsApp ? (
              <>
                <UserPicker kind="email" multiple
                  label={t('channels.allow.people', 'Personas que pueden escribir (directorio)')}
                  helperText={t('channels.allow.peopleHelpWhatsapp', 'Se reconocen por sus teléfonos de la ficha de Usuarios; si alguien no tiene ficha con email, usa las reglas de números.')}
                  value={form.allow_people}
                  onChange={(v) => setForm({ ...form, allow_people: v })}
                />
                <TextField
                  label={t('channels.wizard.fields.fromNumbers')}
                  value={form.from_numbers}
                  onChange={(e) => setForm({ ...form, from_numbers: e.target.value })}
                  fullWidth
                  placeholder="+34600111222, +34"
                  helperText={t('channels.wizard.fields.fromNumbersHelp')}
                />
              </>
            ) : isSlack ? (
              <TextField
                label={t('channels.wizard.fields.slackUsers')}
                value={form.slack_users}
                onChange={(e) => setForm({ ...form, slack_users: e.target.value })}
                fullWidth
                placeholder="U0123ABCD, U0456WXYZ"
                helperText={t('channels.wizard.fields.slackUsersHelp')}
              />
            ) : isTelegram ? (
              <UserPicker kind="email" multiple
                label={t('channels.allow.peopleTelegram', 'Personas que pueden escribir al bot (directorio)') as string}
                helperText={t('channels.allow.peopleHelpTelegram', 'Solo estas personas podrán hablar con el bot. Cada una debe tener su @usuario de Telegram en su ficha (sección Usuarios) para que se le reconozca.') as string}
                value={form.allow_people}
                onChange={(v) => setForm({ ...form, allow_people: v })}
              />
            ) : (
              <>
                <UserPicker kind="email" multiple
                  label={t('channels.allow.people', 'Personas que pueden escribir (directorio)')}
                  helperText={t('channels.allow.peopleHelpEmail', 'Además de las personas elegidas, las reglas de dominio admiten remitentes de forma global.')}
                  value={form.allow_people}
                  onChange={(v) => setForm({ ...form, allow_people: v })}
                />
                <TextField
                  label={t('channels.wizard.fields.fromDomains')}
                  value={form.from_domains}
                  onChange={(e) => setForm({ ...form, from_domains: e.target.value })}
                  fullWidth
                  helperText={t('channels.wizard.fields.fromDomainsHelp')}
                />
                <TextField
                  label={t('channels.wizard.fields.subjectRegex')}
                  value={form.subject_regex}
                  onChange={(e) => setForm({ ...form, subject_regex: e.target.value })}
                  fullWidth
                  helperText={t('channels.wizard.fields.subjectRegexHelp')}
                />
              </>
            )}
            <Box sx={{ display: 'flex', gap: 2, alignItems: 'center' }}>
              <TextField
                label={t('channels.wizard.fields.pollInterval')}
                type="number"
                value={form.poll_interval_sec}
                onChange={(e) => setForm({ ...form, poll_interval_sec: Number(e.target.value) || 60 })}
                inputProps={{ min: capabilities.min_poll_interval_sec, max: 3600, step: 30 }}
                helperText={t('channels.wizard.fields.pollIntervalHelp', { min: capabilities.min_poll_interval_sec })}
                sx={{ flex: 1 }}
              />
              <FormControl sx={{ flex: 1 }}>
                <InputLabel>{t('channels.wizard.fields.replyMode')}</InputLabel>
                <Select
                  value={form.reply_mode}
                  label={t('channels.wizard.fields.replyMode')}
                  onChange={(e) => setForm({ ...form, reply_mode: e.target.value as 'auto' | 'never' })}
                >
                  <MenuItem value="auto">{t('channels.replyMode.auto')}</MenuItem>
                  <MenuItem value="never">{t('channels.replyMode.never')}</MenuItem>
                </Select>
              </FormControl>
            </Box>
            <Box>
              <Typography variant="subtitle2" sx={{ mb: 0.5 }}>
                {t('channels.wizard.fields.processSince')}
              </Typography>
              <Typography variant="caption" color="text.secondary" display="block" sx={{ mb: 1 }}>
                {t('channels.wizard.fields.processSinceHelp')}
              </Typography>
              <RadioGroup
                value={form.process_since_mode}
                onChange={(e) => setForm({ ...form, process_since_mode: e.target.value as FormState['process_since_mode'] })}
              >
                <FormControlLabel
                  value="new"
                  control={<Radio />}
                  label={t('channels.wizard.fields.processSinceNew')}
                />
                <FormControlLabel
                  value="date"
                  control={<Radio />}
                  label={t('channels.wizard.fields.processSinceDate')}
                />
                {form.process_since_mode === 'date' && (
                  <TextField
                    type="datetime-local"
                    size="small"
                    value={form.process_since_date}
                    onChange={(e) => setForm({ ...form, process_since_date: e.target.value })}
                    sx={{ ml: 4, mb: 1, maxWidth: 280 }}
                    InputLabelProps={{ shrink: true }}
                  />
                )}
                <FormControlLabel
                  value="all"
                  control={<Radio />}
                  label={t('channels.wizard.fields.processSinceAll')}
                />
              </RadioGroup>
            </Box>
            {!isWhatsApp && !isSlack && !isTelegram && (
              <TextField
                label={t('channels.wizard.fields.signature')}
                value={form.reply_signature}
                onChange={(e) => setForm({ ...form, reply_signature: e.target.value })}
                fullWidth
                multiline
                minRows={2}
                helperText={t('channels.wizard.fields.signatureHelp')}
              />
            )}
          </Stack>
        )}

        {step === 3 && (
          <Stack spacing={2}>
            <Typography variant="subtitle1">{t('channels.wizard.confirm.title')}</Typography>
            <Alert severity="info">
              {authMethod === 'twilio'
                ? t('channels.wizard.confirm.twilio')
                : authMethod === 'bot_token'
                  ? t('channels.wizard.confirm.slack')
                  : authMethod === 'telegram_bot'
                    ? t('channels.wizard.confirm.telegram', 'Se conectará el bot de Telegram y empezará a escuchar los mensajes que le escriban las personas autorizadas.')
                    : t('channels.wizard.confirm.imap')}
            </Alert>
            <SummaryRow label={t('channels.wizard.fields.displayName')} value={form.display_name} />
            <SummaryRow
              label={t('channels.wizard.confirm.authentication')}
              value={t(`channels.auth.${authMethod}`) as string}
            />
            <SummaryRow
              label={isSlack
                ? (t('channels.wizard.confirm.slackChannel') as string)
                : (t('channels.wizard.confirm.mailbox') as string)}
              value={
                authMethod === 'twilio'
                  ? form.wa_number
                  : authMethod === 'bot_token'
                    ? form.slack_channel_id
                    : authMethod === 'telegram_bot'
                      ? (t('channels.wizard.confirm.tgBot', 'Bot de Telegram (@usuario al conectar)') as string)
                      : form.imap_email
              }
            />
            <SummaryRow
              label={isWhatsApp
                ? (t('channels.wizard.fields.fromNumbers') as string)
                : isSlack
                  ? (t('channels.wizard.fields.slackUsers') as string)
                  : (t('channels.allow.people', 'Personas autorizadas') as string)}
              value={
                isWhatsApp ? (form.from_numbers || '—')
                  : isSlack ? (form.slack_users || (t('channels.wizard.fields.slackUsersAll') as string))
                    : (isTelegram || !isWhatsApp)
                      ? (form.allow_people.length
                          ? `${form.allow_people.length} ${t('channels.allow.peopleCount', 'personas') as string}`
                          : (form.from_domains || '—'))
                      : '—'
              }
            />
            <SummaryRow
              label={t('channels.wizard.fields.replyMode')}
              value={t(`channels.replyMode.${form.reply_mode}`) as string}
            />
            <SummaryRow
              label={t('channels.wizard.fields.pollInterval')}
              value={`${form.poll_interval_sec}s`}
            />
            <SummaryRow
              label={t('channels.wizard.fields.processSince')}
              value={
                form.process_since_mode === 'all'
                  ? (t('channels.wizard.fields.processSinceAll') as string)
                  : form.process_since_mode === 'date'
                    ? new Date(form.process_since_date).toLocaleString()
                    : (t('channels.wizard.fields.processSinceNew') as string)
              }
            />
          </Stack>
        )}
      </DialogContent>
      <DialogActions sx={{ p: 2 }}>
        <Button onClick={onClose} disabled={submitting}>{t('common.cancel')}</Button>
        <Box sx={{ flex: 1 }} />
        {step > 0 && (
          <Button onClick={back} disabled={submitting}>{t('common.back')}</Button>
        )}
        {step < STEPS.length - 1 ? (
          <Button variant="contained" onClick={next} disabled={!canContinue}>
            {t('common.next')}
          </Button>
        ) : (
          <Button variant="contained" onClick={handleSubmit} disabled={submitting || !canContinue}>
            {submitting ? t('common.loading') : t('channels.wizard.confirm.create')}
          </Button>
        )}
      </DialogActions>
      <ChannelSetupHelp
        kind={authMethod as SetupKind}
        open={helpOpen}
        onClose={() => setHelpOpen(false)}
      />
    </Dialog>
  );
}


function defaultForm(_userEmail: string): FormState {
  return {
    display_name: '',
    email_provider: 'gmail',
    imap_email: '',
    imap_password: '',
    imap_host: 'imap.gmail.com',
    imap_port: 993,
    imap_use_ssl: true,
    smtp_host: 'smtp.gmail.com',
    smtp_port: 587,
    smtp_use_tls: true,
    wa_number: '',
    wa_account_sid: '',
    wa_auth_token: '',
    slack_bot_token: '',
    slack_channel_id: '',
    slack_users: '',
    tg_bot_token: '',
    poll_interval_sec: 60,
    process_since_mode: 'new',
    process_since_date: defaultSinceDate(),
    from_domains: '',
    from_numbers: '',
    allow_people: [],
    subject_regex: '',
    reply_mode: 'auto',
    reply_signature: '',
  };
}


/** datetime-local default = now, formatted as YYYY-MM-DDTHH:mm in local time. */
function defaultSinceDate(): string {
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}


/** Parse a comma/space-separated list of Slack user ids into a clean,
 *  upper-cased, deduped array (e.g. "u0123abcd, U0456" → ["U0123ABCD","U0456"]). */
function parseSlackUsers(raw: string): string[] {
  return Array.from(new Set(
    (raw || '')
      .split(/[\s,;]+/)
      .map((s) => s.trim().toUpperCase())
      .filter(Boolean),
  ));
}


/** Resolve the form's "process since" choice into the API value. */
function resolveProcessSince(mode: 'new' | 'date' | 'all', localValue: string): string | null {
  if (mode === 'all') return null;
  if (mode === 'date') {
    const d = new Date(localValue);
    return Number.isNaN(d.getTime()) ? new Date().toISOString() : d.toISOString();
  }
  return new Date().toISOString();
}


interface AuthOptionCardProps {
  value: ChannelAuthMethod;
  selected: boolean;
  disabled: boolean;
  title: string;
  badge?: string;
  pros: string[];
  cons: string[];
  disabledMessage?: string;
}

function AuthOptionCard(props: AuthOptionCardProps) {
  const { value, selected, disabled, title, badge, pros, cons, disabledMessage } = props;
  return (
    <Box
      sx={{
        p: 2,
        borderRadius: 2,
        border: 2,
        borderColor: selected ? 'primary.main' : 'divider',
        background: selected ? 'action.selected' : 'background.paper',
        opacity: disabled ? 0.55 : 1,
        cursor: disabled ? 'not-allowed' : 'pointer',
        position: 'relative',
      }}
    >
      <FormControlLabel
        value={value}
        control={<Radio disabled={disabled} />}
        label={
          <Stack direction="row" spacing={1} alignItems="center">
            <Typography variant="subtitle1" sx={{ fontWeight: 700 }}>{title}</Typography>
            {badge && <Chip label={badge} color="primary" size="small" />}
          </Stack>
        }
      />
      <Box sx={{ pl: 4 }}>
        {pros.map((p) => (
          <Typography key={p} variant="body2" color="success.main" sx={{ fontSize: '0.85rem' }}>
            + {p}
          </Typography>
        ))}
        {cons.map((c) => (
          <Typography key={c} variant="body2" color="text.secondary" sx={{ fontSize: '0.85rem' }}>
            − {c}
          </Typography>
        ))}
        {disabled && disabledMessage && (
          <Alert severity="warning" sx={{ mt: 1 }} icon={false}>
            {disabledMessage}
          </Alert>
        )}
      </Box>
    </Box>
  );
}


function SummaryRow({ label, value }: { label: string; value: string }) {
  return (
    <Box sx={{ display: 'grid', gridTemplateColumns: '160px 1fr', gap: 1, alignItems: 'baseline' }}>
      <Typography variant="caption" color="text.secondary">{label}</Typography>
      <Typography variant="body2" sx={{ fontWeight: 600 }}>{value}</Typography>
    </Box>
  );
}


function ImapAdvancedFields({
  form, setForm,
}: { form: FormState; setForm: (f: FormState) => void }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  return (
    <Box>
      <Button size="small" onClick={() => setOpen((o) => !o)}>
        {open
          ? `↓ ${t('channels.imapAdvanced.hide', 'Ocultar avanzado')}`
          : `↗ ${t('channels.imapAdvanced.show', 'Mostrar avanzado')}`}
      </Button>
      {open && (
        <Stack spacing={1.5} sx={{ mt: 1 }}>
          <Box sx={{ display: 'flex', gap: 1 }}>
            <TextField
              label={t('channels.imapAdvanced.imapHost', 'Servidor IMAP')}
              size="small"
              value={form.imap_host}
              onChange={(e) => setForm({ ...form, imap_host: e.target.value })}
              sx={{ flex: 2 }}
              placeholder="imap.gmail.com"
            />
            <TextField
              label={t('channels.imapAdvanced.port', 'Puerto')}
              size="small"
              type="number"
              value={form.imap_port}
              onChange={(e) => setForm({ ...form, imap_port: Number(e.target.value) || 993 })}
              sx={{ flex: 1 }}
            />
            <FormControlLabel
              control={<Switch checked={form.imap_use_ssl} onChange={(e) => setForm({ ...form, imap_use_ssl: e.target.checked })} />}
              label={t('channels.imapAdvanced.ssl', 'SSL')}
            />
          </Box>
          <Box sx={{ display: 'flex', gap: 1 }}>
            <TextField
              label={t('channels.imapAdvanced.smtpHost', 'Servidor SMTP')}
              size="small"
              value={form.smtp_host}
              onChange={(e) => setForm({ ...form, smtp_host: e.target.value })}
              sx={{ flex: 2 }}
              placeholder="smtp.gmail.com"
            />
            <TextField
              label={t('channels.imapAdvanced.port', 'Puerto')}
              size="small"
              type="number"
              value={form.smtp_port}
              onChange={(e) => setForm({ ...form, smtp_port: Number(e.target.value) || 587 })}
              sx={{ flex: 1 }}
            />
            <FormControlLabel
              control={<Switch checked={form.smtp_use_tls} onChange={(e) => setForm({ ...form, smtp_use_tls: e.target.checked })} />}
              label={t('channels.imapAdvanced.tls', 'TLS')}
            />
          </Box>
        </Stack>
      )}
    </Box>
  );
}
