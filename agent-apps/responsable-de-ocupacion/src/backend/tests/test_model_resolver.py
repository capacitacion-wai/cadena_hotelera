"""R-MODEL-1 regression test — ``agent_proxy._resolve_model``.

Pre-v7.1 every autonomous deployment received ``OPENCODE_DEFAULT_MODEL``
as a comma-separated catalogue (``"openai/gpt-5.4-mini,openai/gpt-5.4,
openai/gpt-5.5"``) and the OpenCode simple-API rejected the entire
string with ``UnknownError: Model not found``. The fix
(``agent_proxy._resolve_model``) picks the first non-empty entry so
historical deployments keep working unchanged.

This test asserts the resolution order and the catalogue split. It
runs sync — no DB, no FastAPI, no asyncio — so the verifier's
"strong" phase can execute it in milliseconds even on a CI runner
without ``pytest-asyncio``.
"""
from __future__ import annotations

import importlib
import os
from typing import Iterator

import pytest


def _reload_proxy():
    """Force a re-read of the module so its module-level
    ``AGENT_DEFAULT_MODEL`` cache picks up the freshly-monkeyed
    env vars. We only need the function, not the cache, but
    ``_resolve_model`` itself reads ``os.environ`` on every call so
    this is just defensive."""
    import app.agent_proxy as mod
    return importlib.reload(mod)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch) -> Iterator[None]:
    # Strip every env var the resolver looks at so each test runs in
    # a known-empty world.
    for name in (
        "OPENCODE_DEFAULT_MODEL",
        "AGENT_AUTONOMOUS_MODEL",
        "AGENT_INTERACTIVE_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    yield


def test_empty_env_returns_empty_string(monkeypatch) -> None:
    proxy = _reload_proxy()
    assert proxy._resolve_model("autonomous") == ""
    assert proxy._resolve_model("interactive") == ""


def test_single_model_returns_it(monkeypatch) -> None:
    monkeypatch.setenv("OPENCODE_DEFAULT_MODEL", "openai/gpt-4")
    proxy = _reload_proxy()
    assert proxy._resolve_model("autonomous") == "openai/gpt-4"
    assert proxy._resolve_model("interactive") == "openai/gpt-4"


def test_csv_catalogue_picks_first_entry(monkeypatch) -> None:
    # This is the exact value the pulse-sentinel / reco-sentinel
    # pods received in production and that crashed the chat with
    # "Model not found". The resolver MUST split it and return only
    # the first entry — verbatim, no rewrites.
    monkeypatch.setenv(
        "OPENCODE_DEFAULT_MODEL",
        "openai/gpt-5.4-mini,openai/gpt-5.4,openai/gpt-5.5",
    )
    proxy = _reload_proxy()
    assert proxy._resolve_model("autonomous") == "openai/gpt-5.4-mini"
    assert proxy._resolve_model("interactive") == "openai/gpt-5.4-mini"


def test_csv_with_whitespace_and_blanks(monkeypatch) -> None:
    # Defensive: a hand-edited env var might have stray spaces or an
    # accidental leading comma. The resolver must skip those instead
    # of returning an empty string.
    monkeypatch.setenv(
        "OPENCODE_DEFAULT_MODEL",
        " , , openai/gpt-5.4-mini ,openai/gpt-5.4",
    )
    proxy = _reload_proxy()
    assert proxy._resolve_model("autonomous") == "openai/gpt-5.4-mini"


def test_autonomous_explicit_override_wins(monkeypatch) -> None:
    # AGENT_AUTONOMOUS_MODEL takes priority over the catalogue —
    # the deployer wires it from ``agent_versions.model_config.
    # autonomous_model`` so the operator can pin one model to the
    # autonomous path independently of the interactive one.
    monkeypatch.setenv("AGENT_AUTONOMOUS_MODEL", "openai/gpt-5.4-mini")
    monkeypatch.setenv("OPENCODE_DEFAULT_MODEL", "openai/gpt-4")
    proxy = _reload_proxy()
    assert proxy._resolve_model("autonomous") == "openai/gpt-5.4-mini"
    # Interactive falls back to the catalogue, untouched.
    assert proxy._resolve_model("interactive") == "openai/gpt-4"


def test_interactive_explicit_override_wins(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_INTERACTIVE_MODEL", "openai/gpt-5.5")
    monkeypatch.setenv("OPENCODE_DEFAULT_MODEL", "openai/gpt-4")
    proxy = _reload_proxy()
    assert proxy._resolve_model("interactive") == "openai/gpt-5.5"
    assert proxy._resolve_model("autonomous") == "openai/gpt-4"
