"""14.27.0 — the connection history: every connect / disconnect is recorded with WHO did it and only the non-secret
account data (a customer asked who disconnected their Odoo and nobody could say)."""
import asyncio
from types import SimpleNamespace

from app import connection_events as ce
from app.routes import mcp_connectors_routes as R


def test_connect_and_disconnect_are_recorded_with_the_person(monkeypatch):
    apuntados = []

    async def record(connector, action, actor, account=None, result="ok", detail=None):
        apuntados.append((connector, action, actor, account, result))

    async def connect(connector, body, user):
        return {"mode": "credentials", "connected": True, "status": "ACTIVE", "connected_account_id": "ca_1"}

    async def disconnect(connector, user):
        return {"deleted": True, "revoked_accounts": 1}
    monkeypatch.setattr(ce, "record", record)
    monkeypatch.setattr(R, "_connect", connect)
    monkeypatch.setattr(R, "_disconnect", disconnect)
    quien = SimpleNamespace(email="sistemas@plexa.com.mx")
    asyncio.run(R.connect_mcp_connector("odoo-17-18", {"credentials": {
        "url": "https://x.odoo.com", "db": "x", "login": "api@x", "api_key": "SECRETO"}}, quien))
    asyncio.run(R.disconnect_mcp_connector("odoo-17-18", quien))
    (c, d) = apuntados
    assert c[:3] == ("odoo-17-18", "connect", "sistemas@plexa.com.mx") and c[4] == "ok"
    assert c[3] == {"url": "https://x.odoo.com", "db": "x", "login": "api@x", "connected_account_id": "ca_1"}
    assert d[:3] == ("odoo-17-18", "disconnect", "sistemas@plexa.com.mx")


def test_only_non_secret_account_fields():
    assert ce.account_of({"login": "a", "password": "p", "api_key": "k", "token": "t"}) == {"login": "a"}
