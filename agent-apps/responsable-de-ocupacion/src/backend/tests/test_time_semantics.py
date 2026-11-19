"""v7.97.2 — timestamps the AGENT reads must carry their timezone.

Post-mortem (WhatsApp, tenant demo): the channel prompt said
``Recibido: 2026-07-11T14:01:02`` — the naive-UTC DB datetime rendered with a
bare ``.isoformat()``. The agent (told elsewhere that business time is
Europe/Madrid) read those UTC digits as wall-clock time, so every inbound
WhatsApp/email time it quoted was shifted by the whole tz offset. The
contract: agent-facing prompts use ``timeutil.local_stamp`` (business tz,
offset spelled out) and MCP tool results serialise datetimes via ``iso_utc``
(explicit ``Z``) — never a bare naive ``.isoformat()``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest


@pytest.fixture
def madrid_tz(monkeypatch):
    import app.timeutil as tu
    monkeypatch.setattr(tu, "current_timezone", lambda: "Europe/Madrid")


def test_local_stamp_naive_utc_gets_business_tz(madrid_tz) -> None:
    from app.timeutil import local_stamp
    # July → CEST (UTC+02:00): 14:01 UTC = 16:01 in Madrid.
    out = local_stamp(datetime(2026, 7, 11, 14, 1, 2))
    assert "2026-07-11 16:01" in out
    assert "(Europe/Madrid, UTC+02:00)" in out


def test_local_stamp_aware_and_none(madrid_tz) -> None:
    from app.timeutil import local_stamp
    aware = local_stamp(datetime(2026, 7, 11, 14, 1, tzinfo=timezone.utc))
    assert "2026-07-11 16:01" in aware
    assert local_stamp(None) == ""


def test_whatsapp_prompt_recibido_in_business_tz(madrid_tz) -> None:
    from app.channels.executor import _build_prompt
    channel = SimpleNamespace(kind="whatsapp", mailbox_address="whatsapp:+34600111222")
    msg = SimpleNamespace(
        sender="whatsapp:+34600999888", subject=None,
        body_full="hola, ¿está mi pedido?", body_excerpt=None,
        received_at=datetime(2026, 7, 11, 14, 1, 2),
        inbound_attachments_json=None,
    )
    prompt = _build_prompt(channel, msg)  # type: ignore[arg-type]
    assert "Received: 2026-07-11 16:01 (Europe/Madrid, UTC+02:00)" in prompt
    assert "T14:01" not in prompt  # the old naive-UTC leak


def test_mcp_server_has_no_bare_isoformat() -> None:
    # Regression tripwire: tool results must go through iso_utc; a naive
    # ``X.isoformat() if X else None`` reintroduces the tz loss.
    import inspect
    import app.mcp_server as m
    src = inspect.getsource(m)
    assert "isoformat() if" not in src
