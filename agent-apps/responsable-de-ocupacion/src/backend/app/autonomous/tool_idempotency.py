"""Replay-or-execute helper for app-local MCP tools.

Every tool invocation made by the agent is identified by the triple
(run_id, step_idx, tool_name). The first call materialises the work
and stores the result in `tool_call_log`. Subsequent calls with the
same triple replay the stored result instead of re-executing — that
way a network retry from the agent never duplicates writes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.app_models.runtime import ToolCallLog

# How long a losing concurrent caller waits for the winner's result before
# assuming the winner died and executing the work itself.
#
# v7.88.0 — raised 10s → 45s. The claim is atomic (unique idempotency_key +
# INSERT…ON CONFLICT, so exactly one winner), and the idempotency_key is
# DETERMINISTIC over (run_id, step_idx, tool, args) — a higher-level retry of the
# same call reuses the SAME key, so re-executing here is the ONLY recovery path
# for a winner that genuinely died mid-work (pod restart). The residual risk is a
# DOUBLE execution when the winner is merely SLOW (still alive past the wait) and
# the tool is non-idempotent; widening the window makes a false "winner died"
# verdict very unlikely for these fast, app-local DB tools while preserving
# dead-winner recovery. (A heartbeat lease would be ideal but a single ``do_work``
# await has no natural point to beat.)
_WAIT_FOR_WINNER_SECONDS = 45.0
_WAIT_POLL_SECONDS = 0.25


def _hash(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()


async def _replay_or_execute(
    session,
    *,
    run_id: int,
    step_idx: int,
    tool_name: str,
    args: dict,
    do_work: Callable[[], Awaitable[Any]],
    mode: str = "autonomous",
) -> tuple[Any, bool]:
    """Return (result, replayed).

    `replayed=True` means a previous identical call was found and its
    stored result is returned without invoking `do_work` again.

    B5: ``mode`` ('autonomous' | 'chat') is part of the idempotency
    key so a chat-side call to e.g. ``log_communication`` with the
    same args as an autonomous call doesn't replay the autonomous
    result (or vice versa). The two flows are independent and must
    not share dedup state even when they happen to share run_id.
    """
    args_hash = _hash(args)
    idempotency_key = f"{mode}:{run_id}:{step_idx}:{tool_name}:{args_hash}"

    # Atomic barrier: claim the key with INSERT ... ON CONFLICT DO
    # NOTHING. Exactly one concurrent caller wins (rowcount=1) and runs
    # ``do_work``; losers replay the winner's stored result. A plain
    # SELECT-then-execute had a race window where two callers both saw
    # "no row" and duplicated the side effect.
    claim = sqlite_insert(ToolCallLog).values(
        run_id=run_id,
        step_idx=step_idx,
        tool_name=tool_name,
        arguments_hash=args_hash,
        idempotency_key=idempotency_key,
        result_hash=None,
        result_json=None,
        executed_at=datetime.now(timezone.utc),
    ).on_conflict_do_nothing(index_elements=[ToolCallLog.idempotency_key])
    res = await session.execute(claim)
    await session.commit()

    if not res.rowcount:
        # Another caller owns the key. If its result is already stored,
        # replay it; otherwise wait briefly for the winner to finish.
        deadline = asyncio.get_event_loop().time() + _WAIT_FOR_WINNER_SECONDS
        while True:
            existing = (
                await session.execute(
                    select(ToolCallLog).where(
                        ToolCallLog.idempotency_key == idempotency_key
                    )
                )
            ).scalar_one_or_none()
            if existing is not None and existing.result_json is not None:
                return json.loads(existing.result_json), True
            if asyncio.get_event_loop().time() >= deadline:
                # Winner most likely died mid-execution (pod restart).
                # Execute ourselves and overwrite the placeholder.
                break
            await asyncio.sleep(_WAIT_POLL_SECONDS)

    result = await do_work()
    result_json = json.dumps(result, default=str)

    await session.execute(
        update(ToolCallLog)
        .where(ToolCallLog.idempotency_key == idempotency_key)
        .values(
            result_hash=_hash(result),
            result_json=result_json,
            executed_at=datetime.now(timezone.utc),
        )
    )
    await session.commit()
    return result, False
