"""Call a WAI APPLICATION with the identity of whoever is asking — not yours.

This module is for a connector whose target is an application built in WAI (the ones in
`workspace/apps/`, the ones put online from «Aplicaciones»). It is not for talking to
an outside system; that is what `conexion_sql.py` and `sesion.py` are for.

The difference from every other connector fits in one sentence: **there is no
credential here**. Not stored, not custodied by anyone, not requested from anyone. What arrives
with each request is the token of the PERSON asking, and all this module does is
put it back on the call to the application:

    employee / code agent / custom agent
        │  Authorization: Bearer <that person's token, signed by the platform>
        ▼
    this connector  ──(the SAME header, as-is)──▶  http://…-service:8080/…
        ▼
    the application decides with ITS usual permissions: project, data area, superuser

Three consequences worth understanding before touching anything:

* **There is no key of its own, and that is correct.** It is this skill's usual rule
  — the lock is needed when the connector STORES the credential, not when the caller
  BRINGS it —. Here the caller brings it, and it is also a credential the application already
  knows how to validate. Whoever arrives without a token gets nothing; whoever arrives with theirs gets
  exactly what they already had.

* **This connector cannot grant more permission than the call brings.** It does not sign tokens, it
  has nothing to sign them with: the key that signs them stays in the platform. That is why this
  module is a twenty-line relay and not a two-hundred-line guard — a connector that
  could issue identities would be a connector able to read anyone's data.

* **The target is FIXED and set by the platform** (`WAI_APP_BASE_URL`, at deploy time). The caller
  does not choose it, which is why this module does NOT use `destinos.py`: the allow-list
  exists for connectors that are told where to go, and this is not one. In fact
  `destinos.py`'s check would reject the correct target — a cluster Service
  resolves to a private address —, so using both at once does not protect: it breaks.

And for the same reason this connector **only works inside WAI**: it goes out to a Kubernetes
Service, which does not exist from the internet.
"""
from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

import httpx

# The application's internal address. The PLATFORM writes it when putting the connector into
# service, because it is the one that knows the Service's name — Kubernetes normalizes the name
# (lowercase, hyphens, 63 max) and rebuilding it here would copy that rule to a place
# where nobody maintains it —. If one day it does not match, the call fails against a Service that
# does not exist and the message says nothing about names: that is why it is not guessed.
BASE = os.environ.get("WAI_APP_BASE_URL", "").rstrip("/")

# How long to wait. A WAI application is in the same cluster, so if it takes longer than
# this something is wrong, not the network being slow.
TIMEOUT = float(os.environ.get("WAI_APP_TIMEOUT", "30"))


class SinIdentidad(Exception):
    """The call arrived without a token. One is not invented: it is reported."""


class AppNoConfigurada(Exception):
    """`WAI_APP_BASE_URL` is missing: the connector does not know which application it serves."""


def _autorizacion(cabeceras: dict) -> str:
    """The incoming request's `Authorization`, or raises saying what is missing.

    Read case-insensitively because HTTP headers are case-insensitive and the MCP client
    does not promise how it writes them."""
    for clave, valor in (cabeceras or {}).items():
        if clave.lower() == "authorization" and valor:
            return valor
    raise SinIdentidad(
        "This call arrived without an identity, and a WAI application is not called without "
        "one: the application applies the permissions of whoever asks (project and data "
        "area), so without knowing who it is there is no answer to give. If this happens with the "
        "connector properly attached, the platform did not set the header — do not fix it "
        "here by setting a fixed token, that turns everyone into the same person."
    )


def _marca(cabeceras: dict) -> dict:
    """The test run mark, if the call comes from one (`X-WAI-Test-Run`): forwarded so the APPLICATION marks what this
    call creates and the run's sweep finds it (built-in testing, `pruebas_de_serie.py`). It only labels."""
    for clave, valor in (cabeceras or {}).items():
        if clave.lower() == "x-wai-test-run" and valor:
            return {"X-WAI-Test-Run": str(valor)}
    return {}


async def llamar(cabeceras: dict, ruta: str, metodo: str = "GET",
                 params: dict | None = None, cuerpo: Any = None) -> Any:
    """Calls the application forwarding the identity of whoever asks.

    `ruta` is the application's path (`/api/pedidos`), NOT a URL: the caller does not choose the target.

    Returns the application's JSON, or its text if it is not JSON. The application's errors
    propagate WITH their body: a 403 from the application means "this person does not have that
    project" and that is the useful answer, not a bare "error 403"."""
    if not BASE:
        raise AppNoConfigurada(
            "`WAI_APP_BASE_URL` is missing. The platform sets it when putting the connector into "
            "service; if it is not there, it was deployed without telling it which application it serves."
        )
    autorizacion = _autorizacion(cabeceras)

    # The path is normalized here and not trusted: a tool that receives `../..` from the
    # model must not be able to escape the application's prefix.
    limpia = "/" + "/".join(quote(t, safe="") for t in ruta.split("/") if t and t != "..")

    async with httpx.AsyncClient(timeout=TIMEOUT) as cliente:
        r = await cliente.request(
            metodo.upper(), f"{BASE}{limpia}",
            params=params or None,
            json=cuerpo if cuerpo is not None else None,
            # ONLY the authorization (and the test run mark, if any). Forwarding the rest of the incoming request's headers
            # would drag along `host`, `content-length` and the MCP session's own, and some of
            # them break the call in ways that are hard to see.
            headers={"Authorization": autorizacion, "Accept": "application/json", **_marca(cabeceras)},
        )
    if r.status_code >= 400:
        detalle = ""
        try:
            detalle = str((r.json() or {}).get("detail") or "")
        except Exception:  # noqa: BLE001 — the body may not be JSON
            detalle = (r.text or "")[:300]
        raise RuntimeError(f"The application answered {r.status_code}: {detalle}")
    try:
        return r.json()
    except Exception:  # noqa: BLE001
        return r.text


async def descargar(cabeceras: dict, ruta: str, params: dict | None = None,
                    nombre: str = "") -> "ficheros.Fichero":
    """Fetches a FILE from the application, with the identity of whoever asks.

    Same as `llamar`, but without going through JSON: what the application returns is bytes, and
    they are wrapped in a `Fichero` so the routing takes them off the text path — where
    a PDF ends up truncated to 2,000 characters without anything saying so —.

    The name comes from the `Content-Disposition` header if the application sends it, which is
    right: whoever serves the file knows its name better than whoever asks for it."""
    import ficheros

    if not BASE:
        raise AppNoConfigurada("`WAI_APP_BASE_URL` is missing.")
    autorizacion = _autorizacion(cabeceras)
    limpia = "/" + "/".join(quote(t, safe="") for t in ruta.split("/") if t and t != "..")

    async with httpx.AsyncClient(timeout=TIMEOUT) as cliente:
        r = await cliente.get(f"{BASE}{limpia}", params=params or None,
                              headers={"Authorization": autorizacion, **_marca(cabeceras)})
    if r.status_code >= 400:
        raise RuntimeError(f"The application answered {r.status_code}: {(r.text or '')[:300]}")

    if not nombre:
        disp = r.headers.get("content-disposition", "")
        if "filename=" in disp:
            nombre = disp.split("filename=")[-1].strip('"; ')
    return ficheros.Fichero(
        nombre=nombre or os.path.basename(limpia) or "fichero",
        contenido=r.content,
        tipo_mime=(r.headers.get("content-type") or "").split(";")[0].strip(),
    )


async def subir(cabeceras: dict, ruta: str, fichero: "ficheros.Fichero",
                campo: str = "file", datos: dict | None = None) -> Any:
    """Sends a FILE to the application, with the identity of whoever asks.

    It goes as `multipart/form-data`, which is what a FastAPI `UploadFile` expects — and a
    WAI application is a FastAPI app —. Putting it in a JSON as base64 would force the application
    to decode it by hand and to duplicate the size caps on its side.

    `datos` are the regular fields accompanying the file (an identifier, a description):
    they travel in the same form."""
    if not BASE:
        raise AppNoConfigurada("`WAI_APP_BASE_URL` is missing.")
    autorizacion = _autorizacion(cabeceras)
    limpia = "/" + "/".join(quote(t, safe="") for t in ruta.split("/") if t and t != "..")

    async with httpx.AsyncClient(timeout=TIMEOUT) as cliente:
        r = await cliente.post(
            f"{BASE}{limpia}",
            files={campo: (fichero.nombre, fichero.contenido, fichero.tipo_mime)},
            data={k: str(v) for k, v in (datos or {}).items()},
            headers={"Authorization": autorizacion, **_marca(cabeceras)},
        )
    if r.status_code >= 400:
        detalle = ""
        try:
            detalle = str((r.json() or {}).get("detail") or "")
        except Exception:  # noqa: BLE001
            detalle = (r.text or "")[:300]
        raise RuntimeError(f"The application answered {r.status_code}: {detalle}")
    try:
        return r.json()
    except Exception:  # noqa: BLE001
        return r.text


# How to talk to the PLATFORM, not to the application. The deployment sets it, like the
# target: it serves one single purpose, downloading a file from the asker's workspace.
ENGINE = os.environ.get("WAI_ENGINE_URL", "").rstrip("/")
# And which header carries the session. The deployment says so too instead of it being written
# here: the name lives in the platform and so there are no two places that can drift apart.
CABECERA_SESION = os.environ.get("WAI_SESSION_HEADER", "X-WAI-Session").lower()
#: The key to READ the files of that session, set by the platform (04/11/2026). It is not anyone's identity: it only
#: downloads files of that one session. The person's token no longer works there (the platform refuses it).
LLAVE_DEL_WORKSPACE = "x-wai-workspace-token"


class SinSesion(Exception):
    """The call does not say which session it comes from, so there is no workspace to download from."""


async def fichero_del_workspace(cabeceras: dict, ruta: str) -> "ficheros.Fichero":
    """Downloads a file from the asker's workspace, by its PATH.

    It is the right way for a file to reach a tool, and the reason is
    arithmetic: a 200 KB PDF is 273,000 base64 characters, and if it travels as an argument
    the model has to WRITE it. It is not that it is expensive: it does not fit. With this the argument
    is `uploads/vuelo.pdf` — thirty characters — and the bytes go underneath, from the platform
    to the connector, without going through the conversation.

    Who may download what is not decided by this code. From an employee, the caller's `Authorization` is
    forwarded to its workspace. From a code-agent session, the platform's key for THAT session
    (`X-WAI-Workspace-Token`) is forwarded: it only reads that session's files. A connector with this
    function CANNOT read someone else's workspace, even if given another session identifier.

    The path is relative to the workspace (`uploads/vuelo.pdf`), as the session's files
    panel shows it."""
    import ficheros

    autorizacion = _autorizacion(cabeceras)
    limpia = "/".join(t for t in ruta.split("/") if t and t != "..")

    # WHERE the file is downloaded from. There are two places and they are not interchangeable,
    # because they depend on who is asking:
    #
    # · An EMPLOYEE: what you upload to its chat lives in THAT EMPLOYEE's workspace,
    #   which is a separate application. It says where it is itself, with the
    #   `X-WAI-Workspace-Url` header — set by its policy proxy, which knows its
    #   address for certain. This case was missing entirely: the connector only looked at
    #   the platform, there was nothing there, and the employee answered that it "has not
    #   identified the session" — true and completely useless.
    #
    # · The CODE AGENT or a custom one: its files are in its opencode session's
    #   workspace, served by the platform. There the session identifier is
    #   needed.
    #
    # The announced workspace is tried first: it is the asker's.
    destino = ""
    for clave, valor in (cabeceras or {}).items():
        if clave.lower() == "x-wai-workspace-url" and valor:
            destino = valor.rstrip("/")
            break

    if destino:
        async with httpx.AsyncClient(timeout=TIMEOUT) as cliente:
            r = await cliente.get(destino, params={"path": limpia},
                                  headers={"Authorization": autorizacion})
    else:
        if not ENGINE:
            raise AppNoConfigurada(
                "`WAI_ENGINE_URL` is missing. The platform sets it when putting the connector into "
                "service; without it this connector does not know whom to ask for the files."
            )
        sesion = ""
        for clave, valor in (cabeceras or {}).items():
            if clave.lower() == CABECERA_SESION and valor:
                sesion = valor
                break
        if not sesion:
            raise SinSesion(
                "This call says neither which session it comes from nor where the asker's "
                "workspace is, so there is nowhere to download the file from. Both "
                "are set by the platform: if they are missing, this connector is not "
                "declared as a WAI application connector, or the employee using it "
                "is from a version older than the one that announces its workspace."
            )
        llave = ""
        for clave, valor in (cabeceras or {}).items():
            if clave.lower() == LLAVE_DEL_WORKSPACE and valor:
                llave = valor
                break
        if not llave:
            raise SinSesion(
                "This call does not carry the platform's key to read that session's files "
                "(`X-WAI-Workspace-Token`), so the file cannot be downloaded. The platform sets it when the "
                "session starts: if it is missing, the session started before the platform gave it — start "
                "the conversation again."
            )
        async with httpx.AsyncClient(timeout=TIMEOUT) as cliente:
            r = await cliente.get(
                f"{ENGINE}/opencode/{quote(sesion, safe='')}/files/download",
                params={"path": limpia},
                headers={"X-WAI-Workspace-Token": llave},
            )
    if r.status_code == 404:
        # The platform answers 404 both when the file is not there and when the session is not
        # the asker's — on purpose: what is not yours does not exist for you —. It cannot be told
        # apart here, so both are said: blaming only the path sends people looking in the wrong
        # place when the problem was identity.
        raise RuntimeError(
            f"Could not fetch '{limpia}': either that file does not exist, or that session does not "
            f"belong to whoever asks. Paths are relative to the workspace, as the files panel "
            f"shows them (for example 'uploads/vuelo.pdf')."
        )
    if r.status_code >= 400:
        raise RuntimeError(f"The platform answered {r.status_code} when asked for '{limpia}'.")

    return ficheros.Fichero(
        nombre=os.path.basename(limpia) or "fichero",
        contenido=r.content,
        tipo_mime=(r.headers.get("content-type") or "").split(";")[0].strip(),
    )


def estado() -> dict:
    """For `/health`: which application it serves. Never a token: health logs
    get pasted into incidents and end up in a chat."""
    return {"app_configurada": bool(BASE), "destino": BASE or None,
            "puede_bajar_ficheros": bool(ENGINE)}
