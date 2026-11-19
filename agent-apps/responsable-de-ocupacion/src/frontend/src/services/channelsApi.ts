/**
 * Channels REST client — talks to `/api/channels/*`.
 *
 * The SPA's "Canales" section uses this thin axios wrapper so the
 * Channels page is feature-flag friendly (skip the import if you don't
 * want the feature) and so the wizard / list / detail share the same
 * type definitions.
 */
import api from './api';

export type ChannelKind = 'email' | 'whatsapp' | 'slack' | 'telegram';
export type ChannelAuthMethod = 'oauth_composio' | 'imap_smtp' | 'twilio' | 'bot_token' | 'telegram_bot';
export type ChannelStatus = 'pending' | 'active' | 'unauthorized' | 'error';
export type ChannelReplyMode = 'auto' | 'never';

export interface ChannelFilterConfig {
  /** email channels — GLOBAL rules: whitelist of sender domains */
  from_domain_allow?: string[];
  /** whatsapp channels — GLOBAL rules: sender numbers (E.164, prefix allowed) */
  from_numbers_allow?: string[];
  /** slack channels — OPTIONAL whitelist of sender user ids (U…/W…) */
  from_users_allow?: string[];
  /** email/whatsapp — PEOPLE from the directory (canonical emails, filled by
   *  UserPicker; v7.24.0). WhatsApp senders are matched by resolving their
   *  number to its directory owner. */
  allow_directory_users?: string[];
  subject_regex?: string | null;
  label?: string | null;
}

export interface ChannelReplyConfig {
  signature?: string | null;
  cc?: string[];
  bcc?: string[];
  auto_mark_read?: boolean;
}

export type ChannelNotifyPolicy = 'agent' | 'critical_only' | 'never';

export interface ChannelNotifyConfig {
  /** Proactive-notification recipients (operator-set; the agent never chooses them) */
  notify_to?: string[];
  policy?: ChannelNotifyPolicy;
  /** Anti-spam cap per day (0 = unlimited) */
  max_per_day?: number;
}

export interface Channel {
  id: number;
  kind: ChannelKind;
  display_name: string;
  mailbox_address: string;
  auth_method: ChannelAuthMethod;
  composio_toolkit: string | null;
  composio_user_id: string | null;
  status: ChannelStatus;
  status_reason: string | null;
  poll_interval_sec: number;
  cursor: string | null;
  /** ISO datetime: only react to messages received at/after it; null = all history */
  process_since: string | null;
  filter_config: ChannelFilterConfig;
  reply_mode: ChannelReplyMode;
  reply_config: ChannelReplyConfig;
  notify_config: ChannelNotifyConfig;
  is_active: boolean;
  last_polled_at: string | null;
  last_message_at: string | null;
  messages_received: number;
  messages_replied: number;
  errors_count: number;
  created_at: string;
  updated_at: string;
  counters_24h?: { received: number; replied: number };
}

export interface ChannelMessage {
  id: number;
  provider_message_id: string;
  provider_thread_id: string | null;
  direction: 'inbound' | 'notification';
  sender: string | null;
  subject: string | null;
  body_excerpt: string | null;
  received_at: string;
  status: 'pending' | 'filtered' | 'processing' | 'replied' | 'skipped' | 'error';
  event_id: number | null;
  run_id: number | null;
  reply_provider_message_id: string | null;
  replied_at: string | null;
  reply_subject: string | null;
  reply_body: string | null;
  reply_attachments: { filename: string; url: string }[];
  error: string | null;
}

export interface ChannelCapabilities {
  enabled: boolean;
  allowed_kinds: ChannelKind[];
  allowed_auth_methods: ChannelAuthMethod[];
  imap_available: boolean;
  oauth_available: boolean;
  min_poll_interval_sec: number;
  defaults: Record<string, unknown>;
  supported_oauth_toolkits: string[];
  supported_imap_domains: string[];
  whatsapp_available?: boolean;
  slack_available?: boolean;
  telegram_available?: boolean;
}

export interface CreateOAuthChannelRequest {
  display_name: string;
  kind: ChannelKind;
  auth_method: 'oauth_composio';
  composio_user_id: string;
  composio_toolkit: string;
  poll_interval_sec: number;
  /** ISO datetime to start from, or null for all history (default: now) */
  process_since?: string | null;
  filter_config: ChannelFilterConfig;
  reply_mode: ChannelReplyMode;
  reply_config: ChannelReplyConfig;
  return_origin?: string;
}

export interface CreateImapChannelRequest {
  display_name: string;
  kind: ChannelKind;
  auth_method: 'imap_smtp';
  email: string;
  password: string;
  imap_host?: string;
  imap_port?: number;
  imap_use_ssl?: boolean;
  smtp_host?: string;
  smtp_port?: number;
  smtp_use_tls?: boolean;
  poll_interval_sec: number;
  /** ISO datetime to start from, or null for all history (default: now) */
  process_since?: string | null;
  filter_config: ChannelFilterConfig;
  reply_mode: ChannelReplyMode;
  reply_config: ChannelReplyConfig;
}

export interface CreateWhatsAppChannelRequest {
  display_name: string;
  kind: 'whatsapp';
  auth_method: 'twilio';
  whatsapp_number: string;
  account_sid: string;
  auth_token: string;
  poll_interval_sec: number;
  /** ISO datetime to start from, or null for all history (default: now) */
  process_since?: string | null;
  filter_config: ChannelFilterConfig;
  reply_mode: ChannelReplyMode;
  reply_config: ChannelReplyConfig;
}

export interface CreateSlackChannelRequest {
  display_name: string;
  kind: 'slack';
  auth_method: 'bot_token';
  bot_token: string;
  slack_channel_id: string;
  poll_interval_sec: number;
  /** ISO datetime to start from, or null for all history (default: now) */
  process_since?: string | null;
  filter_config: ChannelFilterConfig;
  reply_mode: ChannelReplyMode;
  reply_config: ChannelReplyConfig;
}

export interface CreateTelegramChannelRequest {
  display_name: string;
  kind: 'telegram';
  auth_method: 'telegram_bot';
  bot_token: string;  // "123456789:ABC-…" from @BotFather
  poll_interval_sec: number;
  /** ISO datetime to start from, or null for all history (default: now) */
  process_since?: string | null;
  filter_config: ChannelFilterConfig;
  reply_mode: ChannelReplyMode;
  reply_config: ChannelReplyConfig;
}

export type CreateChannelRequest =
  | CreateOAuthChannelRequest
  | CreateImapChannelRequest
  | CreateWhatsAppChannelRequest
  | CreateSlackChannelRequest
  | CreateTelegramChannelRequest;

export interface CreateChannelResponse {
  channel: Channel;
  authorize_url?: string;
  test_result?: { ok: boolean; detail?: Record<string, unknown> };
}

export interface TestChannelResponse {
  ok: boolean;
  message: string;
  detail?: Record<string, unknown>;
}

export interface ListMessagesResponse {
  total: number;
  rows: ChannelMessage[];
}

export const channelsApi = {
  capabilities: () =>
    api.get<ChannelCapabilities>('/channels/capabilities').then((r) => r.data),
  list: (params?: { kind?: string; status?: string }) =>
    api.get<{ channels: Channel[] }>('/channels', { params }).then((r) => r.data.channels),
  get: (id: number) => api.get<Channel>(`/channels/${id}`).then((r) => r.data),
  create: (body: CreateChannelRequest) =>
    api.post<CreateChannelResponse>('/channels', body).then((r) => r.data),
  patch: (id: number, body: Partial<Channel>) =>
    api.patch<Channel>(`/channels/${id}`, body).then((r) => r.data),
  delete: (id: number) =>
    api.delete<{ deleted: number }>(`/channels/${id}`).then((r) => r.data),
  authorize: (id: number, body: Record<string, unknown>) =>
    api.post<{ authorize_url?: string; reauthorized?: boolean; detail?: Record<string, unknown> }>(
      `/channels/${id}/authorize`,
      body,
    ).then((r) => r.data),
  test: (id: number) =>
    api.post<TestChannelResponse>(`/channels/${id}/test`).then((r) => r.data),
  messages: (
    id: number,
    params?: { limit?: number; offset?: number; status?: string; direction?: string },
  ) =>
    api.get<ListMessagesResponse>(`/channels/${id}/messages`, { params }).then((r) => r.data),

  // NOTE (v7.24.0): the old "contact map" (WhatsApp number → email) is gone —
  // the users directory (/api/agent/users, Usuarios page) is the ONLY identity
  // source; phones live on each person's ficha and legacy rows are absorbed
  // into it at boot.
};
