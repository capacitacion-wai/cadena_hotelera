"""External channels (email + whatsapp, base for slack / outlook / teams).

This module is part of the ``wai-agent-app-builder`` SKILL. It ships the
two tables that turn the autonomous agent into a *responsive* surface:

* ``Channel``         — the per-channel configuration (which mailbox or
                        WhatsApp number, how to authenticate, how to
                        filter, how often to poll).
* ``ChannelMessage``  — the inbound message log (idempotent by
                        ``(channel_id, provider_message_id)``), linked to
                        the ``events`` row the poller emits and to the
                        ``agent_runs`` row the executor materialises.

Architecture (see ``patterns/external-channels.md``):

```
inbox/number  →  channels/poller.py  →  ChannelMessage + Event(external_message)
                                                                   │
                                                            event_machine (claim)
                                                                   │
                                                channels/executor.run_channel_message
                                                                   │
                                                              adapter.send_reply
                                                                   │
                                                                provider
```

The poller is implemented in ``app/channels/poller.py`` and the adapters
live under ``app/channels/`` (``imap_smtp.py``, ``whatsapp_twilio.py``,
``slack_bot.py``). (Email is IMAP/SMTP only — the Composio/OAuth email
adapter was retired; see ``channels/base.py::get_adapter``.) The model
carries enough state for all auth backends side-by-side; ``auth_method``
is the discriminator. No secrets
are stored in plain text — IMAP passwords and Twilio credentials are
encrypted with Fernet using the K8s Secret ``CHANNEL_ENCRYPTION_KEY``
and stored in ``imap_config_encrypted`` (a generic credentials blob
despite the historical name).
"""

from datetime import datetime
from sqlalchemy import (
    Column, DateTime, ForeignKey, Index, Integer, String, Text,
    UniqueConstraint,
)
from app.app_database import AppBase


class Channel(AppBase):
    """A configured external channel (email or whatsapp)."""
    __tablename__ = "channels"

    id = Column(Integer, primary_key=True, autoincrement=True)
    kind = Column(String, nullable=False, index=True)           # 'email' | 'whatsapp' (slack|outlook|teams in the future)
    display_name = Column(String, nullable=False)
    mailbox_address = Column(String, nullable=False, index=True)  # mailbox or E.164 WhatsApp number being watched

    # Discriminator: which auth backend handles this channel.
    auth_method = Column(String, nullable=False, index=True)     # 'imap_smtp' | 'twilio' | 'bot_token'

    # LEGACY (auth_method == 'oauth_composio'): the Composio/OAuth email adapter
    # is retired; these nullable columns are kept only so pre-existing rows still
    # load. New channels never set them.
    composio_user_id = Column(String, nullable=True)             # = autonomous_user_email of the app
    composio_toolkit = Column(String, nullable=True)             # 'gmail' | 'outlook' | …
    composio_connection_id = Column(String, nullable=True)

    # Encrypted credentials blob (Fernet token, base64 url-safe). The
    # column keeps its historical IMAP name but is auth-method generic:
    #   imap_smtp → {host, port, user, password, use_ssl, smtp_host, smtp_port, smtp_use_tls}
    #   twilio    → {account_sid, auth_token, whatsapp_number}
    imap_config_encrypted = Column(Text, nullable=True)

    # Lifecycle
    status = Column(String, nullable=False, default="pending", index=True)
    # pending      — created, not yet authorized (OAuth flow pending)
    # active       — ready to poll
    # unauthorized — provider rejected the credentials; needs reauth
    # error        — transient failures, watcher will retry with backoff
    status_reason = Column(Text, nullable=True)

    # Polling cadence + cursor (gmail historyId, IMAP UIDNEXT, …)
    poll_interval_sec = Column(Integer, nullable=False, default=60)
    cursor = Column(String, nullable=True)

    # "Process messages since" floor. The operator chooses, when creating
    # the channel, FROM WHEN the agent should start reacting to inbound
    # messages so connecting a busy mailbox does NOT replay months of
    # history through the agent:
    #   NULL          → process ALL history (explicit opt-in to the old
    #                   behaviour: "todos los mensajes").
    #   <datetime>    → only react to messages received at/after this
    #                   instant (UTC). The wizard defaults this to the
    #                   channel's creation time ("solo mensajes nuevos").
    # The poller enforces it as a hard floor on ``Message.received_at``
    # (works for every adapter); adapters additionally use it to narrow
    # the provider query on first connect (IMAP SINCE, Gmail after:,
    # Twilio DateSent) so they don't page through the whole mailbox.
    process_since = Column(DateTime, nullable=True)

    # JSON: {"from_domain_allow": ["acme.com"], "subject_regex": "...", "label": "..."}
    filter_config = Column(Text, nullable=False, default="{}")

    # 'auto' = always try to reply with reply.body from STRUCTURED_RESPONSE.
    # 'never' = ingest the message but never reply (digest / triage only).
    reply_mode = Column(String, nullable=False, default="auto")
    # JSON: {"signature": "— Bot Soporte Acme", "cc": [], "auto_mark_read": true}
    reply_config = Column(Text, nullable=False, default="{}")

    # Proactive notifications (channel used as an OUTBOUND surface for
    # autonomous runs not triggered by this channel). The OPERATOR sets
    # the recipients here — the agent never chooses "to whom", it only
    # decides "whether" and "what" via the STRUCTURED_RESPONSE.notify
    # block. Empty / no recipients ⇒ the channel never notifies.
    # JSON: {"notify_to": ["ops@acme.com"], "policy": "agent"|"critical_only"|"never", "max_per_day": 10}
    #   policy: 'agent'         — notify whenever the agent decides it's relevant (default when notify_to set)
    #           'critical_only' — only when the agent flags severity='critical'
    #           'never'         — recipients configured but notifications paused (default when notify_to empty)
    notify_config = Column(Text, nullable=False, default="{}")

    # Telemetry
    last_polled_at = Column(DateTime, nullable=True)
    last_message_at = Column(DateTime, nullable=True)
    messages_received = Column(Integer, nullable=False, default=0)
    messages_replied = Column(Integer, nullable=False, default=0)
    errors_count = Column(Integer, nullable=False, default=0)

    is_active = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime, nullable=False,
        default=datetime.utcnow, onupdate=datetime.utcnow,
    )

    __table_args__ = (
        # One channel per (kind, address): duplicate rows would double
        # the polling and the agent cost for the same inbox/number. The
        # route layer and the seeder also enforce this with a friendly
        # 409 / skip, the constraint is the last line of defence.
        UniqueConstraint(
            "kind", "mailbox_address", name="uq_channels_kind_address",
        ),
        Index("ix_channels_kind_status", "kind", "status"),
        Index("ix_channels_active_polled", "is_active", "last_polled_at"),
    )


class ChannelMessage(AppBase):
    """One inbound message from an external channel.

    Idempotency is keyed on ``(channel_id, provider_message_id)`` — Gmail
    re-delivers the same message id when a label changes, IMAP returns
    the same UID for unmoved messages, so the watcher relies on this
    uniqueness to dedupe naturally.
    """
    __tablename__ = "channel_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    channel_id = Column(
        Integer,
        ForeignKey("channels.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    provider_message_id = Column(String, nullable=False)
    provider_thread_id = Column(String, nullable=True, index=True)

    # 'inbound'      — a message received on the channel (the default,
    #                  reactive flow: poller → executor → reply).
    # 'notification' — an OUTBOUND message the platform sent proactively
    #                  on behalf of an autonomous (non-channel) run. The
    #                  synthetic ``provider_message_id`` is
    #                  ``notify:{run_id}:{channel_id}`` so the unique
    #                  constraint dedupes retries for free. Content lives
    #                  in ``reply_subject`` / ``reply_body`` /
    #                  ``reply_attachments_json``; ``sender`` holds the
    #                  recipient(s) it was addressed to.
    direction = Column(String, nullable=False, default="inbound", index=True)

    # Parsed envelope
    sender = Column(String, nullable=True, index=True)
    subject = Column(String, nullable=True)
    # Short preview (first ~2000 chars) used by the SPA message list.
    body_excerpt = Column(Text, nullable=True)
    # Full inbound body (whole quoted chain) capped at a generous safety
    # limit (MAX_INBOUND_BODY_CHARS) so the agent keeps the conversation
    # context. The executor clamps this to a context-window budget only
    # when a single mail is large enough to threaten the window.
    body_full = Column(Text, nullable=True)
    # Inbound attachments staged into the origin's workspace folder.
    # JSON list of {"filename","workspace_path","mime","size"} (paths
    # relative to /workspace); NULL when the message had none. The bytes
    # live in the shared workspace, never in this row.
    inbound_attachments_json = Column(Text, nullable=True)
    received_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    # Lifecycle
    status = Column(String, nullable=False, default="pending", index=True)
    # pending        — inserted by the poller, event still PENDING
    # filtered       — rejected by the operator's filter_config (see ``error``); no event emitted
    # processing     — executor picked it up
    # replied        — outbound message sent (reply_provider_message_id set)
    # skipped        — agent decided not to reply (reply.body empty or reply_mode='never')
    # error          — agent run or sending failed; see ``error`` for diagnosis
    event_id = Column(
        Integer, ForeignKey("events.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    run_id = Column(
        Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    reply_provider_message_id = Column(String, nullable=True)
    replied_at = Column(DateTime, nullable=True)
    # Outbound content actually sent (or attempted) — lets the SPA show
    # the full thread (inbound mail + the agent's answer) instead of
    # only the inbound half. JSON list for attachments metadata
    # ([{"filename", "url"}]) — content bytes are never persisted.
    reply_subject = Column(String, nullable=True)
    reply_body = Column(Text, nullable=True)
    reply_attachments_json = Column(Text, nullable=True)
    error = Column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "channel_id", "provider_message_id",
            name="uq_channel_message_provider",
        ),
        Index("ix_channel_messages_channel_received", "channel_id", "received_at"),
        # NOTE: do NOT add Index("ix_channel_messages_status", "status") here —
        # the ``status`` column already declares ``index=True``, which auto-creates
        # an index of that exact name. Declaring both makes ``create_all`` emit
        # CREATE INDEX twice on a fresh DB → "index ix_channel_messages_status
        # already exists" and the app fails to boot.
    )


class ChannelContact(AppBase):
    """LEGACY (superseded in v7.24.0 by the users directory). The old
    operator-managed identity map: a non-email channel handle (WhatsApp
    phone in E.164) → the HUMAN's email. Phones now live on each person's
    directory ficha (``AppUser.phones``); at boot
    ``users_directory._absorb_channel_contacts`` MOVES any remaining rows
    into the directory and deletes them. The model is kept only so that
    migration can read the table on apps evolving from older versions —
    no route or lookup uses it anymore.
    """
    __tablename__ = "channel_contacts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Normalised handle. For WhatsApp: E.164 phone WITHOUT the ``whatsapp:``
    # prefix (e.g. ``+34600111222``). Kept generic for future channels.
    handle = Column(String, nullable=False, index=True)
    channel_kind = Column(String, nullable=False, default="whatsapp", index=True)
    user_email = Column(String, nullable=False, index=True)
    display_name = Column(String, nullable=True)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime, nullable=False,
        default=datetime.utcnow, onupdate=datetime.utcnow,
    )

    __table_args__ = (
        # One handle maps to exactly one person.
        UniqueConstraint("handle", name="uq_channel_contacts_handle"),
        Index("ix_channel_contacts_kind_handle", "channel_kind", "handle"),
    )
