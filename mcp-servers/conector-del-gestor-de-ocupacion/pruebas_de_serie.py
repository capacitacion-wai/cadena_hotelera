"""BUILT-IN TESTING of a connector to a WAI APPLICATION: three platform tools, the same in every connector.

Copied as is (`credential_mode: wai_app` only) and installed in `server.py`, after `IMPLEMENTACIONES`:

    import pruebas_de_serie
    pruebas_de_serie.instalar(TOOLS, IMPLEMENTACIONES)

Every WAI application with its own database ships the other half (wai-app-builder `pruebas_de_serie.py`): seed one
of its sample datasets on the fly, mark every row created under a test run, sweep by the mark. These tools are that
half seen from the connector:

* `list_test_datasets()` — which sample datasets the application can seed.
* `seed_test_data(conjunto)` — seeds that dataset ON TOP of the real data, every row with the run's mark (the
  `X-WAI-Test-Run` header the platform sends); answers what it created and `equivalencias`, the new id of each id
  the dataset writes by hand (`{"convocatorias": {"CONV-1": "CONV-1-3f9a2c"}}`). Without a mark it seeds nothing.
* `sweep_test_run(test_run_id)` — deletes everything with that mark: what was seeded and what the agent wrote.

The PLATFORM calls them (an agent's test plan in `preparar`, the connector's real-run tests, the cleanup when a run
ends); the agent never sees them — the platform denies them in every agent's permissions. The mark only labels:
`app_wai` forwards it on every call, so what the agent writes through this connector is marked too, with nothing to
add in your tools.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List

import app_wai
import pruebas

HERRAMIENTAS_DE_SERIE: List[Dict[str, Any]] = [
    {
        "name": "list_test_datasets",
        "description": "Platform tool: the sample datasets the application can seed for a test run.",
        "annotations": {"readOnlyHint": True},
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "seed_test_data",
        "description": ("Platform tool: seeds one of the application's sample datasets for a test run, marked with "
                        "the run (X-WAI-Test-Run); answers what it created and the new id of each dataset id."),
        "annotations": {"readOnlyHint": False},
        "inputSchema": {"type": "object",
                        "properties": {"conjunto": {"type": "string", "description": "Dataset id (e.g. base)"}},
                        "required": ["conjunto"]},
    },
    {
        "name": "sweep_test_run",
        "description": "Platform tool: deletes everything a test run created in the application (by its mark).",
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
        "inputSchema": {"type": "object",
                        "properties": {"test_run_id": {"type": "string", "description": "The run's mark"}},
                        "required": ["test_run_id"]},
    },
]


async def list_test_datasets(cabeceras: dict) -> Any:
    return await app_wai.llamar(cabeceras, "/api/pruebas/conjuntos")


async def seed_test_data(conjunto: str, cabeceras: dict) -> Any:
    pruebas.marca_valida(pruebas.marca_de_pruebas(cabeceras))     # never an unmarked seed in a real application
    return await app_wai.llamar(cabeceras, f"/api/pruebas/conjuntos/{conjunto}", "POST")


async def sweep_test_run(test_run_id: str, cabeceras: dict) -> Any:
    marca = pruebas.marca_valida(test_run_id)                      # FIRST: never a delete without a valid mark
    return await app_wai.llamar(cabeceras, "/api/pruebas", "DELETE", params={"origen": marca})


IMPLEMENTACIONES_DE_SERIE: Dict[str, Callable] = {
    "list_test_datasets": list_test_datasets,
    "seed_test_data": seed_test_data,
    "sweep_test_run": sweep_test_run,
}


def instalar(tools: List[Dict[str, Any]], implementaciones: Dict[str, Callable]) -> None:
    """Adds the three tools to the connector's, replacing an own version of them (a connector from before)."""
    tools[:] = [t for t in tools if t.get("name") not in IMPLEMENTACIONES_DE_SERIE] + HERRAMIENTAS_DE_SERIE
    implementaciones.update(IMPLEMENTACIONES_DE_SERIE)
