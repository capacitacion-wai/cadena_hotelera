"""The employee's own layer of connector permissions (14.8.0).

Contract: ``docs/permisos-de-conectores.md``. The employee inherits its agent or personalises
with the same shape and never opens more than the agent — the PLATFORM checks that. What is
pinned here is the pod's side:

* the old local settings (``mcp_write_policy`` + ``mcp_tool_permissions``) convert ONCE to the
  new shape with exactly the restrictions they imposed (never fewer), and the result is
  something the platform accepts (no rule equal to what is inherited, nothing opening more);
* saving goes through the platform and a rejection reaches the screen as ``{code, message}``
  —the screen translates the code— without saving anything locally;
* the boot sync converts once and afterwards re-asserts what was saved.
"""
from __future__ import annotations

import asyncio
import itertools

import pytest
from fastapi import HTTPException

from app import mcp_policy
from app import runtime_policy_sync as rps

RANGO = {"allow": 0, "ask": 1, "deny": 2}
POR_POTESTAD = {"write": "allow", "write_with_approval": "ask", "read_only": "deny"}


def _peor(a, b):
    return a if RANGO[a] >= RANGO[b] else b


def _tabla(agente="write", conector_auth="write", permisos=("allow", "allow", "allow")):
    """crm: one read and two writes, with the agent's permission on each."""
    io = ("read", "write", None)
    return {"agent_authority": agente, "version_id": 9, "connectors": [{
        "name": "composio-crm", "runtime_name": "crm", "authority": conector_auth,
        "tools": [{"name": f"t{i}", "key": f"crm_t{i}", "io_kind": io[i],
                   "agent_permission": permisos[i], "permission": permisos[i]}
                  for i in range(3)]}]}


def _efectivo_nuevo(tabla, politica):
    """The contract's words: employee = most restrictive of agent and the employee's choice."""
    out = {}
    auth = politica["authority"]
    for c in tabla["connectors"]:
        e = politica["connectors"].get(c["name"]) or {}
        modo = e.get("mode", "inherit")
        for r in c["tools"]:
            if modo == "custom" and r["name"] in (e.get("tools") or {}):
                eleccion = e["tools"][r["name"]]
            elif r["io_kind"] == "read":
                eleccion = None
            elif modo in POR_POTESTAD:
                eleccion = POR_POTESTAD[modo]
            elif auth in POR_POTESTAD:
                eleccion = POR_POTESTAD[auth]
            else:
                eleccion = None
            out[r["name"]] = _peor(r["agent_permission"], eleccion) if eleccion else r["agent_permission"]
    return out


def _efectivo_viejo(tabla, h, casc):
    out = {}
    for c in tabla["connectors"]:
        hc = h.get("connectors") or {}
        h_eff = hc.get("crm") or hc.get("composio-crm") or h.get("default") or "write"
        cc = (casc.get("connectors") or {}).get("crm", casc.get("default", "allow"))
        for r in c["tools"]:
            viejo = (casc.get("tools") or {}).get(r["name"], cc)
            if r["io_kind"] != "read":
                viejo = _peor(viejo, POR_POTESTAD[h_eff])
            out[r["name"]] = _peor(r["agent_permission"], viejo)
    return out


def _aceptable(tabla, politica):
    """What the platform would reject, written from the contract."""
    rango_auth = {"write": 0, "write_with_approval": 1, "read_only": 2}
    if politica["authority"] != "inherit":
        assert rango_auth[politica["authority"]] >= rango_auth[tabla["agent_authority"]], \
            "EMPLEADO_ABRE_MAS"
    for c in tabla["connectors"]:
        e = politica["connectors"].get(c["name"])
        if not e:
            continue
        if e["mode"] in rango_auth:
            assert rango_auth[e["mode"]] >= rango_auth[c["authority"]], "EMPLEADO_ABRE_MAS"
            assert "tools" not in e
        if e["mode"] == "custom":
            assert e["tools"], "PERMISO_SIN_CAMBIO (custom vacío)"
            sin_regla = dict(politica, connectors={c["name"]: {"mode": "custom", "tools": {}}})
            heredado = _efectivo_nuevo(tabla, sin_regla)
            agente = {r["name"]: r["agent_permission"] for r in c["tools"]}
            for t, p in e["tools"].items():
                assert RANGO[p] >= RANGO[agente[t]], "EMPLEADO_ABRE_MAS"
                assert p != heredado[t], "PERMISO_SIN_CAMBIO"


AUTH = ("write", "write_with_approval", "read_only")
PERM = ("allow", "ask", "deny")


@pytest.mark.parametrize("agente,h_def,h_con,c_def,c_t1", list(itertools.product(
    AUTH, AUTH, (None,) + AUTH, PERM, (None,) + PERM)))
def test_lo_viejo_se_convierte_con_las_mismas_restricciones(agente, h_def, h_con, c_def, c_t1):
    tabla = _tabla(agente=agente, conector_auth=agente,
                   permisos=("allow", POR_POTESTAD[agente], POR_POTESTAD[agente]))
    h = {"default": h_def, "connectors": {"composio-crm": h_con} if h_con else {}}
    casc = {"default": c_def, "connectors": {}, "tools": {"t1": c_t1} if c_t1 else {}}
    nueva = rps.convert_legacy(tabla, h, casc)
    assert _efectivo_nuevo(tabla, nueva) == _efectivo_viejo(tabla, h, casc)
    _aceptable(tabla, nueva)


def test_una_cascada_plana_es_su_capa_por_herramienta():
    tabla = _tabla()
    nueva = rps.convert_legacy(tabla, {}, {"crm_t2": "deny"})
    assert nueva == {"authority": "inherit",
                     "connectors": {"composio-crm": {"mode": "custom", "tools": {"t2": "deny"}}}}


def test_un_conector_endurecido_queda_en_modo_fijo():
    nueva = rps.convert_legacy(_tabla(), {"default": "write", "connectors": {"crm": "read_only"}}, {})
    assert nueva == {"authority": "inherit", "connectors": {"composio-crm": {"mode": "read_only"}}}


def test_normalize_deja_solo_lo_que_restringe():
    assert rps.normalize_policy({"authority": "x", "connectors": {
        "a": {"mode": "inherit"}, "b": {"mode": "custom", "tools": {"t": "DENY"},
                                        "reasons": {"t": " ", "z": "no"}}}}) == {
        "authority": "inherit", "connectors": {"b": {"mode": "custom", "tools": {"t": "deny"}}}}


def test_el_rechazo_trae_el_codigo():
    assert rps.rejection(422, {"detail": "EMPLEADO_ABRE_MAS: An employee cannot…"}) == {
        "code": "EMPLEADO_ABRE_MAS", "message": "EMPLEADO_ABRE_MAS: An employee cannot…"}
    assert rps.rejection(500, {"detail": "boom"})["code"] == "HTTP_500"


# ── The routes and the boot sync ─────────────────────────────────────

class _User:
    email = "jefa@x.com"


@pytest.fixture
def plataforma(monkeypatch):
    estado = {"put": [], "respuesta": (200, {"policy": {}}), "guardada": None,
              "legado": None, "tabla": _tabla()}

    async def put(policy):
        estado["put"].append(policy)
        return estado["respuesta"]

    async def guardar(policy, who):
        estado["guardada"] = policy

    async def cargar():
        return estado["guardada"]

    async def legado():
        return estado["legado"]

    async def tabla(**_k):
        return estado["tabla"]

    async def nada(*_a, **_k):
        return None

    monkeypatch.setattr(rps, "_configured", lambda: True)
    monkeypatch.setattr(rps, "put_employee_policy", put)
    monkeypatch.setattr(rps, "save_employee_policy", guardar)
    monkeypatch.setattr(rps, "load_employee_policy", cargar)
    monkeypatch.setattr(rps, "load_legacy", legado)
    monkeypatch.setattr(mcp_policy, "load_table", tabla)
    monkeypatch.setattr("app.agent_memory_utils.expire_config_state_memories", nada)
    return estado


def test_guardar_rechazado_no_guarda_nada_y_da_el_codigo(plataforma):
    from app.routes import mcp_connectors_routes as R
    plataforma["respuesta"] = (422, {"detail": "EMPLEADO_ABRE_MAS: more permissive than the agent"})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(R.put_connector_permissions(
            {"policy": {"authority": "write", "connectors": {}}}, _User()))
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "EMPLEADO_ABRE_MAS"
    assert plataforma["guardada"] is None


def test_guardar_aceptado_guarda_la_forma_nueva(plataforma):
    from app.routes import mcp_connectors_routes as R
    out = asyncio.run(R.put_connector_permissions(
        {"policy": {"authority": "read_only", "connectors": {"composio-crm": {"mode": "inherit"}}}},
        _User()))
    assert plataforma["put"] == [{"authority": "read_only", "connectors": {}}]
    assert plataforma["guardada"] == {"authority": "read_only", "connectors": {}}
    assert out["table"] is plataforma["tabla"]


def test_plataforma_caida_es_503_no_un_guardado_a_medias(plataforma):
    from app.routes import mcp_connectors_routes as R
    plataforma["respuesta"] = (0, {"detail": "connection refused"})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(R.put_connector_permissions({"policy": {}}, _User()))
    assert exc.value.status_code == 503
    assert exc.value.detail["code"] == "PLATAFORMA_NO_DISPONIBLE"


def test_el_arranque_convierte_una_vez_y_luego_reafirma(plataforma):
    plataforma["legado"] = ({"default": "write", "connectors": {"crm": "read_only"}}, {})
    assert asyncio.run(rps.push_local_write_policy()) is True
    convertida = {"authority": "inherit", "connectors": {"composio-crm": {"mode": "read_only"}}}
    assert plataforma["put"] == [convertida]
    assert plataforma["guardada"] == convertida
    # Second boot: the saved one is re-asserted; the old settings are not read again.
    plataforma["legado"] = ({"default": "read_only"}, {})
    assert asyncio.run(rps.push_local_write_policy()) is True
    assert plataforma["put"][-1] == convertida


def test_si_la_plataforma_rechaza_la_conversion_no_se_marca_convertida(plataforma):
    plataforma["legado"] = ({"default": "read_only"}, {})
    plataforma["respuesta"] = (422, {"detail": "CONECTOR_DESCONOCIDO: …"})
    assert asyncio.run(rps.push_local_write_policy()) is False
    assert plataforma["guardada"] is None


def test_sin_nada_viejo_no_se_empuja_nada(plataforma):
    assert asyncio.run(rps.push_local_write_policy()) is True
    assert plataforma["put"] == []
