/**
 * External MCP connectors REST client — talks to
 * `/api/agent/mcp-connectors*`.
 *
 * These are the agent's Composio-backed MCP servers (Odoo, Gmail, …). The
 * operator authenticates the AUTONOMOUS user's account from the Operations
 * → `Conectores` (Connectors) tab so what they connect here is exactly what the agent uses
 * at runtime.
 */
import api from './api';

export interface McpConnectorField {
  name: string;
  label: string;
  required: boolean;
  secret: boolean;
  description?: string;
}

export type McpConnectorMode = 'credentials' | 'oauth';

export interface McpConnector {
  name: string;
  toolkit: string;
  display_name: string;
  auth_scheme: string | null;
  /** redirect = managed OAuth · custom_oauth = OAuth needing own app · credentials · none */
  /** 'propia' = has its own key, set at deploy · 'ninguna' = does not authenticate. */
  credential_kind?: 'propia' | 'ninguna';
  connection_mode: 'redirect' | 'custom_oauth' | 'credentials' | 'none' | null;
  /** Is there an OAuth application to manage (it signs in via OAuth and is not managed
   *    *  by Composio)? `auth_config_managed` also comes as false when there is NONE, so
   *    *  the backend resolves this and here it is only read. */
  app_oauth_propia?: boolean;
  /** Is the one in use THIS employee's own, or the workspace's? */
  app_propia_mia?: boolean;
  managed_oauth_available: boolean | null;
  mode: McpConnectorMode;
  /** Operator-once fields (OAuth app client_id/secret) for custom_oauth. */
  auth_config_fields: McpConnectorField[];
  /** Per-user fields (api key, instance URL/subdomain). */
  connection_fields: McpConnectorField[];
  /** Back-compat alias of connection_fields. */
  fields: McpConnectorField[];
  /** custom_oauth: redirect/callback URI to register in the provider's OAuth app. */
  redirect_uri?: string;
  /** custom_oauth only: whether the operator's OAuth app is registered. */
  auth_config_registered: boolean;
  connected: boolean;
  status: string | null;
  /** v7.135.0 — CUSTOM MCP (non-Composio, e.g. cluster-mcp): wired at deploy
   *  with no per-user auth. `connection_mode === 'none'`; shown as
   *    *  "conectado · sin autenticación" (connected · no authentication; no connect/disconnect), write-policy still
   *  governable. */
  custom?: boolean;
  /** v7.28.0 — EFFECTIVE write policy the app pod's proxy applies:
   *    *  'write' | 'write_with_approval' | 'read_only'. It is configured on the platform
   *    *  (agent admin); locally it can only be tightened. */
  write_policy?: 'write' | 'write_with_approval' | 'read_only';
  connected_account_id: string | null;
  /** External account bound to THIS connector (entity-per-connector). */
  account: string | null;
  auth_error?: string;
  /** The OTHER ways to connect that use credentials (Google Maps: an API key
   *    *  from the customer's Google Cloud project, instead of the Composio app, whose quota
   *    *  all its customers share and runs out without anyone here having done anything). */
  formas_alternativas?: FormaDeConexion[];
  /** Which way it is connected with today, if it is not the main one (`API_KEY`…); '' otherwise. */
  forma_propia?: string;
}

export interface FormaDeConexion {
  auth_scheme: string;
  connection_fields: McpConnectorField[];
}


export interface McpConnectorsResponse {
  available: boolean;
  autonomous_user_email: string | null;
  connectors: McpConnector[];
  error?: string | null;
}

export interface ConnectCredentialsResponse {
  mode: 'credentials';
  connected: boolean;
  status: string | null;
  connected_account_id: string | null;
}

export interface ConnectOauthResponse {
  mode: 'oauth';
  authorize_url: string;
}

export type ConnectResponse = ConnectCredentialsResponse | ConnectOauthResponse;

export type ToolPermission = 'allow' | 'ask' | 'deny';
export type WritePolicy = 'write' | 'write_with_approval' | 'read_only';

export interface McpConnectorTool {
  name: string;
  description: string;
}

export interface McpConnectorTools {
  name: string;
  toolkit: string;
  display_name: string;
  /** 'live' = discovered now · 'cache' = last-good · 'static' = known fallback · 'catalogo' · 'none'. */
  source: 'live' | 'cache' | 'static' | 'catalogo' | 'none';
  tools: McpConnectorTool[];
}

export interface McpConnectorToolsResponse {
  connectors: McpConnectorTools[];
}

// ── Connector permissions (14.8.0) — contract: docs/permisos-de-conectores.md ──
// The screen NEVER calculates a permission: it paints the platform's table (or the table of
// the platform's dry-run while editing) and sends the employee's layer to be validated.

/** A connector's mode. `inherit` = the agent's (or the employee's default) authority. */
export type ConnectorMode = 'inherit' | WritePolicy | 'custom';
/** The employee's default authority: `inherit` = the agent's. */
export type EmployeeAuthority = 'inherit' | WritePolicy;
/** Where a tool's final permission comes from (the table's vocabulary). */
export type PermissionSource = 'read' | 'inherited_agent' | 'connector_mode' | 'custom' | 'employee' | 'platform';

export interface PermissionToolRow {
  tool_id: number | null;
  name: string;
  /** opencode key, `{runtime}_{tool}`. */
  key: string;
  io_kind: 'read' | 'write' | null;
  unclassified: boolean;
  inherited: ToolPermission;
  override: ToolPermission | null;
  reason: string | null;
  /** What the AGENT allows on this tool: the ceiling of the employee. */
  agent_permission: ToolPermission;
  agent_source: PermissionSource;
  employee_permission: ToolPermission | null;
  /** The FINAL permission (agent + employee). */
  permission: ToolPermission;
  source: PermissionSource;
  opens_more_than_agent: boolean;
}

export interface PermissionConnector {
  mcp_server_id: number | null;
  name: string;
  display_name: string;
  runtime_name: string;
  /** The connector's mode in the AGENT. */
  mode: ConnectorMode;
  /** The connector's authority in the agent: the ceiling of a fixed mode in the employee. */
  authority: WritePolicy;
  authority_source: 'agent' | 'connector';
  employee_mode: ConnectorMode | null;
  tools: PermissionToolRow[];
}

export interface PermissionsTable {
  agent_id?: number;
  version_id?: number | null;
  agent_authority: WritePolicy;
  employee: { authority: EmployeeAuthority; legacy_shape: boolean } | null;
  connectors: PermissionConnector[];
  summary: { unclassified: number; opens_more_than_agent: number };
}

export interface EmployeeConnectorPolicy {
  mode: ConnectorMode;
  tools?: Record<string, ToolPermission>;
  reasons?: Record<string, string>;
}

/** The employee's layer — the same shape as the agent's, never more permissive. */
export interface EmployeePolicy {
  authority: EmployeeAuthority;
  connectors: Record<string, EmployeeConnectorPolicy>;
}

export interface EmployeePermissionsResponse {
  /** The table the employee applies (null: never loaded). */
  table: PermissionsTable | null;
  policy: EmployeePolicy;
  /** The layer shown comes from the pre-14.8.0 settings and is not saved yet. */
  converted: boolean;
  /** false = the platform could not be reached; `table` is the last known one. */
  loaded: boolean;
}

export interface PermissionError {
  code: string;
  message: string;
  connector?: string;
  tool?: string;
}

export interface PermissionsDryRun {
  valid: boolean;
  errors: PermissionError[];
  table: PermissionsTable | null;
}

/** One entry of the connection history (14.27.0). `account` holds only non-secret data. */
export interface ConnectionEvent {
  id: number;
  connector: string;
  action: 'connect' | 'change_account' | 'authorize' | 'activate' | 'disconnect';
  actor: string | null;
  account: Record<string, string> | null;
  result: string;
  detail: string | null;
  created_at: string;
}

export interface ConnectionHistory {
  events: ConnectionEvent[];
  /** false = the platform could not be reached. */
  loaded: boolean;
}

export const mcpConnectorsApi = {
  list: () =>
    api
      .get<McpConnectorsResponse>('/agent/mcp-connectors')
      .then((r) => r.data),
  connect: (
    connector: string,
    body: {
      credentials?: Record<string, string>;
      connection_fields?: Record<string, string>;
      return_origin?: string;
      force?: boolean;
      change_account?: boolean;
      /** Connect with a way that is not the main one (see `formas_alternativas`). */
      forma?: string;
    },
  ) =>
    api
      .post<ConnectResponse>(
        `/agent/mcp-connectors/${encodeURIComponent(connector)}/connect`,
        body,
      )
      .then((r) => r.data),
  /**
   * Register (operator-once) the OAuth app credentials for a custom_oauth
   * connector. Pass ``replace=true`` to overwrite credentials registered
   * earlier with wrong values — the backend deletes the stale auth_config and
   * recreates it (account/tenant-wide).
   */
  registerAuthConfig: (
    connector: string,
    credentials: Record<string, string>,
    replace = false,
  ) =>
    api
      .post<{ registered: boolean; auth_config_id: string }>(
        `/agent/mcp-connectors/${encodeURIComponent(connector)}/auth-config`,
        { credentials, replace },
      )
      .then((r) => r.data),
  disconnect: (connector: string) =>
    api
      .post<{ deleted: boolean; detail?: string }>(
        `/agent/mcp-connectors/${encodeURIComponent(connector)}/disconnect`,
      )
      .then((r) => r.data),
  /**
   * Applies a just-completed connection RIGHT NOW: mints the router session (Composio's
   *    * warm-up starts instantly) and notifies the agent's pod so that it
   *    * applies the connector without waiting for its ~2 min refresher.
   */
  activate: (connector: string) =>
    api
      .post<{ connector: string; connected: boolean; activated: boolean; nudged: boolean }>(
        `/agent/mcp-connectors/${encodeURIComponent(connector)}/activate`,
      )
      .then((r) => r.data),
  /** Live catalogue of every connector's tools (names + descriptions). */
  listTools: () =>
    api
      .get<McpConnectorToolsResponse>('/agent/mcp-connectors/tools')
      .then((r) => r.data),
  /** The table the employee applies + its own layer (asked to the platform now). */
  getPermissions: () =>
    api
      .get<EmployeePermissionsResponse>('/agent/mcp-connectors/permissions')
      .then((r) => r.data),
  /** The platform's ensayo of a DRAFT layer: the table it would give + every error. */
  dryRunPermissions: (policy: EmployeePolicy) =>
    api
      .post<PermissionsDryRun>('/agent/mcp-connectors/permissions/dry-run', { policy })
      .then((r) => r.data),
  /** Saves the layer. A rejection comes as 422 `{detail: {code, message}}`. */
  savePermissions: (policy: EmployeePolicy) =>
    api
      .put<{ table: PermissionsTable | null; policy: EmployeePolicy }>(
        '/agent/mcp-connectors/permissions',
        { policy },
      )
      .then((r) => r.data),
  /** Registers THIS employee's OAuth application. Does not touch the workspace's one. */
  setOwnAuthConfig: (connector: string, credentials: Record<string, string>) =>
    api
      .put(`/agent/mcp-connectors/${encodeURIComponent(connector)}/own-auth-config`,
           { credentials })
      .then((r) => r.data),

  /** Goes back to the workspace's one. Deletes nothing in Composio: it just stops being used. */
  clearOwnAuthConfig: (connector: string) =>
    api
      .delete(`/agent/mcp-connectors/${encodeURIComponent(connector)}/own-auth-config`)
      .then((r) => r.data),

  /** 14.27.0 — who connected, changed the account, activated or disconnected each connector (kept by the platform). */
  history: (connector?: string) =>
    api
      .get<ConnectionHistory>('/agent/mcp-connectors/history', { params: connector ? { connector } : {} })
      .then((r) => r.data),

};
