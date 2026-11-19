"""The GOVERNANCE keys of `/api/agent/settings` require admin OF THE EMPLOYEE (v7.109.0).

`autonomy_level` and `triggers_active` decide what the employee does ON ITS OWN: turning the
dial up to `act_autonomous` also makes its automations be born without approval. They went through
`require_project_access` —like the language or the model—, so ANY user of the employee
could change them, while the write policy and per-tool permissions did require
admin. Governance inconsistency: whoever cannot touch permissions should not be able to
raise autonomy either.

The role is governed by the EMPLOYEE'S DIRECTORY (like `require_admin`), not by the platform
superuser: a WAI admin may be a plain member here (INC-AGO-39).

v7.146.0 EXTENDS the list, when opening chat/UI parity: governance is also the MODEL (it decides
the per-token cost and the quality of EVERYTHING it does), the language it writes to people in,
the BUSINESS timezone, self-scheduling and the concurrency caps. None of those is
a personal preference: they change what others receive. The only one that is still each
person's own —and that is why it is the one guarding "we don't break the benign" here— is the color theme.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

MEMBER = "member@wai.com"
ADMIN = "admin@wai.com"


class _User:
    def __init__(self, email: str) -> None:
        self.email = email
        self.is_superuser = True      # PLATFORM superuser: must not be enough


def _patch_directory(monkeypatch, admin_email: str) -> None:
    import app.users_directory as ud

    async def _is_admin(email: str) -> bool:
        return email == admin_email

    monkeypatch.setattr(ud, "is_admin", _is_admin, raising=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("clave, valor", [
    ("autonomy_level", "act_autonomous"),
    ("triggers_active", True),
    # v7.146.0 — what a member could change and should not: which brain it thinks with
    # (and at what price), which language it writes to everyone in, which hours the
    # business lives by, whether it schedules work by itself and how much it can run in parallel.
    ("agent_model", "gpt-5"),
    ("agent_language", "en"),
    ("business_timezone", "Europe/Madrid"),
    ("self_schedule", True),
    ("max_open_tasks", 5),
    ("max_concurrent_runs", 4),
    ("max_autonomous_concurrent", 4),
    # 15.6.0 — how long one task may run is how much a stuck one may spend.
    ("chat_turn_max_minutes", 60),
    ("autonomous_run_max_minutes", 60),
])
async def test_member_no_puede_cambiar_gobierno(monkeypatch, clave, valor) -> None:
    """A user who is NOT an admin of the employee cannot change how it behaves —
    even if they are a platform superuser."""
    from app.routes.manifest_routes import patch_settings

    _patch_directory(monkeypatch, ADMIN)
    with pytest.raises(HTTPException) as exc:
        await patch_settings(body={clave: valor}, user=_User(MEMBER))
    assert exc.value.status_code == 403
    assert clave in str(exc.value.detail)


@pytest.mark.asyncio
async def test_ajustes_inocuos_siguen_abiertos(monkeypatch) -> None:
    """The color theme is NOT governance: it is each person's visual preference and
    changes nothing for anyone else. Tightening the gate cannot sweep away
    what legitimately belongs to each person."""
    from app.routes import manifest_routes as mr

    _patch_directory(monkeypatch, ADMIN)
    escrituras: list[tuple] = []

    async def _upsert(key, value, user):       # noqa: ANN001 — test double
        escrituras.append((key, value))

    monkeypatch.setattr(mr, "_upsert_setting", _upsert)
    await mr.patch_settings(body={"default_color_mode": "dark"}, user=_User(MEMBER))
    assert ("default_color_mode", "dark") in escrituras
