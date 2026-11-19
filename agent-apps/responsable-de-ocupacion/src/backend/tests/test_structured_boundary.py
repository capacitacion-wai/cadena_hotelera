"""B1 regression test — STRUCTURED_RESPONSE boundary safety.

Asserts that the chat SSE forwarder never emits a partial marker
prefix even when the agent's text deltas split exactly at the
``STRUCTURED_RESPONSE:`` boundary. This was the v6.4.0 bug we
chase across most production incidents — keep this test green.
"""
from __future__ import annotations

import pytest

from app.utils.structured import (
    find_safe_emit_end,
    find_structured_tail_index,
    split_structured_tail,
)


@pytest.mark.parametrize(
    "buffer, expected_safe_end",
    [
        ("hello world", 11),                # no marker prefix → all safe
        ("hello \nSTRU", 7),                # 4-char marker prefix
        ("foo bar S", 8),                   # 1-char marker prefix
        ("STRUCTURED_RESPONSE", 0),         # full marker, no colon — entire buffer is the prefix
        # Full marker present in the middle of the buffer — the trailing
        # ``{`` is not a marker prefix, so ``find_safe_emit_end`` returns
        # the buffer length (26). In production this case is always
        # short-circuited by the caller's prior ``find_structured_tail_index``
        # call (see ``test_boundary_split_does_not_leak_partial_marker`` below).
        ("foo STRUCTURED_RESPONSE: {", 26),
    ],
)
def test_safe_emit_end(buffer: str, expected_safe_end: int) -> None:
    assert find_safe_emit_end(buffer) == expected_safe_end


def test_boundary_split_does_not_leak_partial_marker() -> None:
    """Simulate the SSE forwarder receiving the marker across two
    deltas: ``"\\nSTRU"`` then ``"CTURED_RESPONSE: {}"``."""
    streamed = ""
    forwarded_len = 0
    forwarded_chunks: list[str] = []

    deltas = ["hello ", "world", "\nSTRU", "CTURED_RESPONSE: ", '{"a":1}']
    structured_tripped = False

    for delta in deltas:
        streamed += delta
        if structured_tripped:
            continue
        idx = find_structured_tail_index(streamed)
        if idx is not None:
            structured_tripped = True
            visible = streamed[forwarded_len:idx]
            if visible:
                forwarded_chunks.append(visible)
            forwarded_len = idx
            continue
        safe_end = find_safe_emit_end(streamed)
        if safe_end > forwarded_len:
            forwarded_chunks.append(streamed[forwarded_len:safe_end])
            forwarded_len = safe_end

    full_emission = "".join(forwarded_chunks)
    # The operator never sees any character of the marker.
    assert "STRU" not in full_emission
    assert "STRUCTURED" not in full_emission
    # The legitimate text and the newline ahead of the marker DO
    # get through — only the marker itself is suppressed.
    assert full_emission == "hello world\n"


def test_split_structured_tail_strips_payload() -> None:
    text = "All done.\nSTRUCTURED_RESPONSE: {\"summary\": \"ok\"}"
    clean, payload = split_structured_tail(text)
    assert clean == "All done."
    assert payload == {"summary": "ok"}
