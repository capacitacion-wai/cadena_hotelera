import logging
import time
from collections import defaultdict, deque

import httpx
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from pydantic import BaseModel

from app.config import WAI_BACKEND_URL

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


# P4-1: tiny in-memory lockout. Per-IP sliding window of failed
# logins; on the 6th failure within 5 minutes we 429 the IP for the
# rest of the window. The U3 LoginPage already renders 429 as
# "Demasiados intentos…"; without this gate the message never
# fires in production.
#
# This is intentionally process-local — it survives a single pod
# restart but not multi-replica scale-out. For a real deployment
# behind a HA platform, replace with a Redis-backed counter or
# rely on the ingress's rate-limit annotations. The single-replica
# default of the agent-app pod makes this gate sufficient by
# itself for the typical deployment.
_FAIL_WINDOW_S = 300
_FAIL_THRESHOLD = 5
_failures: dict[str, deque[float]] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _record_failure(ip: str) -> int:
    now = time.monotonic()
    bucket = _failures[ip]
    bucket.append(now)
    while bucket and (now - bucket[0]) > _FAIL_WINDOW_S:
        bucket.popleft()
    return len(bucket)


def _record_success(ip: str) -> None:
    _failures.pop(ip, None)


def _is_locked(ip: str) -> bool:
    now = time.monotonic()
    bucket = _failures.get(ip)
    if not bucket:
        return False
    while bucket and (now - bucket[0]) > _FAIL_WINDOW_S:
        bucket.popleft()
    return len(bucket) >= _FAIL_THRESHOLD


class LoginRequest(BaseModel):
    email: str
    password: str


# v7.123.0 — minimum length of the new password. The platform issues the OTP with
# ``generate_one_time_password`` (minimum 8, see backend/app/core/security.py), so
# requiring 8 here cannot reject a valid temporary one and prevents the employee from
# degrading the credential when changing it. The SPA validates the same before calling
# (double gate).
MIN_PASSWORD_LENGTH = 8

# v7.123.0 — platform functional codes (``X-Auth-Error-Code`` header) that are NOT
# "wrong credential": the employee PROPAGATES them with their real status instead of
# flattening them to 401, and they do NOT count towards the per-IP lockout (retrying fixes
# nothing, and locking the user out for coming in through the wrong door is hostile). The
# star case is METHOD_NOT_ALLOWED (403): a Google-only account typing a password — the SPA
# must say «this account signs in with Google», not «invalid credentials».
_NON_CREDENTIAL_CODES = {"METHOD_NOT_ALLOWED", "INACTIVE_USER", "ACCOUNT_LOCKED"}


def _auth_error_headers(resp: httpx.Response) -> dict[str, str]:
    """Re-exposes the platform's error contract to the employee's SPA: the functional
    code and (if present) the attempts left before the lockout."""
    out: dict[str, str] = {}
    code = resp.headers.get("X-Auth-Error-Code", "")
    if code:
        out["X-Auth-Error-Code"] = code
    remaining = resp.headers.get("X-Auth-Remaining-Attempts", "")
    if remaining:
        out["X-Auth-Remaining-Attempts"] = remaining
    return out


def _with_must_change(payload):
    """Guarantees the ``must_change_password`` key in the login response.

    The platform emits it in its ``Token`` (auth F1: the one-time password forces a
    change on first access), but the employee's contract cannot depend on it being
    there: we normalize to bool WITHOUT discarding the rest of the body, so the SPA can
    block the app until the change with a single read.
    """
    if isinstance(payload, dict):
        payload["must_change_password"] = bool(payload.get("must_change_password", False))
    return payload


def _upstream_detail(resp: httpx.Response, fallback: str) -> str:
    try:
        body = resp.json()
    except Exception:
        return fallback
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, str) and detail:
            return detail
    return fallback


def _host(x: str) -> str:
    """The host of a URL, whether it comes whole or bare. Comparing hosts is what stops
    the frontend from proposing one destination and the pass ending up opening something
    else."""
    from urllib.parse import urlsplit
    d = (x or "").strip().lower()
    if not d:
        return ""
    if "//" not in d:
        d = "//" + d
    return (urlsplit(d).hostname or "").strip(".")


@router.post("/login")
async def login(body: LoginRequest, request: Request):
    ip = _client_ip(request)
    if _is_locked(ip):
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed attempts; wait {_FAIL_WINDOW_S}s",
        )
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{WAI_BACKEND_URL}/backend/auth/token",
            data={"username": body.email, "password": body.password},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if resp.status_code != 200:
        code = resp.headers.get("X-Auth-Error-Code", "")
        headers = _auth_error_headers(resp)
        # NON-credential failure (Google-only, inactive account, account already locked
        # by the platform): it is propagated as is and does not add to the window counter.
        if code in _NON_CREDENTIAL_CODES:
            raise HTTPException(
                status_code=resp.status_code,
                detail=_upstream_detail(resp, code),
                headers=headers,
            )
        count = _record_failure(ip)
        # On the threshold-th failure, 429 immediately so the SPA
        # doesn't have to wait for the next attempt to learn it's
        # locked out.
        if count >= _FAIL_THRESHOLD:
            raise HTTPException(
                status_code=429,
                detail=f"Too many failed attempts; wait {_FAIL_WINDOW_S}s",
            )
        raise HTTPException(
            status_code=401, detail="Invalid credentials", headers=headers or None
        )
    _record_success(ip)
    return _with_must_change(resp.json())


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


# ── Going to ITS application without signing in again ────────────────────────────────
# The employee returns links to the application it has associated. Clicking one opens
# ANOTHER origin, with its own session, so whoever had just been talking to it —signed in—
# found a login screen: the stumble that gives away that two pieces of the same product do
# not know each other.
#
# This asks the platform for a PASS valid for sixty seconds and a single use, bound to the
# destination. What travels in the URL is worthless as soon as it is used. The reason for
# not sending the token is in the platform's migration V103.
#
# It is issued with THE PERSON'S token, not with the pod's secret: the pass opens THEIR
# session, and requesting it on their behalf with a service key would mean being able to
# open anyone's.
#
# IT IS A NAVIGATION, NOT A CALL (12.54.0). The chat submits a form to a new tab and here we
# answer with a 303 to the destination, pass included. Until 12.53 the frontend opened an
# empty tab on click, requested the pass with `fetch` and then redirected it: the person saw
# `about:blank` on every link, and the juggling with the reference to that tab failed twice
# (the `noopener` that left it as null). A form is born from the click itself, so the
# browser does not block it as a popup, and a redirect leaves no trace in «back». The token
# goes in the BODY of a POST to the same origin: neither URL nor history.
#
# The 303 forces the CSP `form-action` to allow its application's origin —the browser
# applies that directive to the redirect too—: `app/main.py` sets it.


def _a_donde(destino: str, mia: str) -> str:
    """The destination if it belongs to ITS application; otherwise its application's home.

    This is what stops the endpoint from being an open redirect —that anyone could use it
    to send someone to another website with our address in front—. And comparing hosts is
    NOT enough: `https://otra.com\\@su-app/` is su-app for Python and `otra.com` for the
    browser. That is why only path, query and fragment are taken from `destino`: scheme
    and host ALWAYS come from `mia`.
    """
    from urllib.parse import quote, urlsplit, urlunsplit
    base = urlsplit(mia if "//" in mia else f"https://{mia}")
    d = urlsplit((destino or "").strip())
    if not d.hostname or _host(destino) != _host(mia) or d.scheme not in ("http", "https"):
        return urlunsplit((base.scheme or "https", base.netloc, base.path or "/", "", ""))
    # `quote` with what is ALREADY legal in each part: nothing valid is re-encoded, and
    # what is not (a line break, a space) never reaches the `Location` header.
    return urlunsplit((
        base.scheme or "https", base.netloc,
        quote(d.path or "/", safe="/%:@!$&'()*+,;=-._~"),
        quote(d.query, safe="/?%:@!$&'()*+,;=-._~"),
        quote(d.fragment, safe="/?#%:@!$&'()*+,;=-._~"),
    ))


def _con_pase(url: str, codigo: str) -> str:
    """The pass goes in the QUERY, which is what survives a hash route:
    `https://app/#/viajes/12` → `https://app/?wai=…#/viajes/12`. Placed after the hash,
    the application's router would swallow it."""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    u = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if k != "wai"]
    q.append(("wai", codigo))
    return urlunsplit((u.scheme, u.netloc, u.path, urlencode(q), u.fragment))


def _ir(url: str) -> RedirectResponse:
    r = RedirectResponse(url, status_code=303)
    # The destination has no need to know which conversation it was reached from, and the
    # response carries a pass in the URL: none of this is stored in any cache.
    r.headers["Referrer-Policy"] = "no-referrer"
    r.headers["Cache-Control"] = "no-store"
    return r


@router.post("/ir-a-la-aplicacion")
async def ir_a_la_aplicacion(destino: str = Form(""), token: str = Form("")):
    """Opens ITS application with the session set: 303 to the destination with `?wai=<pase>`.

    If the pass cannot be obtained —expired token, platform down— it goes ANYWAY, without
    a pass: the person will see the login screen, which is what always happened until
    12.51, and that is much better than a tab stuck on an error."""
    from app.mi_aplicacion import url as url_de_mi_aplicacion
    mia = await url_de_mi_aplicacion()
    if not mia:
        # Only happens if the association was removed in the last few minutes (the chat
        # only sends its application's links here). There is nowhere to go: say so,
        # without redirecting.
        return PlainTextResponse("Este empleado ya no tiene una aplicación asociada.",
                                 status_code=404)
    ir_a = _a_donde(destino, mia)
    if not token:
        return _ir(ir_a)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                f"{WAI_BACKEND_URL}/backend/auth/app-handoff",
                json={"destino": ir_a},
                headers={"Authorization": f"Bearer {token}"},
            )
        codigo = str((r.json() or {}).get("codigo") or "") if r.status_code < 400 else ""
    except Exception:  # noqa: BLE001 — without a pass it goes anyway: see the docstring
        logger.warning("ir_a_la_aplicacion: la plataforma no emitió el pase", exc_info=True)
        codigo = ""
    return _ir(_con_pase(ir_a, codigo) if codigo else ir_a)


@router.post("/change-password")
async def change_password(body: ChangePasswordRequest, request: Request):
    """Password change by PASS-THROUGH to the platform (auth F1).

    The employee stores no credentials: it forwards ``{current_password, new_password}`` to
    ``{WAI_BACKEND_URL}/backend/auth/change-password`` with the **user's own Bearer**
    (the platform identifies the user by the token, not by the body), and the platform
    clears the ``must_change_password`` flag there. It is the same contract the WAI panel
    uses, so the password works for both surfaces.
    """
    authorization = request.headers.get("authorization", "")
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    new_password = body.new_password or ""
    if len(new_password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"La contraseña debe tener al menos {MIN_PASSWORD_LENGTH} caracteres.",
            headers={"X-Auth-Error-Code": "PASSWORD_TOO_SHORT"},
        )
    if new_password == (body.current_password or ""):
        raise HTTPException(
            status_code=400,
            detail="La nueva contraseña debe ser distinta de la temporal.",
            headers={"X-Auth-Error-Code": "PASSWORD_UNCHANGED"},
        )
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{WAI_BACKEND_URL}/backend/auth/change-password",
            json={
                "current_password": body.current_password,
                "new_password": new_password,
            },
            headers={"Authorization": authorization},
        )
    if resp.status_code == 200:
        # A successful change also clears the local per-IP lockout: the previous failure
        # window was, almost always, the user struggling with the temporary password.
        _record_success(_client_ip(request))
        return {"status": "ok"}
    if resp.status_code in (400, 401, 403):
        raise HTTPException(
            status_code=resp.status_code,
            detail=_upstream_detail(resp, "No se pudo cambiar la contraseña."),
            headers=_auth_error_headers(resp) or None,
        )
    raise HTTPException(status_code=502, detail="change password failed")


class GoogleCompleteRequest(BaseModel):
    login_ref: str


@router.post("/google/start")
async def google_start(request: Request):
    """Starts «Sign in with Google» by PASS-THROUGH to the platform (same pattern as
    /login): the employee has no auth of its own — the platform verifies the Google
    identity (via Composio). Returns {redirect_url, login_ref} for the SPA's popup."""
    origin = request.headers.get("origin") or ""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{WAI_BACKEND_URL}/backend/auth/google/start",
            params={"return_origin": origin},
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="google login start failed")
    return resp.json()


@router.post("/google/complete")
async def google_complete(body: GoogleCompleteRequest):
    """Claims the token after consent. PROPAGATES the platform status as is (incl. 425
    with X-Auth-Error-Code=GOOGLE_LOGIN_PENDING) so the SPA keeps polling until the
    Composio connection is ACTIVE and the platform issues the token."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{WAI_BACKEND_URL}/backend/auth/google/complete",
            json={"login_ref": body.login_ref},
        )
    if resp.status_code == 200:
        # Same contract as /login: the Google token may also carry the forced-change
        # flag (a user with a pending OTP who signs in through Google).
        return _with_must_change(resp.json())
    code = resp.headers.get("X-Auth-Error-Code", "")
    raise HTTPException(
        status_code=resp.status_code,
        detail=code or "google login failed",
        headers={"X-Auth-Error-Code": code} if code else {},
    )
