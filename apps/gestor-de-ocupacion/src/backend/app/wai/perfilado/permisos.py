"""The profiling configuration: `permisos.json`, GENERATED from the app's approved Definition.

INVARIANT FILE — installed by `wai-app-builder/assets/modulos.py`; do not edit it. What changes
per app is `permisos.json` (next to this file), which is never written by hand either:
`modulos.py actualizar perfilado <app>` regenerates it from `definition/architecture.json`, and the
verifier fails if it differs from what the Definition generates.

Shape (format 1):
    {"formato": 1, "modulo": "perfilado", "nivel": 1|2|3, "ajustes": {...},
     "acciones": [...], "alcances": [...ALCANCES], "capacidades": [{"id", "que"}],
     "roles": [{"id", "nombre", "quien", "fijo", "administra", "alcance",
                "permisos": {"<CAP-id>|*": ["ver", ...]},
                "alcances": {"<CAP-id>": "propio"}                       (levels 1-2: informative)
                         or {"<CAP-id>": {"ver": "equipo", ...}}}],      (level 3: per action, applied)
     "usuarios_de_prueba": [{"email", "roles", "equipo"?, "organizacion"?}],
     "datos_con_dueno": [{"tabla", "para"}],          (level 3)
     "equipos_de_prueba": [{"id", "nombre"}],          (level 3)
     "datos_de_la_organizacion": [{"tabla", "para"}],  (level 4)
     "organizaciones_de_prueba": [{"id", "nombre"}],   (level 4; and each role: "ambito",
                                                         "administra_organizacion")
     "columnas": {"<tabla>": {"dueno": "<column>"}},   (adopted: the app's own owner column)
     "adopcion": {"tabla", "email", "nombre", "rol",   (adopted: the one-off import of the app's
                  "roles": {"<old value>": "<role>"}}}   old user table, `db.importar_adopcion`)

Level 1: two fixed roles (`admin`, `usuario`); any active user may use every capability and only
an administrator manages users. Level 2: one role per profile of the app (`origen: app`) with its
row of the matrix, plus the fixed `admin`. Level 3: the same roles, and each allowed action carries
its SCOPE — `propio` (the rows the person owns), `equipo` (their team's rows, and their own) or
`todo`; the tables marked «with an owner» in the Definition (`datos_con_dueno`) carry the columns
`dueno` (the owner's email) and `equipo` (the team id). Below level 3 every scope is `todo`.
Level 4: each role is `interno` (the company's people: every organisation, or a portfolio) or
`organizacion` (people of an outside organisation: ONLY their organisation's rows); the tables «of the
organisation» (`datos_de_la_organizacion`) carry the column `organizacion` (the organisation id). An
organisation role may administer ITS organisation (`administra_organizacion`), never the app.

Custom roles (setting `roles_personalizables: true`, levels 2-4): an administrator composes roles IN
THE APP from the capability × action pairs the Definition's roles already have (`roles_propios.py`,
table `perfilado_roles_propios`); `roles()` adds them to the Definition's, so every check below goes
through one path. They never administer (neither the app nor an organisation).

An app that ALREADY had its own users and owner columns adopts them (`modulos.py adoptar`,
patterns/perfilado.md §9): its owner column keeps its name (`columna(tabla)`: `owner_email` instead of
`dueno`, no data renamed) and its old user table is imported once into the module (`adopcion()`).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

FICHERO = Path(__file__).resolve().with_name("permisos.json")
ACCIONES = ("ver", "crear", "editar", "borrar", "aprobar", "exportar")
#: From the narrowest to the widest: the union of several roles keeps the widest.
ALCANCES = ("propio", "equipo", "todo")
ROL_ADMIN = "admin"
TODAS = "*"
#: The columns every table «with an owner» carries (level 3).
COLUMNA_DUENO = "dueno"
COLUMNA_EQUIPO = "equipo"
#: The column every table «of the organisation» carries (level 4).
COLUMNA_ORGANIZACION = "organizacion"
AMBITOS = ("interno", "organizacion")

_config: Optional[Dict[str, Any]] = None
#: The custom roles of THIS app (`roles_propios.cargar`, read from its database on every request):
#: they join the Definition's roles only while the setting `roles_personalizables` is on.
_propios: List[Dict[str, Any]] = []


class ConfiguracionInvalida(RuntimeError):
    """`permisos.json` is missing or broken: the app must not start with profiling half-configured."""


def usar(config: Optional[Dict[str, Any]]) -> None:
    """Replaces the loaded configuration (tests) — None = read `permisos.json` again. It forgets the
    custom roles too (the next request reads them again)."""
    global _config, _propios
    _config, _propios = config, []


def config() -> Dict[str, Any]:
    global _config
    if _config is None:
        try:
            _config = json.loads(FICHERO.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfiguracionInvalida(
                f"{FICHERO} cannot be read ({exc}): regenerate it with "
                "`python3 <skill>/assets/modulos.py actualizar perfilado <app>`") from exc
        if not isinstance(_config, dict) or not isinstance(_config.get("roles"), list):
            raise ConfiguracionInvalida(f"{FICHERO} has no `roles`: regenerate it with modulos.py")
    return _config


def nivel() -> int:
    return int(config().get("nivel") or 1)


def ajustes() -> Dict[str, Any]:
    return dict(config().get("ajustes") or {})


def roles_de_la_definicion() -> List[Dict[str, Any]]:
    return [r for r in config().get("roles") or [] if isinstance(r, dict) and r.get("id")]


def personalizables() -> bool:
    """Custom roles are on: setting `roles_personalizables: true`, levels 2-4."""
    return nivel() >= 2 and ajustes().get("roles_personalizables") is True


def usar_propios(lista: List[Dict[str, Any]]) -> None:
    global _propios
    _propios = [dict(r) for r in lista or [] if isinstance(r, dict) and r.get("id")]


def roles() -> List[Dict[str, Any]]:
    """EVERY role the app resolves: the Definition's and, when on, the custom ones — one path for
    `puede`, `alcance_de`, `ambito_de`… (a custom id never shadows a Definition one)."""
    base = roles_de_la_definicion()
    if not _propios or not personalizables():
        return base
    ids = {r["id"] for r in base}
    return base + [r for r in _propios if r["id"] not in ids]


def rol(rid: str) -> Optional[Dict[str, Any]]:
    return next((r for r in roles() if r.get("id") == rid), None)


def ids_de_roles() -> List[str]:
    return [r["id"] for r in roles()]


def capacidades() -> List[Dict[str, Any]]:
    return [c for c in config().get("capacidades") or [] if isinstance(c, dict) and c.get("id")]


def acciones_de_la_capacidad(capacidad: str) -> List[str]:
    """The actions a task really has: those some role of the Definition gives it (not the administrator's `*`). A task
    no role names (level 1, or only `*`) has them all. «My access» lists these and not the six for every task: «View a
    request» cannot be created or approved (02/11/2026)."""
    usadas = {a for r in roles() for a in ((r.get("permisos") or {}).get(capacidad) or []) if a in ACCIONES}
    return [a for a in ACCIONES if a in usadas] or list(ACCIONES)


def capacidades_con_acciones() -> List[Dict[str, Any]]:
    return [{**c, "acciones": acciones_de_la_capacidad(c["id"])} for c in capacidades()]


def ids_de_capacidades() -> List[str]:
    return [c["id"] for c in capacidades()]


def usuarios_de_prueba() -> List[Dict[str, Any]]:
    return [u for u in config().get("usuarios_de_prueba") or [] if isinstance(u, dict) and u.get("email")]


def administra(roles_de_la_persona: List[str]) -> bool:
    return any((rol(r) or {}).get("administra") for r in roles_de_la_persona)


def permisos_de(roles_de_la_persona: List[str]) -> Dict[str, List[str]]:
    """{capability: actions} that the UNION of these roles allows (`*` = every capability)."""
    out: Dict[str, set] = {}
    for rid in roles_de_la_persona:
        for cap, acciones in ((rol(rid) or {}).get("permisos") or {}).items():
            out.setdefault(cap, set()).update(a for a in acciones or [] if a in ACCIONES)
    return {cap: [a for a in ACCIONES if a in acc] for cap, acc in out.items()}


def puede(roles_de_la_persona: List[str], capacidad: str, accion: str) -> bool:
    p = permisos_de(roles_de_la_persona)
    return accion in p.get(capacidad, []) or accion in p.get(TODAS, [])


def datos_con_dueno() -> List[Dict[str, Any]]:
    """The tables marked «with an owner» in the Definition (level 3): [{tabla, para}]."""
    return [x for x in config().get("datos_con_dueno") or [] if isinstance(x, dict) and x.get("tabla")]


def equipos_de_prueba() -> List[Dict[str, Any]]:
    return [x for x in config().get("equipos_de_prueba") or [] if isinstance(x, dict) and x.get("id")]


def _alcance_del_rol(r: Dict[str, Any], capacidad: str, accion: str) -> str:
    """The scope ONE role gives to an action it allows: per action (level 3), per capability, the
    role's own default, else `todo`. A wildcard permission (`*`) is always `todo`."""
    p = r.get("permisos") or {}
    if accion in (p.get(TODAS) or []) or accion not in (p.get(capacidad) or []):
        return "todo"   # allowed through `*`
    a = (r.get("alcances") or {}).get(capacidad)
    if isinstance(a, dict):
        a = a.get(accion)
    if a not in ALCANCES:
        a = r.get("alcance")
    return a if a in ALCANCES else "todo"


def alcance_de(roles_de_la_persona: List[str], capacidad: str, accion: str) -> Optional[str]:
    """The scope with which these roles may do `accion` on `capacidad`: the WIDEST among the roles
    that allow it (`propio` < `equipo` < `todo`); None if none allows it. Below level 3, `todo`."""
    mejor = -1
    for rid in roles_de_la_persona:
        r = rol(rid)
        if not r:
            continue
        p = r.get("permisos") or {}
        if accion in (p.get(capacidad) or []) or accion in (p.get(TODAS) or []):
            mejor = max(mejor, ALCANCES.index(_alcance_del_rol(r, capacidad, accion) if nivel() >= 3 else "todo"))
    return ALCANCES[mejor] if mejor >= 0 else None


def capacidades_por_equipo() -> List[str]:
    """The capabilities where some role reaches «their team» (level 3+), in the Definition's order.
    Empty = teams change nothing anybody sees: the screens then hide «Work groups» (menu, page, column)."""
    if nivel() < 3:
        return []
    return [c for c in ids_de_capacidades()
            if any(_alcance_del_rol(r, c, a) == "equipo" for r in roles() for a in ((r.get("permisos") or {}).get(c) or []))]


def alcances_de(roles_de_la_persona: List[str]) -> Dict[str, Dict[str, str]]:
    """{capability: {action: scope}} of everything these roles allow (the `*` row included)."""
    out: Dict[str, Dict[str, str]] = {}
    for cap, acciones in permisos_de(roles_de_la_persona).items():
        for a in acciones:
            out.setdefault(cap, {})[a] = alcance_de(roles_de_la_persona, cap, a) or "todo"
    return out


# ── level 4: organisations ──────────────────────────────────────────────────────────────────
def datos_de_la_organizacion() -> List[Dict[str, Any]]:
    """The tables «of the organisation» of the Definition (level 4): [{tabla, para}]."""
    return [x for x in config().get("datos_de_la_organizacion") or [] if isinstance(x, dict) and x.get("tabla")]


def organizaciones_de_prueba() -> List[Dict[str, Any]]:
    return [x for x in config().get("organizaciones_de_prueba") or [] if isinstance(x, dict) and x.get("id")]


def ambito_de(rid: str) -> str:
    """`organizacion` only for a role that says so at level 4; anything else is internal."""
    return "organizacion" if nivel() >= 4 and (rol(rid) or {}).get("ambito") == "organizacion" else "interno"


def roles_de_ambito(ambito: str) -> List[str]:
    return [r for r in ids_de_roles() if ambito_de(r) == ambito]


def administra_organizacion(roles_de_la_persona: List[str]) -> bool:
    return nivel() >= 4 and any(ambito_de(r) == "organizacion" and (rol(r) or {}).get("administra_organizacion")
                                for r in roles_de_la_persona)


# ── adopted from the app (`modulos.py adoptar perfilado … --aplicar`) ──────────────────────────
def columna(tabla: Optional[str], cual: str = COLUMNA_DUENO) -> str:
    """The name of the owner column (`cual="dueno"`) of `tabla`: the app's own when it was adopted
    (`columnas`), else the module's (`dueno`)."""
    defecto = {COLUMNA_DUENO: COLUMNA_DUENO, COLUMNA_EQUIPO: COLUMNA_EQUIPO,
               COLUMNA_ORGANIZACION: COLUMNA_ORGANIZACION}.get(cual, cual)
    cols = config().get("columnas") or {}
    v = (cols.get(tabla) or {}).get(cual) if tabla and isinstance(cols, dict) and isinstance(cols.get(tabla), dict) else None
    return v if isinstance(v, str) and v.isidentifier() else defecto


def adopcion() -> Optional[Dict[str, Any]]:
    """The one-off import of the app's old user table, or None."""
    a = config().get("adopcion")
    return a if isinstance(a, dict) and isinstance(a.get("roles"), dict) and a.get("tabla") and a.get("email") else None
