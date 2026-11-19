"""«Capacidades declaradas» tells the TRUTH, not what was declared (7.158.2).

The Settings panel described the employee's security posture from the manifest and from
the NAME of each MCP server. What the user saw in production:

    Servidores MCP:  clusterwai · escritura local
                     composio-github · escritura local
    Permisos:        Cualquier herramienta sin pedir confirmación.

All three lines were false at once: `clusterwai` is a READ-ONLY observation MCP,
`composio-github` is remote (not «local»), and the effective write floor was
`read_only` — the employee could not write anything outside, the exact opposite of «sin
pedir confirmación». A security screen that errs on the permissive side is worse than not
having one: it invites trusting a permission that does not exist, or fearing one that does.

These tests pin the rule: the headline comes from `mcp_policy` — the same module that decides
a real call, which since 14.8.0 reads the platform's permissions table — and never from the
manifest or the name.
"""
from __future__ import annotations

import pathlib

import pytest

from app import mcp_policy
from app.routes import manifest_routes as MR

_FRONT = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "src"


def _fila(nombre, permiso, io="write"):
    return {"name": nombre, "key": nombre, "io_kind": io, "permission": permiso,
            "source": "read" if io == "read" else "inherited_agent"}


def _tabla(**conectores):
    """A table: connector → permission of its ONE write tool (plus a read)."""
    return {"agent_authority": "read_only", "unlisted_connector": {"permission": "deny"},
            "connectors": [{"name": n, "runtime_name": n, "unlisted_tool": {"permission": p},
                            "tools": [_fila("leer", "allow", "read"), _fila("escribir", p)]}
                           for n, p in conectores.items()]}


@pytest.fixture
def con_tabla(monkeypatch):
    """The table the platform returns, and autonomy neutral.

    Since v10 autonomy is a factor and its default value is `propose`. Without
    neutralizing it here, every assertion in this file would also be measuring the
    coupling without saying so."""
    estado = {"tabla": None}

    async def _fetch():
        return estado["tabla"]

    async def _nada(*_a):
        return None

    async def _sin_autonomia():
        return mcp_policy.POLICY_WRITE

    monkeypatch.setattr(mcp_policy, "_fetch_table", _fetch)
    monkeypatch.setattr(mcp_policy, "_persist", _nada)
    monkeypatch.setattr(mcp_policy, "_load_persisted", _nada)
    monkeypatch.setattr(mcp_policy, "_por_autonomia", _sin_autonomia)
    mcp_policy.reset_table_cache()

    def poner(tabla):
        estado["tabla"] = tabla
        mcp_policy.reset_table_cache()
    yield poner
    mcp_policy.reset_table_cache()


@pytest.mark.asyncio
async def test_un_mcp_de_solo_lectura_no_se_anuncia_como_escritura(con_tabla):
    """The exact regression that was seen on screen."""
    con_tabla(_tabla(clusterwai="deny", **{"composio-github": "ask"}))
    classify = await MR._mcp_classifier()
    assert classify("clusterwai") == "read_only"
    assert classify("composio-github") == "write_with_approval"


@pytest.mark.asyncio
async def test_un_servidor_que_la_tabla_no_nombra_toma_su_fila(con_tabla):
    con_tabla(_tabla())
    classify = await MR._mcp_classifier()
    assert classify("lo-que-sea") == "read_only"


@pytest.mark.asyncio
async def test_sin_tabla_nunca_se_anuncia_escritura(con_tabla):
    con_tabla(None)
    classify = await MR._mcp_classifier()
    assert classify("clusterwai") == "read_only"
    resumen = (await MR._permissions_summariser())(None)
    assert "sin pedir confirmación" not in resumen


@pytest.mark.asyncio
async def test_wai_data_es_solo_lectura_aunque_se_abra_el_agente(con_tabla):
    """It does not depend on the policy: it is the project's data view."""
    con_tabla(_tabla(**{"wai-data": "allow"}))
    classify = await MR._mcp_classifier()
    assert classify("wai-data") == "read_only"
    assert classify("wai-data.ventas") == "read_only"


@pytest.mark.asyncio
async def test_el_resumen_NUNCA_promete_escritura_libre_con_todo_cerrado(con_tabla):
    """The most misleading failure: promising a free-for-all on a sealed employee."""
    con_tabla(_tabla(a="deny", b="deny"))
    resumen = (await MR._permissions_summariser())({"*": "allow"})
    assert "sin pedir confirmación" not in resumen
    assert "no puede escribir" in resumen.lower()


@pytest.mark.asyncio
async def test_con_aprobacion_el_resumen_lo_dice(con_tabla):
    con_tabla(_tabla(a="ask"))
    resumen = (await MR._permissions_summariser())(None)
    assert "aprobación" in resumen


@pytest.mark.asyncio
async def test_el_manifiesto_ya_no_manda(con_tabla):
    """Even if the manifest declares a free-for-all, the table rules."""
    con_tabla(_tabla(a="deny"))
    summarise = await MR._permissions_summariser()
    assert summarise({"*": "allow"}) == summarise({"*": "deny"}) == summarise(None)


def test_la_UI_tiene_etiqueta_para_las_TRES_politicas():
    """If the backend sends a policy without a label, the chip comes out empty."""
    for locale in ("es", "en"):
        txt = (_FRONT / "i18n" / f"{locale}.ts").read_text(encoding="utf-8")
        for p in ("read_only", "write_with_approval", "write"):
            assert f"mcpPolicy_{p}:" in txt, f"falta mcpPolicy_{p} en {locale}.ts"


def test_no_queda_rastro_de_la_etiqueta_binaria_vieja():
    ops = (_FRONT / "pages" / "Operations.tsx").read_text(encoding="utf-8")
    assert "mcpKindLocal" not in ops and "local_writes" not in ops
