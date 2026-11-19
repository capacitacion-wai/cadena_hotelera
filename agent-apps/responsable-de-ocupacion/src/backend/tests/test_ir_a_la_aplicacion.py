"""The link to THEIR application is a NAVIGATION: form → 303 with the pass (12.54.0).

Up to 12.53 the chat opened an empty tab and told it where to go once the pass arrived: the
person saw `about:blank` on every link. Now the server answers with the redirect, and that
makes this endpoint the most delicate part of the change: **a 303 to whatever the body says
is an open redirect**. What is frozen here:

  · the destination is honoured —path, query and fragment— ONLY if it belongs to their app;
  · scheme and host ALWAYS come from their application, not from the destination: comparing
    hosts is not enough (`https://otra.com\\@su-app/` is su-app for Python and otra.com for
    the browser);
  · without a pass it goes anyway, without it —the usual login screen—;
  · the pass goes in the QUERY, before the fragment;
  · the CSP allows their application in `form-action`, or the browser cuts the redirect.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

MIA = "https://viajes-demo.usewai.com"


class _Resp:
    def __init__(self, status: int, body: dict):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


def _cliente(monkeypatch, mia: str | None = MIA, plataforma=None) -> tuple[TestClient, list]:
    from app import mi_aplicacion
    from app.routes import auth_routes

    async def url():
        return mia
    monkeypatch.setattr(mi_aplicacion, "url", url)

    llamadas: list = []

    class _Http:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, headers=None):
            llamadas.append({"url": url, "json": json, "headers": headers})
            if isinstance(plataforma, Exception):
                raise plataforma
            return plataforma or _Resp(200, {"codigo": "PASE123", "caduca_en_s": 60})
    monkeypatch.setattr(auth_routes.httpx, "AsyncClient", _Http)

    app = FastAPI()
    app.include_router(auth_routes.router)
    return TestClient(app, follow_redirects=False), llamadas


def _post(c: TestClient, destino: str, token: str = "tok"):
    return c.post("/api/auth/ir-a-la-aplicacion", data={"destino": destino, "token": token})


def test_lleva_a_la_pagina_con_el_pase_antes_de_la_almohadilla(monkeypatch) -> None:
    c, llamadas = _cliente(monkeypatch)
    r = _post(c, f"{MIA}/#/viajes/4/agenda")
    assert r.status_code == 303
    u = urlsplit(r.headers["location"])
    assert (u.scheme, u.netloc) == ("https", "viajes-demo.usewai.com")
    assert parse_qs(u.query)["wai"] == ["PASE123"]
    assert u.fragment == "/viajes/4/agenda"
    # The pass is requested with THE PERSON's token and for THAT destination.
    assert llamadas[0]["headers"]["Authorization"] == "Bearer tok"
    assert urlsplit(llamadas[0]["json"]["destino"]).netloc == "viajes-demo.usewai.com"
    # No cache and no Referer: the URL carries a pass and the destination has no business
    # knowing where you came from.
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"


@pytest.mark.parametrize("ajeno", [
    "https://otra.example.com/robar",
    "https://viajes-demo.usewai.com.otra.example.com/",
    "javascript:alert(1)",
    "//otra.example.com/",
    # Python sees `viajes-demo.usewai.com`; the browser, `otra.example.com`.
    "https://otra.example.com\\@viajes-demo.usewai.com/",
])
def test_un_destino_ajeno_lleva_a_la_portada_de_su_aplicacion(monkeypatch, ajeno) -> None:
    c, _ = _cliente(monkeypatch)
    r = _post(c, ajeno)
    assert r.status_code == 303
    assert urlsplit(r.headers["location"]).netloc == "viajes-demo.usewai.com", r.headers["location"]


def test_ni_un_salto_de_linea_llega_a_la_cabecera(monkeypatch) -> None:
    c, _ = _cliente(monkeypatch)
    r = _post(c, f"{MIA}/a\r\nSet-Cookie: x=1")
    assert r.status_code == 303
    assert "\n" not in r.headers["location"] and "set-cookie" not in r.headers


@pytest.mark.parametrize("plataforma", [_Resp(401, {"detail": "no"}), RuntimeError("caída")])
def test_sin_pase_se_va_igual_sin_el(monkeypatch, plataforma) -> None:
    c, _ = _cliente(monkeypatch, plataforma=plataforma)
    r = _post(c, f"{MIA}/#/viajes/4")
    assert r.status_code == 303
    u = urlsplit(r.headers["location"])
    assert u.netloc == "viajes-demo.usewai.com" and "wai" not in parse_qs(u.query)
    assert u.fragment == "/viajes/4"


def test_sin_testigo_no_se_pide_pase(monkeypatch) -> None:
    c, llamadas = _cliente(monkeypatch)
    r = _post(c, f"{MIA}/", token="")
    assert r.status_code == 303 and "wai=" not in r.headers["location"]
    assert llamadas == []


def test_sin_aplicacion_asociada_no_se_redirige_a_ningun_sitio(monkeypatch) -> None:
    c, llamadas = _cliente(monkeypatch, mia=None)
    r = _post(c, "https://otra.example.com/")
    assert r.status_code == 404 and "location" not in r.headers
    assert llamadas == []


@pytest.mark.asyncio
async def test_la_csp_admite_su_aplicacion_en_form_action(monkeypatch) -> None:
    """The browser also applies `form-action` to the form's redirect: without their
    application there, the click does nothing and only the console tells."""
    from app import mi_aplicacion
    mi_aplicacion._cache.update({"cuando": 0.0, "valor": None})

    async def cual_es(refrescar: bool = False):
        return {"tiene": True, "url": f"{MIA}/", "nombre": "WAI Viajes"}
    monkeypatch.setattr(mi_aplicacion, "cual_es", cual_es)
    assert await mi_aplicacion.origen() == MIA

    async def ninguna(refrescar: bool = False):
        return {"tiene": False}
    monkeypatch.setattr(mi_aplicacion, "cual_es", ninguna)
    assert await mi_aplicacion.origen() is None
