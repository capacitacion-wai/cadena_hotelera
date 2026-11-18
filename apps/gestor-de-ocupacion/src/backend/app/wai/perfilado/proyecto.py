"""The project's members, asked LIVE to the WAI platform with the caller's own token.

INVARIANT FILE — installed by `wai-app-builder/assets/modulos.py`; do not edit it.

Only a project member can get into the app (`require_project_access`), so «sign up from the
project» (the default setting `alta: proyecto`) only admits real members: everyone added this way
can really log in. The path is `/backend/projects/{PROJECT_ID}/users` (the platform mounts its API
under `/backend`; `/api/v1/...` answers 404 — the employees learnt it in v7.155.0).
"""
from __future__ import annotations

import logging
import os
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


async def miembros_del_proyecto(autorizacion: Optional[str]) -> Optional[List[Dict[str, str]]]:
    """[{email, nombre}] of the project's users, or None when it cannot be known (no token, the
    platform does not answer): the caller says so, it never guesses."""
    base = (os.environ.get("WAI_BACKEND_URL") or "").rstrip("/")
    proyecto = (os.environ.get("PROJECT_ID") or "").strip()
    if not (base and proyecto and autorizacion):
        return None
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.get(f"{base}/backend/projects/{proyecto}/users", headers={"Authorization": autorizacion})
        if r.status_code >= 400:
            logger.warning("project members: HTTP %s", r.status_code)
            return None
        cuerpo = r.json()
    except (httpx.HTTPError, ValueError):
        logger.warning("project members: the platform did not answer", exc_info=True)
        return None
    datos = cuerpo.get("data") if isinstance(cuerpo, dict) else cuerpo
    if not isinstance(datos, list):
        return None
    out = []
    for x in datos:
        if isinstance(x, dict) and str(x.get("email") or "").strip():
            out.append({"email": str(x["email"]).strip().lower(),
                        "nombre": str(x.get("name") or x.get("display_name") or x.get("full_name") or "").strip()})
    return out
