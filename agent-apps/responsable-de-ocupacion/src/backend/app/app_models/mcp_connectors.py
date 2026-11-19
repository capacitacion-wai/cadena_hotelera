"""External MCP connector bindings (Composio), per connector.

This module is part of the ``wai-agent-app-builder`` SKILL. It ships ONE
table that records, per external MCP connector declared in the manifest
(``agent_app.mcps[]``), which Composio account the operator authorised and
the current resolution status. It is the MCP-connector analogue of the
``channels`` table for OAuth email channels.

ENTITY-PER-CONNECTOR model (see ``app.config.composio_identity``): every
connector authorises its OWN Composio connected account under the identity
``{tenant}:{autonomous_email}#{connector_name}``. The identity itself is
DERIVED (not stored) — what we persist here is the operator-visible binding
and status so:

* the Operations → Conectores page can show WHICH external account each
  connector is bound to (instead of an opaque shared identity), and
* the boot-time resolver (``GET /agent/runtime-mcp``) can report a connector
  as connected/disabled deterministically and cache its Composio
  ``server_id`` (idempotent per toolkit) to avoid re-creating it every boot.

The agent's WAI identity is unchanged; only the external Composio account
differs per connector.
"""

from datetime import datetime
from sqlalchemy import (
    Column, DateTime, Integer, String, Text, UniqueConstraint,
)
from app.app_database import AppBase


class McpConnector(AppBase):
    """Per-connector Composio binding + resolution status."""
    __tablename__ = "mcp_connectors"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Unique manifest connector name (``agent_app.mcps[].name``), e.g.
    # ``composio-gmail``. This is the connector key everywhere: it feeds the
    # per-connector Composio identity suffix and the opencode.json mcp key.
    connector_name = Column(String, nullable=False, index=True)
    toolkit = Column(String, nullable=False)                     # 'gmail' | 'googledrive' | 'googlecalendar' | …

    # The per-connector Composio identity ``{tenant}:{email}#{connector}``.
    # Stored for operator visibility / debugging; the source of truth is still
    # ``config.composio_identity(autonomous_email, connector_name)``.
    composio_user_id = Column(String, nullable=True)
    # Composio MCP server id (idempotent per toolkit). Cached so boot does not
    # call create-server every time.
    composio_server_id = Column(String, nullable=True)
    # The connected account selected for this connector (Composio side).
    composio_connection_id = Column(String, nullable=True)
    # The external account the operator authorised (e.g. the Google address),
    # for display in the Conectores page. May be NULL until OAuth completes.
    connected_account_email = Column(String, nullable=True)

    # For custom_oauth toolkits (no Composio-managed app, e.g. Kommo/Salesforce):
    # set once the operator registers the OAuth app credentials
    # (client_id/secret). NULL = the per-toolkit OAuth app is not configured yet,
    # so the UI must collect it before any account can connect.
    auth_config_registered_at = Column(DateTime, nullable=True)

    # Lifecycle: 'pending' (OAuth not finished) | 'active' | 'unauthorized'
    # (reauth needed) | 'error'.
    status = Column(String, nullable=False, default="pending", index=True)
    status_reason = Column(Text, nullable=True)

    last_resolved_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        # One binding row per connector.
        UniqueConstraint("connector_name", name="uq_mcp_connectors_name"),
        # NOTE: no Index("ix_mcp_connectors_status", "status") here — the
        # ``status`` column already has ``index=True`` (auto-creates that exact
        # index name). Declaring both makes create_all emit CREATE INDEX twice
        # on a fresh DB → "index ... already exists" and the app fails to boot.
    )
