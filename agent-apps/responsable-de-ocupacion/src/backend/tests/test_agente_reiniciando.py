"""When the agent's pod restarts (it does so to apply a connector just connected), the employee
waits for it instead of failing (14.12.0).

Measured on 04/10/2026: Facebook was connected at 11:49 while the agent's pod was restarting; the
nudge found nobody and the connector waited 4 minutes for the periodic refresher. Meanwhile two
chat messages failed with «stream transport: All connection attempts failed».
"""
import asyncio

from app.routes import agent_routes, mcp_connectors_routes as mcr


class _Agente:
    def __init__(self, vuelve_en: int):
        self.llamadas, self.vuelve_en = 0, vuelve_en

    async def check_health(self) -> bool:
        self.llamadas += 1
        return self.llamadas >= self.vuelve_en


def test_el_chat_espera_al_agente_hasta_que_vuelve():
    a = _Agente(vuelve_en=3)
    assert asyncio.run(agent_routes.esperar_al_agente(a, durante_s=1, cada_s=0.01)) is True
    assert a.llamadas == 3


def test_si_no_vuelve_a_tiempo_se_rinde():
    a = _Agente(vuelve_en=10_000)
    assert asyncio.run(agent_routes.esperar_al_agente(a, durante_s=0.05, cada_s=0.01)) is False


def test_el_aviso_de_conector_nuevo_insiste_hasta_que_llega(monkeypatch):
    intentos = []

    async def avisar(connector):
        intentos.append(connector)
        return len(intentos) >= 3

    monkeypatch.setattr(mcr, "_avisar_al_agente", avisar)
    asyncio.run(mcr._insistir_al_agente("composio-facebook", cada_s=0.01, durante_s=1))
    assert intentos == ["composio-facebook"] * 3


def test_el_aviso_deja_de_insistir_al_agotar_el_tiempo(monkeypatch):
    intentos = []

    async def avisar(connector):
        intentos.append(connector)
        return False

    monkeypatch.setattr(mcr, "_avisar_al_agente", avisar)
    asyncio.run(mcr._insistir_al_agente("composio-facebook", cada_s=0.01, durante_s=0.05))
    assert 1 <= len(intentos) <= 6
