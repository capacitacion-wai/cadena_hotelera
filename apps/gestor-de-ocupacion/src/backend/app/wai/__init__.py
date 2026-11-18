"""Platform modules installed in this application (cross-cutting capabilities).

INVARIANT FILE — installed and refreshed by `wai-app-builder/assets/modulos.py`; the verifier
compares it byte by byte with the skill's copy. Do not edit it: change the module in the skill.

A module is a sub-package of this one (`app/wai/<id>/`) that exposes `montar(app)` and
`preparar()`. `main.py` calls `montar_modulos(app)` ONCE, right after creating the FastAPI app and
BEFORE including the app's own routers (so the modules' routes are registered before the SPA
catch-all). The installer adds that call; if it could not, it says so.

What `montar_modulos` does:
  * mounts each installed module's routes (`montar(app)`);
  * wraps the app's lifespan so every module runs `preparar()` (its numbered migrations and the
    rows it guarantees) before the app starts serving. Each module ALSO prepares itself lazily on
    first use, because the tests drive the app through an ASGI transport that never runs the
    lifespan.
"""
from __future__ import annotations

import importlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, List

logger = logging.getLogger(__name__)


def instalados() -> List[str]:
    """Ids of the modules present in this package (a folder with `__init__.py`), sorted."""
    raiz = Path(__file__).resolve().parent
    return sorted(d.name for d in raiz.iterdir()
                  if d.is_dir() and not d.name.startswith(("_", ".")) and (d / "__init__.py").is_file())


def montar_modulos(app: Any) -> List[str]:
    """Mounts every installed module on `app` and hooks their preparation to the lifespan."""
    modulos = [importlib.import_module(f"{__name__}.{m}") for m in instalados()]
    for mod in modulos:
        mod.montar(app)
    anterior = app.router.lifespan_context

    @asynccontextmanager
    async def ciclo(a):  # noqa: ANN001 — Starlette passes the app
        for mod in modulos:
            await mod.preparar()
        async with anterior(a) as estado:
            yield estado

    app.router.lifespan_context = ciclo
    logger.info("platform modules mounted: %s", ", ".join(m.__name__.rsplit(".", 1)[-1] for m in modulos) or "none")
    return [m.__name__.rsplit(".", 1)[-1] for m in modulos]
