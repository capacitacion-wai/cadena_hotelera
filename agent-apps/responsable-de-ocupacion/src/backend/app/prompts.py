"""Prompt templates per ``event_type``.

The generator writes one entry to ``PROMPTS`` per watcher declared in
``wai-app.json``. Each template is a Python ``str.format`` string that
receives the event payload as kwargs. The autonomous executor selects the
template for the event type via ``executor._prompt_for`` (which looks up
``PROMPTS`` and falls back to ``prompts_defaults.PROMPTS_DEFAULTS`` for the
platform system events) and renders it with ``str.format``.

Structured-response contract
============================
v7 moves the ``STRUCTURED_RESPONSE: {...}`` contract suffix to the
**simple-API sidecar** (see ``opencode-server/simple_api/structured.py``).
This module no longer appends it. The sidecar:

* Appends the suffix to every prompt sent through
  ``POST /v1/sessions/{id}/messages`` when ``mode='autonomous'``
  (the autonomous executor's only code path).
* Parses the tail server-side and returns ``structured`` as a
  separate field of the response — the executor persists it on
  ``agent_runs.structured_json``.
* Strips any stray tail from the streaming endpoint when
  ``mode='interactive'``.

Keep templates focused on the business prompt; don't redeclare the
JSON contract here.

HARD RULE — ``external_message`` is NOT yours
=============================================
NEVER add an ``external_message`` entry to ``PROMPTS``. Channel events
are prompted by ``app/channels/executor.py::_build_prompt`` which
renders the real sender/subject/body from the ``channel_messages`` row
and carries the reply/attachments contract. A template here would
(a) never receive ``{from_address}``-style placeholders (the event
payload only has ``channel_id`` + ``provider_message_id``) and
(b) fight the channel executor's instructions.
"""

PROMPTS: dict[str, str] = {}


def build_prompt(event_type: str, payload: dict) -> str:
    template = PROMPTS[event_type]
    return template.format(**payload)
