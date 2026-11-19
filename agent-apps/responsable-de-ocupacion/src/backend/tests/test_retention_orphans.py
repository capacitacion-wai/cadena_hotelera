"""v7.89.1/v7.89.2 — channel-message retention must never orphan attachments.

The retention job (``main._cleanup_old_channel_messages``) deletes staged
attachment files BEFORE pruning their ``channel_messages`` rows. The contract:
a row whose attachment could NOT be deleted — unlink failure (7.89.1) or the
whole workspace being unresolvable (7.89.2) — is KEPT and retried by the next
run. Deleting the row anyway would leave PII files orphaned on the PVC, the
exact leak retention exists to prevent.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.asyncio

_OLD = datetime.now(timezone.utc) - timedelta(days=90)


async def _seed(att_rel: str | None) -> int:
    """One settled, retention-eligible message (own channel — the mailbox is
    unique-constrained); returns its row id."""
    from uuid import uuid4
    from app.app_database import app_session
    from app.app_models.channels import Channel, ChannelMessage

    async with app_session() as s:
        ch = Channel(
            kind="email",
            auth_method="imap_smtp",
            mailbox_address=f"support-{uuid4().hex[:8]}@acme.com",
            display_name="Support",
            status="active",
        )
        s.add(ch)
        await s.flush()
        msg = ChannelMessage(
            channel_id=ch.id,
            provider_message_id=f"msg-{att_rel or 'plain'}",
            sender="user@acme.com",
            subject="hello",
            body_excerpt="hi",
            received_at=_OLD,
            status="replied",
            inbound_attachments_json=(
                json.dumps([{"workspace_path": att_rel}]) if att_rel else None
            ),
        )
        s.add(msg)
        await s.commit()
        return int(msg.id)


async def _remaining_ids() -> set[int]:
    from sqlalchemy import select
    from app.app_database import app_session
    from app.app_models.channels import ChannelMessage

    async with app_session() as s:
        rows = (await s.execute(select(ChannelMessage.id))).scalars().all()
        return {int(r) for r in rows}


def _enable_retention(monkeypatch) -> None:
    import app.config as config
    monkeypatch.setattr(config, "AGENT_CHANNEL_MESSAGE_RETENTION_DAYS", 30, raising=False)


async def test_prunes_row_and_file_when_workspace_ok(tmp_path, monkeypatch) -> None:
    _enable_retention(monkeypatch)
    from app import workspace_fs as wfs
    att = tmp_path / "uploads" / "dni.pdf"
    att.parent.mkdir(parents=True)
    att.write_bytes(b"pii")
    monkeypatch.setattr(wfs, "workspace_root", lambda: str(tmp_path))
    monkeypatch.setattr(wfs, "resolve", lambda rel, must_exist=True: str(tmp_path / rel))

    row_id = await _seed("uploads/dni.pdf")
    from app.main import _cleanup_old_channel_messages
    await _cleanup_old_channel_messages()

    assert not att.exists(), "attachment file must be deleted"
    assert row_id not in await _remaining_ids(), "row must be pruned after its file"


async def test_keeps_attachment_rows_when_workspace_unresolvable(monkeypatch) -> None:
    """ws_ok=False → no file can be deleted, so no ATTACHMENT row may be
    pruned (plain rows still are). Pre-7.89.2 this path pruned everything."""
    _enable_retention(monkeypatch)
    from app import workspace_fs as wfs
    monkeypatch.setattr(wfs, "workspace_root", lambda: None)

    with_att = await _seed("uploads/dni.pdf")
    plain = await _seed(None)
    from app.main import _cleanup_old_channel_messages
    await _cleanup_old_channel_messages()

    remaining = await _remaining_ids()
    assert with_att in remaining, "row with attachment must be KEPT for retry"
    assert plain not in remaining, "attachment-less row is safe to prune"


async def test_keeps_row_when_unlink_fails(tmp_path, monkeypatch) -> None:
    _enable_retention(monkeypatch)
    from app import workspace_fs as wfs
    # resolve() points at a DIRECTORY: Path.unlink() raises on it, which is
    # exactly the "file could not be removed" failure mode.
    blocker = tmp_path / "uploads" / "dni.pdf"
    blocker.mkdir(parents=True)
    (blocker / "inner").write_bytes(b"x")
    monkeypatch.setattr(wfs, "workspace_root", lambda: str(tmp_path))
    monkeypatch.setattr(wfs, "resolve", lambda rel, must_exist=True: str(tmp_path / rel))

    row_id = await _seed("uploads/dni.pdf")
    from app.main import _cleanup_old_channel_messages
    await _cleanup_old_channel_messages()

    assert row_id in await _remaining_ids(), "row must be KEPT when its file could not be removed"
