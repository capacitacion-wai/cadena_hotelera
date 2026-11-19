"""How much the employee thinks BEFORE answering, chosen request by request.

The model is chosen once (Operations → Model) and applies to everything. What is chosen
here is something else: on THAT model, how much reasoning is asked of this particular
question. "Which trips do I have?" does not need the same as "compare these three offers
and tell me which suits me", and until now both paid the same time.

Three decisions better not undone:

* **The names are product names, not the provider's.** Whoever writes chooses between
  «Directo», «Rápido», «Normal» and «Profundo»; that inside this is
  ``reasoning_effort`` none/low/medium/high is a detail of the model of the day and must
  not show on any screen. The ids are what travels and what the agent pod declares as
  VARIANTS of the model.
* **Empty is a value, not a gap.** Without a level nothing is sent and the model runs
  with whatever the employee has configured (Operations → effort). It is what someone who
  has never chosen sees, and it is deliberate: the employee keeps ruling over its people.

  Careful not to confuse it with «Directo»: they are different things. `none` is an
  ORDER —do not reason—, and sending nothing is letting the model decide with its own
  default. That is why both options exist and neither is superfluous.
* **One single path.** The same table of levels is written by the agent pod into the
  opencode config (``opencode-server/scripts/resolve_mcp.py``,
  ``NIVELES_DE_RAZONAMIENTO``). A level that exists on only one side is a level that does
  nothing AND does not fail — the worst of both worlds.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from app.app_database import app_session, app_write
from app.app_models.runtime import AppSetting
from app.app_models.users import AppUser
from app.users_directory import loads_meta, merge_meta

logger = logging.getLogger(__name__)

# The key in the person's record (``app_users.metadata_json``).
CLAVE_PREFERENCIA = "razonamiento"

# The catalog. The order is the painting order. The texts are the person's UI.
NIVELES: list[dict[str, str]] = [
    {
        "id": "directo",
        "nombre": "Directo",
        "para": "Sin pararse a pensar. Para lo trivial: un dato, un sí o un no.",
        "esfuerzo": "none",
    },
    {
        "id": "rapido",
        "nombre": "Rápido",
        "para": "Consultas directas: buscar, listar, confirmar un dato.",
        "esfuerzo": "low",
    },
    {
        "id": "normal",
        "nombre": "Normal",
        "para": "Lo habitual: entender la petición y hacer el trabajo.",
        "esfuerzo": "medium",
    },
    {
        "id": "profundo",
        "nombre": "Profundo",
        "para": "Comparar, decidir, planificar. Tarda más y cuesta más.",
        "esfuerzo": "high",
    },
]

#: How much it WRITES, which is not the same as how much it thinks. You can want it to think
#: a lot and answer in two lines — in fact that is the norm when someone decides.
#:
#: It has no selector of its own in the composer on purpose: it belongs more to the CHANNEL
#: than to the request (by voice a long speech is always bad) and more to the employee than
#: to the person (the register it writes in is part of its character, like the language).
#: Two wheels next to the send button mean neither gets touched.
LONGITUDES: list[dict[str, str]] = [
    {"id": "breve", "nombre": "Breve", "para": "Lo justo. Por voz, siempre."},
    {"id": "normal", "nombre": "Normal", "para": "Lo que traiga el modelo."},
    {"id": "detallada", "nombre": "Detallada", "para": "Se explaya cuando hace falta."},
]

#: The key in the employee's settings (Operations).
CLAVE_LONGITUD = "agent_verbosity"

#: Where it is spoken → how much it writes, when the employee has set nothing. «normal» is
#: sending nothing, so the channel only really decides on voice.
LONGITUD_POR_CANAL = {"voz": "breve"}

_IDS = {n["id"] for n in NIVELES}
_IDS_LARGO = {n["id"] for n in LONGITUDES}
_POR_ESFUERZO = {n["esfuerzo"]: n["id"] for n in NIVELES}

# The AppSetting key where the OPERATOR leaves the employee's level (Operations).
# It is still stored as the provider's effort because that is what the pod injects into
# its config at startup; what changes is that the screens no longer show it that way.
CLAVE_DEL_EMPLEADO = "agent_effort"


def normalizar(valor: Any) -> str:
    """The level id, or ``""`` (automatic: whatever the employee has)."""
    v = str(valor or "").strip().lower()
    return v if v in _IDS else ""


def normalizar_longitud(valor: Any) -> str:
    """The length id, or ``""`` (= «normal», send nothing)."""
    v = str(valor or "").strip().lower()
    return v if v in _IDS_LARGO and v != "normal" else ""


def catalogo_de_longitudes() -> list[dict[str, str]]:
    return [dict(x) for x in LONGITUDES]


async def longitud_para(canal: str = "") -> str:
    """How much it writes in this turn: what the employee has set and, if not, the channel.

    One single rule, in that order. That setting «detallada» makes it speak at length by
    voice too is consistent: a person chose it on purpose, and their choice applying on all
    channels is easier to explain than an exception."""
    try:
        async with app_session() as s:
            puesto = ((await s.execute(
                select(AppSetting.value).where(AppSetting.key == CLAVE_LONGITUD)
            )).scalar_one_or_none() or "").strip().lower()
    except Exception:  # noqa: BLE001 — never break a turn over a preference
        logger.debug("razonamiento: no se pudo leer la longitud", exc_info=True)
        puesto = ""
    if puesto:
        return normalizar_longitud(puesto)
    return LONGITUD_POR_CANAL.get((canal or "").strip().lower(), "")


def variante(nivel: Any, longitud: Any = "") -> str:
    """The variant id that travels in the request.

    opencode's variants are a FLAT map, so both decisions go combined in one id
    (`profundo-breve`). The ids are generated by the pod with the same table
    (`resolve_mcp.py`): one that exists on only one side is a choice that does nothing AND
    does not fail, which is the worst of both worlds."""
    n, l = normalizar(nivel), normalizar_longitud(longitud)
    if n and l:
        return f"{n}-{l}"
    if n:
        return n
    if l:
        return f"solo-{l}"
    return ""


def catalogo() -> list[dict[str, str]]:
    """What whoever writes can choose. Without the "automatic" level: that is not one
    more option, it is the absence of a choice, and the screens say it in their own
    words («El del empleado»)."""
    return [dict(n) for n in NIVELES]


def esfuerzo_de(nivel: str) -> str:
    """The ``reasoning_effort`` a level corresponds to. Only to show it on a technical
    card or to measure; NOBODY should send this to opencode: what travels is the level
    id (the variant)."""
    n = normalizar(nivel)
    for item in NIVELES:
        if item["id"] == n:
            return item["esfuerzo"]
    return ""


def nivel_de_esfuerzo(esfuerzo: Any) -> str:
    """The level that corresponds to a stored `reasoning_effort`, or ``""``.

    Empty deliberately includes the efforts that are NO longer offered (`none`, `xhigh`): an
    employee that has one stored from before keeps running with it —nobody touches it— and
    the screens say it with its raw name instead of pretending it is one of the three."""
    return _POR_ESFUERZO.get(str(esfuerzo or "").strip().lower(), "")


def equivalente_sin_fijar(modelo: str = "") -> dict:
    """What NOT setting anything is equivalent to, for the running model.

    "Not set" sounds like "let the model decide", and that does not exist: the effort is
    always set by the caller. If nobody sets it, the layer below does — opencode injects
    ``reasoningEffort: "medium"`` for the whole gpt-5 family (verified in the 1.17.15 binary
    the pod image carries). So "not set" is today, for this catalog, Normal.

    It is DEDUCED from the model instead of being written in a text: the day the catalog has
    a model of another family, the screen stops claiming it on its own instead of lying.
    Returns empty ``{id, nombre}`` when nothing can be claimed."""
    fam = (modelo or "").lower()
    if "gpt-5" in fam and "gpt-5-chat" not in fam and "gpt-5-pro" not in fam:
        return {"id": "normal", "nombre": "Normal"}
    return {"id": "", "nombre": ""}


async def del_empleado() -> dict:
    """What someone who does not choose runs with: what the OPERATOR left set in Operations.

    Returns ``{esfuerzo, id, nombre, equivale_a}``. `id`/`nombre` empty when nothing is
    set —and then `equivale_a` says what that really comes to— or when what is stored is an
    effort no longer offered. This is what makes «El del empleado» stop being a black box
    in the chat: you can SAY which one it is, and when it has not been set, what it is
    equivalent to."""
    vacio = {"esfuerzo": "", "id": "", "nombre": "", "equivale_a": {"id": "", "nombre": ""}}
    try:
        async with app_session() as s:
            esfuerzo = ((await s.execute(
                select(AppSetting.value).where(AppSetting.key == CLAVE_DEL_EMPLEADO)
            )).scalar_one_or_none() or "").strip().lower()
            modelo = ((await s.execute(
                select(AppSetting.value).where(AppSetting.key == "agent_model")
            )).scalar_one_or_none() or "").strip()
    except Exception:  # noqa: BLE001 — showing the default never breaks a screen
        logger.debug("razonamiento: no se pudo leer el nivel del empleado", exc_info=True)
        return vacio
    try:
        from app.config import modelo_vigente
        modelo = modelo_vigente(modelo)
    except Exception:  # noqa: BLE001
        pass
    if not modelo:
        try:
            from app.config import DEFAULT_MODEL
            modelo = DEFAULT_MODEL or ""
        except Exception:  # noqa: BLE001
            modelo = ""
    nivel = nivel_de_esfuerzo(esfuerzo)
    nombre = next((n["nombre"] for n in NIVELES if n["id"] == nivel), "")
    return {
        "esfuerzo": esfuerzo,
        "id": nivel,
        "nombre": nombre,
        "equivale_a": {"id": "", "nombre": ""} if nivel else equivalente_sin_fijar(modelo),
    }


async def preferencia_de(email: str | None) -> str:
    """The level that person left set, or ``""`` if they never chose."""
    correo = (email or "").strip().lower()
    if not correo:
        return ""
    try:
        async with app_session() as s:
            raw = (await s.execute(
                select(AppUser.metadata_json).where(AppUser.email == correo)
            )).scalar_one_or_none()
    except Exception:  # noqa: BLE001 — reading a preference does not bring a turn down
        logger.debug("razonamiento: no se pudo leer la preferencia", exc_info=True)
        return ""
    return normalizar(loads_meta(raw).get(CLAVE_PREFERENCIA))


async def fijar_preferencia(email: str | None, nivel: Any) -> str:
    """Sets that person's default level. ``""`` clears it (back to «el del empleado»).
    Returns what stays stored."""
    correo = (email or "").strip().lower()
    if not correo:
        return ""
    valor = normalizar(nivel)
    async with app_write() as s:
        fila = (await s.execute(
            select(AppUser).where(AppUser.email == correo)
        )).scalar_one_or_none()
        if fila is None:
            # Without a record there is nowhere to store it. One is not created: the
            # directory is governed by whoever administers, and a preference registers nobody.
            return ""
        fila.metadata_json = merge_meta(
            fila.metadata_json, {CLAVE_PREFERENCIA: valor or None}
        )
        await s.commit()
    return valor


#: Who ended up setting the level. It goes to the turn's line, and it is not decoration:
#: without this, a model deciding how much another model thinks fails silently —a weaker
#: answer and nothing saying so—. With this you can check over a week whether it gets it right.
QUIEN_PIDIO = ("persona", "su_defecto", "empleado", "conversador", "modelo")

CLAVE_CONVERSADOR_ELIGE = "conversador_elige_nivel"


async def conversador_puede_elegir() -> bool:
    """The Operations switch. On by default: what it replaces is a blind constant, not
    anybody's choice."""
    try:
        async with app_session() as s:
            v = ((await s.execute(
                select(AppSetting.value).where(AppSetting.key == CLAVE_CONVERSADOR_ELIGE)
            )).scalar_one_or_none() or "").strip().lower()
    except Exception:  # noqa: BLE001
        return True
    return v not in ("0", "false", "no", "off")


async def resolver(email: str | None, pedido: Any,
                   propuesto: Any = "") -> tuple[str, str]:
    """Which level runs in THIS request, and WHO set it.

    The order is not negotiable, and the conversational layer comes in on the second-to-last
    step:

      1. what the person chose in this message;
      2. the level they left set in their record;
      3. the employee's (Operations);
      4. what the conversational layer proposes when reading the request;
      5. nothing — and then the layer below sets it (`medium` in the gpt-5 family).

    So the conversational layer **only replaces the fifth**, that constant nobody chose.
    It overrides no person. And it can never propose «directo»: "do not think" can only be
    said by a declaration of yours about a concrete capability, so the worst case of it
    being wrong is a somewhat weaker answer, never one that was not even attempted."""
    elegido = normalizar(pedido)
    if elegido:
        return elegido, "persona"
    mio = await preferencia_de(email)
    if mio:
        return mio, "su_defecto"
    del_emp = await del_empleado()
    if del_emp.get("id"):
        return del_emp["id"], "empleado"
    sugerido = normalizar(propuesto)
    if sugerido and sugerido != "directo" and await conversador_puede_elegir():
        return sugerido, "conversador"
    return "", "modelo"
