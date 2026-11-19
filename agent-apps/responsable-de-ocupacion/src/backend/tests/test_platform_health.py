"""v7.89.1/v7.89.2 — the ``platform_degraded`` health blocker contract.

``skills_catalog.platform_status()`` distinguishes the deployer never
injecting ``WAI_BACKEND_URL``/``BACKEND_API_SECRET`` (``missing``) from the
env being set but the platform not answering / rejecting the secret
(``unreachable``, via a TTL-cached live probe). ``GET /api/agent/health``
surfaces both as a ``platform_degraded`` blocker with ``params.reason`` so
the operator learns the skills catalog AND the (fail-closed, v7.89.2) MCP
bridge are degraded instead of guessing.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


class _StubResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _StubClient:
    """Minimal httpx.AsyncClient stand-in: fixed status or raised error."""

    def __init__(self, status_code: int = 200, exc: Exception | None = None) -> None:
        self._status = status_code
        self._exc = exc

    def __call__(self, *args, **kwargs):  # AsyncClient(timeout=…) → instance
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, *args, **kwargs):
        if self._exc:
            raise self._exc
        return _StubResponse(self._status)


def _fresh_cache(monkeypatch) -> None:
    import app.skills_catalog as sc
    monkeypatch.setattr(sc, "_probe_cache", {"at": 0.0, "status": "unknown"})


def _como_admin(monkeypatch) -> None:
    """The `platform_degraded` notice is addressed to an ADMIN (`audience: admin`), and
    since the per-role filter (`_visibles_para`) exists, reaching it depends on
    `users_directory.is_admin`, which queries the app's directory.

    Without a database that falls —on purpose— on the `member` side, so the test
    received ZERO notices and blamed health for not emitting the one it did emit.
    The role is pinned here: what is being measured is the notice's contract, not
    the directory (which has its own tests)."""
    import app.users_directory as ud
    async def _si(_email=None):
        return True
    monkeypatch.setattr(ud, "is_admin", _si)
    monkeypatch.setattr(ud, "can_approve", _si)


def _configure(monkeypatch, url: str, secret: str) -> None:
    import app.skills_catalog as sc
    monkeypatch.setattr(sc, "WAI_BACKEND_URL", url)
    monkeypatch.setattr(sc, "BACKEND_API_SECRET", secret)


async def test_status_missing_without_env(monkeypatch) -> None:
    import app.skills_catalog as sc
    _configure(monkeypatch, "", "")
    _fresh_cache(monkeypatch)
    assert await sc.platform_status() == "missing"


async def test_status_ok_and_cached(monkeypatch) -> None:
    import app.skills_catalog as sc
    _configure(monkeypatch, "http://backend:5002", "s3cret")
    _fresh_cache(monkeypatch)
    monkeypatch.setattr(sc.httpx, "AsyncClient", _StubClient(200))
    assert await sc.platform_status() == "ok"
    # Second call must serve the TTL cache, not the (now broken) transport.
    monkeypatch.setattr(sc.httpx, "AsyncClient", _StubClient(exc=RuntimeError("net down")))
    assert await sc.platform_status() == "ok"


async def test_status_unreachable_on_error_or_bad_secret(monkeypatch) -> None:
    import app.skills_catalog as sc
    _configure(monkeypatch, "http://backend:5002", "s3cret")
    _fresh_cache(monkeypatch)
    monkeypatch.setattr(sc.httpx, "AsyncClient", _StubClient(exc=OSError("refused")))
    assert await sc.platform_status() == "unreachable"

    _fresh_cache(monkeypatch)
    monkeypatch.setattr(sc.httpx, "AsyncClient", _StubClient(403))
    assert await sc.platform_status() == "unreachable"


async def test_health_surfaces_platform_blocker(monkeypatch) -> None:
    import app.skills_catalog as sc
    from app.auth import TokenPayload
    from app.routes.activity_routes import get_health

    _configure(monkeypatch, "", "")
    _fresh_cache(monkeypatch)
    _como_admin(monkeypatch)
    user = TokenPayload(email="ops@wai.com", is_superuser=True, project_ids=[], data_areas=[])
    health = await get_health(user=user)
    blockers = {b["key"]: b for b in health.get("blockers", [])}
    assert "platform_degraded" in blockers
    assert blockers["platform_degraded"]["params"]["reason"] == "missing"


async def test_health_quiet_when_platform_ok(monkeypatch) -> None:
    import app.skills_catalog as sc
    from app.auth import TokenPayload
    from app.routes.activity_routes import get_health

    _configure(monkeypatch, "http://backend:5002", "s3cret")
    _fresh_cache(monkeypatch)
    monkeypatch.setattr(sc.httpx, "AsyncClient", _StubClient(200))
    _como_admin(monkeypatch)
    user = TokenPayload(email="ops@wai.com", is_superuser=True, project_ids=[], data_areas=[])
    health = await get_health(user=user)
    assert all(b["key"] != "platform_degraded" for b in health.get("blockers", []))
