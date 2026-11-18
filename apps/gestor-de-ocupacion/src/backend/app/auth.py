# INVARIANT FILE — copied byte for byte by `assets/copiar_base.sh` (wai-app-builder, skill 7.0.0); do not edit
# it: `verify_app.sh` fails if it differs from the skill's. Why it is like this: `patterns/authentication.md`.
import asyncio
import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional

import httpx
from fastapi import HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, JWTError
from app.config import WAI_INTROSPECCION_URL, PROJECT_ID, DB_SCHEMA, APP_NAME

security = HTTPBearer()

@dataclass
class TokenPayload:
    email: str
    is_superuser: bool
    project_ids: list
    data_areas: list
    externa: bool = False        # an EXTERNAL account (profiling level 4): limited to ONE app
    app: Optional[str] = None    # the app an external session was issued for

# Cache: a "yes" lasts min(60 s, what is left of the token); a "no", 10 s; at most 5,000
# entries. The key is sha256(token): the token is not stored in clear.
_TTL_SI, _TTL_NO, _MAX = 60.0, 10.0, 5000
_cache: "OrderedDict[str, tuple[float, Optional[TokenPayload]]]" = OrderedDict()
_cliente: Optional[httpx.AsyncClient] = None
_bucle = None

def _http() -> httpx.AsyncClient:
    global _cliente, _bucle
    bucle = asyncio.get_running_loop()
    if _cliente is None or _cliente.is_closed or _bucle is not bucle:
        _cliente, _bucle = httpx.AsyncClient(timeout=3.0), bucle
    return _cliente

def _no_disponible() -> HTTPException:
    return HTTPException(status_code=503, headers={"Retry-After": "5"},
                         detail="Your session cannot be checked right now; try again in a few seconds.")

async def preguntar_a_la_plataforma(token: str) -> dict:
    """THE question: returns the introspection body. It is the ONLY point the tests
    replace (conftest), so everything else —cache, 401/503, permissions— is tested
    for real. 429/5xx/no network → 503."""
    if not WAI_INTROSPECCION_URL:
        raise _no_disponible()
    try:
        # `app`: the platform says «valid» to an external session ONLY when the asker is its app.
        r = await _http().post(WAI_INTROSPECCION_URL, json={"token": token, "app": APP_NAME})
    except httpx.HTTPError:
        raise _no_disponible()
    if r.status_code != 200:
        raise _no_disponible()
    try:
        cuerpo = r.json()
    except ValueError:
        raise _no_disponible()
    if not isinstance(cuerpo, dict):
        raise _no_disponible()
    return cuerpo

def _parece_vivo(token: str) -> bool:
    """Cheap filter that ONLY rejects (JWT shape and `exp` read without verifying): garbage
    and expired tokens do not spend a round trip or the platform's quota. It never grants anything."""
    if not token or len(token) > 4096 or token.count(".") != 2:
        return False
    try:
        exp = jwt.get_unverified_claims(token).get("exp")
    except JWTError:
        return False
    return not (isinstance(exp, (int, float)) and exp <= time.time())

async def decode_token(token: str) -> TokenPayload:
    if not _parece_vivo(token):
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    clave = hashlib.sha256(token.encode()).hexdigest()
    hit = _cache.get(clave)
    if hit and hit[0] > time.monotonic():
        _cache.move_to_end(clave)
        if hit[1] is None:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        return hit[1]
    cuerpo = await preguntar_a_la_plataforma(token)
    claims = cuerpo.get("claims") if cuerpo.get("activo") is True else None
    user = None
    ttl = _TTL_NO
    if isinstance(claims, dict) and claims.get("sub"):
        user = TokenPayload(
            email=claims["sub"],
            is_superuser=bool(claims.get("is_superuser", False)),
            project_ids=claims.get("project_ids") or [],
            data_areas=claims.get("data_areas") or [],
            externa=claims.get("externa") is True,
            app=claims.get("app"),
        )
        exp = claims.get("exp")
        ttl = min(_TTL_SI, exp - time.time()) if isinstance(exp, (int, float)) else _TTL_SI
    if ttl > 0:
        _cache[clave] = (time.monotonic() + ttl, user)
        _cache.move_to_end(clave)
        while len(_cache) > _MAX:
            _cache.popitem(last=False)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return user

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> TokenPayload:
    return await decode_token(credentials.credentials)

async def require_project_access(
    user: TokenPayload = Depends(get_current_user),
) -> TokenPayload:
    if user.externa:
        # An external account (profiling level 4) enters ONLY the app it was issued for, and it has
        # no project: it reaches what the app's profiling lets its organisation reach, nothing else.
        if not APP_NAME or user.app != APP_NAME:
            raise HTTPException(status_code=403, detail="This session is for another application")
        return user
    if user.is_superuser:
        return user
    if PROJECT_ID not in user.project_ids:
        raise HTTPException(status_code=403, detail="No access to this project")
    # The PROJECT is the authorization. The data area is checked only if the person HAS some: a
    # workspace project has none (`data_areas: []`, the app gets DB_SCHEMA="default") and requiring
    # it answered 403 to every non-superuser (COBSA portal, 07/10/2026). Gate:
    # `checks/area_de_datos_opcional.py`.
    if user.data_areas and DB_SCHEMA not in user.data_areas:
        raise HTTPException(status_code=403, detail="No access to this data area")
    return user
