"""No new writer may stay OUTSIDE the write queue.

Incident INC-VIG-59 did not come from a one-off oversight: it came from a
PARTIAL fix. 7.139.4 created the queue (`app_write`) and wired it only into the executor;
the other writers kept going straight to SQLite and one autonomous round was enough
for the autonomy brake to die with «database is locked». A comment
in the docstring does not stop the next writer from repeating the pattern — this test does.

It is a STATIC test on purpose: the pathology only shows up under real
concurrency, so waiting for a dynamic test to catch it is waiting for a customer to
suffer it. Here we check the shape of the code: if an `app_session()` block
commits, it is a write and it belongs in the queue. Reasoned exceptions are declared
in the code itself with the marker below, so the decision lives next to the
line that needs it and not in a separate list that goes stale.
"""
from __future__ import annotations

import re
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"

OPEN_RE = re.compile(r"^(\s*)async with app_session\(\) as (\w+):\s*$")

# Marker that declares a DELIBERATE exception in the code (with its reason next to it).
OPT_OUT = "DELIBERATELY NOT ``app_write``"

# Serialising a block that does slow I/O would be worse than the disease: it would hold the
# write turn during a network call. Those blocks are left out by design.
SLOW_IO = ("httpx.", "aiohttp", "AsyncClient", "subprocess", "asyncio.sleep",
           "run_agent_session", "await client.", "requests.", "opencode")


def _block_end(lines: list[str], start: int, indent: int) -> int:
    i, last = start + 1, start
    while i < len(lines):
        if lines[i].strip():
            cur = len(lines[i]) - len(lines[i].lstrip())
            if cur <= indent:
                break
            last = i
        i += 1
    return last


def _offenders() -> list[str]:
    out: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        if path.name == "app_database.py":
            continue  # it is the one that DEFINES the queue
        lines = path.read_text(encoding="utf-8").splitlines()
        for idx, line in enumerate(lines):
            m = OPEN_RE.match(line)
            if not m:
                continue
            indent, var = m.group(1), m.group(2)
            body = "\n".join(lines[idx + 1:_block_end(lines, idx, len(indent)) + 1])
            if f"{var}.commit()" not in body:
                continue  # read-only: correct outside the queue
            preamble = "\n".join(lines[max(0, idx - 12):idx])
            if OPT_OUT in preamble or any(s in body for s in SLOW_IO):
                continue
            out.append(f"{path.relative_to(APP.parent)}:{idx + 1}")
    return out


def test_toda_escritura_pasa_por_la_cola_serializada() -> None:
    offenders = _offenders()
    assert not offenders, (
        "Estos bloques ESCRIBEN con app_session (fuera de la cola) y pueden morir con "
        "'database is locked' bajo una ronda autónoma — usa app_write, o declara la "
        f"excepción con el marcador «{OPT_OUT}» explicando el porqué:\n  "
        + "\n  ".join(offenders)
    )


def test_el_freno_de_la_autonomia_reintenta_y_no_devuelve_500() -> None:
    """The switch is the write that CANNOT be lost: it requires queue + retry.

    It is checked in the code because its failure mode (losing the race for the
    lock) does not reproduce in a normal request: in a smoke test the endpoint
    answers 200 and looks correct precisely when it is not.
    """
    src = (APP / "routes" / "manifest_routes.py").read_text(encoding="utf-8")
    upsert = src.split("async def _upsert_setting", 1)[1].split("\nasync def ", 1)[0]
    assert "app_write()" in upsert, "el upsert de ajustes debe entrar en la cola"
    assert "write_with_retry" in upsert, "el upsert de ajustes debe reintentar"

    main = (APP / "main.py").read_text(encoding="utf-8")
    assert "exception_handler(WriteBusy)" in main, (
        "sin manejador de WriteBusy, una escritura saturada se le presenta al "
        "operador como un 500 opaco en vez de un 503 «reinténtalo»"
    )


def test_el_reclamo_de_eventos_comparte_la_MISMA_cola() -> None:
    """The noisiest writer (8 workers) uses a raw connection: it must go through app_write_conn.

    Two different queues do not serialise with each other, so SQLite would see two
    writers again and the problem would reappear with another face.
    """
    src = (APP / "autonomous" / "event_machine.py").read_text(encoding="utf-8")
    assert "app_write_conn" in src, "el reclamo de eventos debe usar la cola de escritura"
    assert "app_engine.connect()" not in src, (
        "el reclamo abría su propia conexión al margen de la cola"
    )


def test_las_lecturas_calientes_no_ocupan_el_turno_de_escritura() -> None:
    """The executor's polling (4 times/second) must not queue READS.

    A read in the queue adds no safety (under WAL readers are
    concurrent) and puts a non-writer ahead of the ones that do write.
    """
    src = (APP / "autonomous" / "executor.py").read_text(encoding="utf-8")
    budget = src.split("if DAILY_BUDGET_USD > 0:", 1)[1][:400]
    assert "app_session()" in budget and "app_write()" not in budget, (
        "el escaneo de gasto de 24 h debe leer sin coger el turno de escritura"
    )


# ── And the SYMMETRIC rule: nothing slow INSIDE the queue ────────────────────
#
# The one above stops a writer from staying outside. This one stops the opposite, which
# turned out just as costly: putting SLOW I/O inside the lock. `app_database` says so in
# its docstring —«do slow I/O (network, file downloads, indexing) BEFORE/AFTER it,
# never inside, or you serialize every other writer behind that I/O»— and even so
# `_finish_run` called `report_run_cost`, which does a POST to the platform plus a
# drain of the pending ones, inside the block.
#
# Measured in the Odoo employee on 16/09/2026: ten lock holds of
# **exactly 15.1 s** in 115 minutes. The constancy is the clue — that is a network
# timeout expiring, not database work. With every writer queued behind it, the
# pool and the loop saturate, `/api/health` stops answering in time, the pod drops out
# of the Service and nginx returns 503: the employee disappears for ALL its users
# for minutes. INC-AGO-101 uncovered it, failing because of that 503 while having nothing
# to do with what it measures — the worst kind of red, the one that blames the wrong place.
#
# MIND THE DESIGN OF THIS TEST. The first version only looked at the LITERAL lines
# of the block and passed green on the sick code: the call was one level
# further down, inside `_finish_run`. A guard that does not catch the case that motivates it
# is worse than not having one, because it gives permission. That is why it goes ONE LEVEL down: it resolves the
# package's own functions called inside the block and also looks at their
# bodies. Two levels would already be guessing; one covers the real shape of the failure, and the
# live warning in `report_run_cost` covers the rest.

_LENTAS = (
    "report_run_cost(",          # POST to the platform + drain of pending ones
    "httpx.AsyncClient(",        # any new HTTP client
    "flush_pending_cost_reports(",
)


def _cuerpo_desde(lineas, i):
    """The lines of the block/function starting at `i` (by indentation)."""
    sangria = len(lineas[i]) - len(lineas[i].lstrip())
    cuerpo = []
    for j in range(i + 1, len(lineas)):
        s = lineas[j]
        if s.strip() and (len(s) - len(s.lstrip())) <= sangria:
            break
        cuerpo.append((j + 1, s))
    return cuerpo


def _funciones_del_paquete():
    """name -> [(line, text)] of every `def`/`async def` under app/."""
    fuera = {}
    for f in sorted(APP.rglob("*.py")):
        lineas = f.read_text(encoding="utf-8", errors="ignore").split("\n")
        for i, l in enumerate(lineas):
            m = re.match(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(", l)
            if m:
                fuera.setdefault(m.group(1), []).append((f, _cuerpo_desde(lineas, i)))
    return fuera


def _ofensas_en(cuerpo, fichero, origen):
    out = []
    for n, linea in cuerpo:
        if linea.lstrip().startswith("#"):
            continue
        for pista in _LENTAS:
            if pista in linea and "def " not in linea:
                out.append(f"{fichero}:{n} — `{pista.rstrip('(')}` {origen}")
    return out


def test_ninguna_llamada_de_red_dentro_del_candado_de_escritura():
    funciones = _funciones_del_paquete()
    ofensas = []
    for f in sorted(APP.rglob("*.py")):
        lineas = f.read_text(encoding="utf-8", errors="ignore").split("\n")
        rel = f.relative_to(APP.parent)
        for i, l in enumerate(lineas):
            if not re.match(r"^\s*async with app_write(_conn)?\(", l):
                continue
            cuerpo = _cuerpo_desde(lineas, i)
            ofensas += _ofensas_en(cuerpo, rel, f"dentro del `app_write` de la línea {i+1}")
            # ONE LEVEL DOWN: the package's own functions called in there.
            for n, linea in cuerpo:
                for nombre in re.findall(r"await\s+(?:\w+\.)?(\w+)\s*\(", linea):
                    for ff, ccuerpo in funciones.get(nombre, []):
                        ofensas += _ofensas_en(
                            ccuerpo, ff.relative_to(APP.parent),
                            f"dentro de `{nombre}()`, que se llama desde el "
                            f"`app_write` de {rel}:{i+1}")
    assert not ofensas, (
        "I/O lenta dentro del candado de escritura: mientras dure, TODOS los demás "
        "escritores esperan, el pool se satura y la sonda de disponibilidad saca al "
        "pod del Service (nginx 503). Haz la llamada DESPUÉS de cerrar el bloque.\n  - "
        + "\n  - ".join(sorted(set(ofensas))))


def test_el_reportador_de_coste_denuncia_si_lo_llaman_dentro_del_candado():
    """The safety net for the future: even if the test above does not reach it (two levels, a dynamic
    import), `report_run_cost` itself warns in the log."""
    fuente = (APP / "cost_reporter.py").read_text(encoding="utf-8")
    assert "_in_write" in fuente, (
        "`report_run_cost` ya no comprueba si lo llaman con el candado cogido. Ese "
        "aviso es la última red: sin él, un POST lento vuelve a sacar al pod del "
        "Service y nadie sabrá por qué.")
