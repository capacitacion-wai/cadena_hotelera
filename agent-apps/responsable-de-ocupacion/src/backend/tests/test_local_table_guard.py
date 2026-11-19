"""v7.85.0 — the local.read/local.write TABLE GUARD (prompt-injection hardening).

The generic escape hatch must reach ONLY declared domain/business tables, never an
internal platform table (memory, settings, approvals, grants, channels, audit).
Without this a prompt-injection could read another user's memory or forge an
approval / self-escalate a write grant via raw SQL.
"""
from __future__ import annotations

import pytest

from fastapi import HTTPException


def test_guard_allows_canonical_business_tables() -> None:
    from app.mcp_server import _guard_local_table
    # The 6 canonical business output tables the named tools own are always OK.
    for t in ("alerts", "recommendations", "communications", "scoring",
              "decisions", "scheduled_items"):
        _guard_local_table(t)  # must not raise


def test_guard_blocks_internal_platform_tables() -> None:
    from app.mcp_server import _guard_local_table
    # Memory, governance, guardrails, channels/PII, knowledge, audit — all denied,
    # even case-insensitively, so no `local.read {table:"user_memory"}` etc.
    for t in ("user_memory", "user_episodes", "user_files", "app_settings",
              "approvals", "write_grants", "action_overrides", "automations",
              "channel_messages", "channel_contacts", "app_users",
              "knowledge_documents", "scoped_knowledge", "agent_runs",
              "tool_call_log", "USER_MEMORY", "Approvals",
              # v7.140.0 — the employee's own learning and its audit trail.
              # Reachable via raw SQL, a prompt-injection could forge an
              # "approved" learning effect (self-granting a silence) or erase the
              # log of what it silenced. Both go through governed tools only.
              "agent_memory", "agent_episodes", "learning_effects",
              "signal_silence_log", "signal_suppressions",
              "automation_daily_stats", "automation_eval_log",
              "AGENT_MEMORY", "Learning_Effects"):
        with pytest.raises(HTTPException) as ei:
            _guard_local_table(t)
        assert ei.value.status_code == 403


def test_guard_blocks_undeclared_tables() -> None:
    from app.mcp_server import _guard_local_table
    # A table that is neither a declared domain entity nor canonical is rejected
    # (allowlist, not just denylist) — the fixture manifest declares no local entities.
    with pytest.raises(HTTPException) as ei:
        _guard_local_table("some_random_table")
    assert ei.value.status_code == 403


def test_allowed_set_includes_declared_local_domain_entities(monkeypatch) -> None:
    from app import mcp_server
    from app.config import AGENT_APP
    # Inject a declared local domain entity and confirm it becomes allowed while a
    # denylisted name stays blocked even if (maliciously) declared.
    monkeypatch.setitem(AGENT_APP, "domain_entities", [
        {"slug": "draft_orders", "table": "draft_orders", "source": "local"},
        {"slug": "sneaky", "table": "app_settings", "source": "local"},
        {"slug": "leads", "table": "crm_leads", "source": "project"},  # project → not local
    ])
    allowed = mcp_server._allowed_local_tables()
    assert "draft_orders" in allowed          # declared local entity → allowed
    assert "app_settings" not in allowed      # denylist wins over a hostile declaration
    assert "crm_leads" not in allowed         # project source → not a local write target
    mcp_server._guard_local_table("draft_orders")  # must not raise
