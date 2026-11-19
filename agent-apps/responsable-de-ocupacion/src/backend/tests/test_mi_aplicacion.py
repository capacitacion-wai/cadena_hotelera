"""ITS WAI application: how it is resolved and what the employee is told (12.51.0).

The employee may have an associated application and return links to it. What is pinned
here is the only thing that cannot be seen by looking at a screen:

  · that **not knowing** which application is its own cannot break a turn — it behaves like one
    that has none, which is the normal state for almost everyone;
  · that the **cache** exists (this is queried on every turn) and that it can be forced;
  · that an application associated but **not deployed** produces no link: without an address
    there is nowhere to take you, and promising a screen that does not exist is worse than silence.
"""
from __future__ import annotations

import os
import tempfile

import pytest


@pytest.mark.asyncio
async def test_sin_plataforma_no_tiene_aplicacion_y_no_revienta(monkeypatch) -> None:
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    os.environ["APP_DATABASE_URL"] = f"sqlite+aiosqlite:///{path}"

    from app import mi_aplicacion as mia
    monkeypatch.setattr(mia, "WAI_BACKEND_URL", "", raising=False)
    mia._cache.update({"cuando": 0.0, "valor": None})

    assert await mia.cual_es() == {"tiene": False}
    assert await mia.url() is None
    assert await mia.para_el_prompt() == ""
    os.remove(path)


@pytest.mark.asyncio
async def test_lo_que_se_le_cuenta_lleva_la_direccion_y_cómo_enlazar(monkeypatch) -> None:
    from app import mi_aplicacion as mia
    mia._cache.update({"cuando": 0.0, "valor": None})

    async def falsa(refrescar: bool = False):
        return {"tiene": True, "url": "https://viajes-demo.usewai.com", "nombre": "WAI Viajes"}
    monkeypatch.setattr(mia, "cual_es", falsa)

    texto = await mia.para_el_prompt()
    assert "viajes-demo.usewai.com" in texto
    assert "WAI Viajes" in texto
    # NO route example —each application has its own and an example gets copied— but WITH
    # the instruction to use the ones it knows: its own skills give it its routes, and it
    # writes them correctly (measured on 23/09/2026 in its history: `/#/viajes/4`, `/#/viajes/3/
    # agenda`). A bare "don't invent routes" took away the good along with the bad.
    assert "/#/" not in texto
    assert "TO THE SPECIFIC PAGE" in texto
    assert "GUESS" in texto
    # And that it does NOT warn that you have to sign in: whoever clicks is already inside. That
    # sentence is exactly what would reveal that the two pieces do not know each other.
    assert "sign in" in texto


@pytest.mark.asyncio
async def test_asociada_pero_sin_desplegar_no_da_enlace(monkeypatch) -> None:
    """The platform answers `tiene: false` when the application has no address yet
    (see V102). Here we check that the employee does not invent one."""
    from app import mi_aplicacion as mia
    mia._cache.update({"cuando": 0.0, "valor": None})

    class _R:
        status_code = 200

        @staticmethod
        def json():
            return {"tiene": False}

    class _Cli:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            return _R()

    monkeypatch.setattr(mia, "WAI_BACKEND_URL", "http://plataforma", raising=False)
    monkeypatch.setattr(mia, "BACKEND_API_SECRET", "s3cr3t", raising=False)
    monkeypatch.setattr(mia, "APP_PUBLIC_BASE_URL", "https://empleado.usewai.com", raising=False)
    monkeypatch.setattr(mia.httpx, "AsyncClient", _Cli)

    assert await mia.cual_es() == {"tiene": False}
    assert await mia.para_el_prompt() == ""


@pytest.mark.asyncio
async def test_se_cachea_y_se_puede_refrescar(monkeypatch) -> None:
    """This is queried on EVERY chat turn: without a cache it would be two calls per message to
    the platform for a value that changes at most once a day."""
    from app import mi_aplicacion as mia
    mia._cache.update({"cuando": 0.0, "valor": None})
    veces = {"n": 0}

    class _R:
        status_code = 200

        @staticmethod
        def json():
            return {"tiene": True, "url": "https://viajes.usewai.com", "nombre": "Viajes"}

    class _Cli:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            veces["n"] += 1
            return _R()

    monkeypatch.setattr(mia, "WAI_BACKEND_URL", "http://plataforma", raising=False)
    monkeypatch.setattr(mia, "BACKEND_API_SECRET", "s3cr3t", raising=False)
    monkeypatch.setattr(mia, "APP_PUBLIC_BASE_URL", "https://empleado.usewai.com", raising=False)
    monkeypatch.setattr(mia.httpx, "AsyncClient", _Cli)

    assert (await mia.cual_es())["url"] == "https://viajes.usewai.com"
    await mia.cual_es()
    await mia.cual_es()
    assert veces["n"] == 1, "se preguntó a la plataforma en cada turno"

    await mia.cual_es(refrescar=True)
    assert veces["n"] == 2, "no se puede forzar el refresco"
