"""The employee does NOT touch the assistant's text to compensate for anything.

For a long time the chat trimmed «the echo of the question»: it compared the
start of the answer with what had been sent to the agent and, if they looked
alike, deleted it. It existed for a reason that was NOT in the employee — the
sidecar proxy (`opencode-server/simple_api/proxy.py`) published the parts of the
USER's message on the same channel as the assistant's, so the question came
back as if it were the answer, with the `[INTERNO run_token=…]` block glued
behind it.

Comparing text to guess what is extra never came for free:

* the normalisation folded punctuation, so the match ended at the last LETTER
  and the closing sign was left dangling: the answer started with «?» or «.»
  plus its line break;
* and when the trim got the boundary wrong it ate real characters
  («Ventas» → «s», «hacer» + «pace» → «hacerpace»).

The bug was fixed where it was: the proxy now checks whose each part is.
This file keeps its name —until 12.9.0 it tested the trim— so that evolving an
employee REPLACES the old one in place instead of living alongside it; that is
why it is also INVARIANT. It is the guard that the trim does not come back this
way: if some day text appears that the assistant did not write, the bug is in
whoever emits it, not in whoever paints it.
"""
import app.chat_prompt as chat_prompt


def test_no_hay_recorte_por_parecido():
    """No helper that compares the answer with the question."""
    for nombre in ("strip_leading_echo", "normalize_for_echo", "_empieza_otra_frase"):
        assert not hasattr(chat_prompt, nombre), (
            f"{nombre} ha vuelto: el eco se arregla en el proxy (mirando el rol "
            f"del mensaje), no recortando texto en el empleado."
        )


def test_una_pregunta_repetida_se_muestra_tal_cual():
    """If the MODEL decides to open by repeating the question, that is its answer.

    It is no longer an artefact of ours, so it is shown whole: guessing which of
    the two it was is exactly what broke legitimate answers.
    """
    texto = "¿Cuántos viajes tengo este mes?\n\nTienes 4 viajes en septiembre."
    visible = chat_prompt.normalize_chat_markdown(
        chat_prompt.strip_internal_chat_leak(texto)
    )
    assert visible.startswith("¿Cuántos viajes tengo este mes?")
    assert "Tienes 4 viajes" in visible


def test_no_queda_puntuacion_suelta_al_principio():
    """The concrete regression that showed on screen."""
    respuesta = "Tienes 4 viajes en septiembre."
    visible = chat_prompt.normalize_chat_markdown(
        chat_prompt.strip_internal_chat_leak(respuesta)
    )
    assert visible == respuesta
    assert not visible.lstrip().startswith(("?", ".", "!"))


# ── And the text is not SPLIT either ───────────────────────────────────
#
# The same turn had a second face. The frontend groups text by part
# (`partId`) and joins the blocks with `join('\n\n')`. A `text_delta` that does not
# say which part it comes from falls back to the step counter, and if that counter
# already moved it opens a new block: a break appears mid-text. Inside a URL, the link
# stops being a link and is painted raw — always the LAST one of the answer.
#
# It happened exactly there: the tail that the card filter releases on close was
# emitted without `partId` and with the `step_index` ALREADY incremented by the last
# `step_done`. This test closes the rule for all three places, not just that one.

def _fuente_del_chat() -> str:
    from pathlib import Path
    import app.routes.agent_routes as ar
    return Path(ar.__file__).read_text(encoding="utf-8")


def test_todo_texto_del_chat_dice_de_que_parte_viene():
    """No `text_delta` of the chat stream goes out without `partId`."""
    fuente = _fuente_del_chat()
    bloques = fuente.split('"type": "text_delta"')[1:]
    sin_parte = [
        i for i, b in enumerate(bloques)
        # The slice up to the closing of the emitted dict.
        if "partId" not in b[:b.find("})") + 2 if b.find("})") > 0 else 400]
    ]
    # The first one is the generator of a standalone error message: it forms its own
    # block and does not compete with any assistant text.
    assert sin_parte == [0], (
        f"hay text_delta sin partId en las posiciones {sin_parte}: un trozo que no "
        f"declara su parte parte el texto en dos bloques y el `join` mete un salto."
    )


def test_la_cola_del_filtro_hereda_parte_y_paso():
    """It is released when the step counter has already moved: it must say which
    part and which step it belonged to, not where it happens to be released."""
    fuente = _fuente_del_chat()
    i = fuente.find("_tarjetas.cerrar()")
    assert i > 0, "el filtro de tarjetas ya no cierra: revisa este test"
    trozo = fuente[i:i + 900]
    assert '"partId": parte_texto' in trozo
    assert "paso_del_texto if parte_texto else step_index" in trozo
