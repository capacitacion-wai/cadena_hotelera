"""Structured-response tail handling.

Since v7 the simple-API sidecar injects/parses the structured JSON
contract for autonomous runs; ``prompts.py`` does NOT append a suffix.
This module still provides ``split_structured_tail`` because chat-tuned
models occasionally emit the trailing JSON block anyway.

The two helpers in this module make the contract robust:

* ``split_structured_tail(text)``  — given the agent's full reply,
  return ``(clean_text, parsed_dict_or_None)``. Used by:
   - ``agent_routes.event_stream`` to scrub the chat SSE before
     forwarding deltas to the SPA.
   - ``agent_proxy.send_and_wait`` to populate a separate
     ``structured`` field for autonomous runs.
* ``find_structured_tail_index(text)`` — incremental version that
  returns the start offset of the first ``STRUCTURED_RESPONSE:`` /
  ``STRUCTURED_RESULT:`` marker as soon as enough characters have
  arrived. The chat stream uses it to stop forwarding deltas the
  moment the marker appears, so the operator never even sees a
  flicker of the JSON body.
"""

from __future__ import annotations

import json
import re
from typing import Optional, Tuple

# Match either marker (case-insensitive) followed by a single JSON
# object. The trailing ``\s*$`` keeps the regex anchored to the END of
# the message (so a literal ``STRUCTURED_RESPONSE:`` quoted earlier in
# the conversation is not mistaken for the contract tail).
_STRUCTURED_TAIL_RE = re.compile(
    r"\s*STRUCTURED_(?:RESPONSE|RESULT)\s*:\s*(\{.*\})\s*$",
    re.DOTALL | re.IGNORECASE,
)

# Cheap pre-screen used by the streaming code path: matches the FIRST
# occurrence of ``STRUCTURED_RESPONSE:`` / ``STRUCTURED_RESULT:`` in
# the buffer so we can stop forwarding deltas without paying the full
# JSON-parse cost on every tick.
_MARKER_RE = re.compile(
    r"STRUCTURED_(?:RESPONSE|RESULT)\s*:",
    re.IGNORECASE,
)


def split_structured_tail(text: str) -> Tuple[str, Optional[dict]]:
    """Return ``(clean_text, parsed_dict_or_None)``.

    * ``clean_text`` is ``text`` with any trailing structured block
      removed and right-stripped.
    * ``parsed_dict_or_None`` is the JSON-decoded payload, or
      ``None`` when the tail is missing / malformed.

    The function is **lossless when there is no tail**: it returns
    the original text unchanged. Callers can therefore pipe every
    agent reply through it without having to special-case the
    chat-mode replies.
    """
    if not text:
        return text, None
    m = _STRUCTURED_TAIL_RE.search(text)
    if m is None:
        return text, None
    payload_raw = m.group(1)
    try:
        payload = json.loads(payload_raw)
        if not isinstance(payload, dict):
            payload = {"value": payload}
    except Exception:
        payload = None
    clean = text[: m.start()].rstrip()
    return clean, payload


def find_structured_tail_index(buffer: str) -> Optional[int]:
    """Return the byte offset of the FIRST ``STRUCTURED_*`` marker
    in ``buffer``, or ``None`` when no marker has appeared yet.

    Streaming callers use this to stop forwarding deltas the moment
    the agent starts emitting the autonomous-only contract — even
    though the chat-mode prompt explicitly forbids it, the model will
    occasionally ignore the instruction and we don't want the JSON to
    flash through the operator's bubble.
    """
    if not buffer:
        return None
    m = _MARKER_RE.search(buffer)
    return m.start() if m else None


_MARKER_KEYWORDS = ("STRUCTURED_RESPONSE", "STRUCTURED_RESULT")


def find_safe_emit_end(buffer: str) -> int:
    """Return the offset up to which ``buffer`` can be safely forwarded
    to the chat SPA without risking the leading characters of a
    ``STRUCTURED_RESPONSE:`` / ``STRUCTURED_RESULT:`` marker.

    The streaming forwarder accumulates text deltas in a buffer and
    needs to emit each delta to the operator's bubble in real time. A
    naive forwarder will leak partial markers when a delta arrives
    that *ends* in a prefix of one of the markers — e.g. the buffer
    is ``"hello \\nSTRU"``, the regex doesn't match yet, so without
    this helper the four characters ``STRU`` would flash on the
    operator's screen before the next delta closes the marker.

    The function returns the largest offset such that
    ``buffer[offset:]`` is the longest suffix that is a (prefix of)
    one of the marker keywords. Callers emit ``buffer[forwarded:end]``
    and keep the tail buffered until the next delta arrives, at which
    point either:

    * the suspicious suffix turns out to be a real prefix → the full
      ``find_structured_tail_index`` regex matches and the forwarder
      stops emitting (the structured tail has begun);
    * or the next character disambiguates → ``find_safe_emit_end``
      now returns the full length and the buffered chunk is emitted.

    The check is case-insensitive and ignores at most ``len(marker)``
    suffix characters, so it adds at worst ``max(len(marker))`` bytes
    of latency to the operator's bubble (≈ 20 chars).
    """
    if not buffer:
        return 0
    upper_tail = buffer[-max(len(m) for m in _MARKER_KEYWORDS):].upper()
    longest = 0
    for marker in _MARKER_KEYWORDS:
        for n in range(min(len(marker), len(upper_tail)), 0, -1):
            if upper_tail.endswith(marker[:n]):
                if n > longest:
                    longest = n
                break
    return len(buffer) - longest


__all__ = [
    "split_structured_tail",
    "find_structured_tail_index",
    "find_safe_emit_end",
]
