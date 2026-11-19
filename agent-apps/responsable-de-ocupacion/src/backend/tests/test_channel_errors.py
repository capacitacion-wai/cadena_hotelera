"""Channel delivery error formatting."""

import httpx

from app.channels.base import format_channel_exception


def test_format_channel_exception_uses_message_when_present() -> None:
    assert format_channel_exception(RuntimeError("smtp down")) == "smtp down"


def test_format_channel_exception_read_timeout_has_text() -> None:
    msg = format_channel_exception(httpx.ReadTimeout(""))
    assert msg
    assert "Tiempo de espera" in msg
    assert "adjuntos" in msg
