"""A turn's account: where the time went, in a single log line.

A turn that takes 41 seconds is not diagnosed with the total. You need to know how long
the model was thinking, how long waiting on tools, how many it called, which ones were
expensive, how long it took to say the first word and at what reasoning level it was
running. That is what is gathered here and emitted as ONE JSON line (`event: "turno"`),
which is the format promtail already promotes: it can be filtered, sorted and compared
without opening the application.

Three decisions:

* **Measuring cannot cost a turn.** Nothing here decides anything and none of its
  failures propagates: if a timestamp is missing, that field comes out empty and the
  turn goes on. It is the same exception as `tool_usage.py`.
* **Tool time is the UNION of its intervals, not the sum.** The model can launch
  several at once; summing them gave "38 s of tools" in a 12 s turn and with that the
  breakdown lied exactly where it matters.
* **Model time is a SUBTRACTION, and it is said.** There is no timestamp inside the
  provider: `ms_modelo = ms_total - ms_herramientas` includes the network and our own
  process. It serves to compare turns with each other, not to blame the model for a
  millisecond.
"""
from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# How many distinct tools are detailed in the line. A pathological turn can call
# forty; the log line would stop being readable and what matters (the expensive
# ones) is always at the top.
_TOPE_HERRAMIENTAS = 15


def _ms(desde: float, hasta: float | None = None) -> int:
    # `round` and not `int`: truncating lost up to a millisecond per interval and in a turn
    # with twenty calls that is already a breakdown that does not add up to its own total.
    return round(((hasta if hasta is not None else time.monotonic()) - desde) * 1000)


def _union_ms(intervalos: list[tuple[float, float]]) -> int:
    """Milliseconds covered by the intervals, counting the overlap ONCE."""
    if not intervalos:
        return 0
    total = 0.0
    orden = sorted(intervalos)
    ini, fin = orden[0]
    for a, b in orden[1:]:
        if a > fin:
            total += fin - ini
            ini, fin = a, b
        elif b > fin:
            fin = b
    total += fin - ini
    return round(total * 1000)


class MedicionDelTurno:
    """Accumulates what happens in a chat turn. It is fed the stream's events and at
    the end it produces the summary (and writes it to the log)."""

    def __init__(self, *, run_id: int | None = None, hilo: str | None = None,
                 origen: str = "chat", modelo: str = "", nivel: str = "",
                 esfuerzo: str = "", caracteres_pedidos: int = 0,
                 lo_puso: str = "", longitud: str = "", canal: str = "") -> None:
        self.t0 = time.monotonic()
        self.run_id = run_id
        self.hilo = hilo or ""
        self.origen = origen
        self.modelo = modelo or ""
        self.nivel = nivel or ""
        self.esfuerzo = esfuerzo or ""
        self.caracteres_pedidos = int(caracteres_pedidos or 0)
        # WHO set the level (persona | su_defecto | empleado | conversador | modelo).
        # Without this, letting the conversational layer choose it would be one model
        # silently deciding how much another model thinks: a weaker answer and nothing saying so.
        self.lo_puso = lo_puso or ""
        self.longitud = longitud or ""
        self.canal = canal or ""
        # Whether it was answered by SHORTCUT —without starting the agent— and with which
        # capability. A shortcut that fires where it should not answers something else; this makes it visible.
        self.atajo = ""
        self.atajo_ms = 0
        # What the written reception decided: contesto | pregunto | atajo | delego. Empty =
        # it did not run (voice, attachment, or off). It is what lets you see over a week how
        # many messages are resolved without the agent and how many seconds it costs the rest.
        self.recepcion = ""

        self.ms_primera_senal: int | None = None   # the first thing that arrives (reasoning or writing)
        self.ms_primer_texto: int | None = None    # the first word of the answer
        self.ms_primera_herramienta: int | None = None
        self.pasos = 0
        self.ms_por_paso: list[int] = []
        self._paso_abierto = self.t0

        self._abiertas: dict[str, tuple[str, float]] = {}      # call_id → (nombre, t)
        self._intervalos: list[tuple[float, float]] = []
        self._por_herramienta: dict[str, dict[str, Any]] = {}  # nombre → {n, ms, fallos}

        self.tokens = {"entrada": 0, "salida": 0, "razonamiento": 0,
                       "cache_lectura": 0, "cache_escritura": 0}
        self.coste = 0.0
        self.caracteres_respuesta = 0
        self.reintentos = 0
        self.contexto: dict | None = None

    # ── what happens along the way ─────────────────────────────────
    def senal(self) -> None:
        """Something arrived from the model (text or reasoning). Marks the "it is alive"."""
        if self.ms_primera_senal is None:
            self.ms_primera_senal = _ms(self.t0)

    def texto(self, cuantos: int = 0) -> None:
        self.senal()
        if self.ms_primer_texto is None:
            self.ms_primer_texto = _ms(self.t0)
        self.caracteres_respuesta += int(cuantos or 0)

    def _llave(self, call_id: str, nombre: str) -> str:
        """The key of ONE invocation: the one opencode gives, and failing that, the name.

        Falling back to the name means two simultaneous calls to the SAME tool are counted
        as one. It is deliberate: the alternative —a different key per event— opened an
        interval for every re-emission of the same part and none closed, and with that
        "tool time" came to mean "from the first call to the end of the turn". A figure that
        falls short can be read; one that measures something else, cannot."""
        return (call_id or "").strip() or f"tool:{nombre or 'tool'}"

    def herramienta_empieza(self, call_id: str, nombre: str) -> None:
        llave = self._llave(call_id, nombre)
        if llave in self._abiertas:
            return  # opencode re-emits the same part as it progresses: only once
        if self.ms_primera_herramienta is None:
            self.ms_primera_herramienta = _ms(self.t0)
        self._abiertas[llave] = (nombre or "tool", time.monotonic())

    def herramienta_termina(self, call_id: str, nombre: str, ok: bool = True) -> None:
        abierta = self._abiertas.pop(self._llave(call_id, nombre), None)
        ahora = time.monotonic()
        if abierta is None:
            # A `completed` that closes nothing is a RE-EMISSION of one already counted
            # (opencode sends the same part several times). Counting it inflated the number of
            # calls to multiples of three. It is not counted: only what opens and closes counts.
            return
        nom, t_ini = abierta
        self._intervalos.append((t_ini, ahora))
        ficha = self._por_herramienta.setdefault(
            nombre or nom, {"n": 0, "ms": 0, "fallos": 0})
        ficha["n"] += 1
        ficha["ms"] += _ms(t_ini, ahora)
        if not ok:
            ficha["fallos"] += 1

    def paso_cerrado(self, *, tokens: dict | None = None, coste: Any = None,
                     modelo: str = "") -> None:
        ahora = time.monotonic()
        self.ms_por_paso.append(_ms(self._paso_abierto, ahora))
        self._paso_abierto = ahora
        self.pasos += 1
        if modelo:
            self.modelo = modelo
        try:
            if coste:
                self.coste += float(coste)
        except (TypeError, ValueError):
            pass
        t = tokens or {}
        cache = t.get("cache") if isinstance(t.get("cache"), dict) else {}
        self.tokens["entrada"] += int(t.get("input") or t.get("promptTokens") or 0)
        self.tokens["salida"] += int(t.get("output") or t.get("completionTokens") or 0)
        self.tokens["razonamiento"] += int(t.get("reasoning") or 0)
        self.tokens["cache_lectura"] += int((cache or {}).get("read") or 0)
        self.tokens["cache_escritura"] += int((cache or {}).get("write") or 0)

    def reintento(self) -> None:
        self.reintentos += 1

    def contexto_previo(self, ms: int = 0, n: int = 0, fallos: int = 0) -> None:
        """The turn context was fetched before the model (`app/contexto_de_turno.py`): how
        long it took, how many reads and how many failed. Next to the steps it saves."""
        self.contexto = {"ms": int(ms or 0), "n": int(n or 0), "fallos": int(fallos or 0)}

    def por_atajo(self, capacidad: str) -> None:
        """Answered without starting the agent, with the declared capability `capacidad`."""
        self.atajo = capacidad or "?"

    # ── the summary ────────────────────────────────────────────────
    def resumen(self) -> dict:
        ms_total = _ms(self.t0)
        # Whatever is still open when the turn closes counts up to here: a tool
        # that did not return is exactly the one to look at.
        ahora = time.monotonic()
        intervalos = list(self._intervalos)
        for nom, t_ini in self._abiertas.values():
            intervalos.append((t_ini, ahora))
            ficha = self._por_herramienta.setdefault(nom, {"n": 0, "ms": 0, "fallos": 0})
            ficha["n"] += 1
            ficha["ms"] += _ms(t_ini, ahora)
            ficha["sin_cerrar"] = True
        ms_herramientas = _union_ms(intervalos)
        detalle = sorted(
            ({"nombre": n, **v} for n, v in self._por_herramienta.items()),
            key=lambda d: (-int(d.get("ms") or 0), d["nombre"]),
        )[:_TOPE_HERRAMIENTAS]
        return {
            "ms_total": ms_total,
            "ms_modelo": max(ms_total - ms_herramientas, 0),
            "ms_herramientas": ms_herramientas,
            "ms_primera_senal": self.ms_primera_senal,
            "ms_primer_texto": self.ms_primer_texto,
            "ms_primera_herramienta": self.ms_primera_herramienta,
            "pasos": self.pasos,
            "ms_por_paso": self.ms_por_paso[:30],
            "llamadas": sum(int(v.get("n") or 0) for v in self._por_herramienta.values()),
            "herramientas": detalle,
            "tokens": dict(self.tokens),
            "coste": round(self.coste, 6),
            "modelo": self.modelo,
            "nivel": self.nivel,
            "esfuerzo": self.esfuerzo,
            "caracteres_pedidos": self.caracteres_pedidos,
            "lo_puso": self.lo_puso,
            "longitud": self.longitud,
            "canal": self.canal,
            "atajo": self.atajo,
            "recepcion": self.recepcion,
            "caracteres_respuesta": self.caracteres_respuesta,
            "reintentos": self.reintentos,
            # Only when the definition declares a turn context: {ms, n, fallos}.
            **({"contexto": self.contexto} if self.contexto else {}),
        }

    def registrar(self, estado: str = "ok") -> dict:
        """Emits the line and returns the summary (to store it in the run).

        INFO on purpose: a slow turn is not a bug, and ERROR is reserved for what needs
        attention (the tenant's logging standard). What makes this line diagnosable is
        that ALL the fields go loose, not embedded in the message."""
        datos = self.resumen()
        try:
            logger.info(
                "turno %s en %d ms (modelo %d ms, herramientas %d ms, %d pasos)",
                estado, datos["ms_total"], datos["ms_modelo"],
                datos["ms_herramientas"], datos["pasos"],
                extra={
                    "event": "turno",
                    "estado": estado,
                    "run_id": self.run_id,
                    "hilo": self.hilo,
                    "origen": self.origen,
                    **{k: v for k, v in datos.items() if k != "herramientas"},
                    # Tools go separately and already sorted by time cost: it is the
                    # field you look at first when a turn runs over.
                    "herramientas": datos["herramientas"],
                },
            )
        except Exception:  # noqa: BLE001 — measuring never breaks a turn
            logger.debug("medicion: no se pudo registrar el turno", exc_info=True)
        return datos
