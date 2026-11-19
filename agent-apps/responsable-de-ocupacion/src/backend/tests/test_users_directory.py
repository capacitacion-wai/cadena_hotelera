"""v7.145.0 — the directory is COMPOSED BY HAND, and deciding is its own permission.

Four promises, each one a way the feature silently rots if it breaks:

1. **A new employee is born with exactly ONE user: its administrator.** The old
   behaviour mirrored the project (deploy snapshot + a live «Sincronizar» that
   DEACTIVATED whoever left it), so the directory — and with it who is admin, who
   gets notified and whom the agent recognises — changed under the operator's
   feet. A guard also checks the mirror cannot creep back in as code.
2. **Adding in bulk is idempotent.** It is the default way in now, so hitting
   «añadir» twice must merge, never duplicate: two rows for one email break role
   resolution and phone→email identity.
3. **Bulk never demotes.** Re-adding an existing admin with the batch role
   ``member`` must NOT strip their admin — a silent lockout in one click.
4. **Approving is separate from administering.** Admins always decide; beyond them
   only people an admin designated. And a channel-only contact is never an
   approver: decisions happen in the panel and they cannot log in.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _rows():
    from sqlalchemy import select
    from app.app_database import app_session
    from app.app_models.users import AppUser

    async def go():
        async with app_session() as s:
            return list((await s.execute(select(AppUser))).scalars().all())

    return _run(go())


# ── 1. Born with only its administrator ──────────────────────────────
def test_boot_seeds_only_the_administrator(monkeypatch) -> None:
    from app.users_directory import bootstrap_directory

    monkeypatch.setenv("AGENT_AUTONOMOUS_USER_EMAIL", "admin@wai.com")
    monkeypatch.setenv("USERS_SEED", json.dumps([
        {"email": "admin@wai.com", "name": "Ana Jefa", "project_role": "owner"},
        {"email": "maria@acme.com", "name": "María", "project_role": "member"},
        {"email": "juan@acme.com", "name": "Juan", "project_role": "admin"},
    ]))
    _run(bootstrap_directory())

    rows = _rows()
    assert [r.email for r in rows] == ["admin@wai.com"], (
        "el directorio NO debe sembrarse del proyecto: solo el administrador"
    )
    admin = rows[0]
    assert admin.role == "admin"
    assert admin.source == "deploy", "es una identidad de servicio, no una pertenencia"
    assert admin.display_name == "Ana Jefa", (
        "del USERS_SEED solo sobrevive el nombre del administrador"
    )


def test_boot_never_deactivates_what_the_operator_added(monkeypatch) -> None:
    """A restart does not touch whoever the operator added by hand (the opposite was
    exactly the mirror's damage: the old snapshot resurrected or deactivated people)."""
    from app.users_directory import bootstrap_directory, upsert_directory_user

    monkeypatch.setenv("AGENT_AUTONOMOUS_USER_EMAIL", "admin@wai.com")
    monkeypatch.setenv("USERS_SEED", "[]")
    _run(upsert_directory_user(
        email="externo@proveedor.com", phones=["+34600111222"],
        source="manual", created_by="admin@wai.com",
    ))
    _run(bootstrap_directory())

    emails = sorted(r.email for r in _rows())
    assert emails == ["admin@wai.com", "externo@proveedor.com"]
    assert all(r.active == 1 for r in _rows())


def test_the_mirror_cannot_creep_back_in() -> None:
    """Static guard: no sync route, and no seeding from the project at boot. Either
    would move the directory again without anyone asking for it."""
    app_dir = Path(__file__).resolve().parents[1] / "app"
    routes = (app_dir / "routes" / "users_routes.py").read_text(encoding="utf-8")
    assert '"/users/sync"' not in routes, "la sincronización de usuarios está retirada"

    src = (app_dir / "users_directory.py").read_text(encoding="utf-8")
    boot = src.split("async def bootstrap_directory", 1)[1]
    assert "USERS_SEED" not in boot, (
        "el arranque no debe volver a leer el USERS_SEED para sembrar personas "
        "(el nombre del admin se resuelve en _seed_display_name)"
    )


# ── 2 and 3. Bulk add ────────────────────────────────────────────────
def test_bulk_add_is_idempotent() -> None:
    from app.users_directory import bulk_add_project_users, loads_meta

    first = _run(bulk_add_project_users(
        [{"email": "maria@acme.com", "name": "María"},
         {"email": "juan@acme.com", "name": "Juan"}],
        role="member", created_by="admin@wai.com",
    ))
    assert sorted(first["created"]) == ["juan@acme.com", "maria@acme.com"]
    assert first["merged"] == []

    second = _run(bulk_add_project_users(
        [{"email": "maria@acme.com", "name": "María"},
         {"email": "juan@acme.com", "name": "Juan"},
         {"email": "lucia@acme.com", "name": "Lucía"}],
        role="member", created_by="admin@wai.com",
    ))
    assert second["created"] == ["lucia@acme.com"]
    assert sorted(second["merged"]) == ["juan@acme.com", "maria@acme.com"]

    rows = _rows()
    assert len(rows) == 3, "volver a añadir a alguien NO puede duplicar su ficha"
    for r in rows:
        assert loads_meta(r.metadata_json).get("channel_only") is False, (
            "quien viene del proyecto SÍ puede autenticarse"
        )


def test_bulk_add_never_demotes_an_existing_admin() -> None:
    from app.users_directory import bulk_add_project_users, upsert_directory_user

    _run(upsert_directory_user(
        email="jefa@acme.com", role="admin", source="manual",
        created_by="admin@wai.com", allow_role=True,
    ))
    _run(bulk_add_project_users(
        [{"email": "jefa@acme.com", "name": "Jefa"}],
        role="member", created_by="admin@wai.com",
    ))
    row = next(r for r in _rows() if r.email == "jefa@acme.com")
    assert row.role == "admin", "un alta en lote no puede degradar a un admin"


# ── 4. Who can decide ────────────────────────────────────────────────
@pytest.fixture()
def _no_deploy_identity(monkeypatch):
    """Sets aside the anti-lockout guarantee so the directory role can be measured."""
    monkeypatch.setenv("AGENT_AUTONOMOUS_USER_EMAIL", "empleado@wai.com")


def test_who_can_approve(_no_deploy_identity) -> None:
    from app.users_directory import can_approve, upsert_directory_user

    async def go():
        await upsert_directory_user(email="jefa@acme.com", role="admin",
                                   source="manual", created_by="x", allow_role=True)
        await upsert_directory_user(email="maria@acme.com", role="member",
                                   source="project", created_by="x", allow_role=True)
        designada = await upsert_directory_user(
            email="lucia@acme.com", role="member", source="project",
            created_by="x", allow_role=True)
        return designada

    designada = _run(go())

    from sqlalchemy import update
    from app.app_database import app_write
    from app.app_models.users import AppUser

    async def designate(active: int = 1):
        async with app_write() as s:
            await s.execute(update(AppUser).where(AppUser.id == designada.id)
                            .values(can_approve=1, active=active))
            await s.commit()

    _run(designate())

    assert _run(can_approve("jefa@acme.com")) is True, "un admin siempre decide"
    assert _run(can_approve("maria@acme.com")) is False, "un miembro no decide"
    assert _run(can_approve("lucia@acme.com")) is True, "la designada decide"
    assert _run(can_approve("nadie@acme.com")) is False
    assert _run(can_approve(None)) is False, "sin identidad, cerrado"

    _run(designate(active=0))
    assert _run(can_approve("lucia@acme.com")) is False, (
        "desactivar a alguien le quita también la decisión"
    )


def test_approvers_list_excludes_a_channel_only_contact(_no_deploy_identity) -> None:
    """Even if a row carries the flag (e.g. a contact that got it before anyone knew
    they were not part of the project), it is not offered as an approver: they cannot
    log in to decide, and offering them would leave the action waiting for nobody."""
    from app.users_directory import list_approvers, upsert_directory_user

    row = _run(upsert_directory_user(
        email="externo@proveedor.com", phones=["+34600111222"], role="member",
        source="manual", created_by="x", allow_role=True,
        metadata={"channel_only": True},
    ))

    from sqlalchemy import update
    from app.app_database import app_write
    from app.app_models.users import AppUser

    async def designate():
        async with app_write() as s:
            await s.execute(update(AppUser).where(AppUser.id == row.id)
                            .values(can_approve=1))
            await s.commit()

    _run(designate())
    assert _run(list_approvers()) == []
