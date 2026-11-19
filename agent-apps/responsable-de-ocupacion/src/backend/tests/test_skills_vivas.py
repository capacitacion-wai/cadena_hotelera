"""«Declared capabilities» reads what the pod HAS, not what was written at generation time.

The screen that exists to tell the truth about an employee was the one that lied most
easily. Its list of skills came from ``agent_app.skills`` in ``wai-app.json`` —a
snapshot written the day the application was generated—, so:

* it showed ``wai-odoo@v3.0.0`` while the pod ran **3.3.0**;
* it did not see a skill added to the agent later;
* it kept showing the ones that had been removed;
* and it only covered **one of the three origins**: missing were the functional ones
  the operator attaches and the two the deployer injects (`wai-agent-core`,
  `wai-autonomous-delivery`), which cannot even be disabled.

And it lied **silently**: nothing said it was an old snapshot.

Now the pod is asked, which is where the three origins land. And if it cannot be
asked, that is said — «no skills» and «I don't know» are different things.
"""
from __future__ import annotations

import ast
import pathlib

_APP = pathlib.Path(__file__).resolve().parent.parent / "app"
_MANIFIESTO = (_APP / "routes" / "manifest_routes.py").read_text(encoding="utf-8")
_PROXY = (_APP / "agent_proxy.py").read_text(encoding="utf-8")
_ARBOL = ast.parse(_MANIFIESTO)


def _cuerpo(nombre: str) -> str:
    """The EXACT function, not an N-character window: counting characters eats
    the neighbouring code and the test fails (or passes) for the wrong reason."""
    nodo = next(n for n in ast.walk(_ARBOL)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == nombre)
    return ast.get_source_segment(_MANIFIESTO, nodo) or ""


def test_el_manifiesto_ya_NO_lee_las_skills_del_fichero_generado():
    """The regression: going back to `agent_app.get("skills")` freezes the list again."""
    assert 'agent_app.get("skills"' not in _MANIFIESTO, (
        "las skills han vuelto a salir de wai-app.json, que es una foto del día "
        "en que se generó la aplicación")
    assert "_skills_bloque()" in _MANIFIESTO


def test_no_hay_marcha_atras_a_la_foto_vieja():
    """If the pod cannot be queried, the answer is «I don't know» — never the old
    list disguised as the current one."""
    cuerpo = _cuerpo("_skills_bloque")
    assert '"available": False' in cuerpo
    assert "agent_app" not in cuerpo, (
        "el camino de error no puede caer al manifiesto: sería volver a mostrar "
        "un dato viejo sin avisar")


def test_el_proxy_distingue_vacio_de_desconocido():
    i = _PROXY.index("async def list_skills")
    cuerpo = _PROXY[i:i + 1600]
    assert cuerpo.count('"available": False') >= 3, (
        "cada fallo (transporte, HTTP, JSON) tiene que decir que NO se pudo "
        "consultar, no devolver una lista vacía")
    assert '"reason"' in cuerpo, "hay que decir POR QUÉ no se pudo"


def test_el_bloque_del_agente_es_async():
    """Querying the pod is I/O: if the function calling it is not async, it cannot
    be awaited and the data never arrives."""
    fn = next(n for n in ast.walk(_ARBOL)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "_agent_block")
    assert isinstance(fn, ast.AsyncFunctionDef)


def test_skills_SIGUE_siendo_una_lista():
    """The regression that broke the screen: when the field went from list to object,
    the consumer —which did `.length`— showed «Sin skills declaradas» with ELEVEN
    loaded. Changing the shape of a published datum breaks silently."""
    cuerpo = _cuerpo("_skills_bloque")
    assert '"skills": datos.get("skills") or []' in cuerpo
    assert '"skills_available"' in cuerpo, (
        "la bandera va en su propio campo, no dentro de `skills`")


def test_la_version_tiene_respaldo_en_skill_json():
    """The repo's skills carry their version in `skill.json`, not in the SKILL.md
    header. Without a fallback, the screen shows them without a version — which is
    exactly the figure people look there for."""
    rutas = pathlib.Path(__file__).resolve().parents[4]
    ruta = rutas / "opencode-server" / "simple_api" / "routes.py"
    if not ruta.exists():   # the sidecar does not travel with the generated app
        import pytest; pytest.skip("sidecar fuera de este árbol")
    t = ruta.read_text(encoding="utf-8")
    assert "skill.json" in t and 'entrada["version"]' in t
