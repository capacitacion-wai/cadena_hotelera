"""The profiling logic: who is who in this app, and the anti-lockout rules.

INVARIANT FILE — installed by `wai-app-builder/assets/modulos.py`; do not edit it.

Errors are domain errors (`ErrorDePerfilado`, with a stable `codigo`) translated to HTTP in ONE
place (`rutas.py` / `dependencias.py`), so the screens can tell one case from another by code.

The anti-lockout guarantee: the owner (`APP_OWNER_EMAIL`) is ALWAYS an administrator, whatever
their record says, and the last active administrator can be neither demoted nor deactivated.

Level 3 («by function and data»): each allowed action has a SCOPE (`propio` / `equipo` / `todo`,
`permisos.alcance_de`) and the person belongs to at most one TEAM. What an application uses on the
tables marked «with an owner» (columns `dueno` = owner's email, `equipo` = team id):

    stmt = usuario.filtrar(select(Pedido), "CAP-1", "ver", dueno_col=Pedido.dueno, equipo_col=Pedido.equipo)
    if not usuario.puede_sobre(pedido, "CAP-1", "editar"): ...   # one record (or exigir_sobre → 403)
    marcar_dueno(pedido, usuario)                                # on create: owner + team

Rows without an owner (created before level 3, or whose owner was deleted: `borrar`) are visible ONLY with scope `todo`: they fail
closed until someone assigns them (`sin_dueno` / `asignar_sin_dueno`, never automatically).

Level 4 («by organisation»): a person is INTERNAL (no organisation: every organisation, or only a
portfolio, `cartera`) or of an outside ORGANISATION (`organizacion`: only that organisation's rows).
The tables «of the organisation» carry the column `organizacion`, and the same helpers wall them:

    stmt = usuario.filtrar(select(Pedido), "CAP-1", "ver", dueno_col=Pedido.dueno,
                           equipo_col=Pedido.equipo, org_col=Pedido.organizacion)
    exigir_sobre(usuario, pedido, "CAP-1", "editar")   # 403 PERFILADO_OTRA_ORGANIZACION
    marcar_dueno(pedido, usuario)                       # an organisation person: ALWAYS their organisation

The wall fails closed: a person of an organisation whose organisation is unknown sees nothing, rows
without an organisation are only for internal people who reach every organisation. An organisation's
administrator (`administra_organizacion`) manages the people of ITS organisation with organisation
roles only; the internal administrators manage everything.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import logging
from collections.abc import Mapping

from sqlalchemy import false, or_

from . import db, permisos

logger = logging.getLogger(__name__)

RX_CORREO = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
RX_TABLA = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Aliases: inside `Usuario` the name `permisos` is its method, not the module.
DUENO, EQUIPO, ORGANIZACION = permisos.COLUMNA_DUENO, permisos.COLUMNA_EQUIPO, permisos.COLUMNA_ORGANIZACION
ORIGENES = ("proyecto", "correo", "despliegue", "pruebas", "externa", "adopcion")
MAX_LIMITE = 200


class ErrorDePerfilado(Exception):
    def __init__(self, estado: int, codigo: str, mensaje: str):
        super().__init__(mensaje)
        self.estado, self.codigo, self.mensaje = estado, codigo, mensaje


@dataclass
class Usuario:
    """The person using the app NOW: platform identity + the app's record."""
    email: str
    nombre: Optional[str] = None
    roles: List[str] = field(default_factory=list)
    activo: bool = True
    es_dueno: bool = False
    id: Optional[int] = None
    equipo: Optional[str] = None
    #: Level 4: the person's outside organisation (None = internal), the portfolio of an internal
    #: person (None = every organisation; [] = none) and whether the platform identity is an
    #: EXTERNAL account (limited to this app: always a person of an organisation).
    organizacion: Optional[str] = None
    cartera: Optional[List[str]] = None
    externa: bool = False

    @property
    def es_de_organizacion(self) -> bool:
        """On the OUTSIDE of the wall (level 4): an organisation recorded, an external account or an
        organisation role. Any of them is enough — when they disagree, the narrowest side wins."""
        if permisos.nivel() < 4 or self.es_dueno:
            return False
        return bool(self.organizacion) or self.externa or any(permisos.ambito_de(r) == "organizacion" for r in self.roles)

    @property
    def administra(self) -> bool:
        return self.es_dueno or (not self.es_de_organizacion and permisos.administra(self.roles))

    @property
    def administra_organizacion(self) -> bool:
        """Level 4: administers the people of ITS organisation (delegated administration)."""
        return self.es_de_organizacion and bool(self.organizacion) and permisos.administra_organizacion(self.roles)

    def _roles_efectivos(self) -> List[str]:
        return self.roles if not self.es_dueno else self.roles + [permisos.ROL_ADMIN]

    def puede(self, capacidad: str, accion: str = "ver") -> bool:
        return self.es_dueno or permisos.puede(self.roles, capacidad, accion)

    def permisos(self) -> Dict[str, List[str]]:
        return permisos.permisos_de(self._roles_efectivos())

    # ── level 3: the scope of each permission ───────────────────────────────────────────────
    def alcance(self, capacidad: str, accion: str = "ver") -> Optional[str]:
        """`propio`, `equipo` or `todo` for this action on this capability; None if not allowed.
        Below level 3 (and for the owner of the app) an allowed action is always `todo`."""
        if self.es_dueno:
            return "todo"
        return permisos.alcance_de(self.roles, capacidad, accion)

    def alcances(self) -> Dict[str, Dict[str, str]]:
        return permisos.alcances_de(self._roles_efectivos())

    def muro(self, org_col: Any) -> Any:
        """Level 4: the condition on `org_col` that keeps this person inside their wall — None (no
        wall: below level 4, the owner, an internal person who reaches every organisation),
        `false()` (nothing: a person of an organisation that is unknown, an empty portfolio) or the
        condition (their organisation; the organisations of their portfolio)."""
        if permisos.nivel() < 4 or self.es_dueno:
            return None
        if self.es_de_organizacion:
            return org_col == self.organizacion if self.organizacion else false()
        if self.cartera is None:
            return None
        return org_col.in_(list(self.cartera)) if self.cartera else false()

    def filtrar(self, query: Any, capacidad: str, accion: str = "ver", *, dueno_col: Any = None,
                equipo_col: Any = None, org_col: Any = None) -> Any:
        """`query` (a SQLAlchemy `select`, or an `update`/`delete`) restricted to the rows this person
        may `accion` on `capacidad`: `todo` → unchanged; `equipo` → their team's rows and their own;
        `propio` → their own; not allowed → no row. Rows without an owner only pass with `todo`.
        `equipo_col` is required when the scope can be `equipo` (fails closed without it).
        Level 4: `org_col` (the table's `organizacion`) walls the rows by organisation FIRST; a person
        of an organisation over a query WITHOUT `org_col` gets no row (fails closed)."""
        a = self.alcance(capacidad, accion)
        if a is None:
            return query.where(false())
        if org_col is not None:
            muro = self.muro(org_col)
            if muro is not None:
                query = query.where(muro)
        elif self.es_de_organizacion:
            logger.warning("profiling: filtrar(%s, %s) for a person of an organisation without org_col: no rows",
                           capacidad, accion)
            return query.where(false())
        if a == "todo":
            return query
        if dueno_col is None:
            raise ValueError(f"filtrar({capacidad!r}, {accion!r}): the scope is «{a}» and no dueno_col was given")
        mio = dueno_col == self.email
        if a == "equipo":
            if equipo_col is None:
                raise ValueError(f"filtrar({capacidad!r}, {accion!r}): the scope is «equipo» and no equipo_col was given")
            return query.where(or_(mio, equipo_col == self.equipo) if self.equipo else mio)
        return query.where(mio)

    def motivo_sobre(self, registro: Any, capacidad: str, accion: str = "ver", *, dueno: Optional[str] = None,
                     equipo: str = EQUIPO, organizacion: str = ORGANIZACION) -> Optional[str]:
        """Why this person may NOT `accion` on THIS record: None (they may), `permiso` (no permission),
        `organizacion` (level 4: another organisation's, or outside their portfolio) or `alcance`
        (another owner's / team's). A record WITHOUT the `organizacion` field is not walled; a person
        of an organisation never reaches one (fails closed). `dueno`: the owner field; by default the
        table's (`permisos.columna`: an adopted `owner_email`, else `dueno`)."""
        dueno = dueno or columna_de_dueno(registro)
        a = self.alcance(capacidad, accion)
        if a is None:
            return "permiso"
        if permisos.nivel() >= 4 and not self.es_dueno:
            tiene, org = _campo(registro, organizacion)
            if self.es_de_organizacion:
                if not tiene or not self.organizacion or org != self.organizacion:
                    return "organizacion"
            elif tiene and self.cartera is not None and org not in self.cartera:
                return "organizacion"
        if a == "todo":
            return None
        de = _valor(registro, dueno)
        if de is not None and str(de).strip().lower() == self.email:
            return None
        if a == "equipo" and bool(self.equipo) and _valor(registro, equipo) == self.equipo:
            return None
        return "alcance"

    def puede_sobre(self, registro: Any, capacidad: str, accion: str = "ver", *,
                    dueno: Optional[str] = None, equipo: str = EQUIPO, organizacion: str = ORGANIZACION) -> bool:
        """May this person `accion` on THIS record (an ORM object or a dict with `dueno`/`equipo`, and at
        level 4 `organizacion`)? The same rule as `filtrar`, for one row already loaded (detail, edit,
        delete, approve…)."""
        return self.motivo_sobre(registro, capacidad, accion, dueno=dueno, equipo=equipo, organizacion=organizacion) is None


def columna_de_dueno(registro: Any) -> str:
    """The owner field of a record: its table's adopted column for an ORM object (`__tablename__`),
    else `dueno` (a dict says it with `dueno="<column>"`)."""
    return permisos.columna(getattr(type(registro), "__tablename__", None) if not isinstance(registro, Mapping) else None)


def _campo(registro: Any, campo: str) -> tuple:
    """(does the record HAVE that field?, its value): a dict/mapping by key, an object by attribute."""
    if isinstance(registro, Mapping):
        return campo in registro, registro.get(campo)
    return hasattr(registro, campo), getattr(registro, campo, None)


def _valor(registro: Any, campo: str) -> Any:
    return _campo(registro, campo)[1]


def _poner(registro: Any, campo: str, valor: Any) -> None:
    if isinstance(registro, dict):
        registro[campo] = valor
    else:
        setattr(registro, campo, valor)


def marcar_dueno(registro: Any, usuario: Usuario, *, dueno: Optional[str] = None, equipo: str = EQUIPO,
                 organizacion: str = ORGANIZACION, de_la_organizacion: Optional[bool] = None) -> Any:
    """Stamps owner (the person's email) and team on a record being CREATED (ORM object or dict).
    Never on an update: changing who owns a row is a decision, not a side effect.

    Level 4, on a record «of the organisation» (it has the `organizacion` field — a dict: put the key,
    or pass `de_la_organizacion=True`): a person of an organisation ALWAYS stamps their own (whatever
    the request said); an internal person keeps the organisation the app chose, which must be within
    their portfolio (`ErrorDePerfilado` 403 `PERFILADO_OTRA_ORGANIZACION`; 422
    `PERFILADO_FALTA_ORGANIZACION` when a portfolio person leaves it empty)."""
    if permisos.nivel() >= 4:
        tiene, actual = _campo(registro, organizacion)
        if de_la_organizacion is None:
            de_la_organizacion = tiene
        if de_la_organizacion:
            if usuario.es_de_organizacion:
                if not usuario.organizacion:
                    raise ErrorDePerfilado(403, "PERFILADO_SIN_ORGANIZACION",
                                           "your access has no organisation: ask an administrator of this application")
                _poner(registro, organizacion, usuario.organizacion)
            elif actual in (None, "") and usuario.cartera is not None and not usuario.es_dueno:
                raise ErrorDePerfilado(422, "PERFILADO_FALTA_ORGANIZACION",
                                       "say for which organisation this record is (one of your portfolio)")
            elif actual not in (None, "") and usuario.cartera is not None and not usuario.es_dueno \
                    and actual not in usuario.cartera:
                raise ErrorDePerfilado(403, "PERFILADO_OTRA_ORGANIZACION",
                                       f"the organisation «{actual}» is not in your portfolio")
    _poner(registro, dueno or columna_de_dueno(registro), usuario.email)
    _poner(registro, equipo, usuario.equipo)
    return registro


def _fila_a_dict(f: Dict[str, Any]) -> Dict[str, Any]:
    try:
        roles = [r for r in json.loads(f.get("roles") or "[]") if isinstance(r, str)]
    except ValueError:
        roles = []
    return {"id": f["id"], "email": f["email"], "nombre": f.get("nombre"), "roles": roles,
            "activo": bool(f.get("activo")), "origen": f.get("origen"), "es_dueno": f["email"] == db.dueno(),
            "equipo": f.get("equipo") or None, "organizacion": f.get("organizacion") or None,
            "con_cartera": bool(f.get("con_cartera")),
            "creado_en": f.get("creado_en"), "actualizado_en": f.get("actualizado_en")}


def normalizar_correo(email: Any) -> str:
    e = str(email or "").strip().lower()
    if not RX_CORREO.match(e):
        raise ErrorDePerfilado(422, "PERFILADO_CORREO_NO_VALIDO", f"«{email}» is not a valid email address")
    return e


def validar_roles(roles: Any) -> List[str]:
    if not isinstance(roles, list) or not roles:
        raise ErrorDePerfilado(422, "PERFILADO_SIN_ROLES", "a user needs at least one role")
    malos = [r for r in roles if r not in permisos.ids_de_roles()]
    if malos:
        raise ErrorDePerfilado(422, "PERFILADO_ROL_DESCONOCIDO",
                               f"unknown roles {malos}: the roles are {permisos.ids_de_roles()}")
    return list(dict.fromkeys(roles))


async def usuario_por_correo(email: str) -> Optional[Dict[str, Any]]:
    await db.preparar()
    filas = await db.leer("SELECT * FROM perfilado_usuarios WHERE email = :e", {"e": email.strip().lower()})
    return _fila_a_dict(filas[0]) if filas else None


async def usuario_por_id(uid: int) -> Dict[str, Any]:
    await db.preparar()
    filas = await db.leer("SELECT * FROM perfilado_usuarios WHERE id = :i", {"i": uid})
    if not filas:
        raise ErrorDePerfilado(404, "PERFILADO_USUARIO_NO_EXISTE", f"there is no user {uid}")
    out = _fila_a_dict(filas[0])
    out["cartera"] = await cartera_de(uid) if out["con_cartera"] else None
    return out


async def cartera_de(uid: int) -> List[str]:
    return [f["organizacion"] for f in await db.leer(
        "SELECT organizacion FROM perfilado_carteras WHERE usuario = :u ORDER BY organizacion", {"u": uid})]


async def resolver(email: str, *, externa: bool = False) -> Usuario:
    """The role is read ALWAYS from the app's database by the email of the platform identity —
    never from a token claim. Unknown or inactive → 403 with a code the screens explain.

    `externa`: the identity is an EXTERNAL account of the platform (limited to this app). It is never
    the owner and never internal: it needs level 4 and a record of an ORGANISATION, else 403."""
    e = (email or "").strip().lower()
    if not e:
        raise ErrorDePerfilado(403, "PERFILADO_SIN_IDENTIDAD", "the session carries no email")
    fila = await usuario_por_correo(e)
    # The custom roles join the Definition's ones before anything is checked (read on every request).
    from . import roles_propios
    await roles_propios.cargar()
    if externa and permisos.nivel() < 4:
        raise ErrorDePerfilado(403, "PERFILADO_CUENTA_EXTERNA_NO_ADMITIDA",
                               "external accounts can only use an application with profiling by organisation")
    if e == db.dueno() and not externa:
        roles = list(dict.fromkeys((fila or {}).get("roles", []) + [permisos.ROL_ADMIN]))
        return Usuario(email=e, nombre=(fila or {}).get("nombre"), roles=roles, es_dueno=True,
                       id=(fila or {}).get("id"), equipo=(fila or {}).get("equipo"))
    if fila is None:
        raise ErrorDePerfilado(403, "PERFILADO_SIN_ALTA",
                               "you are not registered in this application: ask one of its administrators to add you")
    if not fila["activo"]:
        raise ErrorDePerfilado(403, "PERFILADO_DESACTIVADO",
                               "your access to this application is deactivated: ask one of its administrators")
    if permisos.nivel() < 4:
        return Usuario(email=e, nombre=fila.get("nombre"), roles=fila["roles"], id=fila["id"], equipo=fila.get("equipo"))
    org = fila.get("organizacion")
    if externa and not org:
        raise ErrorDePerfilado(403, "PERFILADO_EXTERNA_SIN_ORGANIZACION",
                               "an external account only enters as a person of an organisation: ask an administrator")
    u = Usuario(email=e, nombre=fila.get("nombre"), roles=fila["roles"], id=fila["id"], equipo=fila.get("equipo"),
                organizacion=org, externa=externa)
    if u.es_de_organizacion:
        # Only the organisation roles count on the outside of the wall (an internal role there would
        # reach every organisation): a record that mixes them is read the narrow way.
        u.roles = [r for r in u.roles if permisos.ambito_de(r) == "organizacion"]
        if org and not (await db.leer("SELECT id FROM perfilado_organizaciones WHERE id = :i AND activo = 1", {"i": org})):
            raise ErrorDePerfilado(403, "PERFILADO_ORGANIZACION_ARCHIVADA",
                                   "your organisation is not active in this application: ask one of its administrators")
    elif fila.get("con_cartera"):
        u.cartera = await cartera_de(fila["id"])
    return u


def validar_ambito(roles: List[str], organizacion: Optional[str]) -> None:
    """Level 4: a person of an organisation holds ONLY organisation roles; an internal person only
    internal ones (`PERFILADO_ROL_DE_OTRO_AMBITO`)."""
    if permisos.nivel() < 4:
        return
    quiere = "organizacion" if organizacion else "interno"
    malos = [r for r in roles if permisos.ambito_de(r) != quiere]
    if malos:
        raise ErrorDePerfilado(422, "PERFILADO_ROL_DE_OTRO_AMBITO",
                               f"roles {malos} are not for {'a person of an organisation' if organizacion else 'an internal person'}: "
                               f"the roles are {permisos.roles_de_ambito(quiere)}")


def exigir_gestion(gestor: Optional[Usuario], objetivo: Optional[Dict[str, Any]] = None,
                   roles: Optional[List[str]] = None) -> None:
    """Who may manage whom. An internal administrator: everybody. An organisation's administrator
    (level 4): only people of ITS organisation (`PERFILADO_OTRA_ORGANIZACION`), only with organisation
    roles (`PERFILADO_SOLO_ROLES_DE_ORGANIZACION`). Anybody else: `PERFILADO_SOLO_ADMINISTRADORES`."""
    if gestor is None or gestor.administra:
        return
    if not gestor.administra_organizacion:
        raise ErrorDePerfilado(403, "PERFILADO_SOLO_ADMINISTRADORES", "only an administrator of this application can do this")
    if objetivo is not None and objetivo.get("organizacion") != gestor.organizacion:
        raise ErrorDePerfilado(403, "PERFILADO_OTRA_ORGANIZACION", "that person is not of your organisation")
    malos = [r for r in roles or [] if permisos.ambito_de(r) != "organizacion"]
    if malos:
        raise ErrorDePerfilado(403, "PERFILADO_SOLO_ROLES_DE_ORGANIZACION",
                               f"an organisation's administrator only gives organisation roles ({permisos.roles_de_ambito('organizacion')})")


async def listar(limite: int = 50, desplazamiento: int = 0, q: str = "",
                 activos: Optional[bool] = None, equipo: Optional[str] = None,
                 organizacion: Optional[str] = None) -> Dict[str, Any]:
    """`organizacion`: only that organisation's people (an organisation's administrator ALWAYS gets
    their own here, forced by the route)."""
    await db.preparar()
    limite = max(1, min(int(limite), MAX_LIMITE))
    donde, params = [], {"l": limite, "o": max(0, int(desplazamiento))}
    if q.strip():
        donde.append("(email LIKE :q OR nombre LIKE :q)")
        params["q"] = f"%{q.strip()}%"
    if activos is not None:
        donde.append("activo = :a")
        params["a"] = 1 if activos else 0
    if equipo:
        donde.append("equipo = :eq")
        params["eq"] = equipo
    if organizacion:
        donde.append("organizacion = :org")
        params["org"] = organizacion
    w = (" WHERE " + " AND ".join(donde)) if donde else ""
    total = (await db.leer(f"SELECT COUNT(*) AS n FROM perfilado_usuarios{w}", params))[0]["n"]
    filas = await db.leer(f"SELECT * FROM perfilado_usuarios{w} ORDER BY activo DESC, lower(coalesce(nombre, email)) "
                          "LIMIT :l OFFSET :o", params)
    items = [_fila_a_dict(f) for f in filas]
    for u in items:
        u["cartera"] = await cartera_de(u["id"]) if u["con_cartera"] else None
    return {"items": items, "total": total, "limite": limite, "desplazamiento": params["o"]}


async def dar_de_alta(email: str, roles: List[str], *, nombre: Optional[str], origen: str,
                      por: str, organizacion: Optional[str] = None, gestor: Optional[Usuario] = None) -> Dict[str, Any]:
    """Adds a person. Someone already active is left as they are (change their roles with `cambiar`:
    re-adding must never demote an administrator); a deactivated record is reactivated with these roles.
    Level 4: `organizacion` makes them a person of that organisation (organisation roles only). An
    organisation's administrator (`gestor`) only adds to ITS organisation; an email already used by
    someone of another organisation (or internal) is refused without saying whose it is."""
    await db.preparar()
    e = normalizar_correo(email)
    roles = validar_roles(roles)
    if origen not in ORIGENES:
        raise ErrorDePerfilado(422, "PERFILADO_ORIGEN_NO_VALIDO", f"origin must be one of {ORIGENES}")
    organizacion = await organizacion_valida(organizacion)
    exigir_gestion(gestor, {"organizacion": organizacion}, roles)
    validar_ambito(roles, organizacion)
    previo = await usuario_por_correo(e)
    if previo is not None and gestor is not None and not gestor.administra and previo["organizacion"] != organizacion:
        raise ErrorDePerfilado(409, "PERFILADO_CORREO_NO_DISPONIBLE",
                               "that email cannot be added here: it is already used in this application")
    t = db.ahora()
    await db.escribir([
        ("INSERT OR IGNORE INTO perfilado_usuarios (email, nombre, roles, activo, origen, organizacion, creado_por, "
         "creado_en, actualizado_en) VALUES (:e, :n, :r, 1, :o, :org, :p, :t, :t)",
         {"e": e, "n": nombre, "r": json.dumps(roles), "o": origen, "org": organizacion, "p": por, "t": t}),
        # A deactivated record comes back with THESE roles and organisation (they go together, level 4).
        ("UPDATE perfilado_usuarios SET roles = :r, activo = 1, nombre = coalesce(:n, nombre), organizacion = :org, "
         "actualizado_en = :t WHERE email = :e AND activo = 0",
         {"e": e, "n": nombre, "r": json.dumps(roles), "org": organizacion, "t": t}),
    ])
    return await usuario_por_correo(e)  # type: ignore[return-value]


async def marcar_dados_de_alta(miembros: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    """The project's members, each with whether they already have an ACTIVE record here."""
    await db.preparar()
    activos = {f["email"] for f in await db.leer("SELECT email FROM perfilado_usuarios WHERE activo = 1")}
    out = [{"email": m["email"], "nombre": m.get("nombre") or "", "dado_de_alta": m["email"] in activos}
           for m in miembros]
    return sorted(out, key=lambda m: (m["nombre"] or m["email"]).lower())


async def alta_desde_proyecto(emails: List[str], roles: List[str], miembros: List[Dict[str, str]],
                              *, por: str) -> Dict[str, List[str]]:
    """Adds several project members at once. Only REAL members (everyone added can log in); who was
    already active is left as they are; the rest come back in `no_son_del_proyecto`."""
    roles = validar_roles(roles)
    de_proyecto = {m["email"]: m.get("nombre") or None for m in miembros}
    hechos: Dict[str, List[str]] = {"dados_de_alta": [], "ya_estaban": [], "no_son_del_proyecto": []}
    for bruto in dict.fromkeys(str(e or "").strip().lower() for e in emails if str(e or "").strip()):
        if bruto not in de_proyecto:
            hechos["no_son_del_proyecto"].append(bruto)
            continue
        previo = await usuario_por_correo(bruto)
        if previo and previo["activo"]:
            hechos["ya_estaban"].append(bruto)
            continue
        await dar_de_alta(bruto, roles, nombre=de_proyecto[bruto], origen="proyecto", por=por)
        hechos["dados_de_alta"].append(bruto)
    return hechos


async def _administradores_activos_sin(uid: int) -> int:
    """How many administrators would remain if `uid` stopped being one (the owner counts)."""
    filas = await db.leer("SELECT id, email, roles FROM perfilado_usuarios WHERE activo = 1 AND id != :i", {"i": uid})
    n = sum(1 for f in filas if f["email"] == db.dueno() or permisos.administra(_fila_a_dict({**f, "activo": 1})["roles"]))
    if db.dueno() and not any(f["email"] == db.dueno() for f in filas):
        objetivo = await usuario_por_id(uid)
        if objetivo["email"] != db.dueno():
            n += 1   # the owner is an administrator even without a record
    return n


#: «Leave the team as it is» (an explicit None removes the person from their team).
SIN_CAMBIO: Any = object()


async def cambiar(uid: int, *, roles: Optional[List[str]] = None, activo: Optional[bool] = None,
                  nombre: Optional[str] = None, equipo: Any = SIN_CAMBIO, organizacion: Any = SIN_CAMBIO,
                  cartera: Any = SIN_CAMBIO, gestor: Optional[Usuario] = None) -> Dict[str, Any]:
    await db.preparar()
    if gestor is not None and not gestor.administra and any(x is not SIN_CAMBIO for x in (equipo, organizacion, cartera)):
        raise ErrorDePerfilado(403, "PERFILADO_SOLO_ADMINISTRADORES",
                               "only an internal administrator changes teams, organisations and portfolios")
    nueva_org = SIN_CAMBIO if organizacion is SIN_CAMBIO else await organizacion_valida(organizacion)
    nueva_cartera = SIN_CAMBIO if cartera is SIN_CAMBIO or cartera is None else [
        await organizacion_valida(o, activa=False) for o in dict.fromkeys(cartera or [])]
    if cartera is None:
        nueva_cartera = None
    # Check and write under the single writer's lock: two administrators demoting each other at
    # the same time must not both see «there is another one left».
    async with db.ESCRITURA:
        return await _cambiar(uid, roles=roles, activo=activo, nombre=nombre, equipo=equipo, organizacion=nueva_org,
                              cartera=nueva_cartera, gestor=gestor)


async def _cambiar(uid: int, *, roles: Optional[List[str]], activo: Optional[bool],
                   nombre: Optional[str], equipo: Any = SIN_CAMBIO, organizacion: Any = SIN_CAMBIO,
                   cartera: Any = SIN_CAMBIO, gestor: Optional[Usuario] = None) -> Dict[str, Any]:
    actual = await usuario_por_id(uid)
    nuevos = validar_roles(roles) if roles is not None else actual["roles"]
    org_final = actual["organizacion"] if organizacion is SIN_CAMBIO else organizacion
    exigir_gestion(gestor, actual, nuevos if roles is not None else None)
    if roles is not None or organizacion is not SIN_CAMBIO:
        validar_ambito(nuevos, org_final)
    if cartera is not SIN_CAMBIO and org_final:
        raise ErrorDePerfilado(422, "PERFILADO_CARTERA_SOLO_INTERNOS",
                               "a portfolio is for internal people: a person of an organisation sees only theirs")
    sigue_activo = actual["activo"] if activo is None else bool(activo)
    deja_de_administrar = permisos.administra(actual["roles"]) and actual["activo"] and (
        not sigue_activo or not permisos.administra(nuevos))
    if actual["es_dueno"] and (not sigue_activo or not permisos.administra(nuevos)):
        raise ErrorDePerfilado(409, "PERFILADO_DUENO_SIEMPRE_ADMINISTRADOR",
                               "the owner of the application is always an administrator: it cannot be demoted or deactivated")
    if deja_de_administrar and await _administradores_activos_sin(uid) == 0:
        raise ErrorDePerfilado(409, "PERFILADO_ULTIMO_ADMINISTRADOR",
                               "this is the last administrator: add another one before demoting or deactivating this one")
    sentencias = [(
        "UPDATE perfilado_usuarios SET roles = :r, activo = :a, nombre = coalesce(:n, nombre), actualizado_en = :t "
        "WHERE id = :i", {"r": json.dumps(nuevos), "a": 1 if sigue_activo else 0, "n": nombre, "t": db.ahora(),
                          "i": uid})]
    if equipo is not SIN_CAMBIO:
        sentencias.append(("UPDATE perfilado_usuarios SET equipo = :eq WHERE id = :i",
                           {"eq": await _equipo_valido(equipo), "i": uid}))
    if organizacion is not SIN_CAMBIO:
        sentencias.append(("UPDATE perfilado_usuarios SET organizacion = :org WHERE id = :i", {"org": organizacion, "i": uid}))
        if organizacion:   # outside the wall: a portfolio means nothing there
            sentencias += [("UPDATE perfilado_usuarios SET con_cartera = 0 WHERE id = :i", {"i": uid}),
                           ("DELETE FROM perfilado_carteras WHERE usuario = :i", {"i": uid})]
    if cartera is not SIN_CAMBIO:
        sentencias += [("DELETE FROM perfilado_carteras WHERE usuario = :i", {"i": uid}),
                       ("UPDATE perfilado_usuarios SET con_cartera = :c WHERE id = :i", {"c": 0 if cartera is None else 1, "i": uid})]
        sentencias += [("INSERT OR IGNORE INTO perfilado_carteras (usuario, organizacion) VALUES (:i, :o)", {"i": uid, "o": o})
                       for o in cartera or []]
    await db.escribir(sentencias, con_cerrojo=False)
    return await usuario_por_id(uid)


async def dar_de_baja(uid: int, gestor: Optional[Usuario] = None) -> Dict[str, Any]:
    """Deactivates (the record stays: reactivating gives the same person back)."""
    return await cambiar(uid, activo=False, gestor=gestor)


async def borrar(uid: int, gestor: Optional[Usuario] = None, *, por: str = "") -> Dict[str, Any]:
    """Deletes a person from the app (`DELETE /api/perfilado/usuarios/{id}`): their record and portfolio
    go (roles and team live in the record), so they no longer get in and hold no permission; adding the
    same email later creates them afresh. Their BUSINESS rows are never touched: they keep `dueno` = the
    email and count as «without an owner» (`sin_dueno`) until someone is registered with it again or an
    administrator assigns them. Logged in `perfilado_usuarios_historial`. Refused: the owner of the app
    (`PERFILADO_DUENO_NO_SE_BORRA`), yourself (`PERFILADO_NO_PUEDES_BORRARTE`), the last administrator,
    and — an organisation's administrator — anyone outside their organisation (`exigir_gestion`).
    Returns the record as it was. The external account in the platform is closed by the route."""
    await db.preparar()
    async with db.ESCRITURA:
        actual = await usuario_por_id(uid)
        exigir_gestion(gestor, actual)
        if actual["es_dueno"]:
            raise ErrorDePerfilado(409, "PERFILADO_DUENO_NO_SE_BORRA",
                                   "the owner of the application cannot be deleted")
        if gestor is not None and gestor.email == actual["email"]:
            raise ErrorDePerfilado(409, "PERFILADO_NO_PUEDES_BORRARTE",
                                   "you cannot delete yourself: ask another administrator")
        if actual["activo"] and permisos.administra(actual["roles"]) and await _administradores_activos_sin(uid) == 0:
            raise ErrorDePerfilado(409, "PERFILADO_ULTIMO_ADMINISTRADOR",
                                   "this is the last administrator: add another one before deleting this one")
        detalle = {k: actual.get(k) for k in ("nombre", "roles", "activo", "origen", "equipo", "organizacion", "cartera")}
        await db.escribir([
            ("DELETE FROM perfilado_carteras WHERE usuario = :i", {"i": uid}),
            ("DELETE FROM perfilado_usuarios WHERE id = :i", {"i": uid}),
            ("INSERT INTO perfilado_usuarios_historial (email, accion, por, cuando, detalle) VALUES (:e, 'borrado', :p, :c, :d)",
             {"e": actual["email"], "p": por or (gestor.email if gestor else None), "c": db.ahora(),
              "d": json.dumps(detalle, ensure_ascii=False)}),
        ], con_cerrojo=False)
    logger.info("profiling: user %s deleted by %s", actual["email"], por or (gestor.email if gestor else "-"))
    return actual


# ── level 3: teams ──────────────────────────────────────────────────────────────────────────
def _exigir_nivel_3() -> None:
    if permisos.nivel() < 3:
        raise ErrorDePerfilado(409, "PERFILADO_EQUIPOS_DESDE_NIVEL_3",
                               "teams exist from profiling level 3 («by function and data») on")


def _slug(nombre: str) -> str:
    s = unicodedata.normalize("NFKD", nombre).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:40] or "equipo"


def _equipo_a_dict(f: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": f["id"], "nombre": f["nombre"], "activo": bool(f.get("activo")),
            "miembros": int(f.get("miembros") or 0), "creado_en": f.get("creado_en")}


async def listar_equipos(incluir_archivados: bool = False) -> List[Dict[str, Any]]:
    await db.preparar()
    filas = await db.leer(
        "SELECT e.*, (SELECT COUNT(*) FROM perfilado_usuarios u WHERE u.equipo = e.id AND u.activo = 1) AS miembros "
        "FROM perfilado_equipos e" + ("" if incluir_archivados else " WHERE e.activo = 1") +
        " ORDER BY e.activo DESC, lower(e.nombre)")
    return [_equipo_a_dict(f) for f in filas]


async def equipo_por_id(eid: str) -> Dict[str, Any]:
    await db.preparar()
    filas = await db.leer(
        "SELECT e.*, (SELECT COUNT(*) FROM perfilado_usuarios u WHERE u.equipo = e.id AND u.activo = 1) AS miembros "
        "FROM perfilado_equipos e WHERE e.id = :i", {"i": eid})
    if not filas:
        raise ErrorDePerfilado(404, "PERFILADO_EQUIPO_NO_EXISTE", f"there is no team {eid}")
    return _equipo_a_dict(filas[0])


async def _equipo_valido(equipo: Any) -> Optional[str]:
    """None (no team) or the id of an ACTIVE team; anything else is refused."""
    if equipo in (None, ""):
        return None
    _exigir_nivel_3()
    e = await equipo_por_id(str(equipo))
    if not e["activo"]:
        raise ErrorDePerfilado(409, "PERFILADO_EQUIPO_ARCHIVADO", f"the team «{e['nombre']}» is archived")
    return e["id"]


def _nombre_de_equipo(nombre: Any) -> str:
    n = str(nombre or "").strip()
    if not n or len(n) > 80:
        raise ErrorDePerfilado(422, "PERFILADO_EQUIPO_NOMBRE_NO_VALIDO", "a team needs a name (up to 80 characters)")
    return n


async def crear_equipo(nombre: str, *, por: str) -> Dict[str, Any]:
    _exigir_nivel_3()
    await db.preparar()
    n = _nombre_de_equipo(nombre)
    async with db.ESCRITURA:
        if await db.leer("SELECT id FROM perfilado_equipos WHERE lower(nombre) = lower(:n)", {"n": n}):
            raise ErrorDePerfilado(409, "PERFILADO_EQUIPO_REPETIDO", f"there is already a team called «{n}»")
        base, i = _slug(n), 1
        eid = base
        # The id is never reused (an archived team keeps its id): a new team never inherits old rows.
        while await db.leer("SELECT id FROM perfilado_equipos WHERE id = :i", {"i": eid}):
            i += 1
            eid = f"{base}-{i}"
        t = db.ahora()
        await db.escribir([("INSERT INTO perfilado_equipos (id, nombre, activo, creado_por, creado_en, actualizado_en) "
                            "VALUES (:i, :n, 1, :p, :t, :t)", {"i": eid, "n": n, "p": por, "t": t})], con_cerrojo=False)
    return await equipo_por_id(eid)


async def cambiar_equipo(eid: str, *, nombre: Optional[str] = None, activo: Optional[bool] = None) -> Dict[str, Any]:
    """Renames (the id stays: the rows keep pointing at it) or archives/reactivates a team. A team
    with active members cannot be archived: move them first (nobody is left in a ghost team)."""
    _exigir_nivel_3()
    actual = await equipo_por_id(eid)
    async with db.ESCRITURA:
        sentencias = []
        if nombre is not None:
            n = _nombre_de_equipo(nombre)
            if await db.leer("SELECT id FROM perfilado_equipos WHERE lower(nombre) = lower(:n) AND id != :i",
                             {"n": n, "i": eid}):
                raise ErrorDePerfilado(409, "PERFILADO_EQUIPO_REPETIDO", f"there is already a team called «{n}»")
            sentencias.append(("UPDATE perfilado_equipos SET nombre = :n, actualizado_en = :t WHERE id = :i",
                               {"n": n, "t": db.ahora(), "i": eid}))
        if activo is not None:
            if not activo and actual["miembros"]:
                raise ErrorDePerfilado(409, "PERFILADO_EQUIPO_CON_MIEMBROS",
                                       f"the team «{actual['nombre']}» still has {actual['miembros']} active member(s): "
                                       "move them to another team first")
            sentencias.append(("UPDATE perfilado_equipos SET activo = :a, actualizado_en = :t WHERE id = :i",
                               {"a": 1 if activo else 0, "t": db.ahora(), "i": eid}))
        if sentencias:
            await db.escribir(sentencias, con_cerrojo=False)
    return await equipo_por_id(eid)


# ── level 3: rows created before the owner existed ──────────────────────────────────────────
def _tablas_con_dueno() -> List[str]:
    return [x["tabla"] for x in permisos.datos_con_dueno() if RX_TABLA.match(str(x["tabla"]))]


def _sin_dueno_donde(d: str) -> str:
    """The rows of a table «with an owner» (owner column `d`) that have NO owner: none recorded, or
    one DELETED from the app (`borrar`) and not registered again. An owner merely unknown here (an
    adopted row, someone never added) is not counted: only a deletion says the owner is gone."""
    return (f"(\"{d}\" IS NULL OR trim(\"{d}\") = '' OR lower(trim(\"{d}\")) IN ("
            "SELECT h.email FROM perfilado_usuarios_historial h WHERE h.accion = 'borrado' "
            "AND h.email NOT IN (SELECT email FROM perfilado_usuarios)))")


async def sin_dueno() -> List[Dict[str, Any]]:
    """[{tabla, sin_dueno, total, columnas}] for every table «with an owner»: how many rows have no
    owner yet, or whose owner was deleted (`_sin_dueno_donde`; only visible with scope `todo`).
    `columnas: false` = the table lacks the `dueno`/`equipo` columns; `existe: false` = the table is not in the database yet. Never fixes
    anything: the person decides who owns them (`asignar_sin_dueno`)."""
    await db.preparar()
    out = []
    for tabla in _tablas_con_dueno():
        cols = {f["name"] for f in await db.leer(f'PRAGMA table_info("{tabla}")')}
        if not cols:
            out.append({"tabla": tabla, "existe": False, "columnas": False, "sin_dueno": 0, "total": 0})
            continue
        d = permisos.columna(tabla)
        ok = {d, permisos.COLUMNA_EQUIPO} <= cols
        total = (await db.leer(f'SELECT COUNT(*) AS n FROM "{tabla}"'))[0]["n"]
        n = (await db.leer(f'SELECT COUNT(*) AS n FROM "{tabla}" WHERE {_sin_dueno_donde(d)}'))[0]["n"] if ok else total
        out.append({"tabla": tabla, "existe": True, "columnas": ok, "sin_dueno": n, "total": total})
    return out


async def asignar_sin_dueno(tabla: str, dueno: str, equipo: Optional[str]) -> Dict[str, Any]:
    """Gives the rows of `tabla` WITHOUT an owner (none, or a deleted one) to `dueno` (a registered
    user) and `equipo`. Only those rows: an owned row is never reassigned here. Called by an administrator after the
    person agreed the backfill (patterns/perfilado.md) — never by the app on its own."""
    _exigir_nivel_3()
    if tabla not in _tablas_con_dueno():
        raise ErrorDePerfilado(404, "PERFILADO_TABLA_SIN_DUENO",
                               f"«{tabla}» is not a table with an owner in the Definition ({_tablas_con_dueno()})")
    e = normalizar_correo(dueno)
    if await usuario_por_correo(e) is None and e != db.dueno():
        raise ErrorDePerfilado(422, "PERFILADO_DUENO_SIN_ALTA", f"{e} is not registered in this application")
    eq = await _equipo_valido(equipo)
    d = permisos.columna(tabla)
    await db.escribir([(f'UPDATE "{tabla}" SET "{d}" = :d, {permisos.COLUMNA_EQUIPO} = :q WHERE {_sin_dueno_donde(d)}',
                        {"d": e, "q": eq})])
    return next(x for x in await sin_dueno() if x["tabla"] == tabla)


# ── level 4: organisations, portfolios and the rows still without an organisation ─────────────
def _exigir_nivel_4() -> None:
    if permisos.nivel() < 4:
        raise ErrorDePerfilado(409, "PERFILADO_ORGANIZACIONES_DESDE_NIVEL_4",
                               "organisations exist from profiling level 4 («by organisation») on")


def _organizacion_a_dict(f: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": f["id"], "nombre": f["nombre"], "activo": bool(f.get("activo")),
            "miembros": int(f.get("miembros") or 0), "creado_en": f.get("creado_en")}


_SQL_ORGANIZACION = ("SELECT o.*, (SELECT COUNT(*) FROM perfilado_usuarios u WHERE u.organizacion = o.id AND u.activo = 1) "
                     "AS miembros FROM perfilado_organizaciones o")


async def listar_organizaciones(incluir_archivadas: bool = False, solo: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Every organisation (or only those of `solo`: an organisation's person, a portfolio)."""
    await db.preparar()
    filas = await db.leer(_SQL_ORGANIZACION + ("" if incluir_archivadas else " WHERE o.activo = 1") +
                          " ORDER BY o.activo DESC, lower(o.nombre)")
    return [_organizacion_a_dict(f) for f in filas if solo is None or f["id"] in solo]


async def organizacion_por_id(oid: str) -> Dict[str, Any]:
    await db.preparar()
    filas = await db.leer(_SQL_ORGANIZACION + " WHERE o.id = :i", {"i": oid})
    if not filas:
        raise ErrorDePerfilado(404, "PERFILADO_ORGANIZACION_NO_EXISTE", f"there is no organisation {oid}")
    return _organizacion_a_dict(filas[0])


async def organizacion_valida(organizacion: Any, activa: bool = True) -> Optional[str]:
    """None (internal) or the id of an existing (by default ACTIVE) organisation; anything else is refused."""
    if organizacion in (None, ""):
        return None
    _exigir_nivel_4()
    o = await organizacion_por_id(str(organizacion))
    if activa and not o["activo"]:
        raise ErrorDePerfilado(409, "PERFILADO_ORGANIZACION_ARCHIVADA", f"the organisation «{o['nombre']}» is archived")
    return o["id"]


def _nombre_de_organizacion(nombre: Any) -> str:
    n = str(nombre or "").strip()
    if not n or len(n) > 120:
        raise ErrorDePerfilado(422, "PERFILADO_ORGANIZACION_NOMBRE_NO_VALIDO",
                               "an organisation needs a name (up to 120 characters)")
    return n


async def crear_organizacion(nombre: str, *, por: str) -> Dict[str, Any]:
    _exigir_nivel_4()
    await db.preparar()
    n = _nombre_de_organizacion(nombre)
    async with db.ESCRITURA:
        if await db.leer("SELECT id FROM perfilado_organizaciones WHERE lower(nombre) = lower(:n)", {"n": n}):
            raise ErrorDePerfilado(409, "PERFILADO_ORGANIZACION_REPETIDA", f"there is already an organisation called «{n}»")
        base, i = _slug(n), 1
        oid = base
        # The id is never reused (an archived organisation keeps its id): a new one never inherits old rows.
        while await db.leer("SELECT id FROM perfilado_organizaciones WHERE id = :i", {"i": oid}):
            i += 1
            oid = f"{base}-{i}"
        t = db.ahora()
        await db.escribir([("INSERT INTO perfilado_organizaciones (id, nombre, activo, creado_por, creado_en, actualizado_en) "
                            "VALUES (:i, :n, 1, :p, :t, :t)", {"i": oid, "n": n, "p": por, "t": t})], con_cerrojo=False)
    return await organizacion_por_id(oid)


async def cambiar_organizacion(oid: str, *, nombre: Optional[str] = None, activo: Optional[bool] = None) -> Dict[str, Any]:
    """Renames (the id stays) or archives/reactivates. One with active people cannot be archived:
    deactivate them first (nobody is left inside an organisation that no longer exists)."""
    _exigir_nivel_4()
    actual = await organizacion_por_id(oid)
    async with db.ESCRITURA:
        sentencias = []
        if nombre is not None:
            n = _nombre_de_organizacion(nombre)
            if await db.leer("SELECT id FROM perfilado_organizaciones WHERE lower(nombre) = lower(:n) AND id != :i",
                             {"n": n, "i": oid}):
                raise ErrorDePerfilado(409, "PERFILADO_ORGANIZACION_REPETIDA", f"there is already an organisation called «{n}»")
            sentencias.append(("UPDATE perfilado_organizaciones SET nombre = :n, actualizado_en = :t WHERE id = :i",
                               {"n": n, "t": db.ahora(), "i": oid}))
        if activo is not None:
            if not activo and actual["miembros"]:
                raise ErrorDePerfilado(409, "PERFILADO_ORGANIZACION_CON_MIEMBROS",
                                       f"«{actual['nombre']}» still has {actual['miembros']} active person(s): deactivate them first")
            sentencias.append(("UPDATE perfilado_organizaciones SET activo = :a, actualizado_en = :t WHERE id = :i",
                               {"a": 1 if activo else 0, "t": db.ahora(), "i": oid}))
        if sentencias:
            await db.escribir(sentencias, con_cerrojo=False)
    return await organizacion_por_id(oid)


def _tablas_de_la_organizacion() -> List[str]:
    return [x["tabla"] for x in permisos.datos_de_la_organizacion() if RX_TABLA.match(str(x["tabla"]))]


async def sin_organizacion() -> List[Dict[str, Any]]:
    """[{tabla, existe, columnas, sin_organizacion, total}] for every table «of the organisation»:
    rows without an organisation yet (created before level 4) — only internal people who reach every
    organisation see them. Never fixes anything: the person decides (`asignar_sin_organizacion`)."""
    await db.preparar()
    col = permisos.COLUMNA_ORGANIZACION
    out = []
    for tabla in _tablas_de_la_organizacion():
        cols = {f["name"] for f in await db.leer(f'PRAGMA table_info("{tabla}")')}
        if not cols:
            out.append({"tabla": tabla, "existe": False, "columnas": False, "sin_organizacion": 0, "total": 0})
            continue
        total = (await db.leer(f'SELECT COUNT(*) AS n FROM "{tabla}"'))[0]["n"]
        n = (await db.leer(f'SELECT COUNT(*) AS n FROM "{tabla}" WHERE {col} IS NULL OR trim({col}) = \'\''))[0]["n"] \
            if col in cols else total
        out.append({"tabla": tabla, "existe": True, "columnas": col in cols, "sin_organizacion": n, "total": total})
    return out


async def asignar_sin_organizacion(tabla: str, organizacion: str) -> Dict[str, Any]:
    """Gives the rows of `tabla` WITHOUT an organisation to `organizacion` (an active one). Only rows
    with none: a walled row is never moved here. After the person agreed (patterns/perfilado.md §8)."""
    _exigir_nivel_4()
    if tabla not in _tablas_de_la_organizacion():
        raise ErrorDePerfilado(404, "PERFILADO_TABLA_SIN_ORGANIZACION",
                               f"«{tabla}» is not a table of the organisation in the Definition ({_tablas_de_la_organizacion()})")
    org = await organizacion_valida(organizacion)
    if org is None:
        raise ErrorDePerfilado(422, "PERFILADO_FALTA_ORGANIZACION", "say to which organisation")
    col = permisos.COLUMNA_ORGANIZACION
    await db.escribir([(f'UPDATE "{tabla}" SET {col} = :o WHERE {col} IS NULL OR trim({col}) = \'\'', {"o": org})])
    return next(x for x in await sin_organizacion() if x["tabla"] == tabla)


def matriz() -> Dict[str, Any]:
    """The roles × capabilities matrix the administrator reads (permisos.json + the custom roles,
    `origen: aplicacion`, when the setting is on)."""
    return {"nivel": permisos.nivel(), "ajustes": permisos.ajustes(), "acciones": list(permisos.ACCIONES),
            "capacidades": permisos.capacidades(), "por_equipo": permisos.capacidades_por_equipo(),
            "personalizables": permisos.personalizables(),
            "roles": [{k: r.get(k) for k in ("id", "nombre", "quien", "fijo", "administra", "alcance", "permisos",
                                               "alcances", "ambito", "administra_organizacion", "origen",
                                               "creado_por", "creado_en", "cambiado_por", "cambiado_en")}
                      for r in permisos.roles()]}
