"""Connector permissions in the employee's pod (14.8.0): the PLATFORM's table, applied.

The pod no longer calculates a permission: it reads ``GET /agents/{id}/permissions/employee``
(the agent's table with the employee's layer) and looks each call up. What these tests pin:

* the verdict of a call is the table's row — nothing is re-derived from names, env vars or
  local settings (a tool named ``*_CREATE_*`` the catalog calls a read IS a read);
* a tool or connector the table does not name takes the table's ``unlisted_*`` row;
* the platform unreachable → the LAST known table keeps applying (never a wider one), and
  with no table at all every connector call is DENIED (fail-closed);
* autonomy ``propose`` parks a write the table allows; it never opens anything;
* the router meta-tools, the approval coalescence and the loop-guard signature keep
  their contracts.
"""
import asyncio

import pytest

from app import mcp_policy as P


def _row(name, permission, io="write", source="inherited_agent", runtime="kommo"):
    return {"name": name, "key": f"{runtime}_{name}", "io_kind": io, "permission": permission,
            "source": source, "agent_permission": permission}


TABLE = {
    "agent_id": 7, "version_id": 34, "agent_authority": "write_with_approval",
    "unlisted_connector": {"permission": "ask"},
    "connectors": [
        {"name": "composio-kommo", "runtime_name": "kommo", "mode": "read_only",
         "employee_mode": "inherit", "unlisted_tool": {"permission": "deny"},
         "tools": [_row("KOMMO_LIST_LEADS", "allow", "read", "read"),
                   _row("KOMMO_CREATE_LEAD", "deny", source="connector_mode"),
                   # The catalog says it READS although its name looks like a write: the
                   # catalog rules, not the name.
                   _row("KOMMO_CREATE_REPORT_VIEW", "allow", "read", "read")]},
        {"name": "gmail", "runtime_name": "gmail", "mode": "inherit",
         "employee_mode": "custom", "unlisted_tool": {"permission": "ask"},
         "tools": [_row("GMAIL_LIST_MESSAGES", "allow", "read", "read", "gmail"),
                   _row("GMAIL_SEND_EMAIL", "ask", runtime="gmail"),
                   _row("GMAIL_DELETE_MESSAGE", "ask", source="employee", runtime="gmail"),
                   _row("GMAIL_ARCHIVE", "allow", source="custom", runtime="gmail")]},
    ],
}


@pytest.fixture(autouse=True)
def _tabla(monkeypatch):
    """The table the platform returns, and autonomy neutral (``execute``).

    Autonomy is pinned on purpose: its default is `propose`, and without pinning it every
    assertion here would also measure that coupling without saying so."""
    P.reset_table_cache()
    estado = {"tabla": TABLE, "llamadas": 0}

    async def fetch():
        estado["llamadas"] += 1
        if isinstance(estado["tabla"], Exception):
            raise estado["tabla"]
        return estado["tabla"]

    async def sin_persistencia(*_a):
        return None

    async def autonomia():
        return P.POLICY_WRITE

    monkeypatch.setattr(P, "_fetch_table", fetch)
    monkeypatch.setattr(P, "_persist", sin_persistencia)
    monkeypatch.setattr(P, "_load_persisted", sin_persistencia)
    monkeypatch.setattr(P, "_por_autonomia", autonomia)
    yield estado
    P.reset_table_cache()


def d(*a, **kw):
    return asyncio.run(P.decide_call(*a, **kw))


def test_the_verdict_is_the_table_row():
    assert d("kommo", "KOMMO_LIST_LEADS", {})[:2] == ("allow", [])
    assert d("kommo", "KOMMO_CREATE_LEAD", {}) == ("deny", ["KOMMO_CREATE_LEAD"], "read_only")
    assert d("gmail", "GMAIL_SEND_EMAIL", {}) == ("approve", ["GMAIL_SEND_EMAIL"], "approval")
    # A custom `allow` on a write is what the agent decided: it passes.
    assert d("gmail", "GMAIL_ARCHIVE", {})[0] == "allow"
    # The catalog says it reads → it reads, whatever its name says.
    assert d("kommo", "KOMMO_CREATE_REPORT_VIEW", {})[0] == "allow"
    # By catalog name OR runtime name, by name OR opencode key.
    assert d("composio-kommo", "KOMMO_CREATE_LEAD", {})[0] == "deny"
    assert d("kommo", "kommo_KOMMO_LIST_LEADS", {})[0] == "allow"


def test_an_explicit_rule_parks_as_ask_and_is_not_coalesced():
    # The employee's own rule in `custom` is a choice on THAT tool: an approved reaction
    # does not lift it. The write gate (`approval`) is lifted by the reaction's grant.
    assert d("gmail", "GMAIL_DELETE_MESSAGE", {})[2] == "ask"
    assert d("gmail", "GMAIL_DELETE_MESSAGE", {}, write_granted=True)[0] == "approve"
    assert d("gmail", "GMAIL_SEND_EMAIL", {}, write_granted=True)[0] == "allow"
    assert d("kommo", "KOMMO_CREATE_LEAD", {}, write_granted=True)[0] == "deny"


def test_what_the_table_does_not_name_takes_its_unlisted_row():
    assert d("kommo", "KOMMO_FROBNICATE", {})[0] == "deny"
    assert d("gmail", "GMAIL_SOMETHING_NEW", {})[0] == "approve"
    assert d("desconocido", "X_LIST", {})[0] == "approve"


def test_router_execute_is_judged_on_the_inner_slug():
    assert d("kommo", "kommo_COMPOSIO_EXECUTE_TOOL", {"tool_slug": "KOMMO_CREATE_LEAD"})[:2] == \
        ("deny", ["KOMMO_CREATE_LEAD"])
    assert d("kommo", "kommo_COMPOSIO_EXECUTE_TOOL", {"tool_slug": "KOMMO_LIST_LEADS"})[0] == "allow"
    # Unverifiable EXECUTE: worth what an unlisted tool is worth → restricted → deny.
    assert d("kommo", "kommo_COMPOSIO_EXECUTE_TOOL", {"junk": 1}) == ("deny", [], "unverified")
    assert d("kommo", "kommo_COMPOSIO_SEARCH_TOOLS", {})[0] == "allow"
    live = {"session_id": "pine", "current_step": "FETCHING_LEADS_PAGE_1", "thought": "x",
            "tools": [{"tool_slug": "KOMMO_LIST_LEADS", "arguments": {"limit": 250}}]}
    assert P.unwrap_execute_slugs("COMPOSIO_MULTI_EXECUTE_TOOL", live) == ["KOMMO_LIST_LEADS"]
    assert d("kommo", "kommo_COMPOSIO_MULTI_EXECUTE_TOOL", live)[0] == "allow"
    mixed = {"tools": [{"tool_slug": "KOMMO_LIST_LEADS"}, {"tool_slug": "KOMMO_CREATE_LEAD"}]}
    assert d("kommo", "kommo_COMPOSIO_MULTI_EXECUTE_TOOL", mixed)[0] == "deny"
    assert P.unwrap_execute_slugs("COMPOSIO_MULTI_EXECUTE_TOOL", {"payload_blob": "KOMMO_CREATE_LEAD"}) == []


def test_autonomy_propose_parks_an_allowed_write_and_opens_nothing(monkeypatch):
    async def propose():
        return P.POLICY_APPROVAL
    monkeypatch.setattr(P, "_por_autonomia", propose)
    assert d("gmail", "GMAIL_ARCHIVE", {})[:2] == ("approve", ["GMAIL_ARCHIVE"])
    assert d("kommo", "KOMMO_LIST_LEADS", {})[0] == "allow"
    assert d("kommo", "KOMMO_CREATE_LEAD", {})[0] == "deny"
    assert asyncio.run(P.effective_policy("gmail")) == P.POLICY_APPROVAL


def test_platform_unreachable_keeps_the_last_table_never_wider(_tabla, monkeypatch):
    assert d("kommo", "KOMMO_CREATE_LEAD", {})[0] == "deny"
    _tabla["tabla"] = RuntimeError("platform down")
    monkeypatch.setattr(P, "_TABLE_TTL_S", 0.0)
    assert d("kommo", "KOMMO_CREATE_LEAD", {})[0] == "deny"
    assert d("kommo", "KOMMO_LIST_LEADS", {})[0] == "allow"
    assert P.table_is_fresh() is False


def test_no_table_at_all_denies_every_connector_call(_tabla):
    _tabla["tabla"] = None
    assert d("kommo", "KOMMO_LIST_LEADS", {}) == ("deny", ["KOMMO_LIST_LEADS"], "unloaded")
    assert "could not be loaded" in P.deny_message("kommo", "unloaded")
    assert P.filter_tools_list("kommo", [{"name": "KOMMO_LIST_LEADS"}], None) == []


def test_the_persisted_table_survives_a_restart(_tabla, monkeypatch):
    _tabla["tabla"] = None

    async def persistida():
        return TABLE
    monkeypatch.setattr(P, "_load_persisted", persistida)
    assert d("kommo", "KOMMO_LIST_LEADS", {})[0] == "allow"
    assert d("kommo", "KOMMO_CREATE_LEAD", {})[0] == "deny"


def test_cache_and_invalidation(_tabla):
    asyncio.run(P.load_table())
    asyncio.run(P.load_table())
    assert _tabla["llamadas"] == 1
    P.invalidate_table()
    asyncio.run(P.load_table())
    assert _tabla["llamadas"] == 2


def test_tools_list_hides_what_the_table_denies():
    tools = [{"name": "KOMMO_LIST_LEADS"}, {"name": "KOMMO_CREATE_LEAD"},
             {"name": "KOMMO_FROBNICATE"}, {"name": "kommo_COMPOSIO_MULTI_EXECUTE_TOOL"}]
    kept = P.filter_tools_list("kommo", tools, TABLE)
    assert [t["name"] for t in kept] == ["KOMMO_LIST_LEADS", "kommo_COMPOSIO_MULTI_EXECUTE_TOOL"]
    # `ask` stays visible: the agent must know what it can request approval for.
    assert len(P.filter_tools_list("gmail", [{"name": "GMAIL_SEND_EMAIL"}], TABLE)) == 1


def test_headlines_are_a_reading_of_the_table():
    assert P.connector_policy(TABLE, "kommo") == P.POLICY_READ_ONLY
    # The most a connector can do with a write: gmail has one allowed write (custom).
    assert P.connector_policy(TABLE, "gmail") == P.POLICY_WRITE
    assert P.connector_policy(TABLE, "otro") == P.POLICY_APPROVAL
    assert P.connector_policy(None, "gmail") == P.POLICY_READ_ONLY
    assert P.denied_tool_keys(TABLE) == ["kommo_KOMMO_CREATE_LEAD"]
    block = asyncio.run(P.write_policy_block())
    assert "Read-only" in block and "kommo" in block
    assert "WITH APPROVAL" not in block      # gmail can write (one custom allow)


def test_write_signature_metadata_stable():
    a1 = {"session_id": "x", "current_step": "FETCHING_1", "thought": "a",
          "tools": [{"tool_slug": "KOMMO_CREATE_LEAD", "arguments": {"name": "Ana"}}]}
    a2 = {"session_id": "y", "current_step": "STEP_2", "thought": "b",
          "tools": [{"tool_slug": "KOMMO_CREATE_LEAD", "arguments": {"name": "Ana"}}]}
    a3 = {"tools": [{"tool_slug": "KOMMO_CREATE_LEAD", "arguments": {"name": "Bea"}}]}
    s1 = P.write_signature("kommo", "COMPOSIO_MULTI_EXECUTE_TOOL", a1)
    assert s1 == P.write_signature("kommo", "COMPOSIO_MULTI_EXECUTE_TOOL", a2)
    assert s1 != P.write_signature("kommo", "COMPOSIO_MULTI_EXECUTE_TOOL", a3)
    assert s1 != P.write_signature("gmail", "COMPOSIO_MULTI_EXECUTE_TOOL", a1)


def test_args_hash_binding():
    h1 = P.call_args_hash("GMAIL_SEND_EMAIL", {"b": 2, "a": 1})
    h2 = P.call_args_hash("GMAIL_SEND_EMAIL", {"a": 1, "b": 2})
    h3 = P.call_args_hash("GMAIL_SEND_EMAIL", {"a": 1, "b": 3})
    assert h1 == h2 and h1 != h3


def test_veto_texts_for_direct_callers():
    v = asyncio.run(P.veto_for_call("kommo", "KOMMO_CREATE_LEAD", {"n": 1}))
    assert v and "READ-ONLY" in v
    v2 = asyncio.run(P.veto_for_call("gmail", "GMAIL_SEND_EMAIL", {"to": "a@b.c"}))
    assert v2 and "request_approval" in v2 and "request_ref" in v2
    assert asyncio.run(P.veto_for_call("gmail", "GMAIL_SEND_EMAIL", {}, approved_bypass=True)) is None


def test_the_pod_no_longer_reads_the_old_inputs():
    """One calculation, in the platform. If any of these come back, the pod is
    recalculating on its own again — exactly what the model was built to end."""
    import inspect
    src = inspect.getsource(P)
    for viejo in ("os.environ", '"mcp_tool_permissions"', '"mcp_write_policy"',
                  "_local_hardening", "classify_tool", "_READ_VERBS"):
        assert viejo not in src, viejo
