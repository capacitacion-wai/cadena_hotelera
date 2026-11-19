import asyncio
import hashlib
import logging
import time
from collections import OrderedDict
from typing import Optional
from dataclasses import dataclass

import httpx
from fastapi import HTTPException, Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, JWTError
from app.config import PROJECT_ID, DB_SCHEMA, WAI_INTROSPECCION_URL

logger = logging.getLogger(__name__)

# `auto_error=False` for ONE reason: a call that arrives from an APPLICATION through the
# platform carries no session of its own, only the platform's delegation (see
# `_persona_de_una_aplicacion`). Without a bearer AND without a delegation the answer is the
# same 403 «Not authenticated» HTTPBearer gave.
security = HTTPBearer(auto_error=False)

# v7.124.0 — functional code of the quarantine VETO (auth F1). It travels over the TWO
# channels the employee already uses to talk auth with its SPA: the `detail`
# {code,message} (contract of api_errors.py) and the ``X-Auth-Error-Code`` header
# (the same one routes/auth_routes.py propagates from the platform).
PASSWORD_CHANGE_REQUIRED_CODE = "PASSWORD_CHANGE_REQUIRED"

# Quarantine ALLOW-list: the only thing that stays open while the user has not changed
# the one-time password. It is exactly what the change screen needs to be able to CLOSE
# (login, the change proxy, the Google sign-in) plus the K8s probes. Today none of those
# routes declares ``Depends(get_current_user)`` —``routes/auth_routes.py`` is a
# pass-through to the platform with no auth of its own and ``health_routes.py`` is
# public—, so the list is a SAFETY BELT: if tomorrow someone protects the change proxy,
# the gate is not left without an exit (and the user, without an app). Everything else —
# THE WHOLE employee API — is vetoed.
QUARANTINE_ALLOWED_PATHS = frozenset({
    "/api/auth/login",
    "/api/auth/change-password",
    "/api/auth/google/start",
    "/api/auth/google/complete",
    "/api/health",
    "/api/health/live",
    "/api/health/ready",
})


@dataclass
class TokenPayload:
    email: str
    is_superuser: bool
    project_ids: list
    data_areas: list
    # v7.124.0 — platform claim (auth F1): the password is the one-time temporary one and
    # it has to be changed. ABSENT (old token, or platform not yet deployed) ⇒ False ⇒
    # previous behaviour WITHOUT veto: a half-done deployment cannot lock anyone out of
    # their employee.
    must_change_password: bool = False

# ── Verify the token by ASKING the platform (14.0.0) ────────────────────────────────────
# Before, the pod validated the JWT with the tenant's signing key. That key also SIGNS:
# with it in the environment, any code running in the pod could mint the token of any
# person in the tenant. Now `POST {WAI_INTROSPECCION_URL}` is asked with
# `{"token": …}` and the platform answers `{"activo": true, "claims": {…}}` or
# `{"activo": false, "motivo": …}`. One single path: if the platform does not answer, there
# is no fallback local validation — it answers 503.
#
# In-memory cache, because the SPA fires dozens of requests per screen and each one would
# carry a trip to the platform:
#   · key = sha256(token): the whole token is not kept in clear in the pod's memory;
#   · a "yes" is valid min(60 s, what the token has left): a removal or a password change
#     is noticed within a minute at most, and a token never lives in the cache beyond its `exp`;
#   · a "no" is valid 10 s: a repeated bad token does not hammer the platform, and one just
#     renewed does not stay rejected for long;
#   · bounded to 5,000 entries (the least recently used is dropped).
# 429/5xx/no network → 503, NEVER 401: the SPA treats 401 as "session expired" and kicks the
# person out; the platform having a hiccup is no reason to take anyone out of their employee.

_TTL_SI_S = 60.0
_TTL_NO_S = 10.0
_CACHE_MAX = 5000
_TIMEOUT_S = 3.0
# A platform JWT is around 300-600 bytes and the platform cuts the body at 8 KB: anything
# beyond this is not one of its tokens and does not deserve a trip.
_TOKEN_MAX = 4096

# sha256(token) → (caduca_en_monotonic, TokenPayload | None). None = «no» cacheado.
_cache: "OrderedDict[str, tuple[float, Optional[TokenPayload]]]" = OrderedDict()
# One single question per token at a time: when a screen opens several requests arrive
# with the SAME freshly issued token, and without this each one would make its own trip.
_en_vuelo: dict[str, "asyncio.Future[Optional[TokenPayload]]"] = {}
_cliente: Optional[httpx.AsyncClient] = None
_cliente_loop: Optional[asyncio.AbstractEventLoop] = None


def _cliente_http() -> httpx.AsyncClient:
    """Shared client (reuses connections to the backend). Rebuilt if the event loop
    changed — in production there is one; in the tests, one per test."""
    global _cliente, _cliente_loop
    loop = asyncio.get_running_loop()
    if _cliente is None or _cliente.is_closed or _cliente_loop is not loop:
        _cliente = httpx.AsyncClient(timeout=_TIMEOUT_S)
        _cliente_loop = loop
    return _cliente


def _no_disponible(motivo: str) -> HTTPException:
    logger.warning("introspección no disponible: %s", motivo)
    return HTTPException(
        status_code=503,
        detail=("No se puede comprobar tu sesión ahora mismo: la plataforma no responde. "
                "Reinténtalo en unos segundos."),
        headers={"Retry-After": "5"},
    )


def _payload_de_claims(claims: dict) -> Optional[TokenPayload]:
    email = claims.get("sub")
    if not isinstance(email, str) or not email:
        return None
    return TokenPayload(
        email=email,
        is_superuser=bool(claims.get("is_superuser", False)),
        project_ids=claims.get("project_ids") or [],
        data_areas=claims.get("data_areas") or [],
        must_change_password=bool(claims.get("must_change_password", False)),
    )


def _guardar(clave: str, valor: Optional[TokenPayload], ttl: float) -> None:
    if ttl <= 0:
        return
    _cache[clave] = (time.monotonic() + ttl, valor)
    _cache.move_to_end(clave)
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)


def _parece_token_vivo(token: str) -> bool:
    """CHEAP filter and only for rejecting: JWT shape and an unexpired `exp`, read WITHOUT
    verifying the signature. It never grants anything —the "yes" comes from the platform—; it
    serves so that garbage or already expired tokens do not spend a trip or the platform's
    quota (which throttles per IP: a flood of invented tokens would end in 429 for real people)."""
    if not token or len(token) > _TOKEN_MAX or token.count(".") != 2:
        return False
    try:
        claims = jwt.get_unverified_claims(token)
    except JWTError:
        return False
    exp = claims.get("exp")
    if isinstance(exp, (int, float)) and exp <= time.time():
        return False
    return True


async def _preguntar(token: str) -> tuple[Optional[TokenPayload], float]:
    """One question to the platform → (payload or None, how long to cache the answer)."""
    if not WAI_INTROSPECCION_URL:
        raise _no_disponible("WAI_INTROSPECCION_URL vacía")
    try:
        r = await _cliente_http().post(WAI_INTROSPECCION_URL, json={"token": token})
    except httpx.HTTPError as exc:
        raise _no_disponible(f"{type(exc).__name__}: {exc}") from exc
    if r.status_code != 200:
        # 429 (throttle), 5xx or anything else: the question could not be asked, the token
        # has not been judged. Not cached.
        raise _no_disponible(f"HTTP {r.status_code}")
    try:
        cuerpo = r.json()
    except ValueError as exc:
        raise _no_disponible("respuesta sin JSON") from exc
    if not isinstance(cuerpo, dict):
        raise _no_disponible("respuesta con forma inesperada")
    if cuerpo.get("activo") is not True:
        return None, _TTL_NO_S
    claims = cuerpo.get("claims") or {}
    user = _payload_de_claims(claims) if isinstance(claims, dict) else None
    if user is None:
        return None, _TTL_NO_S
    ttl = _TTL_SI_S
    exp = claims.get("exp")
    if isinstance(exp, (int, float)):
        ttl = min(ttl, float(exp) - time.time())
    return user, ttl


async def decode_token(token: str) -> TokenPayload:
    """The token → who it is, according to the PLATFORM. 401 if invalid; 503 if the
    question could not be asked. The usual name so its callers find it; now it is async."""
    if not _parece_token_vivo(token):
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    clave = hashlib.sha256(token.encode("utf-8")).hexdigest()
    guardado = _cache.get(clave)
    if guardado is not None:
        caduca, valor = guardado
        if caduca > time.monotonic():
            _cache.move_to_end(clave)
            if valor is None:
                raise HTTPException(status_code=401, detail="Invalid or expired token")
            return valor
        _cache.pop(clave, None)

    pendiente = _en_vuelo.get(clave)
    if pendiente is not None:
        valor = await asyncio.shield(pendiente)
    else:
        futuro: "asyncio.Future[Optional[TokenPayload]]" = asyncio.get_running_loop().create_future()
        _en_vuelo[clave] = futuro
        try:
            valor, ttl = await _preguntar(token)
            _guardar(clave, valor, ttl)
            futuro.set_result(valor)
        except asyncio.CancelledError:
            futuro.cancel()
            raise
        except Exception as exc:
            futuro.set_exception(exc)
            # Whoever is waiting already gets the exception; this avoids the "nobody read it" warning.
            futuro.exception()
            raise
        finally:
            _en_vuelo.pop(clave, None)
    if valor is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return valor


def request_path(request: Optional[Request]) -> str:
    """Path normalized to check against the allow-list: without ``root_path`` (in case
    the app is mounted under a prefix) and without a trailing slash."""
    if request is None:
        return ""
    path = request.url.path or ""
    root = request.scope.get("root_path") or ""
    if root and path.startswith(root):
        path = path[len(root):] or "/"
    return path.rstrip("/") or "/"


def enforce_password_change(user: TokenPayload, path: str = "") -> None:
    """SERVER VETO of the mandatory password change (auth F1).

    The SPA gate (``auth/ChangePasswordGate.tsx``) is only an INTERFACE barrier: whoever
    has the token can call the API with curl. Here it is really closed — while the JWT
    carries ``must_change_password``, the session is in quarantine and the whole API
    answers 403 ``PASSWORD_CHANGE_REQUIRED`` except ``QUARANTINE_ALLOWED_PATHS``.

    Without the claim there is NO veto (see ``TokenPayload.must_change_password``): the
    app tolerates a platform older than auth F1 instead of blocking everyone.
    """
    if not user.must_change_password:
        return
    if path and path in QUARANTINE_ALLOWED_PATHS:
        return
    raise HTTPException(
        status_code=403,
        detail={
            "code": PASSWORD_CHANGE_REQUIRED_CODE,
            "message": (
                "Debes crear tu contraseña definitiva antes de usar el empleado: "
                "estás entrando con la contraseña temporal de un solo uso."
            ),
        },
        headers={"X-Auth-Error-Code": PASSWORD_CHANGE_REQUIRED_CODE},
    )

async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> TokenPayload:
    # SINGLE choke point: `require_project_access` and `require_admin` hang from here,
    # so the veto reaches EVERY authenticated route of the employee (including those the
    # app builder generates) without having to touch them one by one.
    if credentials is None:
        delegacion = request.headers.get("x-wai-delegation")
        if delegacion:
            return _persona_de_una_aplicacion(request, delegacion)
        raise HTTPException(status_code=403, detail="Not authenticated")
    user = await decode_token(credentials.credentials)
    enforce_password_change(user, request_path(request))
    return user

# ── A person arriving THROUGH AN APPLICATION (docs/aplicaciones-que-llaman-a-empleados.md) ──
#: The ONLY routes an application reaches: the chat and its threads, and (14.23.0) `invocar` —a
#: declared capability with arguments and a validated output, `app/invocacion.py`—. The platform
#: has the same closed list (and checks WHICH capability its Definition declares); if one of the
#: two ever opens, the other still closes.
import re as _re
_RUTAS_DE_APLICACION = _re.compile(
    r"^/api/agent/(chat|chat/abort|chat/active|chat/stream|threads|threads/current"
    r"|threads/[^/]+|threads/[^/]+/history|threads/[^/]+/archive|invocar)/?$")
TIPO_DELEGACION_APP = "WAI-DELEG-APP"
#: 15.8.0 — what a CONTROLLED test chat reaches (`prueba_controlada`): a closed list, apart.
_RUTAS_DE_PRUEBA_CONTROLADA = _re.compile(
    r"^/api/agent/(chat|chat/abort|chat/active|chat/stream|threads|threads/[^/]+/archive|chat/permisos/[^/]+)/?$")


def _persona_de_una_aplicacion(request: Request, delegacion: str) -> TokenPayload:
    """The person an application speaks for, from the platform's signed delegation.

    The platform already checked the application (its key), the person (their session, not an
    external account), that the application's deployed Definition declares THIS employee, and
    that the person has access to its project. Here: the signature and expiry (the same secret
    as colleague calls), that it IS an application delegation, and that the route is the chat.
    Never a superuser: the person must still be in THIS employee's directory
    (`require_project_access`), like anyone. The origin goes into the run (`app:<name>`)."""
    from app.colleagues import verify_delegation
    try:
        datos = verify_delegation(delegacion)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    origen = datos.get("origen") or {}
    tipo = origen.get("tipo")
    if datos.get("typ") != TIPO_DELEGACION_APP or tipo not in ("app", "empleado"):
        raise HTTPException(status_code=403, detail="This delegation is not from an application.")
    # ANOTHER EMPLOYEE using one of this employee's capabilities (15.4.0, docs/red-de-integraciones.md): the platform
    # checked the collaboration authorises THAT capability and signed the same delegation, with origin «empleado».
    # It reaches `invocar` ONLY — the conversation between employees goes through `/mcp/colleague`.
    if tipo == "empleado":
        if not _re.match(r"^/api/agent/invocar/?$", request.url.path or ""):
            raise HTTPException(status_code=403,
                                detail="Another employee only reaches the invocation of this employee's capabilities.")
    elif datos.get("prueba_controlada") is True:
        # 15.8.0 — the platform's code agent trying THIS employee from its chat (`prueba_controlada`): its
        # test conversation, its turn and the answer to a pending write. Never `invocar`, never another
        # thread's history (the chat route checks the conversation is a test one).
        if not _RUTAS_DE_PRUEBA_CONTROLADA.match(request.url.path or ""):
            raise HTTPException(status_code=403,
                                detail="A controlled test only reaches its test conversation and its pending writes.")
        request.state.prueba_controlada = True
    elif not _RUTAS_DE_APLICACION.match(request.url.path or ""):
        raise HTTPException(status_code=403,
                            detail="An application only reaches this employee's chat, its threads "
                                   "and the invocation of its capabilities.")
    request.state.origen = (f"prueba:{origen.get('app') or '?'}" if getattr(request.state, "prueba_controlada", False)
                            else f"{tipo}:{origen.get('app') or '?'}")
    # 15.7.0 — a READ-ONLY test (the live view of an application trying this employee for real,
    # `prueba_solo_lectura.py`). It is a claim of the SIGNED delegation: the application being built
    # can neither forge it nor drop it. It only exists for `invocar` (a chat cannot honour it: fail
    # closed), and the run is recorded as a test, apart from real use.
    if datos.get("solo_lectura") is True:
        if not _re.match(r"^/api/agent/invocar/?$", request.url.path or ""):
            raise HTTPException(status_code=403,
                                detail="A read-only test only reaches the invocation of a capability.")
        request.state.solo_lectura = True
        request.state.origen = f"prueba:{origen.get('app') or '?'}"
    return TokenPayload(
        email=str(datos["sub"]).strip().lower(),
        is_superuser=False,
        project_ids=[int(p) for p in (datos.get("project_ids") or [])],
        data_areas=list(datos.get("data_areas") or []),
    )


async def require_project_access(
    user: TokenPayload = Depends(get_current_user),
) -> TokenPayload:
    if user.is_superuser:
        return user
    if PROJECT_ID not in user.project_ids:
        raise HTTPException(status_code=403, detail="No access to this project")
    # The data area is only checked if the user HAS any: an employee that lives in a
    # WORKSPACE project (no data area, v7.122/V69) receives DB_SCHEMA="default" through the
    # deploy fallback, while the platform issues `data_areas: []` — requiring it gave 403 to
    # THE WHOLE API for any non-superuser, and the SPA silently degraded it to "member" (the
    # symptom was "I am admin in the employee and it treats me as member").
    # The real authorization is the PROJECT; the area is a detail derived from it, so with
    # the list empty there is nothing to check against.
    if user.data_areas and DB_SCHEMA not in user.data_areas:
        raise HTTPException(status_code=403, detail="No access to this data area")

    # AND ALSO: being in the directory of THIS employee (v9.0.0).
    #
    # Until here the authorization was the PROJECT, which is much wider than an employee.
    # Real consequence, measured in demo on 31/08/2026: a person registered for the travel
    # assistant —and only for it— could get into the project's other four employees as
    # `member`. Their «Usuarios» screen said one thing and the door did another.
    #
    # The directory does NOT fill itself (`import_project_users` is an operator action), so
    # it is a hand-curated list: if someone is not there, they have not been given access.
    #
    # There is no need to open up when the directory is empty, and that is why it is not
    # done: startup guarantees the employee's administrator always exists, so "empty" is not
    # a reachable state. A permission with a "just in case" back door is exactly the one
    # nobody exercises and the one that fails the day it matters.
    from app.users_directory import esta_autorizado
    if not await esta_autorizado(user.email):
        raise HTTPException(
            status_code=403,
            detail=("No tienes acceso a este empleado. Pídeselo a su administrador: "
                    "se concede en su pantalla de Usuarios."),
        )
    return user


async def require_admin(
    user: TokenPayload = Depends(require_project_access),
) -> TokenPayload:
    """Admin of THIS employee — gates the Administración endpoints. The role is
    governed by the EMPLOYEE's OWN user directory, NOT the WAI platform: each
    autonomous employee governs its roles, so a platform admin can be a plain
    ``member`` in a given employee. The anti-lockout guarantee comes from
    ``resolve_role`` (the deployment identity is ALWAYS admin); the rest — platform
    superusers included — is whatever the directory says. We do NOT short-circuit on
    ``is_superuser`` (that granted admin of every employee to any WAI superuser,
    breaking role governance and per-user privacy — INC-AGO-39)."""
    # Local import avoids a circular import (users_directory → app_database).
    from app.users_directory import is_admin
    if not await is_admin(user.email):
        raise HTTPException(status_code=403, detail="Employee admin role required")
    return user


async def require_approver(
    user: TokenPayload = Depends(require_project_access),
) -> TokenPayload:
    """May DECIDE the actions the employee parks (v7.145.0) — admins, plus the
    people an admin designates in Usuarios («puede aprobar»).

    Separate from ``require_admin`` on purpose: saying yes to a concrete action is
    a BUSINESS call, and the person responsible for it is usually not the one who
    governs the employee. Gating it on admin forced the operator to hand over the
    whole governance panel (silences, thresholds, credit caps) just so somebody
    could approve a payment. Fails closed."""
    from app.users_directory import can_approve
    if not await can_approve(user.email):
        raise HTTPException(
            status_code=403,
            detail="Only an admin or a designated approver can decide this",
        )
    return user


async def resolve_scoped_email(requested, user: TokenPayload) -> str:
    """For per-user surfaces that are DUAL-use (a member reads/edits their OWN
    data via the Chat side-panel; an admin browses ANYONE's from Administración).
    An admin of THIS employee may target any ``requested`` email; a member is
    pinned to their OWN — requesting someone else's raises 403. Closes the leak
    where a member could pass a colleague's ``user_email`` to read/edit their
    memory. The ADMIN role is set by the employee's DIRECTORY (``is_admin`` →
    ``resolve_role``, with anti-lockout of the deployment identity), NOT by the
    platform's ``is_superuser``: a WAI admin who is ``member`` in this employee is
    pinned to THEIR own data (INC-AGO-39)."""
    own = (getattr(user, "email", "") or "").strip().lower()
    req = (str(requested or "")).strip().lower()
    from app.users_directory import is_admin
    if await is_admin(user.email):
        return req or own
    if req and req != own:
        raise HTTPException(status_code=403, detail="Members can only access their own data")
    return own
