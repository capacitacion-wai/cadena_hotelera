"""A catalogue change (30/09/2026: gpt-5.6 → gpt-6) left the employee with a chosen model the
tenant no longer offers. The choice is kept, but no call uses it: it counts as unset and the default
model is used (`config.modelo_vigente`)."""
import importlib


def _config(monkeypatch, catalogo):
    monkeypatch.setenv("OPENCODE_DEFAULT_MODEL", catalogo)
    import app.config as c
    return importlib.reload(c)


def test_un_modelo_retirado_cuenta_como_vacio(monkeypatch):
    c = _config(monkeypatch, "openai/gpt-6-luna,openai/gpt-6.1-sol")
    assert c.DEFAULT_MODEL == "openai/gpt-6-luna"
    assert c.modelo_vigente("openai/gpt-5.6-luna") == ""
    assert c.modelo_vigente("openai/gpt-6.1-sol") == "openai/gpt-6.1-sol"
    assert c.modelo_vigente("gpt-6.1-sol") == "gpt-6.1-sol"          # id without provider
    assert c.modelo_vigente("") == "" and c.modelo_vigente(None) == ""


def test_sin_catalogo_se_respeta_la_eleccion(monkeypatch):
    c = _config(monkeypatch, "")
    assert c.modelo_vigente("openai/lo-que-sea") == "openai/lo-que-sea"
