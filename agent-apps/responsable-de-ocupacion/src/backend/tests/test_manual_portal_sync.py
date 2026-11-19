"""The employee-portal manual and the screen catalog must not drift apart.

The portal (`wai_marketplace/employee-portal/.../manualContent.ts`) is the human prose
of the same map the chat uses via `self_knowledge` + `describe_app`. Without a guard,
the menu gains a screen and the manual keeps describing the previous world (as happened
with users «Sincronizar» and «solo admin aprueba»).

This test does NOT validate the editorial text — that belongs to people —; it validates the
STRUCTURE: every chapter listed in `CHAPTER_SCREEN_SYNC` exists, it covers all chapters, and
every menu route of the skill has a chapter. That way synchronization is a red, not a
promise.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

# The portal manual lives in the MONOREPO, not inside the generated app. In the
# skill tree the depth to the root is ~9; in an evolving employee (short path like
# /workspace/backend/tests/…) `parents[8]` blew up while *collecting* the module and
# the strong verifier blocked the seal — a defect of the test, not of the app. We
# look for the file walking upwards; if it is not there (generated app / partial
# clone), the cases skip.
def _find_manual() -> Path | None:
    marker = Path("wai_marketplace") / "employee-portal" / "src" / "pages" / "public" / "manual" / "manualContent.ts"
    here = Path(__file__).resolve()
    for root in (here, *here.parents):
        cand = root / marker
        if cand.is_file():
            return cand
    return None


_MANUAL = _find_manual()
_SELF = Path(__file__).resolve().parents[1] / "app" / "self_knowledge.py"
_LAYOUT = (
    Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "Layout.tsx"
)
_HAS_MANUAL = _MANUAL is not None and _MANUAL.is_file()


def _chapter_ids(src: str) -> list[str]:
    return re.findall(r"^\s+id:\s*'([^']+)'", src, flags=re.M)


def _sync_entries(src: str) -> list[dict[str, str | bool | None]]:
    """Parses the CHAPTER_SCREEN_SYNC array without running TypeScript."""
    block = src.split("export const CHAPTER_SCREEN_SYNC", 1)
    if len(block) < 2:
        return []
    body = block[1].split("];", 1)[0]
    out: list[dict[str, str | bool | None]] = []
    for m in re.finditer(
        r"chapterId:\s*'([^']+)'\s*,\s*route:\s*(null|'[^']*')(?:\s*,\s*adminOnly:\s*(true))?",
        body,
    ):
        route_raw = m.group(2)
        out.append({
            "chapterId": m.group(1),
            "route": None if route_raw == "null" else route_raw.strip("'"),
            "adminOnly": bool(m.group(3)),
        })
    return out


def _menu_routes_from_layout(src: str) -> set[str]:
    """Fixed menu routes in Layout.tsx (not the domain ones, which are dynamic)."""
    # Only those in the typed navigation groups: `to: '/…'`
    return set(re.findall(r"\bto:\s*'(/[^']*)'", src))


def _screen_routes_with_nav(src: str) -> dict[str, bool]:
    """``{route: admin_only}`` of the screens with ``nav_key`` in self_knowledge."""
    # Each Screen(… route="…" … admin_only=True? … nav_key="…" …)
    chunks = re.split(r"\n\s*Screen\(", src)
    out: dict[str, bool] = {}
    for chunk in chunks[1:]:
        route_m = re.search(r'route\s*=\s*"([^"]+)"', chunk)
        nav_m = re.search(r"nav_key\s*=\s*\"([^\"]+)\"", chunk)
        if not route_m or not nav_m:
            continue
        admin = bool(re.search(r"admin_only\s*=\s*True", chunk.split(")", 1)[0]))
        out[route_m.group(1)] = admin
    return out


@pytest.mark.skipif(not _HAS_MANUAL, reason="employee-portal no está en este clon")
def test_todo_capitulo_esta_en_el_mapa_de_sync() -> None:
    assert _MANUAL is not None
    src = _MANUAL.read_text(encoding="utf-8")
    chapters = set(_chapter_ids(src))
    sync = {e["chapterId"] for e in _sync_entries(src)}
    assert chapters, "el manual no declara capítulos"
    assert sync, "falta CHAPTER_SCREEN_SYNC: sin él el manual no se puede sincronizar"
    faltan = sorted(chapters - sync)
    fantasma = sorted(sync - chapters)
    assert not faltan, (
        f"capítulos del manual sin entrada en CHAPTER_SCREEN_SYNC: {faltan} — "
        "al añadir un capítulo hay que enlazarlo a su ruta de la app"
    )
    assert not fantasma, (
        f"CHAPTER_SCREEN_SYNC cita capítulos que ya no existen: {fantasma}"
    )


@pytest.mark.skipif(not _HAS_MANUAL, reason="employee-portal no está en este clon")
def test_cada_pantalla_del_menu_tiene_capitulo() -> None:
    """If the menu gains an entry, the manual cannot stay silent."""
    assert _MANUAL is not None
    manual = _MANUAL.read_text(encoding="utf-8")
    layout = _LAYOUT.read_text(encoding="utf-8")
    self_src = _SELF.read_text(encoding="utf-8")

    sync_routes = {
        e["route"] for e in _sync_entries(manual)
        if e["route"] and e["route"] != "/domain/:slug"
    }
    menu = _menu_routes_from_layout(layout)
    # /domain/:slug is dynamic; it does not require a 1:1 chapter.
    menu -= {r for r in menu if r.startswith("/domain/")}
    # Details (:id) are not chapters of their own.
    menu = {r for r in menu if ":id" not in r and "/:" not in r}

    nav_screens = _screen_routes_with_nav(self_src)
    # We only require coverage of what is IN THE MENU (nav_key).
    expected = (menu & set(nav_screens.keys())) | (menu - set(nav_screens.keys()))
    # We prefer the catalog: if Layout and self_knowledge disagree, the other test
    # (test_self_knowledge) already catches it; here we cross-check with the real menu.
    expected = menu

    faltan = sorted(expected - sync_routes)
    assert not faltan, (
        f"el menú de la app tiene pantallas sin capítulo en el manual del portal: "
        f"{faltan}. Añade el capítulo (o una entrada en CHAPTER_SCREEN_SYNC) — "
        "si no, el operador lee una guía que ya no es la app"
    )


@pytest.mark.skipif(not _HAS_MANUAL, reason="employee-portal no está en este clon")
def test_la_marca_admin_del_manual_cuadra_con_el_catalogo() -> None:
    assert _MANUAL is not None
    manual = _MANUAL.read_text(encoding="utf-8")
    self_src = _SELF.read_text(encoding="utf-8")
    nav = _screen_routes_with_nav(self_src)
    mal = []
    for e in _sync_entries(manual):
        route = e["route"]
        if not route or route == "/domain/:slug" or route not in nav:
            continue
        if bool(e["adminOnly"]) != nav[route]:
            mal.append((e["chapterId"], route, e["adminOnly"], nav[route]))
    assert not mal, (
        "CHAPTER_SCREEN_SYNC.adminOnly no cuadra con self_knowledge: "
        f"{mal} (capítulo, ruta, manual, catálogo)"
    )


@pytest.mark.skipif(not _HAS_MANUAL, reason="employee-portal no está en este clon")
def test_el_manual_ya_no_promete_sincronizar_usuarios() -> None:
    """Content regression: the project mirror was retired in 7.145.0."""
    assert _MANUAL is not None
    src = _MANUAL.read_text(encoding="utf-8")
    # Only the users chapter (avoids false positives in other mentions).
    users = src.split("id: 'usuarios'", 1)[1].split("id: 'configuracion'", 1)[0]
    # Mentioning the button to say it NO LONGER exists is fine; what cannot come
    # back is presenting it as a current action (`y “Sincronizar” vuelve a…`).
    assert "vuelve a traer" not in users and "re-imports the project" not in users, (
        "el capítulo Usuarios vuelve a presentar la sincronización como acción "
        "vigente: ese botón ya no existe"
    )
    assert "nace solo con su administrador" in users or "born with only its administrator" in users
    assert "puede aprobar" in users.lower() or "can approve" in users.lower()
    assert "Alta en lote" in users or "Bulk add" in users
