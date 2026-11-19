"""Channel identity: whoever writes via Telegram/WhatsApp IS their person (7.149.0).

Regression it guards against, seen in the deployed Vigía: a Telegram message from `roman_wai`, with
that handle in the profile of `admin@wai.com` for two days already, was treated as an UNIDENTIFIED
SENDER — separate session and folder, no unified memory and none of their permissions (the employee
replied «this is only for administrators, ask admin@wai.com»… to him).

The cause was not the directory but a guard: the deployer sets
`AGENT_AUTONOMOUS_USER_EMAIL` to the operator's email (the norm in small tenants), and
`valid_memory_user()` discards that address so the employee's synthetic identity does not
write memory to itself. Correct for an autonomous run; wrong for a PERSON who writes and
whom the DIRECTORY identifies.

It is tested at unit level because it is pure resolution logic: no cluster or
deployment needed, and the equivalent REST case would need a real inbound message.
"""
from __future__ import annotations

import asyncio

import pytest


def _reload_with_autonomous_identity(monkeypatch, email: str):
    """Reloads the modules with `AGENT_AUTONOMOUS_USER_EMAIL` set: the constant is read
    at `agent_proxy` import time, so setting it afterwards would have no effect."""
    import importlib
    monkeypatch.setenv("AGENT_AUTONOMOUS_USER_EMAIL", email)
    import app.agent_proxy as agent_proxy
    importlib.reload(agent_proxy)
    import app.memory_utils as memory_utils
    importlib.reload(memory_utils)
    import app.agent_sessions as agent_sessions
    importlib.reload(agent_sessions)
    return agent_sessions, memory_utils


@pytest.fixture
def sessions_with_owner_identity(monkeypatch):
    """The real scenario: the employee's autonomous identity IS the operator's email."""
    return _reload_with_autonomous_identity(monkeypatch, "admin@wai.com")


def test_handle_de_telegram_de_su_persona_no_queda_sin_identificar(
        sessions_with_owner_identity, monkeypatch):
    agent_sessions, _ = sessions_with_owner_identity

    async def fake_email_for_telegram(handle):
        return "admin@wai.com" if handle == "roman_wai" else None

    async def fake_email_for_phone(_):
        return None

    import app.users_directory as users_directory
    monkeypatch.setattr(users_directory, "email_for_telegram", fake_email_for_telegram)
    monkeypatch.setattr(users_directory, "email_for_phone", fake_email_for_phone)
    # `raising=False`: against an older version the helper does not exist and what must fail is
    # the identity ASSERTION, not the test setup.
    monkeypatch.setattr(agent_sessions, "_retire_unidentified_session",
                        lambda *a, **k: asyncio.sleep(0), raising=False)

    scope = asyncio.run(agent_sessions.resolve_inbound_scope(1, "roman_wai"))

    # What the operator sees in the panel: the session hangs off the PERSON, not the handle.
    assert scope.scope_key == "channel:1:admin@wai.com", (
        "el remitente sigue cayendo en el cubo del handle crudo → «origen sin identificar»")
    # And what really matters: memory and folder unified with their chat.
    assert scope.memory_user == "admin@wai.com", "sin memoria unificada no recuerda nada de él"
    assert scope.workspace_subdir.startswith("sessions/users/"), (
        "la carpeta debe ser la de la persona, no una de canal")


def test_un_handle_desconocido_sigue_sin_identidad(sessions_with_owner_identity, monkeypatch):
    """The opening is ONLY for whoever is in the directory: a stranger inherits nobody."""
    agent_sessions, _ = sessions_with_owner_identity

    async def none(_):
        return None

    import app.users_directory as users_directory
    monkeypatch.setattr(users_directory, "email_for_telegram", none)
    monkeypatch.setattr(users_directory, "email_for_phone", none)

    scope = asyncio.run(agent_sessions.resolve_inbound_scope(1, "desconocido_99"))

    assert scope.scope_key == "channel:1:desconocido_99"
    assert scope.memory_user is None
    assert scope.workspace_subdir.startswith("sessions/channels/")


def test_un_from_de_correo_con_la_identidad_autonoma_sigue_bloqueado(
        sessions_with_owner_identity):
    """A `From` can be forged; the profile's handle is set by an administrator. The 7.149.0
    opening is for the email resolved by the DIRECTORY, not for the raw sender."""
    agent_sessions, _ = sessions_with_owner_identity

    scope = agent_sessions.channel_scope(2, "admin@wai.com")

    assert scope.memory_user is None, (
        "un remitente crudo que coincide con la identidad autónoma NO puede heredar su memoria")


def test_la_run_autonoma_no_se_escribe_memoria_a_si_misma(sessions_with_owner_identity):
    """The invariant that motivated the guard still stands: no sender, no person."""
    _, memory_utils = sessions_with_owner_identity
    assert memory_utils.valid_memory_user("admin@wai.com") is None
    assert memory_utils.valid_memory_user(
        "admin@wai.com", allow_autonomous_identity=True) == "admin@wai.com"
