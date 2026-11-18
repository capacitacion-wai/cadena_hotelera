"""Tests of the profiling module (`app.wai.perfilado`). They travel WITH the module.

INVARIANT FILE — installed by `wai-app-builder/assets/modulos.py` into the app's backend test suite;
do not edit it. They are self-contained: their own SQLite in a temporary folder, their own
`permisos.json` (injected), a fake platform identity (the `X-Prueba-Email` header replaces
`require_project_access`) and a fake project member list. They never touch the app's data.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import APIRouter, Depends, FastAPI, Header
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Column, Integer, MetaData, String, Table, insert, select
from sqlalchemy.ext.asyncio import create_async_engine

from app.auth import require_project_access
from app.wai import migraciones
from app.wai.perfilado import db, exigir_sobre, externas, marcar_dueno, permisos, proyecto, servicio
from app.wai.perfilado.dependencias import requiere
from app.wai.perfilado.servicio import Usuario

TODAS = ["ver", "crear", "editar", "borrar", "aprobar", "exportar"]
NIVEL_2 = {
    "formato": 1, "modulo": "perfilado", "nivel": 2,
    "ajustes": {"roles_personalizables": False, "alta": "correo"},
    "acciones": TODAS,
    "capacidades": [{"id": "CAP-1", "que": "See orders"}, {"id": "CAP-2", "que": "Approve orders"}],
    "roles": [
        {"id": "admin", "nombre": "Admin", "fijo": True, "administra": True, "alcance": "todo", "permisos": {"*": TODAS}},
        {"id": "comercial", "nombre": "Sales", "fijo": False, "administra": False, "alcance": "propio",
         "permisos": {"CAP-1": ["ver", "crear"]}},
        {"id": "jefe", "nombre": "Manager", "fijo": False, "administra": False, "alcance": "todo",
         "permisos": {"CAP-1": ["ver"], "CAP-2": ["ver", "aprobar"]}},
    ],
    "usuarios_de_prueba": [{"email": "pruebas@pruebas.wai", "roles": ["admin"]},
                           {"email": "comercial@pruebas.wai", "roles": ["comercial"]}],
}
MIEMBROS = [{"email": "ana@x.com", "nombre": "Ana"}, {"email": "luis@x.com", "nombre": "Luis"}]


def _como(email: str) -> dict:
    return {"X-Prueba-Email": email, "Authorization": "Bearer t"}


@pytest_asyncio.fixture
async def entorno(tmp_path, monkeypatch):
    """A tiny app: the module's routes + two business routes protected by `requiere`."""
    for v in ("APP_OWNER_EMAIL", "WAI_MODO_PRUEBAS", "WAI_DATOS_DE_EJEMPLO"):
        monkeypatch.delenv(v, raising=False)
    permisos.usar(dict(NIVEL_2))
    motor = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'perfilado.sqlite'}")
    db.usar_motor(motor)
    estado = {"miembros": list(MIEMBROS)}

    async def miembros(_autorizacion):
        return estado["miembros"]
    monkeypatch.setattr(proyecto, "miembros_del_proyecto", miembros)

    async def identidad(x_prueba_email: str = Header("")):
        # A platform identity that even claims a profile: the module must ignore the claim.
        return SimpleNamespace(email=x_prueba_email, wai_perfil="admin", is_superuser=False)

    negocio = APIRouter(prefix="/api/pedidos")

    @negocio.get("")
    async def ver(_=Depends(requiere("CAP-1", "ver"))):
        return {"ok": True}

    @negocio.post("/aprobar")
    async def aprobar(_=Depends(requiere("CAP-2", "aprobar"))):
        return {"ok": True}

    from app.wai.perfilado.rutas import router
    app = FastAPI()
    app.include_router(router)
    app.include_router(negocio)
    app.dependency_overrides[require_project_access] = identidad
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield SimpleNamespace(c=c, estado=estado, monkeypatch=monkeypatch)
    await motor.dispose()
    db.usar_motor(None)
    permisos.usar(None)


async def _admin(e, email="boss@x.com"):
    await servicio.dar_de_alta(email, ["admin"], nombre="Boss", origen="correo", por="test")


@pytest.mark.asyncio
async def test_an_unknown_person_is_refused_with_a_clear_code(entorno):
    r = await entorno.c.get("/api/perfilado/yo", headers=_como("nobody@x.com"))
    assert r.status_code == 403
    assert r.headers["X-WAI-Codigo"] == "PERFILADO_SIN_ALTA"
    assert "not registered" in r.json()["detail"]


@pytest.mark.asyncio
async def test_the_role_comes_from_the_database_never_from_the_token(entorno):
    # the identity claims `wai_perfil=admin`; without a record it is still refused
    r = await entorno.c.get("/api/perfilado/usuarios", headers=_como("intruder@x.com"))
    assert r.status_code == 403
    await servicio.dar_de_alta("intruder@x.com", ["comercial"], nombre=None, origen="correo", por="t")
    r = await entorno.c.get("/api/perfilado/usuarios", headers=_como("intruder@x.com"))
    assert r.status_code == 403 and r.headers["X-WAI-Codigo"] == "PERFILADO_SOLO_ADMINISTRADORES"


@pytest.mark.asyncio
async def test_the_owner_is_always_an_administrator(entorno):
    entorno.monkeypatch.setenv("APP_OWNER_EMAIL", "Owner@X.com")
    db.usar_motor(db.motor())          # prepare again: the owner gets its record
    r = await entorno.c.get("/api/perfilado/yo", headers=_como("owner@x.com"))
    assert r.status_code == 200, r.text
    yo = r.json()
    assert yo["administra"] and yo["es_dueno"] and "admin" in yo["roles"]
    fila = await servicio.usuario_por_correo("owner@x.com")
    r = await entorno.c.patch(f"/api/perfilado/usuarios/{fila['id']}", json={"roles": ["comercial"]},
                              headers=_como("owner@x.com"))
    assert r.status_code == 409 and r.headers["X-WAI-Codigo"] == "PERFILADO_DUENO_SIEMPRE_ADMINISTRADOR"
    r = await entorno.c.patch(f"/api/perfilado/usuarios/{fila['id']}", json={"activo": False}, headers=_como("owner@x.com"))
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_the_last_administrator_cannot_be_demoted_or_deactivated(entorno):
    await _admin(entorno)
    jefe = await servicio.usuario_por_correo("boss@x.com")
    r = await entorno.c.patch(f"/api/perfilado/usuarios/{jefe['id']}", json={"roles": ["jefe"]},
                              headers=_como("boss@x.com"))
    assert r.status_code == 409 and r.headers["X-WAI-Codigo"] == "PERFILADO_ULTIMO_ADMINISTRADOR"
    r = await entorno.c.patch(f"/api/perfilado/usuarios/{jefe['id']}", json={"activo": False}, headers=_como("boss@x.com"))
    assert r.status_code == 409 and r.headers["X-WAI-Codigo"] == "PERFILADO_ULTIMO_ADMINISTRADOR"
    # with a second administrator it is allowed
    await servicio.dar_de_alta("other@x.com", ["admin"], nombre=None, origen="correo", por="t")
    r = await entorno.c.patch(f"/api/perfilado/usuarios/{jefe['id']}", json={"roles": ["jefe"]},
                              headers=_como("boss@x.com"))
    assert r.status_code == 200 and r.json()["roles"] == ["jefe"]


@pytest.mark.asyncio
async def test_two_administrators_demoting_each_other_at_once_leave_one(entorno):
    await _admin(entorno, "a@x.com")
    await _admin(entorno, "b@x.com")
    a = await servicio.usuario_por_correo("a@x.com")
    b = await servicio.usuario_por_correo("b@x.com")
    resultados = await asyncio.gather(
        servicio.cambiar(a["id"], roles=["jefe"]), servicio.cambiar(b["id"], roles=["jefe"]), return_exceptions=True)
    errores = [x for x in resultados if isinstance(x, servicio.ErrorDePerfilado)]
    assert len(errores) == 1 and errores[0].codigo == "PERFILADO_ULTIMO_ADMINISTRADOR"


@pytest.mark.asyncio
async def test_requiere_allows_what_the_role_allows_and_denies_the_rest(entorno):
    await servicio.dar_de_alta("sales@x.com", ["comercial"], nombre=None, origen="correo", por="t")
    assert (await entorno.c.get("/api/pedidos", headers=_como("sales@x.com"))).status_code == 200
    r = await entorno.c.post("/api/pedidos/aprobar", headers=_como("sales@x.com"))
    assert r.status_code == 403 and r.headers["X-WAI-Codigo"] == "PERFILADO_SIN_PERMISO"
    await servicio.dar_de_alta("manager@x.com", ["jefe"], nombre=None, origen="correo", por="t")
    assert (await entorno.c.post("/api/pedidos/aprobar", headers=_como("manager@x.com"))).status_code == 200


@pytest.mark.asyncio
async def test_level_1_lets_any_active_user_through(entorno):
    permisos.usar({**NIVEL_2, "nivel": 1, "roles": [
        {"id": "admin", "fijo": True, "administra": True, "permisos": {"*": TODAS}},
        {"id": "usuario", "fijo": True, "administra": False, "permisos": {"*": TODAS}}]})
    await servicio.dar_de_alta("u@x.com", ["usuario"], nombre=None, origen="correo", por="t")
    assert (await entorno.c.post("/api/pedidos/aprobar", headers=_como("u@x.com"))).status_code == 200
    assert (await entorno.c.get("/api/perfilado/usuarios", headers=_como("u@x.com"))).status_code == 403


@pytest.mark.asyncio
async def test_a_deactivated_user_is_refused_and_can_be_reactivated(entorno):
    await _admin(entorno)
    await servicio.dar_de_alta("sales@x.com", ["comercial"], nombre=None, origen="correo", por="t")
    fila = await servicio.usuario_por_correo("sales@x.com")
    r = await entorno.c.patch(f"/api/perfilado/usuarios/{fila['id']}", json={"activo": False}, headers=_como("boss@x.com"))
    assert r.status_code == 200 and r.json()["activo"] is False
    r = await entorno.c.get("/api/pedidos", headers=_como("sales@x.com"))
    assert r.status_code == 403 and r.headers["X-WAI-Codigo"] == "PERFILADO_DESACTIVADO"
    r = await entorno.c.patch(f"/api/perfilado/usuarios/{fila['id']}", json={"activo": True},
                              headers=_como("boss@x.com"))
    assert r.status_code == 200
    assert (await entorno.c.get("/api/pedidos", headers=_como("sales@x.com"))).status_code == 200


@pytest.mark.asyncio
async def test_sign_up_by_email_validates_email_and_roles(entorno):
    await _admin(entorno)
    h = _como("boss@x.com")
    r = await entorno.c.post("/api/perfilado/usuarios", json={"email": "New@X.com", "roles": ["jefe"]}, headers=h)
    assert r.status_code == 201 and r.json()["email"] == "new@x.com" and r.json()["origen"] == "correo"
    r = await entorno.c.post("/api/perfilado/usuarios", json={"email": "not-an-email", "roles": ["jefe"]}, headers=h)
    assert r.status_code == 422 and r.headers["X-WAI-Codigo"] == "PERFILADO_CORREO_NO_VALIDO"
    r = await entorno.c.post("/api/perfilado/usuarios", json={"email": "z@x.com", "roles": ["pilot"]}, headers=h)
    assert r.status_code == 422 and r.headers["X-WAI-Codigo"] == "PERFILADO_ROL_DESCONOCIDO"


@pytest.mark.asyncio
async def test_re_adding_an_active_administrator_never_demotes_them(entorno):
    await _admin(entorno)
    r = await entorno.c.post("/api/perfilado/usuarios", json={"email": "boss@x.com", "roles": ["comercial"]},
                             headers=_como("boss@x.com"))
    assert r.status_code == 201 and r.json()["roles"] == ["admin"]


@pytest.mark.asyncio
async def test_sign_up_from_the_project_only_admits_members(entorno):
    permisos.usar({**NIVEL_2, "ajustes": {"roles_personalizables": False, "alta": "proyecto"}})
    await _admin(entorno)
    h = _como("boss@x.com")
    r = await entorno.c.post("/api/perfilado/usuarios", json={"email": "stranger@x.com", "roles": ["jefe"]}, headers=h)
    assert r.status_code == 422 and r.headers["X-WAI-Codigo"] == "PERFILADO_NO_ES_DEL_PROYECTO"
    r = await entorno.c.post("/api/perfilado/usuarios", json={"email": "ana@x.com", "roles": ["jefe"]}, headers=h)
    assert r.status_code == 201 and r.json()["origen"] == "proyecto" and r.json()["nombre"] == "Ana"
    r = await entorno.c.post("/api/perfilado/usuarios/desde-proyecto",
                             json={"emails": ["ana@x.com", "luis@x.com", "ghost@x.com"], "roles": ["comercial"]},
                             headers=h)
    assert r.json() == {"dados_de_alta": ["luis@x.com"], "ya_estaban": ["ana@x.com"],
                        "no_son_del_proyecto": ["ghost@x.com"]}
    miembros = (await entorno.c.get("/api/perfilado/proyecto", headers=h)).json()
    assert miembros["disponible"] and all(m["dado_de_alta"] for m in miembros["miembros"])
    entorno.estado["miembros"] = None   # the platform does not answer
    r = await entorno.c.post("/api/perfilado/usuarios", json={"email": "ana2@x.com", "roles": ["jefe"]}, headers=h)
    assert r.status_code == 503 and r.headers["X-WAI-Codigo"] == "PERFILADO_PLATAFORMA_NO_RESPONDE"
    assert (await entorno.c.get("/api/perfilado/proyecto", headers=h)).json() == {"disponible": False, "miembros": []}


@pytest.mark.asyncio
async def test_the_list_is_paginated_and_the_matrix_readable(entorno):
    await _admin(entorno)
    for n in range(7):
        await servicio.dar_de_alta(f"p{n}@x.com", ["comercial"], nombre=f"P{n}", origen="correo", por="t")
    h = _como("boss@x.com")
    r = (await entorno.c.get("/api/perfilado/usuarios?limit=3&offset=3", headers=h)).json()
    assert r["total"] == 8 and len(r["items"]) == 3 and r["limite"] == 3
    m = (await entorno.c.get("/api/perfilado/roles", headers=h)).json()
    assert [x["id"] for x in m["roles"]] == ["admin", "comercial", "jefe"] and m["nivel"] == 2
    assert m["por_equipo"] == []                            # below level 3 teams change nothing
    yo = (await entorno.c.get("/api/perfilado/yo", headers=_como("p1@x.com"))).json()
    assert yo["permisos"] == {"CAP-1": ["ver", "crear"]} and not yo["administra"]


@pytest.mark.asyncio
async def test_test_mode_seeds_and_restores_the_test_users(entorno):
    entorno.monkeypatch.setenv("WAI_MODO_PRUEBAS", "1")
    db.usar_motor(db.motor())
    yo = (await entorno.c.get("/api/perfilado/yo", headers=_como("comercial@pruebas.wai"))).json()
    assert yo["roles"] == ["comercial"]
    fila = await servicio.usuario_por_correo("comercial@pruebas.wai")
    await servicio.dar_de_baja(fila["id"])
    assert await db.restaurar_usuarios_de_prueba() == 2
    assert (await servicio.usuario_por_correo("comercial@pruebas.wai"))["activo"] is True


@pytest.mark.asyncio
async def test_migrations_are_numbered_recorded_and_idempotent(entorno):
    await db.preparar()
    assert await migraciones.migrar(db.motor(), "perfilado", db.CARPETA_MIGRACIONES) == []
    assert await migraciones.version_aplicada(db.motor(), "perfilado") == max(
        v for v, _, _ in migraciones.pendientes_de(db.CARPETA_MIGRACIONES))
    filas = await db.leer("SELECT COUNT(*) AS n FROM wai_modulos_migraciones WHERE modulo = 'perfilado'")
    assert filas[0]["n"] == len(migraciones.pendientes_de(db.CARPETA_MIGRACIONES))
    columnas = {f["name"] for f in await db.leer("PRAGMA table_info(perfilado_usuarios)")}
    assert {"equipo", "organizacion"} <= columnas   # reserved for levels 3 and 4


@pytest.mark.asyncio
async def test_montar_modulos_mounts_the_routes_and_prepares_at_start_up(tmp_path, monkeypatch):
    from app.wai import montar_modulos
    monkeypatch.setenv("APP_OWNER_EMAIL", "owner@x.com")
    permisos.usar(dict(NIVEL_2))
    motor = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'arranque.sqlite'}")
    db.usar_motor(motor)
    try:
        app = FastAPI()
        assert "perfilado" in montar_modulos(app)
        assert "/api/perfilado/yo" in app.openapi()["paths"]
        async with app.router.lifespan_context(app):
            fila = await servicio.usuario_por_correo("owner@x.com")
        assert fila is not None and fila["roles"] == ["admin"] and fila["origen"] == "despliegue"
    finally:
        await motor.dispose()
        db.usar_motor(None)
        permisos.usar(None)


def test_requiere_refuses_an_unknown_capability_or_action():
    permisos.usar(dict(NIVEL_2))
    try:
        with pytest.raises(ValueError, match="not a capability"):
            requiere("CAP-9", "ver")
        with pytest.raises(ValueError, match="the action is one of"):
            requiere("CAP-1", "fly")
    finally:
        permisos.usar(None)


def test_the_apps_permisos_json_is_valid():
    """The app's REAL configuration (generated from its Definition) loads and has an administrator."""
    permisos.usar(None)
    assert permisos.FICHERO.is_file(), "permisos.json missing: run modulos.py actualizar perfilado <app>"
    assert any(r.get("administra") for r in permisos.roles())
    assert permisos.nivel() in (1, 2, 3, 4)


# ── level 3: scope (own / team / all) and teams ──────────────────────────────────────────────
NIVEL_3 = {
    **NIVEL_2, "nivel": 3,
    "roles": [
        {"id": "admin", "nombre": "Admin", "fijo": True, "administra": True, "alcance": "todo", "permisos": {"*": TODAS}},
        {"id": "comercial", "nombre": "Sales", "fijo": False, "administra": False, "alcance": "propio",
         "permisos": {"CAP-1": ["ver", "crear", "editar"]},
         "alcances": {"CAP-1": {"ver": "propio", "crear": "propio", "editar": "propio"}}},
        {"id": "jefe", "nombre": "Manager", "fijo": False, "administra": False, "alcance": "equipo",
         "permisos": {"CAP-1": ["ver", "editar"]}, "alcances": {"CAP-1": {"ver": "equipo", "editar": "equipo"}}},
        {"id": "direccion", "nombre": "Board", "fijo": False, "administra": False, "alcance": "todo",
         "permisos": {"CAP-1": ["ver"]}, "alcances": {"CAP-1": {"ver": "todo"}}},
    ],
    "datos_con_dueno": [{"tabla": "pedidos", "para": ["CAP-1"]}],
    "equipos_de_prueba": [{"id": "pruebas", "nombre": "Pruebas"}, {"id": "pruebas-otro", "nombre": "Other"}],
}
PEDIDOS = Table("pedidos", MetaData(), Column("id", Integer, primary_key=True), Column("ref", String),
                Column("dueno", String), Column("equipo", String))


@pytest_asyncio.fixture
async def nivel3(tmp_path, monkeypatch):
    """A tiny app at level 3: a table «with an owner» and the four routes an app writes over it."""
    for v in ("APP_OWNER_EMAIL", "WAI_MODO_PRUEBAS", "WAI_DATOS_DE_EJEMPLO"):
        monkeypatch.delenv(v, raising=False)
    permisos.usar(dict(NIVEL_3))
    motor = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'nivel3.sqlite'}")
    async with motor.begin() as con:
        await con.run_sync(PEDIDOS.metadata.create_all)
    db.usar_motor(motor)

    async def identidad(x_prueba_email: str = Header("")):
        return SimpleNamespace(email=x_prueba_email, is_superuser=False)

    negocio = APIRouter(prefix="/api/pedidos")

    @negocio.get("")
    async def listar(usuario: Usuario = Depends(requiere("CAP-1", "ver"))):
        stmt = usuario.filtrar(select(PEDIDOS.c.ref), "CAP-1", "ver", dueno_col=PEDIDOS.c.dueno,
                               equipo_col=PEDIDOS.c.equipo).order_by(PEDIDOS.c.ref)
        async with motor.connect() as con:
            return [r[0] for r in (await con.execute(stmt)).all()]

    @negocio.get("/{ref}")
    async def detalle(ref: str, usuario: Usuario = Depends(requiere("CAP-1", "ver"))):
        async with motor.connect() as con:
            fila = (await con.execute(select(PEDIDOS).where(PEDIDOS.c.ref == ref))).mappings().first()
        exigir_sobre(usuario, dict(fila), "CAP-1", "ver")
        return {"ref": fila["ref"]}

    @negocio.post("")
    async def crear(body: dict, usuario: Usuario = Depends(requiere("CAP-1", "crear"))):
        fila = marcar_dueno({"ref": body["ref"]}, usuario)
        async with motor.begin() as con:
            await con.execute(insert(PEDIDOS).values(**fila))
        return fila

    from app.wai.perfilado.rutas import router
    app = FastAPI()
    app.include_router(router)
    app.include_router(negocio)
    app.dependency_overrides[require_project_access] = identidad
    await servicio.dar_de_alta("boss@x.com", ["admin"], nombre="Boss", origen="correo", por="t")
    norte = await servicio.crear_equipo("Norte", por="t")
    sur = await servicio.crear_equipo("Sur", por="t")
    for email, rol, eq in (("ana@x.com", "comercial", norte), ("luis@x.com", "comercial", norte),
                           ("eva@x.com", "comercial", sur), ("jefa@x.com", "jefe", norte), ("dir@x.com", "direccion", None)):
        u = await servicio.dar_de_alta(email, [rol], nombre=None, origen="correo", por="t")
        if eq:
            await servicio.cambiar(u["id"], equipo=eq["id"])
    async with motor.begin() as con:
        await con.execute(insert(PEDIDOS), [
            {"ref": "A1", "dueno": "ana@x.com", "equipo": norte["id"]},
            {"ref": "L1", "dueno": "luis@x.com", "equipo": norte["id"]},
            {"ref": "E1", "dueno": "eva@x.com", "equipo": sur["id"]},
            {"ref": "X0", "dueno": None, "equipo": None},
        ])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield SimpleNamespace(c=c, norte=norte, sur=sur, motor=motor)
    await motor.dispose()
    db.usar_motor(None)
    permisos.usar(None)


async def _refs(e, email):
    r = await e.c.get("/api/pedidos", headers=_como(email))
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.asyncio
async def test_propio_sees_only_its_own_rows(nivel3):
    assert await _refs(nivel3, "ana@x.com") == ["A1"]


@pytest.mark.asyncio
async def test_equipo_sees_its_teams_rows(nivel3):
    assert await _refs(nivel3, "jefa@x.com") == ["A1", "L1"]


@pytest.mark.asyncio
async def test_todo_sees_every_row_including_those_without_an_owner(nivel3):
    assert await _refs(nivel3, "dir@x.com") == ["A1", "E1", "L1", "X0"]
    assert await _refs(nivel3, "boss@x.com") == ["A1", "E1", "L1", "X0"]


@pytest.mark.asyncio
async def test_a_record_of_another_owner_is_403(nivel3):
    assert (await nivel3.c.get("/api/pedidos/A1", headers=_como("ana@x.com"))).status_code == 200
    r = await nivel3.c.get("/api/pedidos/L1", headers=_como("ana@x.com"))
    assert r.status_code == 403 and r.headers["X-WAI-Codigo"] == "PERFILADO_FUERA_DE_ALCANCE"
    assert (await nivel3.c.get("/api/pedidos/L1", headers=_como("jefa@x.com"))).status_code == 200
    r = await nivel3.c.get("/api/pedidos/E1", headers=_como("jefa@x.com"))   # another team
    assert r.status_code == 403 and r.headers["X-WAI-Codigo"] == "PERFILADO_FUERA_DE_ALCANCE"
    r = await nivel3.c.get("/api/pedidos/X0", headers=_como("jefa@x.com"))   # no owner: fails closed
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_creating_stamps_owner_and_team(nivel3):
    r = await nivel3.c.post("/api/pedidos", json={"ref": "A2"}, headers=_como("ana@x.com"))
    assert r.json() == {"ref": "A2", "dueno": "ana@x.com", "equipo": nivel3.norte["id"]}
    assert await _refs(nivel3, "ana@x.com") == ["A1", "A2"]
    assert "A2" in await _refs(nivel3, "jefa@x.com")


@pytest.mark.asyncio
async def test_a_team_change_takes_effect_on_the_next_request(nivel3):
    jefa = await servicio.usuario_por_correo("jefa@x.com")
    assert await _refs(nivel3, "jefa@x.com") == ["A1", "L1"]
    r = await nivel3.c.patch(f"/api/perfilado/usuarios/{jefa['id']}", json={"equipo": nivel3.sur["id"]},
                             headers=_como("boss@x.com"))
    assert r.status_code == 200 and r.json()["equipo"] == nivel3.sur["id"]
    assert await _refs(nivel3, "jefa@x.com") == ["E1"]
    r = await nivel3.c.patch(f"/api/perfilado/usuarios/{jefa['id']}", json={"equipo": None}, headers=_como("boss@x.com"))
    assert r.status_code == 200 and r.json()["equipo"] is None
    assert await _refs(nivel3, "jefa@x.com") == []          # no team: only what she owns


@pytest.mark.asyncio
async def test_my_access_says_the_team_and_each_scope(nivel3):
    yo = (await nivel3.c.get("/api/perfilado/yo", headers=_como("jefa@x.com"))).json()
    assert yo["nivel"] == 3 and yo["equipo"] == nivel3.norte["id"] and yo["nombre_de_equipo"] == "Norte"
    assert yo["alcances"] == {"CAP-1": {"ver": "equipo", "editar": "equipo"}}


@pytest.mark.asyncio
async def test_the_screens_know_whether_teams_change_anything(nivel3):
    """«Work groups» (menu, page, column) only show when some permission says «their team»."""
    h = _como("boss@x.com")
    assert (await nivel3.c.get("/api/perfilado/roles", headers=h)).json()["por_equipo"] == ["CAP-1"]
    assert (await nivel3.c.get("/api/perfilado/yo", headers=h)).json()["usa_equipos"] is True
    permisos.usar({**NIVEL_3, "roles": [r for r in NIVEL_3["roles"] if r["id"] != "jefe"]})
    assert (await nivel3.c.get("/api/perfilado/roles", headers=h)).json()["por_equipo"] == []
    assert (await nivel3.c.get("/api/perfilado/yo", headers=h)).json()["usa_equipos"] is False


@pytest.mark.asyncio
async def test_a_teams_people_are_listed_and_moved_one_by_one(nivel3):
    """The «Work groups» screen edits members with the same change as «Edit access» in Users."""
    h = _como("boss@x.com")
    norte = (await nivel3.c.get(f"/api/perfilado/usuarios?equipo={nivel3.norte['id']}", headers=h)).json()
    assert sorted(u["email"] for u in norte["items"]) == ["ana@x.com", "jefa@x.com", "luis@x.com"]
    eva = await servicio.usuario_por_correo("eva@x.com")
    assert (await nivel3.c.patch(f"/api/perfilado/usuarios/{eva['id']}", json={"equipo": nivel3.norte["id"]}, headers=h)).status_code == 200
    ana = await servicio.usuario_por_correo("ana@x.com")
    assert (await nivel3.c.patch(f"/api/perfilado/usuarios/{ana['id']}", json={"equipo": None}, headers=h)).json()["equipo"] is None
    norte = (await nivel3.c.get(f"/api/perfilado/usuarios?equipo={nivel3.norte['id']}", headers=h)).json()
    assert sorted(u["email"] for u in norte["items"]) == ["eva@x.com", "jefa@x.com", "luis@x.com"]
    equipos = {e["id"]: e["miembros"] for e in (await nivel3.c.get("/api/perfilado/equipos", headers=h)).json()}
    assert equipos == {nivel3.norte["id"]: 3, nivel3.sur["id"]: 0}


@pytest.mark.asyncio
async def test_teams_are_managed_by_administrators_and_archived_not_deleted(nivel3):
    h = _como("boss@x.com")
    r = await nivel3.c.post("/api/perfilado/equipos", json={"nombre": "Este"}, headers=h)
    assert r.status_code == 201 and r.json()["id"] == "este"
    assert (await nivel3.c.post("/api/perfilado/equipos", json={"nombre": "este"}, headers=h)).headers["X-WAI-Codigo"] == "PERFILADO_EQUIPO_REPETIDO"
    assert (await nivel3.c.post("/api/perfilado/equipos", json={"nombre": "Oeste"}, headers=_como("ana@x.com"))).status_code == 403
    r = await nivel3.c.delete(f"/api/perfilado/equipos/{nivel3.norte['id']}", headers=h)
    assert r.status_code == 409 and r.headers["X-WAI-Codigo"] == "PERFILADO_EQUIPO_CON_MIEMBROS"
    r = await nivel3.c.patch("/api/perfilado/equipos/este", json={"nombre": "Levante"}, headers=h)
    assert r.json() == {**r.json(), "id": "este", "nombre": "Levante"}
    assert (await nivel3.c.delete("/api/perfilado/equipos/este", headers=h)).json()["activo"] is False
    nombres = [e["nombre"] for e in (await nivel3.c.get("/api/perfilado/equipos", headers=_como("ana@x.com"))).json()]
    assert nombres == ["Norte", "Sur"]
    r = await nivel3.c.post("/api/perfilado/equipos", json={"nombre": "Este"}, headers=h)
    assert r.json()["id"] == "este-2"                       # an archived team's id is never reused
    ana = await servicio.usuario_por_correo("ana@x.com")
    r = await nivel3.c.patch(f"/api/perfilado/usuarios/{ana['id']}", json={"equipo": "este"}, headers=h)
    assert r.status_code == 409 and r.headers["X-WAI-Codigo"] == "PERFILADO_EQUIPO_ARCHIVADO"


@pytest.mark.asyncio
async def test_rows_without_an_owner_are_reported_and_assigned_only_on_request(nivel3):
    h = _como("boss@x.com")
    assert (await nivel3.c.get("/api/perfilado/sin-dueno", headers=h)).json() == [
        {"tabla": "pedidos", "existe": True, "columnas": True, "sin_dueno": 1, "total": 4}]
    r = await nivel3.c.post("/api/perfilado/sin-dueno/pedidos", json={"dueno": "nobody@x.com"}, headers=h)
    assert r.status_code == 422 and r.headers["X-WAI-Codigo"] == "PERFILADO_DUENO_SIN_ALTA"
    assert (await nivel3.c.post("/api/perfilado/sin-dueno/usuarios", json={"dueno": "ana@x.com"}, headers=h)).status_code == 404
    r = await nivel3.c.post("/api/perfilado/sin-dueno/pedidos", json={"dueno": "eva@x.com", "equipo": nivel3.sur["id"]}, headers=h)
    assert r.json()["sin_dueno"] == 0
    assert await _refs(nivel3, "eva@x.com") == ["E1", "X0"]
    assert await _refs(nivel3, "ana@x.com") == ["A1"]       # an owned row is never reassigned


@pytest.mark.asyncio
async def test_an_existing_table_gets_the_owner_columns_at_level_3(tmp_path):
    """An app raised from level 2: its table exists WITHOUT `dueno`/`equipo`; preparing adds them."""
    permisos.usar(dict(NIVEL_3))
    motor = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'subida.sqlite'}")
    async with motor.begin() as con:
        await con.exec_driver_sql("CREATE TABLE pedidos (id INTEGER PRIMARY KEY, ref TEXT)")
        await con.exec_driver_sql("INSERT INTO pedidos (ref) VALUES ('old')")
    db.usar_motor(motor)
    try:
        await db.preparar()
        columnas = {f["name"] for f in await db.leer("PRAGMA table_info(pedidos)")}
        assert {"dueno", "equipo"} <= columnas
        assert (await servicio.sin_dueno())[0]["sin_dueno"] == 1      # reported, never assigned
    finally:
        await motor.dispose()
        db.usar_motor(None)
        permisos.usar(None)


def test_below_level_3_every_allowed_action_is_todo():
    permisos.usar({**NIVEL_3, "nivel": 2})
    try:
        u = Usuario(email="ana@x.com", roles=["comercial"])
        assert u.alcance("CAP-1", "ver") == "todo" and u.alcance("CAP-1", "borrar") is None
        assert u.puede_sobre({"dueno": "other@x.com"}, "CAP-1", "ver")
    finally:
        permisos.usar(None)


def test_the_widest_scope_of_several_roles_wins_and_teams_need_level_3():
    permisos.usar(dict(NIVEL_3))
    try:
        assert Usuario(email="a@x.com", roles=["comercial", "jefe"]).alcance("CAP-1", "ver") == "equipo"
        assert Usuario(email="a@x.com", roles=["comercial", "direccion"]).alcance("CAP-1", "ver") == "todo"
        assert Usuario(email="a@x.com", roles=["comercial"]).alcance("CAP-1", "aprobar") is None
        assert Usuario(email="o@x.com", roles=[], es_dueno=True).alcance("CAP-1", "ver") == "todo"
        assert marcar_dueno({}, Usuario(email="a@x.com", equipo="norte")) == {"dueno": "a@x.com", "equipo": "norte"}
    finally:
        permisos.usar(None)


# ── level 4: organisations, portfolios, delegated administration, external accounts ──────────
NIVEL_4 = {
    **NIVEL_2, "nivel": 4,
    "ajustes": {"roles_personalizables": False, "alta": "correo", "nombre_organizacion": "Proveedores"},
    "roles": [
        {"id": "admin", "nombre": "Admin", "fijo": True, "administra": True, "alcance": "todo", "permisos": {"*": TODAS},
         "ambito": "interno", "administra_organizacion": False},
        {"id": "comprador", "nombre": "Buyer", "fijo": False, "administra": False, "alcance": "todo",
         "permisos": {"CAP-1": ["ver", "crear", "editar"]}, "alcances": {"CAP-1": {"ver": "todo", "crear": "todo", "editar": "todo"}},
         "ambito": "interno", "administra_organizacion": False},
        {"id": "proveedor", "nombre": "Supplier", "fijo": False, "administra": False, "alcance": "todo",
         "permisos": {"CAP-1": ["ver", "crear", "editar"]}, "alcances": {"CAP-1": {"ver": "todo", "crear": "todo", "editar": "todo"}},
         "ambito": "organizacion", "administra_organizacion": False},
        {"id": "proveedor_admin", "nombre": "Supplier admin", "fijo": False, "administra": False, "alcance": "todo",
         "permisos": {"CAP-1": ["ver"]}, "alcances": {"CAP-1": {"ver": "todo"}},
         "ambito": "organizacion", "administra_organizacion": True},
    ],
    "usuarios_de_prueba": [{"email": "pruebas@pruebas.wai", "roles": ["admin"]},
                           {"email": "proveedor@pruebas.wai", "roles": ["proveedor"], "organizacion": "pruebas-org"}],
    "datos_con_dueno": [],
    "datos_de_la_organizacion": [{"tabla": "ofertas", "para": ["CAP-1"]}],
    "equipos_de_prueba": [],
    "organizaciones_de_prueba": [{"id": "pruebas-org", "nombre": "Test org"}, {"id": "pruebas-otra-org", "nombre": "Other test org"}],
}
OFERTAS = Table("ofertas", MetaData(), Column("id", Integer, primary_key=True), Column("ref", String),
                Column("dueno", String), Column("equipo", String), Column("organizacion", String))


def _externa(email: str, app: str = "pedidos") -> dict:
    return {**_como(email), "X-Prueba-Externa": "1", "X-Prueba-App": app}


@pytest_asyncio.fixture
async def nivel4(tmp_path, monkeypatch):
    """A tiny app at level 4: a table «of the organisation» and the routes an app writes over it."""
    for v in ("APP_OWNER_EMAIL", "WAI_MODO_PRUEBAS", "WAI_DATOS_DE_EJEMPLO"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("APP_NAME", "pedidos")
    permisos.usar(dict(NIVEL_4))
    motor = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'nivel4.sqlite'}")
    async with motor.begin() as con:
        await con.run_sync(OFERTAS.metadata.create_all)
    db.usar_motor(motor)

    async def identidad(x_prueba_email: str = Header(""), x_prueba_externa: str = Header(""), x_prueba_app: str = Header("")):
        return SimpleNamespace(email=x_prueba_email, is_superuser=False, externa=x_prueba_externa == "1",
                               app=x_prueba_app or None)

    negocio = APIRouter(prefix="/api/ofertas")

    @negocio.get("")
    async def listar(usuario: Usuario = Depends(requiere("CAP-1", "ver"))):
        stmt = usuario.filtrar(select(OFERTAS.c.ref), "CAP-1", "ver", dueno_col=OFERTAS.c.dueno,
                               equipo_col=OFERTAS.c.equipo, org_col=OFERTAS.c.organizacion).order_by(OFERTAS.c.ref)
        async with motor.connect() as con:
            return [r[0] for r in (await con.execute(stmt)).all()]

    @negocio.get("/sin-muro")
    async def sin_muro(usuario: Usuario = Depends(requiere("CAP-1", "ver"))):
        # An app query that FORGOT org_col: a person of an organisation gets nothing (fails closed).
        stmt = usuario.filtrar(select(OFERTAS.c.ref), "CAP-1", "ver", dueno_col=OFERTAS.c.dueno)
        async with motor.connect() as con:
            return [r[0] for r in (await con.execute(stmt)).all()]

    @negocio.get("/{ref}")
    async def detalle(ref: str, usuario: Usuario = Depends(requiere("CAP-1", "ver"))):
        async with motor.connect() as con:
            fila = (await con.execute(select(OFERTAS).where(OFERTAS.c.ref == ref))).mappings().first()
        exigir_sobre(usuario, dict(fila), "CAP-1", "ver")
        return {"ref": fila["ref"]}

    @negocio.post("")
    async def crear(body: dict, usuario: Usuario = Depends(requiere("CAP-1", "crear"))):
        fila = marcar_dueno({"ref": body["ref"], "organizacion": body.get("organizacion")}, usuario)
        async with motor.begin() as con:
            await con.execute(insert(OFERTAS).values(**fila))
        return fila

    from app.wai.perfilado.rutas import router
    app = FastAPI()
    app.include_router(router)
    app.include_router(negocio)
    app.dependency_overrides[require_project_access] = identidad
    await servicio.dar_de_alta("boss@x.com", ["admin"], nombre="Boss", origen="correo", por="t")
    acme = await servicio.crear_organizacion("Acme", por="t")
    globex = await servicio.crear_organizacion("Globex", por="t")
    for email, rol, org in (("compra@x.com", "comprador", None), ("cartera@x.com", "comprador", None),
                            ("ana@acme.com", "proveedor", acme["id"]), ("jefe@acme.com", "proveedor_admin", acme["id"]),
                            ("eva@globex.com", "proveedor", globex["id"])):
        await servicio.dar_de_alta(email, [rol], nombre=None, origen="correo", por="t", organizacion=org)
    cartera = await servicio.usuario_por_correo("cartera@x.com")
    await servicio.cambiar(cartera["id"], cartera=[acme["id"]])
    # An inconsistent record (an organisation role and no organisation): it must see NOTHING.
    await db.asegurar_usuario("nada@x.com", ["proveedor"], origen="correo", reactivar=True)
    async with motor.begin() as con:
        await con.execute(insert(OFERTAS), [{"ref": "A1", "organizacion": acme["id"]},
                                            {"ref": "G1", "organizacion": globex["id"]},
                                            {"ref": "X0", "organizacion": None}])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield SimpleNamespace(c=c, acme=acme, globex=globex, motor=motor, monkeypatch=monkeypatch)
    await motor.dispose()
    db.usar_motor(None)
    permisos.usar(None)


async def _ofertas(e, cabeceras):
    r = await e.c.get("/api/ofertas", headers=cabeceras)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.asyncio
async def test_a_person_of_an_organisation_sees_only_their_organisation(nivel4):
    assert await _ofertas(nivel4, _como("ana@acme.com")) == ["A1"]
    assert await _ofertas(nivel4, _como("eva@globex.com")) == ["G1"]
    assert await _ofertas(nivel4, _como("nada@x.com")) == []                  # no organisation: nothing
    assert (await nivel4.c.get("/api/ofertas/sin-muro", headers=_como("ana@acme.com"))).json() == []


@pytest.mark.asyncio
async def test_internal_people_see_every_organisation_or_their_portfolio(nivel4):
    assert await _ofertas(nivel4, _como("compra@x.com")) == ["A1", "G1", "X0"]   # rows without one too
    assert await _ofertas(nivel4, _como("cartera@x.com")) == ["A1"]
    r = await nivel4.c.get("/api/ofertas/G1", headers=_como("cartera@x.com"))
    assert r.status_code == 403 and r.headers["X-WAI-Codigo"] == "PERFILADO_OTRA_ORGANIZACION"


@pytest.mark.asyncio
async def test_a_record_of_another_organisation_is_403(nivel4):
    assert (await nivel4.c.get("/api/ofertas/A1", headers=_como("ana@acme.com"))).status_code == 200
    for ref in ("G1", "X0"):
        r = await nivel4.c.get(f"/api/ofertas/{ref}", headers=_como("ana@acme.com"))
        assert r.status_code == 403 and r.headers["X-WAI-Codigo"] == "PERFILADO_OTRA_ORGANIZACION"


@pytest.mark.asyncio
async def test_creating_stamps_the_organisation_of_the_person_whatever_was_sent(nivel4):
    r = await nivel4.c.post("/api/ofertas", json={"ref": "A2", "organizacion": nivel4.globex["id"]}, headers=_como("ana@acme.com"))
    assert r.status_code == 200 and r.json()["organizacion"] == nivel4.acme["id"]
    r = await nivel4.c.post("/api/ofertas", json={"ref": "G2", "organizacion": nivel4.globex["id"]}, headers=_como("cartera@x.com"))
    assert r.status_code == 403 and r.headers["X-WAI-Codigo"] == "PERFILADO_OTRA_ORGANIZACION"
    r = await nivel4.c.post("/api/ofertas", json={"ref": "Z1"}, headers=_como("cartera@x.com"))
    assert r.status_code == 422 and r.headers["X-WAI-Codigo"] == "PERFILADO_FALTA_ORGANIZACION"
    r = await nivel4.c.post("/api/ofertas", json={"ref": "G3", "organizacion": nivel4.globex["id"]}, headers=_como("compra@x.com"))
    assert r.status_code == 200 and r.json()["organizacion"] == nivel4.globex["id"]


@pytest.mark.asyncio
async def test_an_organisations_administrator_manages_only_their_organisation(nivel4):
    h = _como("jefe@acme.com")
    gente = (await nivel4.c.get("/api/perfilado/usuarios?organizacion=" + nivel4.globex["id"], headers=h)).json()
    assert sorted(u["email"] for u in gente["items"]) == ["ana@acme.com", "jefe@acme.com"]   # forced to their own
    assert [r["id"] for r in (await nivel4.c.get("/api/perfilado/roles", headers=h)).json()["roles"]] == ["proveedor", "proveedor_admin"]
    r = await nivel4.c.post("/api/perfilado/usuarios", json={"email": "new@acme.com", "roles": ["proveedor"],
                                                             "organizacion": nivel4.globex["id"]}, headers=h)
    assert r.status_code == 201 and r.json()["organizacion"] == nivel4.acme["id"]
    r = await nivel4.c.post("/api/perfilado/usuarios", json={"email": "x@acme.com", "roles": ["comprador"]}, headers=h)
    assert r.status_code == 403 and r.headers["X-WAI-Codigo"] == "PERFILADO_SOLO_ROLES_DE_ORGANIZACION"
    r = await nivel4.c.post("/api/perfilado/usuarios", json={"email": "x@acme.com", "roles": ["admin"]}, headers=h)
    assert r.status_code == 403
    for email in ("eva@globex.com", "boss@x.com"):          # another organisation, an internal person
        u = await servicio.usuario_por_correo(email)
        r = await nivel4.c.patch(f"/api/perfilado/usuarios/{u['id']}", json={"activo": False}, headers=h)
        assert r.status_code == 403 and r.headers["X-WAI-Codigo"] == "PERFILADO_OTRA_ORGANIZACION"
        assert (await nivel4.c.delete(f"/api/perfilado/usuarios/{u['id']}", headers=h)).status_code == 403
    r = await nivel4.c.post("/api/perfilado/usuarios", json={"email": "eva@globex.com", "roles": ["proveedor"]}, headers=h)
    assert r.status_code == 409 and r.headers["X-WAI-Codigo"] == "PERFILADO_CORREO_NO_DISPONIBLE"
    ana = await servicio.usuario_por_correo("ana@acme.com")
    r = await nivel4.c.patch(f"/api/perfilado/usuarios/{ana['id']}", json={"roles": ["comprador"]}, headers=h)
    assert r.status_code == 403 and r.headers["X-WAI-Codigo"] == "PERFILADO_SOLO_ROLES_DE_ORGANIZACION"
    r = await nivel4.c.patch(f"/api/perfilado/usuarios/{ana['id']}", json={"organizacion": None}, headers=h)
    assert r.status_code == 403
    r = await nivel4.c.patch(f"/api/perfilado/usuarios/{ana['id']}", json={"roles": ["proveedor_admin"]}, headers=h)
    assert r.status_code == 200 and r.json()["roles"] == ["proveedor_admin"]
    assert (await nivel4.c.post("/api/perfilado/organizaciones", json={"nombre": "Mine"}, headers=h)).status_code == 403
    assert (await nivel4.c.get("/api/perfilado/usuarios", headers=_como("eva@globex.com"))).status_code == 403
    yo = (await nivel4.c.get("/api/perfilado/yo", headers=h)).json()
    assert yo["administra_organizacion"] and not yo["administra"] and yo["nombre_de_organizacion"] == "Acme"
    assert yo["nombre_de_las_organizaciones"] == "Proveedores"


@pytest.mark.asyncio
async def test_internal_administrators_manage_organisations_people_and_portfolios(nivel4):
    h = _como("boss@x.com")
    r = await nivel4.c.post("/api/perfilado/organizaciones", json={"nombre": "Initech"}, headers=h)
    assert r.status_code == 201 and r.json()["id"] == "initech"
    r = await nivel4.c.delete(f"/api/perfilado/organizaciones/{nivel4.acme['id']}", headers=h)
    assert r.status_code == 409 and r.headers["X-WAI-Codigo"] == "PERFILADO_ORGANIZACION_CON_MIEMBROS"
    compra = await servicio.usuario_por_correo("compra@x.com")
    r = await nivel4.c.patch(f"/api/perfilado/usuarios/{compra['id']}", json={"cartera": [nivel4.globex["id"]]}, headers=h)
    assert r.status_code == 200 and r.json()["cartera"] == [nivel4.globex["id"]]
    assert await _ofertas(nivel4, _como("compra@x.com")) == ["G1"]
    r = await nivel4.c.patch(f"/api/perfilado/usuarios/{compra['id']}", json={"cartera": []}, headers=h)
    assert await _ofertas(nivel4, _como("compra@x.com")) == []                # an empty portfolio: none
    await nivel4.c.patch(f"/api/perfilado/usuarios/{compra['id']}", json={"cartera": None}, headers=h)
    assert await _ofertas(nivel4, _como("compra@x.com")) == ["A1", "G1", "X0"]
    ana = await servicio.usuario_por_correo("ana@acme.com")
    r = await nivel4.c.patch(f"/api/perfilado/usuarios/{ana['id']}", json={"cartera": [nivel4.acme["id"]]}, headers=h)
    assert r.status_code == 422 and r.headers["X-WAI-Codigo"] == "PERFILADO_CARTERA_SOLO_INTERNOS"
    r = await nivel4.c.patch(f"/api/perfilado/usuarios/{compra['id']}", json={"organizacion": nivel4.acme["id"]}, headers=h)
    assert r.status_code == 422 and r.headers["X-WAI-Codigo"] == "PERFILADO_ROL_DE_OTRO_AMBITO"
    r = await nivel4.c.patch(f"/api/perfilado/usuarios/{compra['id']}",
                             json={"organizacion": nivel4.acme["id"], "roles": ["proveedor"]}, headers=h)
    assert r.status_code == 200 and r.json()["organizacion"] == nivel4.acme["id"]
    assert await _ofertas(nivel4, _como("compra@x.com")) == ["A1"]
    nombres = [o["nombre"] for o in (await nivel4.c.get("/api/perfilado/organizaciones", headers=_como("ana@acme.com"))).json()]
    assert nombres == ["Acme"]                                                # a supplier only learns its own name


@pytest.mark.asyncio
async def test_an_external_session_only_enters_this_app_and_only_as_an_organisation(nivel4):
    assert await _ofertas(nivel4, _externa("ana@acme.com")) == ["A1"]
    r = await nivel4.c.get("/api/ofertas", headers=_externa("ana@acme.com", app="otra"))
    assert r.status_code == 403 and r.headers["X-WAI-Codigo"] == "PERFILADO_SESION_DE_OTRA_APLICACION"
    r = await nivel4.c.get("/api/perfilado/yo", headers=_externa("boss@x.com"))            # an internal record
    assert r.status_code == 403 and r.headers["X-WAI-Codigo"] == "PERFILADO_EXTERNA_SIN_ORGANIZACION"
    nivel4.monkeypatch.setenv("APP_OWNER_EMAIL", "boss@x.com")
    r = await nivel4.c.get("/api/perfilado/yo", headers=_externa("boss@x.com"))            # never the owner
    assert r.status_code == 403
    assert (await nivel4.c.get("/api/perfilado/yo", headers=_como("boss@x.com"))).json()["es_dueno"] is True


@pytest.mark.asyncio
async def test_below_level_4_an_external_session_is_refused(entorno):
    await servicio.dar_de_alta("sales@x.com", ["comercial"], nombre=None, origen="correo", por="t")
    with pytest.raises(servicio.ErrorDePerfilado) as e:
        await servicio.resolver("sales@x.com", externa=True)
    assert e.value.codigo == "PERFILADO_CUENTA_EXTERNA_NO_ADMITIDA"
    assert (await servicio.resolver("sales@x.com")).roles == ["comercial"]


@pytest.mark.asyncio
async def test_with_external_accounts_the_platform_invites_with_the_callers_token(nivel4):
    permisos.usar({**NIVEL_4, "ajustes": {**NIVEL_4["ajustes"], "alta": "externas"}})
    invitados = []

    async def invitar(email, *, nombre, organizacion, autorizacion):
        invitados.append((email, organizacion, autorizacion))
        return {"email": email, "activo": True}

    async def retirar(email, *, autorizacion):
        invitados.append((email, "retirada", autorizacion))
        return {"email": email, "activo": False}
    nivel4.monkeypatch.setattr(externas, "invitar", invitar)
    nivel4.monkeypatch.setattr(externas, "retirar", retirar)
    h = {**_como("jefe@acme.com"), "Authorization": "Bearer jefe"}
    r = await nivel4.c.post("/api/perfilado/usuarios", json={"email": "luis@acme.com", "roles": ["proveedor"]}, headers=h)
    assert r.status_code == 201 and r.json()["origen"] == "externa" and r.json()["organizacion"] == nivel4.acme["id"]
    assert invitados == [("luis@acme.com", nivel4.acme["id"], "Bearer jefe")]
    luis = await servicio.usuario_por_correo("luis@acme.com")
    await nivel4.c.patch(f"/api/perfilado/usuarios/{luis['id']}", json={"activo": False}, headers=h)
    assert invitados[-1] == ("luis@acme.com", "retirada", "Bearer jefe")      # the platform account closes too
    # An internal person is not an external account: no invitation.
    r = await nivel4.c.post("/api/perfilado/usuarios", json={"email": "int@x.com", "roles": ["comprador"]}, headers=_como("boss@x.com"))
    assert r.status_code == 201 and len(invitados) == 2

    async def caida(*a, **k):
        raise externas.PlataformaNoResponde("down")
    nivel4.monkeypatch.setattr(externas, "invitar", caida)
    r = await nivel4.c.post("/api/perfilado/usuarios", json={"email": "sin@acme.com", "roles": ["proveedor"]}, headers=h)
    assert r.status_code == 503 and await servicio.usuario_por_correo("sin@acme.com") is None


@pytest.mark.asyncio
async def test_the_sign_in_with_a_code_is_relayed_with_this_apps_name(nivel4):
    r = await nivel4.c.post("/api/perfilado/externas/codigo", json={"email": "ana@acme.com"})
    assert r.status_code == 404 and r.headers["X-WAI-Codigo"] == "PERFILADO_SIN_CUENTAS_EXTERNAS"
    permisos.usar({**NIVEL_4, "ajustes": {**NIVEL_4["ajustes"], "alta": "externas"}})
    nivel4.monkeypatch.setenv("WAI_BACKEND_URL", "http://platform")
    llamadas = []

    async def llamar(metodo, ruta, *, autorizacion=None, cuerpo=None):
        llamadas.append((metodo, ruta, cuerpo))
        if ruta.endswith("/entrar") and cuerpo["codigo"] != "123456":
            raise externas.Rechazado(401)
        return {"access_token": "tok", "token_type": "bearer", "expires_in": 60} if ruta.endswith("/entrar") else {}
    nivel4.monkeypatch.setattr(externas, "_llamar", llamar)
    assert (await nivel4.c.post("/api/perfilado/externas/codigo", json={"email": "Ana@Acme.com"})).status_code == 202
    assert llamadas[-1] == ("POST", "/backend/auth/externos/codigo", {"email": "ana@acme.com", "app": "pedidos"})
    r = await nivel4.c.post("/api/perfilado/externas/entrar", json={"email": "ana@acme.com", "codigo": "000000"})
    assert r.status_code == 401 and r.headers["X-WAI-Codigo"] == "PERFILADO_CODIGO_NO_VALIDO"
    r = await nivel4.c.post("/api/perfilado/externas/entrar", json={"email": "ana@acme.com", "codigo": "123456"})
    assert r.status_code == 200 and r.json()["access_token"] == "tok"
    assert llamadas[-1][2]["app"] == "pedidos"
    await externas.invitar("x@acme.com", nombre=None, organizacion="acme", autorizacion="Bearer z")
    assert llamadas[-1][1] == "/backend/apps/pedidos/externos/invitar"


@pytest.mark.asyncio
async def test_raising_to_level_4_adds_the_column_and_reports_rows_without_an_organisation(tmp_path):
    """An app raised from level 3: its table exists WITHOUT `organizacion`; preparing adds it, never assigns."""
    permisos.usar(dict(NIVEL_4))
    motor = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'subida4.sqlite'}")
    async with motor.begin() as con:
        await con.exec_driver_sql("CREATE TABLE ofertas (id INTEGER PRIMARY KEY, ref TEXT)")
        await con.exec_driver_sql("INSERT INTO ofertas (ref) VALUES ('old')")
    db.usar_motor(motor)
    try:
        await db.preparar()
        assert "organizacion" in {f["name"] for f in await db.leer("PRAGMA table_info(ofertas)")}
        assert (await servicio.sin_organizacion())[0]["sin_organizacion"] == 1     # reported, never assigned
        org = await servicio.crear_organizacion("Acme", por="t")
        assert (await servicio.asignar_sin_organizacion("ofertas", org["id"]))["sin_organizacion"] == 0
        assert {f["name"] for f in await db.leer("PRAGMA table_info(perfilado_usuarios)")} >= {"organizacion", "con_cartera"}
    finally:
        await motor.dispose()
        db.usar_motor(None)
        permisos.usar(None)


@pytest.mark.asyncio
async def test_test_mode_puts_the_test_users_in_their_organisation(tmp_path, monkeypatch):
    monkeypatch.setenv("WAI_MODO_PRUEBAS", "1")
    permisos.usar(dict(NIVEL_4))
    motor = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pruebas4.sqlite'}")
    db.usar_motor(motor)
    try:
        await db.preparar()
        u = await servicio.resolver("proveedor@pruebas.wai")
        assert u.organizacion == "pruebas-org" and u.es_de_organizacion
        assert {o["id"] for o in await servicio.listar_organizaciones()} == {"pruebas-org", "pruebas-otra-org"}
    finally:
        await motor.dispose()
        db.usar_motor(None)
        permisos.usar(None)


# ── adopted from the app: its own owner column and its old user table (patterns/perfilado.md §9) ──
ADOPTADO = {
    **NIVEL_3,
    "datos_con_dueno": [{"tabla": "viajes", "para": ["CAP-1"]}],
    "columnas": {"viajes": {"dueno": "owner_email"}},
    "adopcion": {"tabla": "user_accounts", "email": "email", "nombre": "name", "rol": "role",
                 "roles": {"admin": "admin", "traveler": "comercial"}},
}


@pytest.mark.asyncio
async def test_an_adopted_owner_column_keeps_its_name(tmp_path):
    """The app's `owner_email` IS the owner: the module adds `equipo` only, reports and assigns rows by
    it, and the record helpers read it for an ORM object of that table."""
    from sqlalchemy.orm import declarative_base
    permisos.usar(dict(ADOPTADO))
    motor = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'adoptado.sqlite'}")
    async with motor.begin() as con:
        await con.exec_driver_sql("CREATE TABLE viajes (id INTEGER PRIMARY KEY, titulo TEXT, owner_email TEXT)")
        await con.exec_driver_sql("INSERT INTO viajes (titulo, owner_email) VALUES ('Roma', 'ana@x.com'), ('Oslo', NULL)")
    db.usar_motor(motor)
    try:
        await db.preparar()
        columnas = {f["name"] for f in await db.leer("PRAGMA table_info(viajes)")}
        assert "equipo" in columnas and "dueno" not in columnas
        assert (await servicio.sin_dueno()) == [{"tabla": "viajes", "existe": True, "columnas": True, "sin_dueno": 1, "total": 2}]
        Base = declarative_base()

        class Viaje(Base):
            __tablename__ = "viajes"
            id = Column(Integer, primary_key=True)
            owner_email = Column(String)
            equipo = Column(String)

        ana = Usuario(email="ana@x.com", roles=["comercial"], equipo="norte")
        assert ana.puede_sobre(Viaje(owner_email="ana@x.com"), "CAP-1", "ver")
        assert not ana.puede_sobre(Viaje(owner_email="luis@x.com"), "CAP-1", "ver")
        nuevo = marcar_dueno(Viaje(), ana)
        assert nuevo.owner_email == "ana@x.com" and nuevo.equipo == "norte"
        assert marcar_dueno({}, ana, dueno="owner_email") == {"owner_email": "ana@x.com", "equipo": "norte"}
    finally:
        await motor.dispose()
        db.usar_motor(None)
        permisos.usar(None)


@pytest.mark.asyncio
async def test_the_old_user_table_is_imported_once_and_never_touched(tmp_path, monkeypatch):
    monkeypatch.delenv("APP_OWNER_EMAIL", raising=False)
    permisos.usar(dict(ADOPTADO))
    motor = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'importa.sqlite'}")
    async with motor.begin() as con:
        await con.exec_driver_sql("CREATE TABLE user_accounts (email TEXT PRIMARY KEY, name TEXT, role TEXT, preferences TEXT)")
        await con.exec_driver_sql("INSERT INTO user_accounts VALUES ('Admin@wai.com', 'Admin', 'admin', '{}'), "
                                  "('eva@x.com', 'Eva', 'traveler', '{\"seat\": \"window\"}'), ('raro@x.com', 'R', 'guest', NULL)")
    db.usar_motor(motor)
    try:
        await db.preparar()
        filas = {f["email"]: f for f in await db.leer("SELECT email, nombre, roles, origen FROM perfilado_usuarios")}
        assert filas["admin@wai.com"]["roles"] == '["admin"]' and filas["admin@wai.com"]["origen"] == "adopcion"
        assert filas["eva@x.com"]["roles"] == '["comercial"]' and filas["eva@x.com"]["nombre"] == "Eva"
        assert "raro@x.com" not in filas                      # no mapping: never a role by guess
        assert len(await db.leer("SELECT * FROM user_accounts")) == 3     # the old table stays as it was
        # Once: a person added to the old table later is NOT imported (one user system from now on).
        await servicio.cambiar((await servicio.usuario_por_correo("eva@x.com"))["id"], roles=["admin"])
        async with motor.begin() as con:
            await con.exec_driver_sql("INSERT INTO user_accounts VALUES ('new@x.com', 'New', 'traveler', NULL)")
        db.usar_motor(motor)
        await db.preparar()
        assert await servicio.usuario_por_correo("new@x.com") is None
        assert (await servicio.usuario_por_correo("eva@x.com"))["roles"] == ["admin"]     # never overwritten
    finally:
        await motor.dispose()
        db.usar_motor(None)
        permisos.usar(None)


# ── custom roles (setting `roles_personalizables`, levels 2-4) ────────────────────────────────
def _con_roles_propios(base: dict) -> None:
    permisos.usar({**base, "ajustes": {**base.get("ajustes", {}), "roles_personalizables": True}})


async def _historial(rid: str) -> list:
    return [f["accion"] for f in await db.leer("SELECT accion FROM perfilado_roles_propios_historial WHERE rol = :r ORDER BY id",
                                                {"r": rid})]


@pytest.mark.asyncio
async def test_custom_roles_are_off_unless_the_setting_says_so(nivel3):
    h = _como("boss@x.com")
    for metodo, ruta in (("GET", "/api/perfilado/roles/propios/piezas"), ("POST", "/api/perfilado/roles/propios"),
                         ("DELETE", "/api/perfilado/roles/propios/propio_x")):
        r = await nivel3.c.request(metodo, ruta, headers=h, json={"nombre": "X", "permisos": {"CAP-1": ["ver"]}} if metodo == "POST" else None)
        assert r.status_code == 404 and r.headers["X-WAI-Codigo"] == "PERFILADO_ROLES_PROPIOS_NO_ACTIVADOS", (ruta, r.text)
    # A role left in the database while the setting is off gives nothing (fails closed).
    await db.escribir([("INSERT INTO perfilado_roles_propios (id, nombre, permisos, alcances, creado_en) "
                        "VALUES ('propio_x', 'X', '{\"CAP-1\": [\"ver\"]}', '{}', 't')", {})])
    await db.asegurar_usuario("x@x.com", ["propio_x"], origen="correo", reactivar=True)
    assert (await nivel3.c.get("/api/pedidos", headers=_como("x@x.com"))).status_code == 403
    assert (await nivel3.c.get("/api/perfilado/roles", headers=h)).json()["personalizables"] is False


@pytest.mark.asyncio
async def test_a_custom_role_only_combines_what_the_definition_declares(nivel3):
    _con_roles_propios(NIVEL_3)
    h = _como("boss@x.com")
    piezas = (await nivel3.c.get("/api/perfilado/roles/propios/piezas", headers=h)).json()
    assert piezas == {"interno": {"CAP-1": {"ver": ["propio", "equipo", "todo"], "crear": ["propio"], "editar": ["propio", "equipo"]}}}
    for cuerpo, codigo in (
            ({"nombre": "A", "permisos": {"CAP-2": ["aprobar"]}}, "PERFILADO_ROL_PERMISO_NO_DECLARADO"),   # nobody has it
            ({"nombre": "A", "permisos": {"CAP-1": ["borrar"]}}, "PERFILADO_ROL_PERMISO_NO_DECLARADO"),
            ({"nombre": "A", "permisos": {"CAP-9": ["ver"]}}, "PERFILADO_ROL_PERMISO_NO_DECLARADO"),     # invented
            ({"nombre": "A", "permisos": {"CAP-1": ["crear"]}, "alcances": {"CAP-1": {"crear": "todo"}}}, "PERFILADO_ROL_ALCANCE_NO_PERMITIDO"),
            ({"nombre": "A", "permisos": {}}, "PERFILADO_ROL_SIN_PERMISOS"),
            ({"nombre": "A", "ambito": "organizacion", "permisos": {"CAP-1": ["ver"]}}, "PERFILADO_ROL_AMBITO_NO_VALIDO"),
            ({"nombre": "Sales", "permisos": {"CAP-1": ["ver"]}}, "PERFILADO_ROL_NOMBRE_REPETIDO")):
        r = await nivel3.c.post("/api/perfilado/roles/propios", json=cuerpo, headers=h)
        assert r.status_code in (400, 409, 422) and r.headers["X-WAI-Codigo"] == codigo, (cuerpo, r.text)
    assert (await nivel3.c.post("/api/perfilado/roles/propios", json={"nombre": "B", "permisos": {"CAP-1": ["ver"]}},
                                headers=_como("ana@x.com"))).status_code == 403
    r = await nivel3.c.post("/api/perfilado/roles/propios", json={"nombre": "Lector", "quien": "Solo mira",
                                                                  "permisos": {"CAP-1": ["ver"]}}, headers=h)
    assert r.status_code == 201, r.text
    rol = r.json()
    assert rol["id"] == "propio_lector" and rol["alcances"] == {"CAP-1": {"ver": "todo"}}   # the widest allowed
    assert rol["administra"] is False and rol["origen"] == "aplicacion" and rol["creado_por"] == "boss@x.com"


@pytest.mark.asyncio
async def test_custom_roles_go_through_the_same_checks_and_changes_apply_at_once(nivel3):
    _con_roles_propios(NIVEL_3)
    h = _como("boss@x.com")
    rol = (await nivel3.c.post("/api/perfilado/roles/propios", json={
        "nombre": "Supervisor", "permisos": {"CAP-1": ["ver"]}, "alcances": {"CAP-1": {"ver": "equipo"}}}, headers=h)).json()
    luis = await servicio.usuario_por_correo("luis@x.com")
    assert (await nivel3.c.patch(f"/api/perfilado/usuarios/{luis['id']}", json={"roles": [rol["id"]]}, headers=h)).status_code == 200
    assert await _refs(nivel3, "luis@x.com") == ["A1", "L1"]                  # their team's (Norte)
    assert (await nivel3.c.post("/api/pedidos", json={"ref": "Z"}, headers=_como("luis@x.com"))).status_code == 403
    m = (await nivel3.c.get("/api/perfilado/roles", headers=h)).json()
    propio = next(r for r in m["roles"] if r["id"] == rol["id"])
    assert m["personalizables"] is True and propio["personas"] == [{"email": "luis@x.com", "nombre": None}]
    r = await nivel3.c.patch(f"/api/perfilado/roles/propios/{rol['id']}", headers=h, json={
        "nombre": "Supervisor", "permisos": {"CAP-1": ["ver"]}, "alcances": {"CAP-1": {"ver": "propio"}}})
    assert r.status_code == 200 and r.json()["cambiado_por"] == "boss@x.com"
    assert await _refs(nivel3, "luis@x.com") == ["L1"]                        # the change reaches who holds it
    assert await _historial(rol["id"]) == ["creado", "cambiado"]
    r = await nivel3.c.patch("/api/perfilado/roles/propios/comercial", headers=h, json={"nombre": "X", "permisos": {"CAP-1": ["ver"]}})
    assert r.status_code == 409 and r.headers["X-WAI-Codigo"] == "PERFILADO_ROL_DE_LA_DEFINICION"


@pytest.mark.asyncio
async def test_a_role_in_use_is_deleted_only_moving_its_people(nivel3):
    _con_roles_propios(NIVEL_3)
    h = _como("boss@x.com")
    rol = (await nivel3.c.post("/api/perfilado/roles/propios", json={"nombre": "Temporal", "permisos": {"CAP-1": ["ver"]}},
                               headers=h)).json()
    luis = await servicio.usuario_por_correo("luis@x.com")
    await nivel3.c.patch(f"/api/perfilado/usuarios/{luis['id']}", json={"roles": [rol["id"], "comercial"]}, headers=h)
    r = await nivel3.c.delete(f"/api/perfilado/roles/propios/{rol['id']}", headers=h)
    assert r.status_code == 409 and r.headers["X-WAI-Codigo"] == "PERFILADO_ROL_EN_USO" and "luis@x.com" in r.json()["detail"]
    r = await nivel3.c.delete(f"/api/perfilado/roles/propios/{rol['id']}?pasar_a=comercial", headers=h)
    assert r.status_code == 200 and r.json()["pasados"] == 1
    assert (await servicio.usuario_por_correo("luis@x.com"))["roles"] == ["comercial"]
    assert rol["id"] not in [x["id"] for x in (await nivel3.c.get("/api/perfilado/roles", headers=h)).json()["roles"]]
    assert await _historial(rol["id"]) == ["creado", "borrado"]
    sin_uso = (await nivel3.c.post("/api/perfilado/roles/propios", json={"nombre": "Nadie", "permisos": {"CAP-1": ["ver"]}},
                                   headers=h)).json()
    assert (await nivel3.c.delete(f"/api/perfilado/roles/propios/{sin_uso['id']}", headers=h)).status_code == 200


@pytest.mark.asyncio
async def test_an_organisation_custom_role_never_crosses_the_wall(nivel4):
    _con_roles_propios(NIVEL_4)
    h = _como("boss@x.com")
    piezas = (await nivel4.c.get("/api/perfilado/roles/propios/piezas", headers=h)).json()
    assert set(piezas) == {"interno", "organizacion"}
    rol = (await nivel4.c.post("/api/perfilado/roles/propios", json={
        "nombre": "Proveedor lector", "ambito": "organizacion", "permisos": {"CAP-1": ["ver"]}}, headers=h)).json()
    assert rol["ambito"] == "organizacion"
    # An organisation's administrator ASSIGNS it to their people, but never creates one.
    ana = await servicio.usuario_por_correo("ana@acme.com")
    r = await nivel4.c.patch(f"/api/perfilado/usuarios/{ana['id']}", json={"roles": [rol["id"]]}, headers=_como("jefe@acme.com"))
    assert r.status_code == 200, r.text
    assert (await nivel4.c.post("/api/perfilado/roles/propios", json={"nombre": "Otro", "ambito": "organizacion",
                                                                      "permisos": {"CAP-1": ["ver"]}},
                                headers=_como("jefe@acme.com"))).status_code == 403
    assert await _ofertas(nivel4, _como("ana@acme.com")) == ["A1"]           # only their organisation's
    r = await nivel4.c.get("/api/ofertas/G1", headers=_como("ana@acme.com"))
    assert r.status_code == 403 and r.headers["X-WAI-Codigo"] == "PERFILADO_OTRA_ORGANIZACION"
    assert (await nivel4.c.post("/api/ofertas", json={"ref": "A9"}, headers=_como("ana@acme.com"))).status_code == 403
    # Never given to an internal person (the side of the wall goes with the role).
    compra = await servicio.usuario_por_correo("compra@x.com")
    r = await nivel4.c.patch(f"/api/perfilado/usuarios/{compra['id']}", json={"roles": [rol["id"]]}, headers=h)
    assert r.status_code == 422 and r.headers["X-WAI-Codigo"] == "PERFILADO_ROL_DE_OTRO_AMBITO"
    # Moving its people must keep them on their side.
    r = await nivel4.c.delete(f"/api/perfilado/roles/propios/{rol['id']}?pasar_a=comprador", headers=h)
    assert r.status_code == 422 and r.headers["X-WAI-Codigo"] == "PERFILADO_ROL_DE_OTRO_AMBITO"


# ── «Borrar» (1.4.1): deleting a person, never what they created ─────────────────────────────
async def _borrados(email: str) -> list:
    return await db.leer("SELECT accion, por FROM perfilado_usuarios_historial WHERE email = :e", {"e": email})


@pytest.mark.asyncio
async def test_an_administrator_deletes_a_user_and_the_email_comes_back_fresh(entorno):
    await _admin(entorno)
    await servicio.dar_de_alta("sales@x.com", ["comercial"], nombre="Sales", origen="correo", por="t")
    fila = await servicio.usuario_por_correo("sales@x.com")
    r = await entorno.c.delete(f"/api/perfilado/usuarios/{fila['id']}", headers=_como("boss@x.com"))
    assert r.status_code == 200, r.text
    assert r.json() == {"borrado": True, "id": fila["id"], "email": "sales@x.com", "nombre": "Sales",
                        "cuenta_externa_abierta": False}
    assert await servicio.usuario_por_correo("sales@x.com") is None
    r = await entorno.c.get("/api/pedidos", headers=_como("sales@x.com"))
    assert r.status_code == 403 and r.headers["X-WAI-Codigo"] == "PERFILADO_SIN_ALTA"
    assert await _borrados("sales@x.com") == [{"accion": "borrado", "por": "boss@x.com"}]
    r = await entorno.c.delete(f"/api/perfilado/usuarios/{fila['id']}", headers=_como("boss@x.com"))
    assert r.status_code == 404
    # Adding the same email later creates a NEW record with the roles given now.
    nuevo = await servicio.dar_de_alta("sales@x.com", ["jefe"], nombre=None, origen="correo", por="t")
    assert nuevo["id"] != fila["id"] and nuevo["roles"] == ["jefe"] and nuevo["activo"]
    # Somebody who is not an administrator cannot delete anyone.
    r = await entorno.c.delete(f"/api/perfilado/usuarios/{nuevo['id']}", headers=_como("sales@x.com"))
    assert r.status_code == 403 and r.headers["X-WAI-Codigo"] == "PERFILADO_SOLO_ADMINISTRADORES"


@pytest.mark.asyncio
async def test_the_owner_and_yourself_cannot_be_deleted(entorno):
    entorno.monkeypatch.setenv("APP_OWNER_EMAIL", "owner@x.com")
    db.usar_motor(db.motor())          # prepare again: the owner gets its record
    await _admin(entorno)
    dueno = await servicio.usuario_por_correo("owner@x.com")
    jefe = await servicio.usuario_por_correo("boss@x.com")
    r = await entorno.c.delete(f"/api/perfilado/usuarios/{dueno['id']}", headers=_como("boss@x.com"))
    assert r.status_code == 409 and r.headers["X-WAI-Codigo"] == "PERFILADO_DUENO_NO_SE_BORRA"
    assert "owner" in r.json()["detail"]
    r = await entorno.c.delete(f"/api/perfilado/usuarios/{dueno['id']}", headers=_como("owner@x.com"))
    assert r.status_code == 409 and r.headers["X-WAI-Codigo"] == "PERFILADO_DUENO_NO_SE_BORRA"
    r = await entorno.c.delete(f"/api/perfilado/usuarios/{jefe['id']}", headers=_como("boss@x.com"))
    assert r.status_code == 409 and r.headers["X-WAI-Codigo"] == "PERFILADO_NO_PUEDES_BORRARTE"
    assert await servicio.usuario_por_correo("owner@x.com") and await servicio.usuario_por_correo("boss@x.com")
    # The owner deletes another administrator.
    r = await entorno.c.delete(f"/api/perfilado/usuarios/{jefe['id']}", headers=_como("owner@x.com"))
    assert r.status_code == 200 and await servicio.usuario_por_correo("boss@x.com") is None


@pytest.mark.asyncio
async def test_an_organisations_administrator_deletes_only_people_of_their_organisation(nivel4):
    h = _como("jefe@acme.com")
    for email in ("eva@globex.com", "boss@x.com", "compra@x.com"):     # another organisation, internal people
        u = await servicio.usuario_por_correo(email)
        r = await nivel4.c.delete(f"/api/perfilado/usuarios/{u['id']}", headers=h)
        assert r.status_code == 403 and r.headers["X-WAI-Codigo"] == "PERFILADO_OTRA_ORGANIZACION"
        assert await servicio.usuario_por_correo(email) is not None
    ana = await servicio.usuario_por_correo("ana@acme.com")
    assert (await nivel4.c.delete(f"/api/perfilado/usuarios/{ana['id']}", headers=h)).status_code == 200
    assert await servicio.usuario_por_correo("ana@acme.com") is None
    # The internal administrator deletes anyone (and a portfolio goes with the record).
    cartera = await servicio.usuario_por_correo("cartera@x.com")
    assert (await nivel4.c.delete(f"/api/perfilado/usuarios/{cartera['id']}", headers=_como("boss@x.com"))).status_code == 200
    assert await db.leer("SELECT * FROM perfilado_carteras WHERE usuario = :u", {"u": cartera["id"]}) == []


@pytest.mark.asyncio
async def test_deleting_an_external_account_also_closes_it_in_the_platform(nivel4):
    permisos.usar({**NIVEL_4, "ajustes": {**NIVEL_4["ajustes"], "alta": "externas"}})
    retiradas = []

    async def retirar(email, *, autorizacion):
        retiradas.append((email, autorizacion))
        return {"email": email, "activo": False}
    nivel4.monkeypatch.setattr(externas, "retirar", retirar)
    h = {**_como("jefe@acme.com"), "Authorization": "Bearer jefe"}
    ana = await servicio.usuario_por_correo("ana@acme.com")
    r = await nivel4.c.delete(f"/api/perfilado/usuarios/{ana['id']}", headers=h)
    assert r.status_code == 200 and r.json()["cuenta_externa_abierta"] is False
    assert retiradas == [("ana@acme.com", "Bearer jefe")]                  # the same call as «Desactivar»
    compra = await servicio.usuario_por_correo("compra@x.com")              # internal: no platform account
    assert (await nivel4.c.delete(f"/api/perfilado/usuarios/{compra['id']}", headers=_como("boss@x.com"))).status_code == 200
    assert len(retiradas) == 1

    async def caida(*a, **k):
        raise externas.PlataformaNoResponde("down")
    nivel4.monkeypatch.setattr(externas, "retirar", caida)
    eva = await servicio.usuario_por_correo("eva@globex.com")
    r = await nivel4.c.delete(f"/api/perfilado/usuarios/{eva['id']}", headers=_como("boss@x.com"))
    assert r.status_code == 200 and r.json()["cuenta_externa_abierta"] is True   # said, and still out of the app
    r = await nivel4.c.get("/api/ofertas", headers=_externa("eva@globex.com"))
    assert r.status_code == 403 and r.headers["X-WAI-Codigo"] == "PERFILADO_SIN_ALTA"


@pytest.mark.asyncio
async def test_the_rows_of_a_deleted_owner_stay_and_count_as_without_an_owner(nivel3):
    h = _como("boss@x.com")
    antes = (await nivel3.c.get("/api/perfilado/sin-dueno", headers=h)).json()[0]
    assert (antes["sin_dueno"], antes["total"]) == (1, 4)                   # X0 only
    luis = await servicio.usuario_por_correo("luis@x.com")
    assert (await nivel3.c.delete(f"/api/perfilado/usuarios/{luis['id']}", headers=h)).status_code == 200
    async with nivel3.motor.connect() as con:
        assert (await con.execute(select(PEDIDOS.c.dueno).where(PEDIDOS.c.ref == "L1"))).scalar() == "luis@x.com"
    despues = (await nivel3.c.get("/api/perfilado/sin-dueno", headers=h)).json()[0]
    assert (despues["sin_dueno"], despues["total"]) == (2, 4)               # X0 + L1, nothing deleted
    assert await _refs(nivel3, "boss@x.com") == ["A1", "E1", "L1", "X0"]
    # A deactivated owner is still an owner: their rows are not «without an owner».
    eva = await servicio.usuario_por_correo("eva@x.com")
    await nivel3.c.patch(f"/api/perfilado/usuarios/{eva['id']}", json={"activo": False}, headers=h)
    assert (await servicio.sin_dueno())[0]["sin_dueno"] == 2
    # Assigning gives BOTH (the never-owned and the deleted owner's) to someone, and only those.
    r = await nivel3.c.post("/api/perfilado/sin-dueno/pedidos", json={"dueno": "ana@x.com", "equipo": nivel3.norte["id"]}, headers=h)
    assert r.status_code == 200 and r.json()["sin_dueno"] == 0
    assert await _refs(nivel3, "ana@x.com") == ["A1", "L1", "X0"]
    async with nivel3.motor.connect() as con:
        assert (await con.execute(select(PEDIDOS.c.dueno).where(PEDIDOS.c.ref == "E1"))).scalar() == "eva@x.com"
