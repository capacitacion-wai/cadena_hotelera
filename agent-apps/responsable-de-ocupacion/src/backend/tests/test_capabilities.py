"""v7.65.0 — rigor tests for the Capabilities resolver's PURE logic.

These pin the thing that must never drift: the mode-lens semantics that distinguish a
HARD gate (``approve``) from a POSTURE bias (autonomy). The connector-side verdict reads
the SAME row of the platform's permissions table the proxy reads
(``mcp_policy.table_permission``, 14.8.0), so it cannot diverge from the runtime by
construction; local tools are exempt from connector permissions by design.
"""
from app import capabilities as caps


def test_worse_ordering():
    assert caps._worse("allow", "approve") == "approve"
    assert caps._worse("deny", "approve") == "deny"
    assert caps._worse("unavailable", "deny") == "unavailable"
    assert caps._worse("allow", "allow") == "allow"


def test_internal_allow_acts_alone_everywhere():
    r = caps._by_mode("allow", False, "propose")
    assert r["autonomous"]["state"] == "can"
    assert r["chat"]["state"] == "can"
    assert r["channels"]["state"] == "can"


def test_outward_allow_is_posture_gated_when_not_autonomous():
    # The permission would allow it, but under 'propose' the employee does NOT act
    # outward on its own → autonomous shows 'approval' flagged as a POSTURE bias,
    # while chat (you are present) shows 'can'.
    r = caps._by_mode("allow", True, "propose")
    assert r["autonomous"] == {"state": "approval", "reason": "posture_observe"}
    assert r["chat"]["state"] == "can"


def test_outward_allow_acts_alone_under_full_autonomy():
    r = caps._by_mode("allow", True, "act_autonomous")
    assert r["autonomous"]["state"] == "can"


def test_approve_is_hard_gate_not_posture():
    r = caps._by_mode("approve", True, "act_autonomous")
    assert r["autonomous"] == {"state": "approval", "reason": "gate"}
    assert r["chat"]["state"] == "ask"          # interactive, you are present
    assert r["channels"]["state"] == "approval"  # parked before it goes out


def test_deny_and_unavailable():
    assert caps._by_mode("deny", True, "act_autonomous")["autonomous"]["state"] == "deny"
    assert caps._by_mode("unavailable", True, "propose")["autonomous"]["state"] == "unavailable"


def test_catalog_state_composio_is_dynamic_not_unavailable():
    """An empty catalog means OPPOSITE things depending on whose connector it is.

    This test used to talk about "router" and "raw", which was the Composio switch from when
    there were two paths. When the legacy was retired, the question stopped being which mode we
    are in and became whose connector it is, and the signature changed from a mode (text) to a
    boolean. The test kept the old one and failed against the pristine assets — blocking the
    update of ALL employees, not only the one being migrated.
    """
    # With a real list, it does not matter whose it is: we have it.
    assert caps._connector_catalog_state(True, True) == "enumerated"
    assert caps._connector_catalog_state(True, False) == "enumerated"
    # Empty on a Composio connector: BY DESIGN. It only exposes its meta-tools, so there is
    # no static catalog to enumerate. Calling it a fault is the false negative that made it
    # say "not connected" about a perfectly connected connector.
    assert caps._connector_catalog_state(False, True) == "dynamic"
    # Empty on an OWN connector: that IS a fault, and disguising it as "resolved on the
    # fly" was hiding it.
    assert caps._connector_catalog_state(False, False) == "unavailable"


def test_dynamic_connector_reads_allow_and_writes_follow_floor():
    # A router connector with no static catalog must NOT read as 'unavailable':
    # it is connected + usable, so reads are allowed and writes follow the floor.
    from app import mcp_policy as p
    assert caps._unenumerated_verdicts(p.POLICY_WRITE, "dynamic") == (caps.V_ALLOW, caps.V_ALLOW)
    assert caps._unenumerated_verdicts(p.POLICY_APPROVAL, "dynamic") == (caps.V_ALLOW, caps.V_APPROVE)
    assert caps._unenumerated_verdicts(p.POLICY_READ_ONLY, "dynamic") == (caps.V_ALLOW, caps.V_DENY)


def test_unavailable_connector_is_honest_unknown():
    # Raw + empty catalog → HONEST unknown: reads NA; writes report the floor only
    # when the policy forbids/gates, else NA (no fabricated 'puede').
    from app import mcp_policy as p
    assert caps._unenumerated_verdicts(p.POLICY_WRITE, "unavailable") == (caps.V_NA, caps.V_NA)
    assert caps._unenumerated_verdicts(p.POLICY_APPROVAL, "unavailable") == (caps.V_NA, caps.V_APPROVE)
    assert caps._unenumerated_verdicts(p.POLICY_READ_ONLY, "unavailable") == (caps.V_NA, caps.V_DENY)


def test_count_summary_buckets_outward_deny_as_read_only():
    groups = [{"capabilities": [
        {"verdict": "allow", "outward": False},
        {"verdict": "approve", "outward": True},
        {"verdict": "deny", "outward": True},    # a blocked WRITE → read_only
        {"verdict": "deny", "outward": False},   # a blocked internal → cannot
        {"verdict": "unavailable", "outward": True},
    ]}]
    assert caps._count_summary(groups) == {
        "can": 1, "approval": 1, "read_only": 1, "cannot": 1, "unavailable": 1,
    }


def test_platform_tools_are_no_capability_of_the_employee(monkeypatch):
    """15.8.2 — a connector to a WAI application carries the platform's test tools (built-in testing); the platform
    denies them to every agent (``source: platform``). The read one made the whole connector read «cannot read»
    (WAI Viajes in demo, 08/11/2026): they are not the employee's, so they are left out of its verdicts."""
    import asyncio
    from app import mcp_policy, mcp_runtime
    tabla = {"connectors": [{"name": "wai-viajes", "tools": [
        {"name": "list_trips", "permission": "allow", "source": "read", "io_kind": "read"},
        {"name": "list_test_datasets", "permission": "deny", "source": "platform", "io_kind": "read"},
        {"name": "sweep_test_run", "permission": "deny", "source": "platform", "io_kind": "write"}]}]}

    async def _tabla(**_):
        return tabla

    async def _autonomia():
        return mcp_policy.POLICY_WRITE

    async def _cache():
        return {}

    monkeypatch.setattr(mcp_policy, "load_table", _tabla)
    monkeypatch.setattr(mcp_policy, "_por_autonomia", _autonomia)
    monkeypatch.setattr(mcp_runtime, "composio_connectors", lambda: [])
    monkeypatch.setattr(mcp_runtime, "custom_connectors", lambda *a, **k: [{"name": "wai-viajes", "display_name": "WAI Viajes"}])
    monkeypatch.setattr(caps, "_tools_cache", _cache)
    (g,) = asyncio.run(caps._connector_groups(mcp_policy.POLICY_WRITE))
    assert g["read_verdict"] == caps.V_ALLOW
    assert [r["name"] for r in g["tools"]] == ["list_trips"]
