"""v7.89.2 — the MCP bridge NEVER bypasses the policy proxy (fail-closed).

The policy proxy is not just the write-policy: it enforces per-tool
allow/ask/deny, the global and per-connector defaults, and parks ``ask``
calls for operator approval. So when the deployer did not inject
``APP_INTERNAL_BASE_URL``/``APP_MCP_TOKEN`` there is NO acceptable fallback:
``_bridge_url()`` must disable the connector for EVERY policy. The
pre-7.89.2 code bridged ``write`` connectors directly to the credentialed
upstream — a full permissions bypass on older deployers.
"""
from __future__ import annotations

UPSTREAM = "https://mcp.composio.dev/composio/server/x/mcp"


def test_bridge_disabled_without_proxy_env(monkeypatch) -> None:
    monkeypatch.delenv("APP_INTERNAL_BASE_URL", raising=False)
    monkeypatch.delenv("APP_MCP_TOKEN", raising=False)
    from app.mcp_policy import POLICY_APPROVAL, POLICY_READ_ONLY, POLICY_WRITE
    from app.mcp_runtime import _bridge_url

    for policy in (POLICY_WRITE, POLICY_APPROVAL, POLICY_READ_ONLY):
        assert _bridge_url("kommo", UPSTREAM, policy=policy) == "", (
            f"policy={policy!r} must be DISABLED without the proxy env — "
            "a direct upstream bypasses per-tool permissions and ask-approval"
        )


def test_bridge_disabled_with_partial_proxy_env(monkeypatch) -> None:
    # Base URL without token (and vice versa) is still an unusable proxy.
    monkeypatch.setenv("APP_INTERNAL_BASE_URL", "http://app-svc:8000")
    monkeypatch.delenv("APP_MCP_TOKEN", raising=False)
    from app.mcp_runtime import _bridge_url
    assert _bridge_url("kommo", UPSTREAM, policy="write") == ""

    monkeypatch.delenv("APP_INTERNAL_BASE_URL", raising=False)
    monkeypatch.setenv("APP_MCP_TOKEN", "tok123")
    assert _bridge_url("kommo", UPSTREAM, policy="write") == ""


def test_bridge_prefers_proxy_when_configured(monkeypatch) -> None:
    monkeypatch.setenv("APP_INTERNAL_BASE_URL", "http://app-svc:8000")
    monkeypatch.setenv("APP_MCP_TOKEN", "tok123")
    from app.mcp_runtime import _bridge_url
    assert (
        _bridge_url("kommo", UPSTREAM, policy="write")
        == "http://app-svc:8000/mcp-proxy/tok123/kommo"
    )


def test_bridge_empty_upstream_stays_disabled(monkeypatch) -> None:
    monkeypatch.setenv("APP_INTERNAL_BASE_URL", "http://app-svc:8000")
    monkeypatch.setenv("APP_MCP_TOKEN", "tok123")
    from app.mcp_runtime import _bridge_url
    assert _bridge_url("kommo", "", policy="write") == ""
