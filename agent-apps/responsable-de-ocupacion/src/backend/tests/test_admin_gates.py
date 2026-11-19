"""v7.93.1 — the "real human admin" gate must discriminate by RUN SURFACE,
never by email equality alone.

Many deployments set ``AGENT_AUTONOMOUS_USER_EMAIL`` to a REAL admin's email
(e.g. both ``admin@wai.com``). The pre-7.93.1 check excluded that email
unconditionally, so the human boss chatting with their own employee was
misclassified as "not a person" and locked out of automations/knowledge
("only an administrator can manage the employee's automations").
The contract now: on a HUMAN surface (an interactive chat run — the only
place ``agent_runs.mode='chat'`` rows are minted) the directory role decides;
on autonomous/channel surfaces the autonomous-identity exclusion stands so
the employee can never self-authorize a governing change.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio

ADMIN = "admin@wai.com"


def _patch_role(monkeypatch, role: str) -> None:
    from app import users_directory

    async def _fake_resolve_role(email: str) -> str:
        return role

    monkeypatch.setattr(users_directory, "resolve_role", _fake_resolve_role)


async def test_colliding_admin_is_human_on_chat_surface(monkeypatch) -> None:
    """autonomous email == boss email → the boss STILL passes from chat."""
    from app.mcp_server import _is_real_human_admin
    monkeypatch.setenv("AGENT_AUTONOMOUS_USER_EMAIL", ADMIN)
    _patch_role(monkeypatch, "admin")
    assert await _is_real_human_admin(ADMIN, human_surface=True) is True


async def test_colliding_admin_denied_on_background_surface(monkeypatch) -> None:
    """Same collision on an autonomous/channel run → still denied: the
    employee serving itself must never self-authorize."""
    from app.mcp_server import _is_real_human_admin
    monkeypatch.setenv("AGENT_AUTONOMOUS_USER_EMAIL", ADMIN)
    _patch_role(monkeypatch, "admin")
    assert await _is_real_human_admin(ADMIN) is False
    assert await _is_real_human_admin(ADMIN, human_surface=False) is False


async def test_member_denied_even_on_chat_surface(monkeypatch) -> None:
    """human_surface skips the autonomous exclusion, NOT the role check."""
    from app.mcp_server import _is_real_human_admin
    monkeypatch.setenv("AGENT_AUTONOMOUS_USER_EMAIL", ADMIN)
    _patch_role(monkeypatch, "member")
    assert await _is_real_human_admin("maria@acme.com", human_surface=True) is False


async def test_missing_actor_fails_closed(monkeypatch) -> None:
    from app.mcp_server import _is_real_human_admin
    _patch_role(monkeypatch, "admin")
    assert await _is_real_human_admin(None, human_surface=True) is False
    assert await _is_real_human_admin("", human_surface=True) is False


async def test_distinct_service_identity_unaffected(monkeypatch) -> None:
    """The healthy setup (dedicated service email) keeps working: the human
    admin passes on any surface, the service identity never does."""
    from app.mcp_server import _is_real_human_admin
    monkeypatch.setenv("AGENT_AUTONOMOUS_USER_EMAIL", "empleado@wai.com")
    _patch_role(monkeypatch, "admin")
    assert await _is_real_human_admin(ADMIN) is True
    assert await _is_real_human_admin(ADMIN, human_surface=True) is True
    assert await _is_real_human_admin("empleado@wai.com") is False
