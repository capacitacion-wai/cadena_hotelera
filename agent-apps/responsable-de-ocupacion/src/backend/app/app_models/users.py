"""Managed users — the employee's own CONTACT DIRECTORY (v7.20.0).

A single directory of the people the employee knows / serves. Each entry carries
contact points (email + phones) and recognition aliases (alias + nicknames) so
the agent can resolve "manda esto a Juan" → Juan's email/number, and a ``role``
that — for anyone who logs into the app via the platform — gates the
Administración area (``admin`` vs ``member``).

Design decisions (locked with the operator):
* It is a CONTACT directory, NOT a login system — the platform owns auth. The
  ``role`` here only gates the admin AREA for those who authenticate.
* It is COMPOSED BY HAND, not mirrored from the project (v7.145.0): a new
  employee is born with exactly ONE entry, its own administrator (the deploy
  identity), and an admin adds the rest on purpose — in bulk from the project's
  users, or as a channel-only contact. Mirroring meant the directory changed
  under the operator's feet (the deploy snapshot resurrected deleted people; the
  live sync deactivated whoever left the project).
* Supersedes ``ChannelContact`` as the identity source: ``resolve_inbound_scope``
  resolves a WhatsApp phone → email via ``phones`` here (with ChannelContact
  kept as a legacy fallback).
* The agent (MCP) may create/enrich contacts but MUST NOT grant the ``admin``
  role — that is operator-only (no privilege escalation).

Lists (``phones``, ``nicknames``) are stored as JSON text (local SQLite).
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, Text, Index, text

from app.app_database import AppBase

ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"
VALID_ROLES = (ROLE_ADMIN, ROLE_MEMBER)

# Provenance of a directory entry.
SOURCE_PROJECT = "project"   # seeded/synced from the platform project
SOURCE_MANUAL = "manual"     # added by an operator in the Usuarios page
SOURCE_AGENT = "agent"       # created by the employee via MCP
SOURCE_LOGIN = "login"       # auto-registered on first login (member)
SOURCE_DEPLOY = "deploy"     # the deploy identity (anti-lockout admin) — NOT a
                             # project row, so a project ``/sync`` never touches it


class AppUser(AppBase):
    __tablename__ = "app_users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Primary identity. Nullable so a phone-only contact can exist, but the
    # directory always tries to key on email (memory/scope unify on it).
    email = Column(String, nullable=True, index=True)
    # E.164 phone numbers (JSON list) — used to resolve WhatsApp senders.
    phones_json = Column(Text, nullable=False, default="[]", server_default="[]")
    # Telegram @username (lowercased, no '@') — used to resolve Telegram senders to
    # this person (email_for_telegram), just like phones resolve WhatsApp. Nullable.
    telegram = Column(String, nullable=True, index=True)
    display_name = Column(String, nullable=True)
    alias = Column(String, nullable=True)
    # Extra names the agent should recognise (JSON list of strings).
    nicknames_json = Column(Text, nullable=False, default="[]", server_default="[]")
    # admin | member. Gates the admin area for users who log in. Operator-only.
    role = Column(String, nullable=False, default=ROLE_MEMBER, server_default=ROLE_MEMBER, index=True)
    # 1 = may decide (approve/reject) the actions the employee parks, WITHOUT being
    # an admin (v7.145.0). Deciding whether the employee acts is a business call —
    # the responsible person is often not the one who governs the employee. Admins
    # can always decide (no flag needed); this opens it to named people. A
    # channel-only contact can never carry it: decisions happen in the panel and
    # they cannot log in.
    can_approve = Column(Integer, nullable=False, default=0, server_default="0", index=True)
    source = Column(String, nullable=False, default=SOURCE_MANUAL, server_default=SOURCE_MANUAL)
    # 1 = active, 0 = deactivated (e.g. removed from the project on sync).
    active = Column(Integer, nullable=False, default=1, server_default="1", index=True)
    # Extensible bag for future attributes (no migration needed to grow).
    metadata_json = Column(Text, nullable=False, default="{}", server_default="{}")
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow,
    )

    __table_args__ = (
        Index("ix_app_users_email_active", "email", "active"),
        # One directory row per email (email is the identity key: memory, role
        # and phone→email resolution all unify on it). Partial so multiple
        # phone-only contacts (email IS NULL) remain allowed. On a FRESH SQLite
        # this is created by ``create_all``; for an app EVOLVING on an existing
        # DB, ``bootstrap_directory`` dedups + creates it best-effort (create_all
        # never ALTERs an existing table). ``upsert_directory_user`` still merges
        # by email, so this is a safety net against the concurrent-insert race.
        Index("uq_app_users_email", "email", unique=True,
              sqlite_where=text("email IS NOT NULL")),
    )
