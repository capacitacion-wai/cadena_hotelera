"""Custom roles («roles personalizables», levels 2-4, setting `roles_personalizables: true`).

INVARIANT FILE — installed by `wai-app-builder/assets/modulos.py`; do not edit it.

An internal administrator of the app composes roles IN THE APP — name, one line «who it is for»,
(level 4) internal or of an organisation, and per capability the actions it gets — ONLY from the
pieces the Definition already declares: a capability × action pair some Definition role of the same
side has (`piezas`), with a scope no wider than the widest the Definition gives that pair (narrower
only where the capability is served by data with an owner, where it is applied; «their team» only if
the Definition uses teams). What runs is still a combination of tested pieces: the backend refuses
anything else (`PERFILADO_ROL_PERMISO_NO_DECLARADO`, `PERFILADO_ROL_ALCANCE_NO_PERMITIDO`).

A custom role never administers (neither the app nor an organisation); an organisation role stays
behind its organisation's wall like any other (the wall is the side, not an option). The roles live
in `perfilado_roles_propios` (id `propio_<slug>`, never a Definition id) and are read on EVERY request
(`cargar`, from `servicio.resolver`) into `permisos.roles()`: several processes see the same roles and
`requiere` / `filtrar` / `puede_sobre` need no special case. Every change is logged in
`perfilado_roles_propios_historial` (who, when, what). A role someone holds is not deleted unless
they are moved to another role in the same step. With the setting off every route answers 404 and
the stored roles are ignored (whoever held one loses what it gave: it fails closed).
"""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Dict, List, Optional

from . import db, permisos
from .servicio import ErrorDePerfilado

PREFIJO = "propio_"
MAX_NOMBRE, MAX_QUIEN = 60, 200


def exigir_activado() -> None:
    if not permisos.personalizables():
        raise ErrorDePerfilado(404, "PERFILADO_ROLES_PROPIOS_NO_ACTIVADOS",
                               "custom roles are not turned on in this application (setting roles_personalizables)")


def _json(v: Any, defecto: Any) -> Any:
    try:
        x = json.loads(v) if isinstance(v, str) else v
    except ValueError:
        return defecto
    return x if isinstance(x, type(defecto)) else defecto


def _fila_a_rol(f: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": f["id"], "nombre": f["nombre"], "quien": f.get("quien") or "", "fijo": False, "administra": False,
            "alcance": "todo", "permisos": _json(f.get("permisos"), {}), "alcances": _json(f.get("alcances"), {}),
            "ambito": f.get("ambito") or "interno", "administra_organizacion": False, "origen": "aplicacion",
            "creado_por": f.get("creado_por"), "creado_en": f.get("creado_en"),
            "cambiado_por": f.get("cambiado_por"), "cambiado_en": f.get("cambiado_en")}


async def _leer() -> List[Dict[str, Any]]:
    await db.preparar()
    return [_fila_a_rol(f) for f in await db.leer("SELECT * FROM perfilado_roles_propios ORDER BY lower(nombre)")]


async def cargar() -> None:
    """The custom roles into `permisos.roles()` — on every request (cheap: a handful of rows)."""
    if not permisos.personalizables():
        permisos.usar_propios([])
        return
    permisos.usar_propios(await _leer())


def _ambito_de_la_definicion(r: Dict[str, Any]) -> str:
    return "organizacion" if permisos.nivel() >= 4 and r.get("ambito") == "organizacion" else "interno"


def piezas() -> Dict[str, Dict[str, Dict[str, List[str]]]]:
    """What a custom role may be built from: {side: {capability: {action: [scopes, narrowest first]}}}.
    The pairs are those some Definition role of that side has BY NAME (a «*» row gives nothing to copy).
    Below level 3 the only scope is `todo`; from level 3, up to the widest the Definition gives the pair,
    narrower options only where the capability has data with an owner (elsewhere a narrower scope would
    not be applied), and `equipo` only if the Definition uses teams."""
    nivel = permisos.nivel()
    lados = ["interno", "organizacion"] if nivel >= 4 else ["interno"]
    con_dueno = {c for x in permisos.datos_con_dueno() for c in (x.get("para") or [])}
    definicion = permisos.roles_de_la_definicion()
    usa_equipos = nivel >= 3 and any(permisos._alcance_del_rol(r, c, a) == "equipo"
                                     for r in definicion for c, acc in (r.get("permisos") or {}).items() if c != permisos.TODAS
                                     for a in acc or [])
    out: Dict[str, Dict[str, Dict[str, List[str]]]] = {}
    for lado in lados:
        caps: Dict[str, Dict[str, List[str]]] = {}
        for c in permisos.ids_de_capacidades():
            maximo: Dict[str, int] = {}
            for r in definicion:
                if _ambito_de_la_definicion(r) != lado:
                    continue
                for a in (r.get("permisos") or {}).get(c) or []:
                    if a in permisos.ACCIONES:
                        al = permisos._alcance_del_rol(r, c, a) if nivel >= 3 else "todo"
                        maximo[a] = max(maximo.get(a, -1), permisos.ALCANCES.index(al))
            if not maximo:
                continue
            caps[c] = {}
            for a in permisos.ACCIONES:
                if a not in maximo:
                    continue
                if nivel < 3:
                    caps[c][a] = ["todo"]
                elif c not in con_dueno:
                    caps[c][a] = [permisos.ALCANCES[maximo[a]]]
                else:
                    caps[c][a] = [s for s in permisos.ALCANCES[:maximo[a] + 1] if s != "equipo" or usa_equipos]
        out[lado] = caps
    return out


def _slug(nombre: str) -> str:
    s = unicodedata.normalize("NFKD", nombre).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")[:40] or "rol"


def _validar(datos: Dict[str, Any]) -> Dict[str, Any]:
    """The role as it will be stored, or a plain reason why not."""
    nombre = str(datos.get("nombre") or "").strip()
    if not nombre or len(nombre) > MAX_NOMBRE:
        raise ErrorDePerfilado(422, "PERFILADO_ROL_NOMBRE_NO_VALIDO", f"a role needs a name (up to {MAX_NOMBRE} characters)")
    quien = str(datos.get("quien") or "").strip()
    if len(quien) > MAX_QUIEN:
        raise ErrorDePerfilado(422, "PERFILADO_ROL_DESCRIPCION_NO_VALIDA", f"the description is one line (up to {MAX_QUIEN} characters)")
    ambito = datos.get("ambito") or "interno"
    if ambito not in permisos.AMBITOS or (ambito == "organizacion" and permisos.nivel() < 4):
        raise ErrorDePerfilado(400, "PERFILADO_ROL_AMBITO_NO_VALIDO",
                               "a role is for internal people or (profiling by organisation) for people of an organisation")
    disponibles = piezas().get(ambito) or {}
    pedidos = datos.get("permisos")
    if not isinstance(pedidos, dict):
        raise ErrorDePerfilado(400, "PERFILADO_ROL_SIN_PERMISOS", "say what the role can do: {capability: [actions]}")
    fuera = [f"{c}:{a}" for c, acc in pedidos.items() for a in (acc if isinstance(acc, list) else [acc])
             if a not in (disponibles.get(c) or {})]
    if fuera:
        raise ErrorDePerfilado(400, "PERFILADO_ROL_PERMISO_NO_DECLARADO",
                               f"{fuera} is not something a Definition role for {ambito} people can do: a custom role "
                               "only combines what the Definition declares")
    acciones = {c: [a for a in permisos.ACCIONES if a in acc] for c, acc in pedidos.items() if acc}
    if not acciones:
        raise ErrorDePerfilado(400, "PERFILADO_ROL_SIN_PERMISOS", "a role must be able to do something")
    pedidos_al = datos.get("alcances") if isinstance(datos.get("alcances"), dict) else {}
    alcances: Dict[str, Dict[str, str]] = {}
    for c, acc in acciones.items():
        por_accion = pedidos_al.get(c) if isinstance(pedidos_al.get(c), dict) else {}
        for a in acc:
            opciones = disponibles[c][a]
            al = por_accion.get(a) or opciones[-1]
            if al not in opciones:
                raise ErrorDePerfilado(400, "PERFILADO_ROL_ALCANCE_NO_PERMITIDO",
                                       f"{c}:{a} can only reach {opciones} in this application, not «{al}»")
            alcances.setdefault(c, {})[a] = al
    return {"nombre": nombre, "quien": quien, "ambito": ambito, "permisos": acciones, "alcances": alcances}


async def personas_con(rid: str) -> List[Dict[str, Any]]:
    """Everyone whose record holds the role (active or not: a deactivated record comes back with it)."""
    await db.preparar()
    filas = await db.leer("SELECT id, email, nombre, roles, activo FROM perfilado_usuarios WHERE roles LIKE :p",
                          {"p": f'%"{rid}"%'})
    return [{"id": f["id"], "email": f["email"], "nombre": f.get("nombre"), "activo": bool(f["activo"]),
             "roles": _json(f["roles"], [])} for f in filas if rid in _json(f["roles"], [])]


def _nombre_libre(nombre: str, salvo: Optional[str] = None) -> None:
    for r in permisos.roles():
        if r["id"] != salvo and str(r.get("nombre") or "").strip().lower() == nombre.lower():
            raise ErrorDePerfilado(409, "PERFILADO_ROL_NOMBRE_REPETIDO", f"there is already a role called «{nombre}»")


async def _propio(rid: str) -> Dict[str, Any]:
    filas = await db.leer("SELECT * FROM perfilado_roles_propios WHERE id = :i", {"i": rid})
    if not filas:
        if any(r["id"] == rid for r in permisos.roles_de_la_definicion()):
            raise ErrorDePerfilado(409, "PERFILADO_ROL_DE_LA_DEFINICION",
                                   "this role comes from the application's Definition: it is changed there")
        raise ErrorDePerfilado(404, "PERFILADO_ROL_NO_EXISTE", f"there is no custom role {rid}")
    return _fila_a_rol(filas[0])


def _historial(rid: str, accion: str, por: str, cuando: str, detalle: Dict[str, Any]) -> tuple:
    return ("INSERT INTO perfilado_roles_propios_historial (rol, accion, por, cuando, detalle) VALUES (:r, :a, :p, :c, :d)",
            {"r": rid, "a": accion, "p": por, "c": cuando, "d": json.dumps(detalle, ensure_ascii=False)})


async def crear(datos: Dict[str, Any], *, por: str) -> Dict[str, Any]:
    exigir_activado()
    await cargar()
    rol = _validar(datos)
    async with db.ESCRITURA:
        await cargar()
        _nombre_libre(rol["nombre"])
        usados = set(permisos.ids_de_roles()) | {f["id"] for f in await db.leer("SELECT id FROM perfilado_roles_propios")}
        base = rid = PREFIJO + _slug(rol["nombre"])
        n = 1
        while rid in usados:
            n += 1
            rid = f"{base}_{n}"
        t = db.ahora()
        await db.escribir([
            ("INSERT INTO perfilado_roles_propios (id, nombre, quien, ambito, permisos, alcances, creado_por, creado_en, "
             "cambiado_por, cambiado_en) VALUES (:i, :n, :q, :am, :p, :al, :por, :t, :por, :t)",
             {"i": rid, "n": rol["nombre"], "q": rol["quien"], "am": rol["ambito"], "p": json.dumps(rol["permisos"]),
              "al": json.dumps(rol["alcances"]), "por": por, "t": t}),
            _historial(rid, "creado", por, t, rol)], con_cerrojo=False)
    await cargar()
    return await _propio(rid)


async def cambiar(rid: str, datos: Dict[str, Any], *, por: str) -> Dict[str, Any]:
    """Edits a custom role: it applies at once to everybody who holds it. Its side cannot change while
    someone holds it (they would end up with a role of the other side of the wall)."""
    exigir_activado()
    await cargar()
    antes = await _propio(rid)
    rol = _validar(datos)
    if rol["ambito"] != antes["ambito"] and await personas_con(rid):
        raise ErrorDePerfilado(409, "PERFILADO_ROL_EN_USO",
                               "people hold this role: it cannot change between internal and organisation people")
    async with db.ESCRITURA:
        await cargar()
        _nombre_libre(rol["nombre"], salvo=rid)
        t = db.ahora()
        await db.escribir([
            ("UPDATE perfilado_roles_propios SET nombre = :n, quien = :q, ambito = :am, permisos = :p, alcances = :al, "
             "cambiado_por = :por, cambiado_en = :t WHERE id = :i",
             {"i": rid, "n": rol["nombre"], "q": rol["quien"], "am": rol["ambito"], "p": json.dumps(rol["permisos"]),
              "al": json.dumps(rol["alcances"]), "por": por, "t": t}),
            _historial(rid, "cambiado", por, t, {**rol, "antes": {k: antes[k] for k in ("nombre", "quien", "ambito", "permisos", "alcances")}})],
            con_cerrojo=False)
    await cargar()
    return await _propio(rid)


async def borrar(rid: str, *, por: str, pasar_a: Optional[str] = None) -> Dict[str, Any]:
    """Deletes a custom role. If someone holds it, only together with moving them to `pasar_a` (a role
    of the same side), in ONE transaction; otherwise 409 with who holds it."""
    exigir_activado()
    await cargar()
    rol = await _propio(rid)
    personas = await personas_con(rid)
    if personas and not pasar_a:
        raise ErrorDePerfilado(409, "PERFILADO_ROL_EN_USO",
                               f"{len(personas)} person(s) hold this role ({', '.join(p['email'] for p in personas[:5])}): "
                               "move them to another role in the same step (pasar_a)")
    if pasar_a:
        if pasar_a == rid or pasar_a not in permisos.ids_de_roles():
            raise ErrorDePerfilado(422, "PERFILADO_ROL_DESCONOCIDO", f"there is no role {pasar_a} to move them to")
        if permisos.ambito_de(pasar_a) != permisos.ambito_de(rid):
            raise ErrorDePerfilado(422, "PERFILADO_ROL_DE_OTRO_AMBITO",
                                   "they must be moved to a role of the same side (internal / organisation)")
    async with db.ESCRITURA:
        t = db.ahora()
        sentencias = []
        for p in await personas_con(rid):
            nuevos = list(dict.fromkeys([pasar_a if r == rid else r for r in p["roles"]]))
            sentencias.append(("UPDATE perfilado_usuarios SET roles = :r, actualizado_en = :t WHERE id = :i",
                               {"r": json.dumps(nuevos), "t": t, "i": p["id"]}))
        sentencias += [("DELETE FROM perfilado_roles_propios WHERE id = :i", {"i": rid}),
                       _historial(rid, "borrado", por, t, {"nombre": rol["nombre"], "pasados_a": pasar_a,
                                                           "personas": [p["email"] for p in personas]})]
        await db.escribir(sentencias, con_cerrojo=False)
    await cargar()
    return {"id": rid, "borrado": True, "pasados": len(personas), "pasados_a": pasar_a}


async def en_uso() -> Dict[str, List[Dict[str, Any]]]:
    """{custom role id: [{nombre, email}]} — who holds each one (the screen says it before deleting)."""
    ids = {r["id"] for r in permisos.roles() if r.get("origen") == "aplicacion"}
    if not ids:
        return {}
    out: Dict[str, List[Dict[str, Any]]] = {i: [] for i in ids}
    for f in await db.leer("SELECT email, nombre, roles FROM perfilado_usuarios"):
        for r in _json(f["roles"], []):
            if r in out:
                out[r].append({"email": f["email"], "nombre": f.get("nombre")})
    return out
