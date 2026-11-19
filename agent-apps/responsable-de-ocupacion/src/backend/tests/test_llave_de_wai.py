"""The employee's WAI key renews itself (12/11/2026): eleven employees in six tenants lost their connectors the same
day because their 30-day key expired and only a redeploy renewed it."""
from __future__ import annotations

import asyncio
import base64
import json
import time

import httpx
import pytest

from app import llave_de_wai as LL

ENGINE = "http://demo-wai-engine-api.demo.svc.cluster.local:5001"


def _jwt(days: float, agent: int = 6, sub: str = "nora@demo") -> str:
    def b(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{b({'alg': 'HS256'})}.{b({'sub': sub, 'agent_id': agent, 'is_agent_runtime': True, 'exp': int(time.time() + days * 86400)})}.sig"


@pytest.fixture
def engine(monkeypatch, tmp_path):
    seen: dict = {"requests": [], "answer": (200, {"llave": _jwt(30)})}

    def handle(request: httpx.Request) -> httpx.Response:
        seen["requests"].append(request)
        status, body = seen["answer"]
        return httpx.Response(status, json=body)

    real = httpx.AsyncClient
    monkeypatch.setattr(LL.httpx, "AsyncClient", lambda *a, **kw: real(*a, **{**kw, "transport": httpx.MockTransport(handle)}))
    monkeypatch.setattr(LL, "KEPT_PATH", tmp_path / "wai-key.json")
    monkeypatch.setattr(LL, "_last_try", 0.0)
    return seen


def test_a_key_about_to_expire_is_renewed_kept_and_reused(engine):
    old = _jwt(5)
    new = asyncio.run(LL.llave(old, ENGINE))
    assert new == engine["answer"][1]["llave"]
    req = engine["requests"][0]
    expected_path = "/" + "opencode/empleado/llave/renovar"
    assert str(req.url) == f"{ENGINE}{expected_path}" and req.headers["Authorization"] == f"Bearer {old}"
    # After a restart the Secret still has the old one: the kept one wins, without asking again.
    assert asyncio.run(LL.llave(old, ENGINE)) == new and len(engine["requests"]) == 1


def test_with_time_to_spare_expired_or_not_a_jwt_nothing_is_asked(engine):
    for base in (_jwt(20), _jwt(-1), "not-a-jwt"):
        assert asyncio.run(LL.llave(base, ENGINE)) == base
    assert not engine["requests"]


def test_a_refused_or_failed_renewal_keeps_the_current_key_and_waits_before_retrying(engine):
    engine["answer"] = (401, {"code": "LLAVE_NO_RENOVABLE"})
    old = _jwt(5)
    assert asyncio.run(LL.llave(old, ENGINE)) == old
    assert asyncio.run(LL.llave(old, ENGINE)) == old
    assert len(engine["requests"]) == 1                     # not on every call


def test_a_kept_key_of_another_employee_is_never_used(engine):
    LL.KEPT_PATH.write_text(json.dumps({"llave": _jwt(25, agent=7)}))
    old = _jwt(15)
    assert asyncio.run(LL.llave(old, ENGINE)) == old
