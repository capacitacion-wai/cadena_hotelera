"""The employee cannot explain an application that is no longer its own.

Here is the only reason self-knowledge does not drift out of sync: it is not
a manual someone remembers to update, it is a table this test CROSS-CHECKS against
reality. If you add a screen or a menu entry and do not declare it, this turns
red; if you declare one that no longer exists, too.

It is cross-checked against the three sources that truly define the application:
  * the routes in ``App.tsx`` (what can be opened),
  * the menu entries in ``Layout.tsx`` with their admin mark (what is shown),
  * the i18n keys in ``es`` and ``en`` (what they are called).

What is NOT declared by hand —each employee's domain screens and the API route
table with its gate— is derived from the manifest and from the FastAPI app
itself, so it cannot lie and needs no sync test.
"""
from __future__ import annotations

import re
from pathlib import Path

FRONT = Path(__file__).resolve().parents[2] / "frontend" / "src"
APP = Path(__file__).resolve().parents[1] / "app"

# `<Route path="/x" …>` — wildcard (`*`) and redirect routes are not
# screens to explain.
ROUTE_RE = re.compile(r'<Route\s+path="([^"]+)"')
# `{ to: '/x', labelKey: 'nav.y' }` — the fixed menu. Domain entries are
# built from a template (`/domain/${e.slug}`) and are left out by design.
NAV_RE = re.compile(r"\{\s*to:\s*'([^']+)',\s*labelKey:\s*'([^']+)'")


def _app_routes() -> set[str]:
    src = (FRONT / "App.tsx").read_text(encoding="utf-8")
    return {r for r in ROUTE_RE.findall(src) if r != "*"}


def _nav_entries() -> dict[str, str]:
    src = (FRONT / "components" / "Layout.tsx").read_text(encoding="utf-8")
    return {to: key for to, key in NAV_RE.findall(src)}


def test_toda_pantalla_de_la_app_esta_declarada() -> None:
    from app.self_knowledge import SCREENS
    declared = {s.route for s in SCREENS}
    faltan = sorted(_app_routes() - declared)
    assert not faltan, (
        "Estas pantallas existen en App.tsx y el empleado NO sabe nada de ellas: no "
        "podrá explicarlas ni decir a quién le tocan. Declárelas en "
        "self_knowledge.SCREENS:\n  " + "\n  ".join(faltan)
    )


def test_no_se_declara_una_pantalla_que_ya_no_existe() -> None:
    from app.self_knowledge import SCREENS
    routes = _app_routes()
    sobran = sorted({s.route for s in SCREENS} - routes)
    assert not sobran, (
        "El empleado cree que estas pantallas existen, pero no están en App.tsx: "
        "mandará a la gente a una pantalla muerta.\n  " + "\n  ".join(sobran)
    )


def test_el_menu_declarado_es_el_menu_real() -> None:
    from app.self_knowledge import menu_routes
    real, declared = _nav_entries(), menu_routes()
    faltan = sorted(set(real) - set(declared))
    assert not faltan, (
        "Estas entradas SÍ están en el menú y el catálogo no las marca como tal "
        "(nav_key), así que el empleado no sabrá decir «lo tienes en el menú»:\n  "
        + "\n  ".join(faltan)
    )
    inventadas = sorted(set(declared) - set(real))
    assert not inventadas, (
        "El catálogo dice que estas pantallas están en el menú y no lo están:\n  "
        + "\n  ".join(inventadas)
    )
    distintas = sorted(f"{r}: catálogo={declared[r]} menú={real[r]}"
                       for r in set(real) & set(declared) if declared[r] != real[r])
    assert not distintas, "la clave i18n del menú no coincide:\n  " + "\n  ".join(distintas)


def test_las_etiquetas_del_menu_existen_en_los_dos_idiomas() -> None:
    """A misspelled i18n key in the catalog shows up as a raw key."""
    from app.self_knowledge import menu_routes
    es = (FRONT / "i18n" / "es.ts").read_text(encoding="utf-8")
    en = (FRONT / "i18n" / "en.ts").read_text(encoding="utf-8")
    huerfanas = []
    for key in menu_routes().values():
        leaf = key.split(".")[-1]
        if not (re.search(rf"\b{re.escape(leaf)}\s*:", es)
                and re.search(rf"\b{re.escape(leaf)}\s*:", en)):
            huerfanas.append(key)
    assert not huerfanas, f"claves i18n inexistentes en es/en: {huerfanas}"


def test_lo_que_solo_ve_un_administrador_esta_marcado_como_tal() -> None:
    """If the catalog is wrong here, the employee promises what cannot be opened.

    ``ADMIN_PREFIXES`` in Layout.tsx is the list the SPA uses to decide what
    belongs to the Administration area: it is the source it is cross-checked against.
    """
    from app.self_knowledge import SCREENS
    src = (FRONT / "components" / "Layout.tsx").read_text(encoding="utf-8")
    block = src.split("ADMIN_PREFIXES", 1)[1].split("]", 1)[0]
    prefixes = tuple(re.findall(r"'([^']+)'", block))
    assert prefixes, "no se ha podido leer ADMIN_PREFIXES de Layout.tsx"
    mal = [s.route for s in SCREENS
           if s.route.startswith(prefixes) and not s.admin_only]
    assert not mal, (
        "Estas pantallas son del área de Administración y el catálogo NO las marca "
        f"admin_only, así que se las ofrecerá a un member: {mal}"
    )


def test_las_herramientas_que_cita_el_catalogo_existen_de_verdad() -> None:
    """'I can do that myself' has to be true."""
    from app.mcp_server import TOOLS
    from app.self_knowledge import SCREENS
    inventadas = sorted({t for s in SCREENS for t in s.tools} - set(TOOLS))
    assert not inventadas, (
        "El catálogo dice que estas herramientas hacen por chat lo de una pantalla, "
        "pero no están registradas:\n  " + "\n  ".join(inventadas)
    )


def test_las_pantallas_de_gobierno_tienen_paridad_o_dicen_por_que_no() -> None:
    """Not a single mute screen: either the employee knows how to do it, or knows why it cannot.

    It is the less showy half of parity and the one that helps most: when something can
    only be done on the screen (an OAuth wizard, uploading a file), saying so
    turns an "I can't" into an "I'll take you there".
    """
    from app.self_knowledge import SCREENS
    mudas = [s.route for s in SCREENS
             if s.actions and not s.tools and not s.ui_only and not s.detail_of]
    assert not mudas, (
        "Estas pantallas permiten hacer cosas y el empleado no tiene ni herramienta "
        f"ni explicación de por qué no puede: {mudas}"
    )


def test_el_indice_del_priming_es_compacto() -> None:
    """The priming is already 8-12 kB and the sidecar prunes: the map cannot be the manual."""
    from app.self_knowledge import index_block
    block = index_block()
    assert "describe_app" in block, (
        "el índice debe decir cómo pedir el detalle, o el empleado se lo inventará"
    )
    assert len(block) < 4000, (
        f"el índice se ha ido a {len(block)} caracteres: el detalle va en "
        "describe_app, no en el prompt de cada sesión"
    )


def test_el_turno_de_chat_nombra_la_herramienta_y_el_rol() -> None:
    """What does not travel in every turn is lost at the first compaction.

    It already cost us two rounds with memory: the priming gets pruned, so the mention
    of `describe_app` and the asker's role have to go in the internal line
    of every turn.
    """
    src = (APP / "chat_prompt.py").read_text(encoding="utf-8")
    turn = src.split("def build_chat_turn_prompt", 1)[1].split("\ndef ", 1)[0]
    assert "describe_app" in turn
    assert "rol=" in turn

    routes = (APP / "routes" / "agent_routes.py").read_text(encoding="utf-8")
    assert "role=_role" in routes, "el turno no está pasando el rol de quien escribe"
    assert "index_block" in routes, "el priming no inyecta el mapa de la aplicación"


def test_el_sello_del_contrato_cambia_con_el_mapa_de_la_app() -> None:
    """An EVOLVE that renames a screen must reach the open sessions."""
    src = (APP / "memory_contract.py").read_text(encoding="utf-8")
    stamp = src.split("def prompt_contract_stamp", 1)[1].split("\ndef ", 1)[0]
    assert "index_block" in stamp


def test_el_detalle_de_ejecucion_deja_leer_el_prompt_completo() -> None:
    """Autonomous prompts are long and mix markdown: they must be possible to
    format, copy and maximise — not leave them in a 480px <pre>."""
    src = (FRONT / "pages" / "RunDetail.tsx").read_text(encoding="utf-8")
    assert "function PromptViewer" in src
    assert "viewFormatted" in src
    assert "FullscreenIcon" in src
    assert "remarkGfm" in src
    assert "pb: 6" in src, "sin margen inferior el final del prompt queda bajo el borde"


def test_la_pantalla_de_ayuda_lee_del_mismo_mapa_que_el_chat() -> None:
    """`GET /api/agent/app-map` cannot have its own text.

    If the UI help kept its own copy of the map, we would have two truths and the
    one that goes stale is always the one nobody looks at. It is also the surface that
    allows validating against the DEPLOYED employee —with its domain screens— without
    spending a token, so it has to keep existing and keep filtering by role.
    """
    src = (APP / "routes" / "manifest_routes.py").read_text(encoding="utf-8")
    assert '"/app-map"' in src, "la superficie del mapa de la app ha desaparecido"
    body = src.split('@router.get("/app-map")', 1)[1].split("\n@router.", 1)[0]
    assert "self_knowledge.describe" in body, (
        "el endpoint del mapa no reutiliza describe(): sería una segunda descripción "
        "de la app, condenada a desfasarse de la que usa el chat"
    )
    assert "is_admin" in body and "Actor(" in body, (
        "el mapa no se está filtrando por quién pregunta: un member vería explicadas "
        "las pantallas de administración"
    )
