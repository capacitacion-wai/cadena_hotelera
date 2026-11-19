"""The PRODUCT's language of the employee (14.5.0).

The agent's Definition (`presentation.idioma_principal`, written by the code agent and approved by a
person) says in which language the employee produces what goes OUTWARD. Without an operator choice in
Operations (`app_settings.agent_language`), that is the language of its autonomous runs; the
operator's choice still wins, and chat replies keep following each person's language.
"""
from __future__ import annotations

import asyncio

import pytest

from app import agent_locale, conversacion


class _SinBase:
    """`app_session` with nothing configured: the setting cannot be read."""

    def __call__(self):
        raise RuntimeError("no database in this test")


def _correr(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.mark.parametrize("definicion, esperado", [
    ({"idioma_principal": "en"}, "en"),
    ({"idioma_principal": "en-GB"}, "en"),
    ({"idioma_principal": "pt-BR"}, "pt"),
    ({}, ""),
])
def test_el_idioma_del_producto_sale_de_la_definicion(monkeypatch, definicion, esperado):
    monkeypatch.setattr(conversacion, "declaracion", lambda: definicion)
    assert agent_locale.product_language() == esperado


def test_sin_eleccion_del_operador_manda_la_definicion(monkeypatch):
    monkeypatch.setattr(agent_locale, "app_session", _SinBase())
    monkeypatch.setattr(conversacion, "declaracion", lambda: {"idioma_principal": "en"})
    assert _correr(agent_locale.resolve_agent_language()) == "en"
    # A language the autonomous prompts do not have yet: the old default, not a guess.
    monkeypatch.setattr(conversacion, "declaracion", lambda: {"idioma_principal": "fr"})
    assert _correr(agent_locale.resolve_agent_language()) == "es"
    monkeypatch.setattr(conversacion, "declaracion", lambda: {})
    assert _correr(agent_locale.resolve_agent_language()) == "es"
