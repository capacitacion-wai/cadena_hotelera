"""The variables the code interpolates exist in the translated string (7.166.0).

What showed up on screen: **`Válido hasta {{date}}`**, braces and all. The code
passed `when` and the resource said `{{date}}`, so i18next had nothing to substitute and
painted the placeholder raw.

What makes it especially treacherous here: the `i18n/*.ts` files are **SCAFFOLD**,
not INVARIANT. `--fix-invariants` does not restore them, so an already generated employee
keeps its old `es.ts`. If someone renames the interpolation variable without renaming the
KEY, that app's old resource wins over the code's `defaultValue` and the text breaks
only in existing employees, never in the repo, where both files travel together.

Hence the two rules:

  1. Every variable the code passes to `t()` must appear in the resource string.
  2. When changing a key's variables, change the KEY. A new key has no old resource
     to hijack it, so the code's `defaultValue` applies.
"""
from __future__ import annotations

import pathlib
import re

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "src"

# `t('a.b.c', { defaultValue: '…', var: … })`: only the form with an options object,
# which is the only one that interpolates.
_LLAMADA = re.compile(
    r"t\(\s*'([a-zA-Z0-9_.]+)'\s*,\s*\{(.*?)\}\s*\)", re.S)
_RESERVADAS = {"defaultValue", "count", "context", "ns", "replace", "lng"}


def _recursos(locale: str) -> dict[str, str]:
    """FULL PATH (`operations.automations.next.label`) → string.

    The leaf alone is not enough: `label`, `summary`, `showing` or `when` repeat across
    several sections of the file, and comparing against the first one that appears invents
    failures that do not exist. The nested object is walked keeping the stack of open keys.
    """
    txt = (_SRC / "i18n" / f"{locale}.ts").read_text(encoding="utf-8")
    out: dict[str, str] = {}
    pila: list[str] = []
    abre = re.compile(r"^\s*([a-zA-Z0-9_]+):\s*\{\s*$")
    hoja = re.compile(r"^\s*([a-zA-Z0-9_]+):\s*'((?:[^'\\]|\\.)*)',?\s*$")
    for linea in txt.splitlines():
        m = abre.match(linea)
        if m:
            pila.append(m.group(1))
            continue
        m = hoja.match(linea)
        if m:
            out[".".join(pila + [m.group(1)])] = m.group(2)
            continue
        if re.match(r"^\s*\},?\s*$", linea) and pila:
            pila.pop()
    return out


def _llamadas():
    for tsx in _SRC.rglob("*.tsx"):
        txt = tsx.read_text(encoding="utf-8")
        for m in _LLAMADA.finditer(txt):
            clave, cuerpo = m.group(1), m.group(2)
            if "defaultValue" not in cuerpo:
                continue
            defecto = re.search(r"defaultValue:\s*'((?:[^'\\]|\\.)*)'", cuerpo)
            # Variable names are read from the body WITHOUT the strings: the text
            # contains colons (`Próximo disparo: {{when}}`) and without stripping them
            # the test invented a variable called `disparo`.
            sin_cadenas = re.sub(r"'(?:[^'\\]|\\.)*'", "''", cuerpo)
            sin_cadenas = re.sub(r"`(?:[^`\\]|\\.)*`", "``", sin_cadenas)
            # Two forms: `nombre: valor` and the SHORTHAND `{ name, reason }`, which is
            # the most used here. Without the second one the test reported as unfilled
            # placeholders variables that were in fact passed.
            variables = {
                # The delimiter goes in a LOOKAHEAD: if it is consumed, `{ from, to, total }`
                # only catches "from" and "total", since the comma closing one is the one
                # opening the next. That detail produced cascading false positives.
                v for v in re.findall(r"(?:^|[,{])\s*(\w+)\s*(?=[,:}])", sin_cadenas + "}")
                if v not in _RESERVADAS
            }
            yield tsx.name, clave, variables, (defecto.group(1) if defecto else "")


@pytest.mark.parametrize("locale", ["es", "en"])
def test_toda_variable_interpolada_existe_en_la_cadena(locale):
    res = _recursos(locale)
    rotas = []
    for fichero, clave, variables, defecto in _llamadas():
        if not variables:
            continue
        cadena = res.get(clave)
        if cadena is None:
            continue  # no resource: the code's defaultValue rules
        # Direction matters: passing an EXTRA variable is harmless (i18next ignores it).
        # What gets painted raw is a placeholder in the STRING that nobody fills, which is
        # exactly what was seen: `Válido hasta {{date}}`.
        for hueco in set(re.findall(r"\{\{\s*(\w+)\s*\}\}", cadena)):
            if hueco not in variables and hueco not in _RESERVADAS:
                rotas.append(
                    f"{fichero}: {locale}.ts dice «{cadena}» y {clave} NO pasa «{hueco}»")
    assert not rotas, (
        "El código interpola variables que la cadena no tiene: se pintará el hueco en "
        "crudo, con las llaves. Y como los i18n son SCAFFOLD, al cambiar las variables "
        "hay que cambiar la CLAVE o los empleados ya generados seguirán con la vieja:\n  "
        + "\n  ".join(rotas))


def test_el_defaultValue_usa_las_mismas_variables_que_se_pasan():
    """If the code's fallback does not use them, the app without a translation breaks too."""
    rotas = []
    for fichero, clave, variables, defecto in _llamadas():
        if not defecto:
            continue
        for hueco in set(re.findall(r"\{\{\s*(\w+)\s*\}\}", defecto)):
            if hueco not in variables and hueco not in _RESERVADAS:
                rotas.append(
                    f"{fichero}: {clave} tiene «{{{{{hueco}}}}}» en su defaultValue y no lo pasa")
    assert not rotas, (
        "Un hueco del respaldo del código que nadie rellena se pinta en crudo en "
        "cualquier app sin traducción para esa clave:\n  " + "\n  ".join(rotas))


def test_la_clave_que_ya_fallo_no_vuelve():
    """`validUntil` was left with `{{date}}` in the deployed bundles; the new key is
    `validUntilOn`. Going back to the old one would reopen the bug in every existing employee."""
    visor = (_SRC / "components" / "LearningViewer.tsx").read_text(encoding="utf-8")
    assert "learning.validUntilOn" in visor
    assert "learning.validUntil'" not in visor
