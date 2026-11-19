"""The TEST RUN mark: what a test writes gets swept (2.0.0).

An agent is tested by letting it write. What it creates in a test has to be deletable afterwards, also what nobody
foresaw («create a test incident»). Two obligations for every connector that WRITES — the verifier checks both:

1. Tag what you create with the run mark (`X-WAI-Test-Run`): `con_marca(datos, headers)` in every operation that
   CREATES.
2. Expose `sweep_test_run(test_run_id)`: delete everything tagged with that mark. Call `marca_valida(test_run_id)`
   FIRST: a sweep never deletes without a valid run mark (a mass delete one empty argument away from disaster).

The platform calls `sweep_test_run` itself when a test run finishes, with the SAME headers as the run's calls (for a
connector to a WAI application, the signed `Authorization` of whoever ran the tests: forward it like any other
call). The agent never sees it: the platform denies it in the agent's permissions.

The mark NEVER changes permissions nor skips checks: it only tags.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Mapping

#: The header that says the call comes from a test run, and with which mark.
CABECERA = "X-WAI-Test-Run"
#: What a run mark looks like: `prueba-a<agent>v<version>-<n>` (the platform generates it).
_MARCA = re.compile(r"prueba-a\d+v\d+-\d+")


def marca_de_pruebas(headers: Mapping[str, Any]) -> str:
    """The run mark of this call, or "" if it does not come from a test run."""
    return next((str(v) for k, v in (headers or {}).items() if str(k).lower() == CABECERA.lower() and v), "")


def con_marca(datos: Dict[str, Any], headers: Mapping[str, Any], campo: str = "origen") -> Dict[str, Any]:
    """`datos` with the run mark in `campo` when the call comes from a test run (as-is otherwise)."""
    marca = marca_de_pruebas(headers)
    return {**datos, campo: marca} if marca else dict(datos)


def marca_valida(test_run_id: Any) -> str:
    """The mark to sweep, or ValueError if it is not a run mark: the sweep never deletes without it."""
    marca = str(test_run_id or "").strip()
    if not _MARCA.fullmatch(marca):
        raise ValueError("sweep_test_run needs a test run mark (prueba-a<agent>v<version>-<n>); nothing was deleted.")
    return marca
