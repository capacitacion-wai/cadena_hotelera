"""A `select()` and its loop cannot silently drift out of sync.

Where it comes from: when adding the `voz` concept one more column went into the `select()` of
`consumption()` and an EARLIER loop kept unpacking the old number of columns.
The result was a **500 on the whole Consumption page** —not one value fewer: the
whole query down— and nothing caught it, because the failure only shows at runtime and
with rows involved.

It is a STATIC test on purpose, like the write-queue one: it checks the SHAPE.
A loop that unpacks a fixed number of columns over `rows` is a time bomb
for the next person who touches that `select`; with `*_` at the end it tolerates new
columns and keeps reading the ones it cares about.
"""
from __future__ import annotations

import re
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"

# `for a, b, c in rows:` — fixed unpacking, no remainder.
FIJO = re.compile(r"^\s*for\s+([\w_]+(?:\s*,\s*[\w_]+){2,})\s+in\s+rows\s*:", re.M)


def _sin_resto(firma: str) -> bool:
    return not firma.rstrip().endswith("_") or "*" not in firma


def test_los_bucles_sobre_rows_toleran_columnas_nuevas():
    """Every loop over `rows` with three or more names must end in `*_`.

    The reasonable exception would be a single-function `select`, used in a single
    place; but telling it apart here requires following the flow, and the cost of adding `*_` is
    zero. If it ever gets in the way, the conversation is "why can this select not
    grow", which is exactly the one to have."""
    malos: list[str] = []
    for f in sorted(APP.rglob("*.py")):
        for m in FIJO.finditer(f.read_text(encoding="utf-8")):
            firma = m.group(1)
            if "*" in firma:
                continue
            linea = f.read_text(encoding="utf-8")[:m.start()].count("\n") + 1
            malos.append(f"{f.relative_to(APP.parent)}:{linea} → for {firma} in rows:")
    assert not malos, (
        "Estos bucles se romperán cuando alguien añada una columna a su `select()`, y el "
        "síntoma será un 500 entero, no un dato de menos. Acaba la firma en `*_`:\n  "
        + "\n  ".join(malos)
    )
