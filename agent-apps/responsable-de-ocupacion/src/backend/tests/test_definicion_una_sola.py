"""The employee runs THE definition, not one of its own (20/09/2026).

THE FAILURE THIS PREVENTS, and it is not hypothetical. There were two definitions of the same employee:
the employee skill's one in its `wai-app.json` and the agent skill's one on the platform.
Nobody synced them, so they aged separately — measured in the travel
assistant: **3 capabilities and no shortcut** in the pod versus **8 capabilities and 3 shortcuts** in
the published version. The employee ran one and the person reviewed and approved the other.

And it did not fail loudly: the shortcuts simply never fired and the conversation
offered something else.
"""
from __future__ import annotations

import json

from app import conversacion


def test_manda_la_de_la_plataforma(tmp_path, monkeypatch):
    """It is the one that is versioned, sealed and approved. The manifest's one does not govern."""
    f = tmp_path / "presentation.json"
    f.write_text(json.dumps({"soy": "la buena", "hago": [{"id": "x", "que": "X"}]}),
                 encoding="utf-8")
    monkeypatch.setattr(conversacion, "RUTA_DEFINICION", str(f))
    monkeypatch.setattr("app.config.APP_META",
                        {"agent_app": {"presentation": {"soy": "la vieja del manifiesto"}}},
                        raising=False)
    assert conversacion.declaracion()["soy"] == "la buena"


def test_sin_la_de_la_plataforma_vale_la_del_manifiesto(tmp_path, monkeypatch):
    """An employee deployed with an older deployer does not go mute. It is not an alternative
    path that gets chosen: it is the previous state, which ends at the first deployment
    with the new deployer."""
    monkeypatch.setattr(conversacion, "RUTA_DEFINICION", str(tmp_path / "no-existe.json"))
    monkeypatch.setattr("app.config.APP_META",
                        {"agent_app": {"presentation": {"soy": "la del manifiesto"}}},
                        raising=False)
    assert conversacion.declaracion()["soy"] == "la del manifiesto"


def test_una_definicion_ilegible_no_tumba_el_pod(tmp_path, monkeypatch):
    f = tmp_path / "presentation.json"
    f.write_text("{esto no es json", encoding="utf-8")
    monkeypatch.setattr(conversacion, "RUTA_DEFINICION", str(f))
    monkeypatch.setattr("app.config.APP_META",
                        {"agent_app": {"presentation": {"soy": "la del manifiesto"}}},
                        raising=False)
    assert conversacion.declaracion()["soy"] == "la del manifiesto"


def test_los_atajos_y_la_voz_leen_LA_MISMA(tmp_path, monkeypatch):
    """Three copies of the same read were three places to fix the day it
    changed. If someone reads the manifest on their own again, this turns red."""
    from app import atajos, voz
    f = tmp_path / "presentation.json"
    f.write_text(json.dumps({
        "soy": "la buena",
        "hago": [{"id": "consultar", "que": "Consultar", "apoyo": ["wai_x"],
                  "resuelve_con": {"herramienta": "wai_x", "argumentos": ["persona"]}}],
    }), encoding="utf-8")
    monkeypatch.setattr(conversacion, "RUTA_DEFINICION", str(f))
    assert [c["id"] for c in atajos.catalogo()] == ["consultar"]
    assert voz._declaracion()["soy"] == "la buena"
