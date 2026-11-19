"""Cards: structured data the employee emits and EACH interface paints its own way.

The problem it solves. The interfaces (web and mobile) are generic and global: ONE for all
employees. Until now, to paint a route, the mobile app reverse-engineered the prose —it
looked for a Google Maps URL and extracted origin and stops from it with regular
expressions—. That tied the interface to a provider and a URL format, and above all it LOST
data: the employee had received the coordinates from its connector and threw them away when
formatting the URL, so the map came out empty. Measured on 31/08/2026 with the five stops of
a route around Madrid: the connector resolved all five in ONE 1.5 s call; the mobile
geocoder, none (it resolves postal addresses, and these were points of interest).

The inversion: **the employee delivers DATA, the interface decides the presentation.** A URL
is already a painting decision; `{nombre, lat, lon}` is data.

HOW IT TRAVELS. Inside the message itself, in a ```wai-card fenced block with JSON. Not as a
live-channel event, and the reason is concrete: a thread's history is repainted from
`AgentRun.response_md` —"the text arrives, not the process"—, so a card travelling as an
event would DISAPPEAR when the conversation is reopened. What is stored is the raw text, with
its block; what is emitted adapts to whoever asks (see `podar`). That is why "it dirties the
text" does not actually happen: a client that does not declare it can paint them has them
removed before they go out, from the same stored message.

WHAT IT DOES NOT DO. It does not replace the prose. The card ENRICHES; the written answer
stands on its own, so a client that cannot paint a type loses an improvement, not the answer.

HOW IT GROWS. A new type only comes in when TWO different clients would paint it better than
as text. Without that rule the vocabulary becomes a junk drawer and no type ends up well
supported. Each card carries its own `v`, so you can be on `ruta` v1 and `tabla` v2 without
synchronizing anything.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable

logger = logging.getLogger(__name__)

APERTURA = "```wai-card"
CIERRE = "```"
VERSION = 1

#: Vocabulary v1. Deliberately TWO types (see "how it grows" above).
TIPOS: tuple[str, ...] = ("lugar", "ruta")

MODOS = ("andando", "coche", "transporte", "bici", "")
#: Cap on stops. A fifty-point route can be neither painted nor read; trimming is better
#: than sending the client something it cannot draw.
MAX_PARADAS = 25


#: A "name" that is actually coordinates ("40.4188,-3.7219"). The model does it when it
#: already has the pair in front of it and does not bother to recover the place's name.
_SOLO_COORDENADAS = re.compile(
    r"^\s*(-?\d{1,3}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)\s*$")


def _punto(x: Any) -> dict | None:
    """A point is a NAME and, if known, its coordinates.

    The coordinates are optional on purpose: an employee WITHOUT a maps connector can still
    present the list of stops, and the client will show the card without a map. What is not
    acceptable is inventing them."""
    if not isinstance(x, dict):
        return None
    nombre = str(x.get("nombre") or "").strip()
    if not nombre:
        return None
    p: dict[str, Any] = {"nombre": nombre[:200]}
    lat, lon = x.get("lat"), x.get("lon")
    # If the "name" is a pair of coordinates, at least they are RECOVERED as such: a card with
    # its map and an ugly label is preferable to a card without a map. Seen in production on
    # 31/08/2026: a whole route whose five stops were called "40.41…,-3.72…".
    coords = _SOLO_COORDENADAS.match(nombre)
    if coords and lat is None and lon is None:
        lat, lon = coords.group(1), coords.group(2)
    try:
        if lat is not None and lon is not None:
            lat, lon = float(lat), float(lon)
            # Out of range = corrupt data, not approximate data: the whole pair is discarded
            # instead of painting a pin in the middle of the ocean.
            if abs(lat) <= 90 and abs(lon) <= 180:
                p["lat"], p["lon"] = lat, lon
    except (TypeError, ValueError):
        pass
    if isinstance(x.get("direccion"), str) and x["direccion"].strip():
        p["direccion"] = x["direccion"].strip()[:300]
    return p


def _enlaces(x: Any) -> dict:
    """`enlaces` is a map PER PROVIDER (`{"google": "...", "apple": "..."}`), never "the"
    URL: which one to open is a decision of the client, which is who knows what the user
    has installed."""
    if not isinstance(x, dict):
        return {}
    fuera = {}
    for k, v in list(x.items())[:6]:
        if isinstance(v, str) and v.startswith(("http://", "https://")):
            fuera[str(k)[:20]] = v[:2000]
    return fuera


def validar(obj: Any) -> dict | None:
    """Normalizes a card. `None` = it is not valid and it is DISCARDED.

    It is discarded silently for the client and with a warning in the log: the user keeps the
    prose, which stands on its own. Showing them a block of broken JSON would be worse."""
    if not isinstance(obj, dict):
        return None
    tipo = str(obj.get("tipo") or "").strip().lower()
    if tipo not in TIPOS:
        return None
    try:
        v = int(obj.get("v") or 0)
    except (TypeError, ValueError):
        return None
    if v != VERSION:
        return None

    if tipo == "lugar":
        p = _punto(obj)
        if p is None:
            return None
        return {"tipo": "lugar", "v": VERSION, **p, "enlaces": _enlaces(obj.get("enlaces"))}

    paradas = [q for q in (_punto(p) for p in (obj.get("paradas") or [])) if q]
    # A "route" of a single point is not a route: it is a place, and the client would paint it
    # differently. Better to discard it than show a card that lies about what it is.
    if len(paradas) < 2:
        return None
    modo = str(obj.get("modo") or "").strip().lower()
    return {
        "tipo": "ruta",
        "v": VERSION,
        "modo": modo if modo in MODOS else "",
        "paradas": paradas[:MAX_PARADAS],
        "enlaces": _enlaces(obj.get("enlaces")),
    }


def _coordenadas(x: Any) -> tuple[float, float] | None:
    """`{latitude, longitude}` (Google v4), `{lat, lng}` (v3) or `{lat, lon}`."""
    if not isinstance(x, dict):
        return None
    lat = x.get("latitude", x.get("lat"))
    lon = x.get("longitude", x.get("lng", x.get("lon")))
    try:
        return (float(lat), float(lon)) if lat is not None and lon is not None else None
    except (TypeError, ValueError):
        return None


def lugar_de_geocodificacion(resultado: Any, nombre: str = "") -> dict | None:
    """The PLACE card of a geocoding result (the first one found), or None.

    Why it exists. A shortcut that resolves «take me to X» answers with a write-up of the
    tool's result, and that write-up is prose: the coordinates the connector returned were
    lost and the person got an address with NOTHING to press to go there. Measured on
    06/10/2026 (travel employee, «Llévame al hotel Marriott de Heidelberg»): two turns of
    «Llévame» answered with text only. The card is built HERE, from the data, never by the
    small model that writes the answer: it cannot invent a coordinate.

    It walks the result whatever wraps it (the Composio router nests it two levels deep) and
    takes the first object that carries coordinates AND an address."""
    vistos = 0
    pendientes: list[Any] = [resultado]
    while pendientes and vistos < 2000:
        x = pendientes.pop(0)
        vistos += 1
        if isinstance(x, str) and x.strip()[:1] in "[{":
            try:
                pendientes.append(json.loads(x))
            except (json.JSONDecodeError, ValueError):
                pass
            continue
        if isinstance(x, list):
            pendientes.extend(x)
            continue
        if not isinstance(x, dict):
            continue
        direccion = x.get("formattedAddress") or x.get("formatted_address")
        coords = _coordenadas(x.get("location")) or _coordenadas((x.get("geometry") or {}).get("location"))
        if isinstance(direccion, str) and direccion.strip() and coords:
            etiqueta = x.get("displayName")
            etiqueta = etiqueta.get("text") if isinstance(etiqueta, dict) else etiqueta
            return validar({"tipo": "lugar", "v": VERSION,
                            "nombre": str(etiqueta or nombre or direccion).strip(),
                            "lat": coords[0], "lon": coords[1], "direccion": direccion.strip()})
        pendientes.extend(v for v in x.values() if isinstance(v, (dict, list, str)))
    return None


def bloque(tarjeta: dict) -> str:
    """The ```wai-card block of an already validated card."""
    return _bloque(tarjeta)


def _bloque(tarjeta: dict) -> str:
    return f"\n{APERTURA}\n{json.dumps(tarjeta, ensure_ascii=False)}\n{CIERRE}\n"


def _procesar(crudo: str, soportados: Iterable[str] | None) -> str:
    """A raw block → the normalized block, or empty if discarded."""
    try:
        obj = json.loads(crudo.strip())
    except (json.JSONDecodeError, ValueError):
        logger.warning("tarjeta descartada: no es JSON válido")
        return ""
    t = validar(obj)
    if t is None:
        logger.warning("tarjeta descartada: no cumple el esquema (tipo=%r)",
                       (obj or {}).get("tipo") if isinstance(obj, dict) else None)
        return ""
    # `None` = "unknown what it supports" ⇒ nothing is pruned. A client that declares nothing
    # is an old client, and that one has everything removed (the caller does it by passing []).
    if soportados is not None and t["tipo"] not in soportados:
        return ""
    return _bloque(t)


def podar(texto: str, soportados: Iterable[str] | None) -> str:
    """Leaves the text ready for ONE concrete client: validates each card and removes the ones
    it cannot paint. Used in the history and in the final message; streaming uses `Filtro`."""
    if not texto or APERTURA not in texto:
        return texto
    f = Filtro(soportados)
    return f.consumir(texto) + f.cerrar()


class Filtro:
    """The live-channel sanitizer. ONE state machine for everything that has to be held
    back while the text arrives in chunks.

    Two jobs, and they are the same: nothing that depends on seeing several characters in a
    row can be decided by looking at a loose fragment.

    1. **Cards.** A ```wai-card block can be split anywhere; without holding it back, the
       client would see half-JSON arrive.
    2. **Leaks of the internal block** (v10.3.1). `strip_internal_chat_leak` looks for its
       markers INSIDE each fragment, so a marker split between two —"[TIT" + "ULO:"— is found by
       neither and goes out whole to the conversation. It happened on 31/08/2026: the operator
       read the complete trailer, with workspace paths and tool names, followed by the real
       answer. Here it is cut AS SOON AS the marker is confirmed, and whatever comes after the
       cut is discarded until the end of the turn: a spoiled trailer recovers nothing useful
       behind it.
    """

    #: Length of the fingerprint used to recognise what was injected. Long enough that it
    #: cannot match a legitimate sentence by chance, and short enough to recognise an echo
    #: that starts in the middle.
    HUELLA = 48

    def __init__(self, soportados: Iterable[str] | None,
                 cortar_en: Iterable[str] = (),
                 inyectado: str = ""):
        self._soportados = None if soportados is None else set(soportados)
        # WHAT WE INJECT, chopped into overlapping fingerprints. This is what turns the cut
        # into a rule and not a list of cases: it does not look for "a known marker", it looks
        # for the text the application itself put in the prompt. If tomorrow the content of
        # the internal block changes, this keeps working without touching it, and if an echo
        # starts in the middle —which is how it happened— it is recognised too.
        # It is stored WHOLE and compared exactly as injected: the model reproduces it
        # literally (checked in both leaks). There is no list of fingerprints or markers —
        # the rule is "a window of the emitted text that appears INSIDE what we injected is a
        # leak", and it works wherever the echo comes from.
        self._inyectado = inyectado or ""
        self._cortes = tuple(m for m in cortar_en if m)
        self._cortado = False
        self._buf = ""
        self._dentro = False

    def _corte(self, texto: str) -> int:
        """Where the earliest leak starts, or -1.

        Two criteria: a known marker, and —the one that really closes the case— a window of
        HUELLA characters that appears INSIDE the block we injected. The second does not
        depend on the echo starting at the beginning: it equally recognises one that starts
        in the middle, which is how it happened both times."""
        bajo = texto.lower()
        pos = [i for i in (bajo.find(m.lower()) for m in self._cortes) if i >= 0]
        if self._inyectado:
            for i in range(0, len(texto) - self.HUELLA + 1):
                if texto[i:i + self.HUELLA] in self._inyectado:
                    pos.append(i)
                    break
        return min(pos) if pos else -1

    def _reserva_de_corte(self, texto: str) -> int:
        """How much tail must be held back in case it is the start of a leak.

        The longest tail that APPEARS in the injected block is held back (not only one that
        starts it): without that, an echo starting in the middle escapes character by
        character before the window gets to complete. In normal prose this is almost always
        zero —a real sentence does not match the internal block— and at most it holds HUELLA-1."""
        tope = min(self.HUELLA - 1, len(texto))
        for m in self._cortes:
            for n in range(min(len(m) - 1, len(texto)), 0, -1):
                if m.lower().startswith(texto[-n:].lower()):
                    return n
        if self._inyectado:
            for n in range(tope, 0, -1):
                if texto[-n:] in self._inyectado:
                    return n
        return 0

    def consumir(self, delta: str) -> str:
        if self._cortado:
            return ""
        self._buf += delta or ""
        fuera = []
        while True:
            if self._dentro:
                i = self._buf.find(CIERRE)
                if i < 0:
                    return "".join(fuera)      # block still open: keep holding back
                fuera.append(_procesar(self._buf[:i], self._soportados))
                self._buf = self._buf[i + len(CIERRE):]
                self._dentro = False
                continue
            i = self._buf.find(APERTURA)
            if i >= 0:
                fuera.append(self._buf[:i])
                self._buf = self._buf[i + len(APERTURA):]
                self._dentro = True
                continue
            # Nothing open: release everything EXCEPT what could really be half an opening
            # ("```wai-c" split between two chunks). The longest suffix that is a prefix of
            # the opening is computed, which is almost always ZERO.
            #
            # In 10.2.0 10 characters were ALWAYS held back, and that was not prudence: the end
            # of each step came out glued to the next step, so the answer appeared cut
            # ("…n el mapa" instead of "…en el mapa"). Holding back text that cannot be an
            # opening protects from nothing and corrupts what is actually answer.
            # Has a leak of the internal block started? It is cut here and nothing else goes out.
            if self._cortes or self._inyectado:
                i = self._corte(self._buf)
                if i >= 0:
                    fuera.append(self._buf[:i].rstrip())
                    self._buf, self._cortado = "", True
                    logger.warning("fuga del bloque interno cortada en el canal")
                    return "".join(fuera)
            reserva = 0
            for n in range(min(len(APERTURA) - 1, len(self._buf)), 0, -1):
                if APERTURA.startswith(self._buf[-n:]):
                    reserva = n
                    break
            reserva = max(reserva, self._reserva_de_corte(self._buf))
            if reserva:
                fuera.append(self._buf[:-reserva])
                self._buf = self._buf[-reserva:]
            else:
                fuera.append(self._buf)
                self._buf = ""
            return "".join(fuera)

    def cerrar(self) -> str:
        """End of the turn. A block left OPEN is discarded: the employee cut itself off
        halfway and half a JSON is not a card."""
        if self._cortado:
            return ""
        if self._dentro:
            logger.warning("tarjeta descartada: el bloque quedó sin cerrar")
            self._buf, self._dentro = "", False
            return ""
        if self._cortes or self._inyectado:
            i = self._corte(self._buf)
            if i >= 0:
                resto = self._buf[:i].rstrip()
                self._buf, self._cortado = "", True
                return resto
        resto, self._buf = self._buf, ""
        return resto
