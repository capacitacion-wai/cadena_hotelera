"""External channels — inbound buzon → autonomous agent → reply.

This package implements the *Channels* feature documented in
``patterns/external-channels.md``. Each channel has a ``kind`` (email,
whatsapp, slack; outlook/teams in the future) and an ``auth_method``
(``imap_smtp`` for email, ``twilio`` for whatsapp, ``bot_token`` for
slack). The watcher polls the channel, the executor runs the agent and
the adapter delivers the reply.

Public API:

* ``get_adapter(channel)`` — resolve the adapter for a Channel row.
* ``Message`` / ``Reply`` / ``TestResult`` — normalised payloads the
  watcher / executor exchange with the adapters.
* ``ChannelError`` / ``ChannelUnauthorizedError`` — exceptions the
  watcher catches to flip ``status='unauthorized'``.
* ``poller.run_forever()`` — the long-lived poll loop spawned from
  ``main.py`` alongside the watcher / executor.
"""

from app.channels.base import (
    ChannelAdapter,
    ChannelError,
    ChannelUnauthorizedError,
    Message,
    Reply,
    TestResult,
    get_adapter,
)

__all__ = [
    "ChannelAdapter",
    "ChannelError",
    "ChannelUnauthorizedError",
    "Message",
    "Reply",
    "TestResult",
    "get_adapter",
]
