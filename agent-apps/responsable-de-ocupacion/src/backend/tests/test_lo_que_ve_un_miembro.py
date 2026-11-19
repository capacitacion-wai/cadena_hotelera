"""What a MEMBER of the employee can reach (14.18.0).

A member USES the employee (chat, manual, its own encargos); it does not operate it. The
screens that expose everyone's work are refused HERE, in the backend — the menu only reflects
it. Before, the Panel of a member showed the whole team's 1,125 conversations.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException


def _deps(route) -> set[str]:
    """Names of every dependency in the route's chain (router-level ones included)."""
    nombres, pila = set(), [route.dependant]
    while pila:
        d = pila.pop()
        for sub in d.dependencies:
            nombres.add(getattr(sub.call, "__name__", ""))
            pila.append(sub)
    return nombres


def _rutas():
    from app.main import app
    return [r for r in app.routes if hasattr(r, "dependant")]


@pytest.mark.parametrize("prefijo", [
    "/api/dashboard", "/api/agent/activity", "/api/agent/health", "/api/calendar",
    "/api/domain", "/api/runs", "/api/events", "/api/data", "/api/agent/tasks/producers",
])
def test_lo_que_un_miembro_no_puede_pedir(prefijo):
    rutas = [r for r in _rutas() if r.path == prefijo or r.path.startswith(prefijo + "/")]
    assert rutas, f"no route under {prefijo}"
    for r in rutas:
        assert "require_admin" in _deps(r), f"{sorted(r.methods)} {r.path} is open to a member"


def test_las_aprobaciones_solo_para_quien_puede_aprobar():
    for r in _rutas():
        if r.path.startswith("/api/approvals"):
            assert {"require_approver", "require_admin"} & _deps(r), f"{r.path} is open to a member"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_un_miembro_solo_ve_sus_encargos(monkeypatch):
    from app.app_database import app_write
    from app.app_models.business import ScheduledItem
    from app.auth import TokenPayload
    from app.routes import scheduling_routes as S

    async def es_admin(email):
        return email == "jefa@acme.com"
    monkeypatch.setattr("app.users_directory.is_admin", es_admin)

    async def crear():
        async with app_write() as s:
            ids = []
            for k, quien in (("a", "ana@acme.com"), ("b", "luis@acme.com")):
                it = ScheduledItem(idempotency_key=f"t-{k}", title=f"encargo de {quien}",
                                   assignee="agent", created_by=quien)
                s.add(it)
                await s.flush()
                ids.append(it.id)
            await s.commit()
            return ids

    ana_id, luis_id = _run(crear())

    def quien(email):
        return TokenPayload(email=email, is_superuser=False, project_ids=[], data_areas=[])

    assert _run(S._solo_suyos(quien("jefa@acme.com"))) is None, "an admin sees every encargo"
    with pytest.raises(HTTPException) as e:
        _run(S.get_task(luis_id, quien("ana@acme.com")))
    assert e.value.status_code == 404, "someone else's encargo does not exist for a member"
    assert _run(S.get_task(ana_id, quien("ana@acme.com")))["id"] == ana_id
    # A WAI (platform) admin who is a plain member of THIS employee is a member here (15.0.2, AGO-185).
    plataforma = TokenPayload(email="ana@acme.com", is_superuser=True, project_ids=[], data_areas=[])
    assert _run(S._solo_suyos(plataforma)) is not None, "the platform's superuser flag does not make an admin here"
    with pytest.raises(HTTPException):
        _run(S.get_task(luis_id, plataforma))
