"""R-CHAN-1 / v6.40.2 — channel reply idempotency contract tests.

Covers the guards that prevent double agent invocation / double reply:

* ``run_channel_message`` early exit on terminal status / provider id
* ``reprocess`` blocked when the message is already answered, even when
  ``channel_messages.event_id`` was never linked (NULL).
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.asyncio


def _ops_user():
    from app.auth import TokenPayload
    return TokenPayload(
        email="ops@wai.com",
        is_superuser=True,
        project_ids=[],
        data_areas=[],
    )


async def _seed_channel_message(
    *,
    status: str = "pending",
    reply_provider_message_id: str | None = None,
    event_id: int | None = None,
) -> tuple[int, str, str]:
    """Return ``(channel_id, provider_message_id, sender)``."""
    from app.app_database import AppBase, app_engine, app_session
    from app.app_models import channels  # noqa: F401
    from app.app_models import runtime  # noqa: F401
    from app.app_models.channels import Channel, ChannelMessage

    async with app_engine.begin() as conn:
        await conn.run_sync(AppBase.metadata.create_all)

    provider_id = "msg-provider-1"
    async with app_session() as s:
        ch = Channel(
            kind="email",
            auth_method="imap_smtp",
            mailbox_address="support@acme.com",
            display_name="Support",
            status="active",
        )
        s.add(ch)
        await s.flush()
        s.add(ChannelMessage(
            channel_id=ch.id,
            provider_message_id=provider_id,
            sender="user@acme.com",
            subject="hello",
            body_excerpt="hi",
            received_at=datetime.now(timezone.utc),
            status=status,
            reply_provider_message_id=reply_provider_message_id,
            event_id=event_id,
        ))
        await s.commit()
        return int(ch.id), provider_id, "user@acme.com"


@pytest.mark.asyncio
async def test_run_channel_message_skips_replied_without_agent() -> None:
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    os.environ["APP_DATABASE_URL"] = f"sqlite+aiosqlite:///{path}"

    cid, pid, _sender = await _seed_channel_message(status="replied")
    from app.channels.executor import run_channel_message

    result = await run_channel_message(
        {
            "id": 1,
            "payload_json": json.dumps({
                "channel_id": cid,
                "provider_message_id": pid,
            }),
        },
        run_id=42,
    )
    assert result["tokens_in"] == 0
    assert "already replied" in result["structured"]["summary"]

    os.remove(path)


@pytest.mark.asyncio
async def test_run_channel_message_skips_when_provider_id_set() -> None:
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    os.environ["APP_DATABASE_URL"] = f"sqlite+aiosqlite:///{path}"

    cid, pid, _sender = await _seed_channel_message(
        status="processing",
        reply_provider_message_id="smtp-out-99",
    )
    from app.channels.executor import run_channel_message

    result = await run_channel_message(
        {
            "id": 2,
            "payload_json": json.dumps({
                "channel_id": cid,
                "provider_message_id": pid,
            }),
        },
        run_id=43,
    )
    assert result["tokens_in"] == 0
    assert "no re-send" in result["structured"]["summary"]

    os.remove(path)


@pytest.mark.asyncio
async def test_reprocess_blocked_by_payload_key_without_event_link() -> None:
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    os.environ["APP_DATABASE_URL"] = f"sqlite+aiosqlite:///{path}"

    cid, pid, _sender = await _seed_channel_message(
        status="replied",
        reply_provider_message_id="out-1",
        event_id=None,
    )

    from fastapi import HTTPException
    from app.app_database import app_session
    from app.app_models.runtime import Event
    from app.routes.events_routes import reprocess

    async with app_session() as s:
        ev = Event(
            fingerprint="fp-reprocess-1",
            type="external_message",
            source="channel",
            payload_json=json.dumps({
                "channel_id": cid,
                "provider_message_id": pid,
            }),
            status="COMPLETED",
        )
        s.add(ev)
        await s.commit()
        event_id = ev.id

    with pytest.raises(HTTPException) as exc:
        await reprocess(event_id=int(event_id), user=_ops_user())
    assert exc.value.status_code == 409
    assert "re-send" in str(exc.value.detail).lower()

    os.remove(path)


def test_notification_retry_allowed_matrix() -> None:
    from datetime import timedelta
    from app.channels.notifier import _notification_retry_allowed

    now = datetime.now(timezone.utc)
    old = now - timedelta(seconds=300)
    fresh = now - timedelta(seconds=5)

    assert not _notification_retry_allowed(
        "replied", "out-1", old, stale_sec=120, now=now,
    )
    assert not _notification_retry_allowed(
        "processing", None, fresh, stale_sec=120, now=now,
    )
    assert _notification_retry_allowed(
        "processing", None, old, stale_sec=120, now=now,
    )
    assert _notification_retry_allowed(
        "error", None, fresh, stale_sec=120, now=now,
    )


@pytest.mark.asyncio
async def test_notify_reclaims_stuck_processing(monkeypatch) -> None:
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    os.environ["APP_DATABASE_URL"] = f"sqlite+aiosqlite:///{path}"
    os.environ["AGENT_NOTIFY_STALE_PROCESSING_SEC"] = "30"

    from datetime import timedelta
    from app.app_database import AppBase, app_engine, app_session
    from app.app_models import channels  # noqa: F401
    from app.app_models import runtime  # noqa: F401
    from app.app_models.channels import Channel, ChannelMessage
    from app.channels.notifier import NotifyIntent, _notify_one_channel

    async with app_engine.begin() as conn:
        await conn.run_sync(AppBase.metadata.create_all)

    provider_id = "notify:99:1"
    stuck_at = datetime.now(timezone.utc) - timedelta(seconds=120)
    async with app_session() as s:
        # channel_messages.run_id → agent_runs.id is a real FK (PRAGMA
        # foreign_keys=ON): seed the run this notification belongs to, or the
        # INSERT fails. In production the run always exists before we notify.
        from app.app_models.runtime import AgentRun
        s.add(AgentRun(id=99, idempotency_key="test-notify-run-99", prompt=""))
        await s.flush()
        ch = Channel(
            kind="email",
            auth_method="imap_smtp",
            mailbox_address="ops@acme.com",
            display_name="Ops",
            status="active",
            notify_config='{"notify_to":["ops@acme.com"],"policy":"agent"}',
        )
        s.add(ch)
        await s.flush()
        s.add(ChannelMessage(
            channel_id=ch.id,
            provider_message_id=provider_id,
            direction="notification",
            sender="ops@acme.com",
            subject="stuck",
            received_at=stuck_at,
            status="processing",
            run_id=99,
            reply_body="pending body",
        ))
        await s.commit()
        channel_id = int(ch.id)

    class _FakeAdapter:
        async def send_reply(self, _msg, _reply):
            return "smtp-reclaim-1"

    monkeypatch.setattr(
        "app.channels.notifier.get_adapter",
        lambda _ch: _FakeAdapter(),
    )

    async with app_session() as s:
        from sqlalchemy import select
        ch = (await s.execute(
            select(Channel).where(Channel.id == channel_id)
        )).scalar_one()

    sent, errors = await _notify_one_channel(
        ch,
        ["ops@acme.com"],
        0,
        NotifyIntent(
            severity="info",
            subject="Alert",
            body="Recovered notification",
            attachments=[],
            channels=[],
        ),
        run_id=99,
        provider_id=provider_id,
    )
    assert sent == ["smtp-reclaim-1"]
    assert errors == []

    async with app_session() as s:
        from sqlalchemy import select
        row = (await s.execute(
            select(ChannelMessage).where(
                ChannelMessage.provider_message_id == provider_id,
            )
        )).scalar_one()
    assert row.status == "replied"
    assert row.reply_provider_message_id == "smtp-reclaim-1"

    os.remove(path)


@pytest.mark.asyncio
async def test_notify_skips_fresh_processing_without_resend(monkeypatch) -> None:
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    os.environ["APP_DATABASE_URL"] = f"sqlite+aiosqlite:///{path}"
    os.environ["AGENT_NOTIFY_STALE_PROCESSING_SEC"] = "300"

    from app.app_database import AppBase, app_engine, app_session
    from app.app_models import channels  # noqa: F401
    from app.app_models import runtime  # noqa: F401
    from app.app_models.channels import Channel, ChannelMessage
    from app.channels.notifier import NotifyIntent, _notify_one_channel

    async with app_engine.begin() as conn:
        await conn.run_sync(AppBase.metadata.create_all)

    provider_id = "notify:100:1"
    async with app_session() as s:
        from app.app_models.runtime import AgentRun
        s.add(AgentRun(id=100, idempotency_key="test-notify-run-100", prompt=""))
        await s.flush()
        ch = Channel(
            kind="email",
            auth_method="imap_smtp",
            mailbox_address="ops2@acme.com",
            display_name="Ops2",
            status="active",
        )
        s.add(ch)
        await s.flush()
        s.add(ChannelMessage(
            channel_id=ch.id,
            provider_message_id=provider_id,
            direction="notification",
            sender="ops2@acme.com",
            received_at=datetime.now(timezone.utc),
            status="processing",
            run_id=100,
        ))
        await s.commit()
        channel_id = int(ch.id)

    async def _boom(_ch):
        raise AssertionError("send_reply must not run for fresh processing")

    monkeypatch.setattr("app.channels.notifier.get_adapter", _boom)

    async with app_session() as s:
        from sqlalchemy import select
        ch = (await s.execute(
            select(Channel).where(Channel.id == channel_id)
        )).scalar_one()

    sent, errors = await _notify_one_channel(
        ch,
        ["ops2@acme.com"],
        0,
        NotifyIntent(
            severity="info",
            subject="Wait",
            body="Should not send",
            attachments=[],
            channels=[],
        ),
        run_id=100,
        provider_id=provider_id,
    )
    assert sent == []
    assert errors == []

    os.remove(path)
