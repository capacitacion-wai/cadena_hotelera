"""14.0.0 — the employee no longer holds the platform's TWO shared secrets.

* The tenant's JWT signing key: it served to validate the token of whoever comes in and
  also to FORGE anyone's. Now the token is checked by asking the platform
  (`WAI_INTROSPECCION_URL`), with a short cache and 503 —not 401— if it does not answer.
* The auth-service's GLOBAL HMAC secret: with it one could act as any identity of any
  tenant. Now Composio is reached through the tenant's engine
  (`/opencode/empleado/composio/{operacion}`) with the employee's runtime JWT.

What is frozen here is the CONTRACT with those two pieces (URL, headers, body, what is
cached and for how long, which code the SPA sees), and that the file-link key derives from
`AGENT_SHARED_TOKEN`. All against fake transports: no network, no platform.
"""
from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
from fastapi import HTTPException
from jose import jwt

from app import auth
from app import mcp_connectors as mc

ENGINE = "http://demo-wai-engine-api.demo.svc.cluster.local:5001"
RUNTIME = "jwt-de-runtime-del-empleado"
INTRO = "http://demo-wai-backend.demo.svc.cluster.local:5002/backend/auth/introspeccion"


# ── Composio through the engine ───────────────────────────────────────────────────────


@pytest.fixture
def engine(monkeypatch):
    """Replaces the HTTP client of `mcp_connectors` with one that records the requests and
    answers whatever `respuesta` says (status, json)."""
    visto: dict = {"peticiones": [], "respuesta": (200, {"ok": True})}

    def manejar(request: httpx.Request) -> httpx.Response:
        visto["peticiones"].append(request)
        status, cuerpo = visto["respuesta"]
        return httpx.Response(status, json=cuerpo)

    real = httpx.AsyncClient

    def cliente(*a, **kw):
        kw["transport"] = httpx.MockTransport(manejar)
        return real(*a, **kw)

    monkeypatch.setattr(mc.httpx, "AsyncClient", cliente)
    monkeypatch.setattr(mc, "WAI_ENGINE_URL", ENGINE)
    monkeypatch.setattr(mc, "WAI_AGENT_RUNTIME_JWT", RUNTIME)
    return visto


def test_disponible_depende_del_engine_y_del_jwt_de_runtime(monkeypatch):
    monkeypatch.setattr(mc, "WAI_ENGINE_URL", ENGINE)
    monkeypatch.setattr(mc, "WAI_AGENT_RUNTIME_JWT", "")
    assert mc.is_enabled() is False
    monkeypatch.setattr(mc, "WAI_AGENT_RUNTIME_JWT", RUNTIME)
    assert mc.is_enabled() is True
    monkeypatch.setattr(mc, "WAI_ENGINE_URL", "")
    assert mc.is_enabled() is False


def test_la_llamada_va_al_engine_con_el_jwt_de_runtime_y_sin_firma(engine):
    engine["respuesta"] = (200, {"connections": [{"id": "ca_1", "toolkit": "gmail"}]})
    cuentas = asyncio.run(mc.list_connections("Admin@WAI.com", "composio-gmail"))
    assert cuentas == [{"id": "ca_1", "toolkit": "gmail"}]

    (r,) = engine["peticiones"]
    assert str(r.url) == f"{ENGINE}/opencode/empleado/composio/connections"
    assert r.headers["authorization"] == f"Bearer {RUNTIME}"
    # Neither the global-secret signature nor the tenant: the engine sets the tenant, its own.
    assert "x-wai-oauth-signature" not in r.headers
    assert "x-wai-tenant" not in r.headers
    cuerpo = json.loads(r.content)
    # The usual identity, byte for byte: the body does not change, only who it is sent to.
    assert cuerpo == {"user_email": mc.composio_identity("admin@wai.com", "composio-gmail")}


def test_operacion_con_barra_conserva_la_ruta(engine):
    engine["respuesta"] = (200, {"server_id": "srv_1"})
    assert asyncio.run(mc.ensure_server("google_maps")) == "srv_1"
    (r,) = engine["peticiones"]
    assert str(r.url) == f"{ENGINE}/opencode/empleado/composio/mcp/create-server"
    assert json.loads(r.content) == {"name": "wai-google-maps", "toolkit": "google_maps"}


def test_desconectar_manda_la_identidad_de_la_cuenta(engine, monkeypatch):
    engine["respuesta"] = (200, {"ok": True})
    asyncio.run(mc.disconnect("ca_9", "admin@wai.com", "composio-gmail"))
    (r,) = engine["peticiones"]
    assert str(r.url).endswith("/opencode/empleado/composio/disconnect")
    cuerpo = json.loads(r.content)
    assert cuerpo["connected_account_id"] == "ca_9"
    assert cuerpo["user_email"] == mc.composio_identity("admin@wai.com", "composio-gmail")


def test_desconectar_sin_identidad_no_llega_a_llamar(engine):
    with pytest.raises(mc.McpConnectorError, match="identidad"):
        asyncio.run(mc.disconnect("ca_9"))
    assert engine["peticiones"] == []


@pytest.mark.parametrize("status, cuerpo, esperado", [
    (401, {"detail": "x", "code": "NO_AUTENTICADO"}, "credencial de este empleado"),
    (403, {"detail": "x", "code": "IDENTIDAD_AJENA"}, "rechaza la identidad"),
    (403, {"detail": "x", "code": "CUENTA_AJENA"}, "no es de este conector"),
    (400, {"detail": "x", "code": "FALTA_IDENTIDAD"}, "falta la identidad"),
    (502, {"detail": "x", "code": "AUTH_SERVICE_NO_RESPONDE"}, "no responde"),
])
def test_cada_no_del_engine_se_explica(engine, status, cuerpo, esperado):
    engine["respuesta"] = (status, cuerpo)
    with pytest.raises(mc.McpConnectorError) as exc:
        asyncio.run(mc.auth_info("gmail"))
    assert esperado in str(exc.value)
    assert cuerpo["code"] in str(exc.value)


def test_sin_engine_no_se_intenta(monkeypatch):
    monkeypatch.setattr(mc, "WAI_ENGINE_URL", "")
    with pytest.raises(mc.McpConnectorError, match="WAI_ENGINE_URL"):
        asyncio.run(mc.auth_info("gmail"))


# ── Introspection of the incoming user's token ──────────────────────────────────────────


def _token(sub: str = "maria@acme.com", exp_en: float = 3600) -> str:
    # The signature does not matter: the pod no longer has the key and does not check it —
    # the platform does. It is only used for the cheap shape and `exp` filter.
    return jwt.encode({"sub": sub, "exp": int(time.time() + exp_en)}, "irrelevante",
                      algorithm="HS256")


@pytest.fixture
def plataforma(monkeypatch):
    """The fake platform: answers `respuesta` (status, json) or raises `fallo`."""
    visto: dict = {"llamadas": 0, "respuesta": None, "fallo": None, "espera": 0.0}

    async def manejar(request: httpx.Request) -> httpx.Response:
        visto["llamadas"] += 1
        visto["ultimo"] = json.loads(request.content)
        if visto["espera"]:
            await asyncio.sleep(visto["espera"])
        if visto["fallo"] is not None:
            raise visto["fallo"]
        status, cuerpo = visto["respuesta"]
        return httpx.Response(status, json=cuerpo)

    def cliente():
        return httpx.AsyncClient(transport=httpx.MockTransport(manejar), timeout=3.0)

    monkeypatch.setattr(auth, "_cliente_http", cliente)
    monkeypatch.setattr(auth, "WAI_INTROSPECCION_URL", INTRO)
    auth._cache.clear()
    auth._en_vuelo.clear()
    yield visto
    auth._cache.clear()


def _activo(**claims) -> tuple[int, dict]:
    base = {"sub": "maria@acme.com", "is_superuser": False, "project_ids": [1],
            "data_areas": [], "exp": int(time.time() + 3600)}
    base.update(claims)
    return 200, {"activo": True, "claims": base}


def test_el_claim_de_cuarentena_llega_desde_la_plataforma(plataforma):
    plataforma["respuesta"] = _activo(must_change_password=True)
    tk = _token()
    user = asyncio.run(auth.decode_token(tk))
    assert user.email == "maria@acme.com"
    assert user.must_change_password is True
    assert user.project_ids == [1]
    assert plataforma["ultimo"] == {"token": tk}


def test_un_si_se_cachea_y_no_vuelve_a_preguntar(plataforma):
    plataforma["respuesta"] = _activo()
    tk = _token()
    asyncio.run(auth.decode_token(tk))
    asyncio.run(auth.decode_token(tk))
    assert plataforma["llamadas"] == 1
    # The key is the hash: the token is not stored in clear.
    assert tk not in auth._cache


def test_el_si_no_dura_mas_que_el_token(plataforma):
    plataforma["respuesta"] = _activo(exp=int(time.time() + 5))
    tk = _token(exp_en=5)
    asyncio.run(auth.decode_token(tk))
    (caduca, _), = auth._cache.values()
    assert caduca - time.monotonic() <= 5.5


def test_un_no_da_401_y_se_cachea_diez_segundos(plataforma):
    plataforma["respuesta"] = (200, {"activo": False, "motivo": "firma"})
    tk = _token()
    for _ in range(2):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(auth.decode_token(tk))
        assert exc.value.status_code == 401
    assert plataforma["llamadas"] == 1
    (caduca, valor), = auth._cache.values()
    assert valor is None and caduca - time.monotonic() <= auth._TTL_NO_S


@pytest.mark.parametrize("respuesta, fallo", [
    ((429, {"detail": "frena"}), None),
    ((500, {"detail": "roto"}), None),
    ((503, {"detail": "roto"}), None),
    (None, httpx.ConnectError("sin red")),
    (None, httpx.ReadTimeout("lento")),
])
def test_si_la_plataforma_no_contesta_es_503_y_no_se_cachea(plataforma, respuesta, fallo):
    plataforma["respuesta"], plataforma["fallo"] = respuesta, fallo
    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth.decode_token(_token()))
    # 503, NEVER 401: the SPA treats 401 as an expired session and would log the person out.
    assert exc.value.status_code == 503
    assert auth._cache == {}


def test_basura_y_caducados_no_gastan_un_viaje(plataforma):
    plataforma["respuesta"] = _activo()
    for malo in ("", "no-es-un-jwt", "a.b.c", _token(exp_en=-10), "x" * 5000):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(auth.decode_token(malo))
        assert exc.value.status_code == 401
    assert plataforma["llamadas"] == 0


def test_la_cache_esta_acotada(plataforma, monkeypatch):
    monkeypatch.setattr(auth, "_CACHE_MAX", 3)
    plataforma["respuesta"] = _activo()
    for i in range(5):
        asyncio.run(auth.decode_token(_token(sub=f"p{i}@acme.com")))
    assert len(auth._cache) == 3


def test_peticiones_simultaneas_con_el_mismo_token_preguntan_una_vez(plataforma):
    plataforma["respuesta"] = _activo()
    plataforma["espera"] = 0.05
    tk = _token()

    async def varias():
        return await asyncio.gather(*(auth.decode_token(tk) for _ in range(8)))

    usuarios = asyncio.run(varias())
    assert {u.email for u in usuarios} == {"maria@acme.com"}
    assert plataforma["llamadas"] == 1


def test_sin_url_de_introspeccion_es_503(plataforma, monkeypatch):
    monkeypatch.setattr(auth, "WAI_INTROSPECCION_URL", "")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth.decode_token(_token()))
    assert exc.value.status_code == 503


# ── What is left of the Composio email channel ─────────────────────────────────────────


def test_el_aviso_del_auth_service_esta_retirado_con_410():
    from app.routes import channels_routes

    ruta = next(r for r in channels_routes.router.routes
                if getattr(r, "path", "").endswith("/oauth/callback"))
    assert "POST" in ruta.methods
    with pytest.raises(HTTPException) as exc:
        asyncio.run(channels_routes.oauth_callback())
    assert exc.value.status_code == 410


# ── Signed file links ──────────────────────────────────────────────────────────────────


def test_los_enlaces_se_firman_con_una_llave_derivada_de_agent_shared_token(monkeypatch):
    import hashlib
    import hmac as _hmac

    from app import signed_media as sm

    monkeypatch.setattr(sm, "AGENT_SHARED_TOKEN", "token-de-esta-app")
    tk = sm.sign_media_token("out/informe.pdf")
    assert sm.verify_media_token(tk) == "out/informe.pdf"
    # The key is NOT the shared token as-is: a link is not valid as a Basic credential.
    esperado = _hmac.new(b"token-de-esta-app", b"wai-media-v1", hashlib.sha256).digest()
    assert sm._secret() == esperado
    # Another app (another token) does not accept the link.
    monkeypatch.setattr(sm, "AGENT_SHARED_TOKEN", "token-de-otra-app")
    assert sm.verify_media_token(tk) is None


def test_sin_agent_shared_token_no_hay_enlace(monkeypatch):
    from app import signed_media as sm

    monkeypatch.setattr(sm, "AGENT_SHARED_TOKEN", "")
    monkeypatch.setattr(sm, "public_base_url", lambda channel=None: "https://x.example.com")
    assert sm.signed_media_url("out/informe.pdf") is None
    assert sm.verify_media_token("a.b") is None
