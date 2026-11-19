"""WHERE the conversation happens changes what can be offered, and that is DECLARED.

Before, web and mobile received the same text —it was written that way on purpose— and a
single text only allows two behaviours, both bad: offering a photo on a laptop, or never
offering it on a phone that carries one in the pocket.
"""
from __future__ import annotations

from app.superficie import CAPACIDADES, instrucciones, normalizar

MOVIL = ["pantalla", "ubicacion", "camara", "galeria", "ficheros", "notificaciones"]
WEB = ["pantalla", "ubicacion", "ficheros"]


def test_el_movil_puede_pedir_lo_que_la_web_no():
    m, w = instrucciones("movil", MOVIL), instrucciones("web", WEB)
    # The CAPABILITY is checked in the person's language and IN WHICH BLOCK it falls, not the
    # function name: since 12.43.0 the conversationalist has no tools —device things are
    # requested by the employee— so technical names no longer travel. And the block must be
    # checked because the photo appears in BOTH texts: on mobile as something available, and
    # on the web inside the "you do NOT have here" block, which is exactly the opposite.
    def hay(texto: str) -> str:
        return texto.split("Here you do NOT have")[0]

    def no_hay(texto: str) -> str:
        p = texto.split("Here you do NOT have")
        return p[1] if len(p) > 1 else ""

    assert "a photo taken on the spot" in hay(m), "el móvil no ofrece la foto"
    assert "a photo taken on the spot" in no_hay(w), "la web no dice que ahí no hay cámara"
    assert "where they are right now" in hay(m) and "where they are right now" in hay(w)


def test_lo_que_NO_hay_se_dice():
    """Without that sentence the model assumes the usual and offers a photo on a laptop,
    which costs the person a whole round trip."""
    w = instrucciones("web", WEB)
    assert "Here you do NOT have" in w
    assert "a photo taken on the spot" in w.split("Here you do NOT have")[1]


def test_toda_herramienta_declarada_tiene_QUIEN_la_dispare():
    """THE RULE, and it is the same as in the conversation machine: nothing is declared
    without a producer.

    The catalogue said the camera, the gallery and the file picker were requested with a
    function. That was not true since 12.43.0: the conversationalist was left without tools
    and the employee has only ONE device tool. So the speaker was told «if any of that is
    needed, you will be asked in one sentence» for three things nobody was ever going to
    ask for — and that does not fail, does not warn, and shows up as the model offering
    them and then nothing happening.

    Here it is checked against the real list: the EMPLOYEE's tools."""
    from app.mcp_server import TOOLS

    for k, c in CAPACIDADES.items():
        h = c.get("herramienta")
        if not h:
            continue
        assert h["name"] in TOOLS, (
            f"«{k}» declara la función {h['name']!r} y no existe en las herramientas del "
            f"empleado: nadie puede dispararla, así que el conversador espera en balde")


def test_lo_que_resuelve_la_PERSONA_no_se_ofrece_como_mecanismo():
    """The photo and the document are sent by the person with the paperclip. Telling the
    conversationalist it is something «you will be asked for» makes it wait for a mechanism
    that does not exist; telling it that the person attaches it makes it ask for it by voice,
    which is what works."""
    for k in ("camara", "galeria", "ficheros"):
        assert CAPACIDADES[k]["herramienta"] is None, (
            f"«{k}» vuelve a declarar una función: no hay quien la dispare")
        assert "themselves" in CAPACIDADES[k]["cuando"], (
            f"«{k}» no dice quién lo hace, y sin eso el conversador supone que lo hace él")


def test_lo_desconocido_se_descarta_sin_romper():
    """The client is newer than the server half of the time —a published app cannot be
    rewound— and breaking the conversation over that punishes the person for our
    deployment."""
    assert normalizar(["ubicacion", "telepatia", "UBICACION"]) == ["ubicacion"]


def test_un_canal_que_no_existe_no_inventa_nada():
    assert instrucciones("fax", MOVIL) == ""


def test_cada_capacidad_dice_CUÁNDO_y_no_solo_qué():
    """A model with a tool at hand uses it just in case, and asking for the location for a
    question that does not need it is exactly how trust is lost."""
    for k, c in CAPACIDADES.items():
        assert c.get("cuando"), f"«{k}» dice qué se puede pedir pero no cuándo"
