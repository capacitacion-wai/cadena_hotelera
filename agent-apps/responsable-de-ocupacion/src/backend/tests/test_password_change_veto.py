"""v7.124.0 — the mandatory password change (auth F1) is enforced ON THE SERVER.

The SPA gate is a UI barrier: with the token in hand, anyone can call the API with
curl. The contract frozen here:

* the ``must_change_password`` claim of the platform JWT reaches ``TokenPayload``;
* if it is ``true`` the session is QUARANTINED → 403 with the functional code
  ``PASSWORD_CHANGE_REQUIRED`` (in the ``detail`` and in ``X-Auth-Error-Code``);
* the allow-list (login, password change, Google sign-in, health probes) stays
  open: otherwise the change screen could never be closed;
* WITHOUT the claim there is NO veto — an old token or a platform older than auth F1
  must not lock anyone out of their employee.
"""
from __future__ import annotations

import pytest

from app.auth import (
    PASSWORD_CHANGE_REQUIRED_CODE,
    QUARANTINE_ALLOWED_PATHS,
    TokenPayload,
    enforce_password_change,
)
from fastapi import HTTPException


def _user(must_change: bool) -> TokenPayload:
    return TokenPayload(
        email="maria@acme.com",
        is_superuser=False,
        project_ids=[1],
        data_areas=[],
        must_change_password=must_change,
    )


def test_claim_defaults_to_false() -> None:
    """Token without the claim (platform older than auth F1) ⇒ no veto."""
    legacy = TokenPayload(email="a@b.c", is_superuser=False, project_ids=[], data_areas=[])
    assert legacy.must_change_password is False
    enforce_password_change(legacy, "/api/agent/manifest")  # does not raise


def test_quarantine_vetoes_the_whole_api() -> None:
    with pytest.raises(HTTPException) as exc:
        enforce_password_change(_user(True), "/api/agent/manifest")
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == PASSWORD_CHANGE_REQUIRED_CODE
    assert exc.value.headers["X-Auth-Error-Code"] == PASSWORD_CHANGE_REQUIRED_CODE


@pytest.mark.parametrize("path", sorted(QUARANTINE_ALLOWED_PATHS))
def test_whitelist_stays_open(path: str) -> None:
    """The minimum for the change screen to be closable (and for K8s to keep probing
    the pod) is NOT vetoed."""
    enforce_password_change(_user(True), path)


def test_superuser_has_no_exemption() -> None:
    """The flag is set by the platform: not even a superuser escapes the quarantine."""
    boss = TokenPayload(
        email="admin@wai.com",
        is_superuser=True,
        project_ids=[1],
        data_areas=[],
        must_change_password=True,
    )
    with pytest.raises(HTTPException):
        enforce_password_change(boss, "/api/agent/manifest")


def test_decode_token_reads_the_claim(monkeypatch) -> None:
    """The claim comes from the PLATFORM (introspection, 14.0.0), not from an optimistic default.
    The pod no longer holds the signing key: the test token's signature does not matter."""
    import asyncio
    import time

    import httpx
    from jose import jwt

    from app import auth as auth_mod

    def respuesta(request: httpx.Request) -> httpx.Response:
        import json as _json
        token = _json.loads(request.content)["token"]
        claims = jwt.get_unverified_claims(token)
        return httpx.Response(200, json={"activo": True, "claims": claims})

    monkeypatch.setattr(auth_mod, "WAI_INTROSPECCION_URL", "http://backend/introspeccion")
    monkeypatch.setattr(auth_mod, "_cliente_http",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(respuesta)))
    auth_mod._cache.clear()

    exp = int(time.time()) + 600
    token = jwt.encode(
        {"sub": "maria@acme.com", "project_ids": [1], "must_change_password": True, "exp": exp},
        "cualquiera", algorithm="HS256",
    )
    assert asyncio.run(auth_mod.decode_token(token)).must_change_password is True

    clean = jwt.encode(
        {"sub": "maria@acme.com", "project_ids": [1], "exp": exp},
        "cualquiera", algorithm="HS256",
    )
    assert asyncio.run(auth_mod.decode_token(clean)).must_change_password is False
    auth_mod._cache.clear()
