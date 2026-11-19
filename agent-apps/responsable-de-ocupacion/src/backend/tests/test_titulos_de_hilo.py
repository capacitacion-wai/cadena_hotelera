"""What a conversation is called (v10.6.0).

Titling has failed twice in the same way: the model returns the INSTRUCTION instead of
obeying it, and that text ended up in the list (`«Título de esta conversación en 3 a…»`,
`«tres a cinco palabras que digan de…»`). It was not bad wording: the "separate" session is
still the employee's agent, the same channel that already proved it repeats what it is told.

Hence the design protected here: the GOOD title is the deterministic one, and the model's only
gets in if it passes a gate. When the model fails, it fails towards the good title.
"""
from app import chat_threads as T


def test_el_titulo_determinista_se_lee_como_un_nombre():
    assert T.titulo_de("cómo ir a vallecas") == "Cómo ir a vallecas"
    # No trailing punctuation, and trimmed to what fits in a row.
    assert T.titulo_de("¿Dame una ruta de Madrid Río?") == "¿Dame una ruta de Madrid Río"
    assert len(T.titulo_de(" ".join(["palabra"] * 30)).split(" ")) == 6
    # An empty message cannot leave the row blank.
    assert T.titulo_de("") == "Conversación"


def test_la_puerta_rechaza_el_eco_de_la_instruccion():
    """The two titles that reached production, and why they do not come back."""
    for eco in ("Título de esta conversación en 3 a 5 palabras",
                "tres a cinco palabras que digan de qué va",
                "Responde SOLO con el título, sin comillas"):
        assert T.titulo_valido_del_modelo(eco, "da igual") == "", eco


def test_la_puerta_rechaza_lo_que_no_es_un_titulo():
    # An explanation, not a title.
    largo = "Esta conversación trata sobre cómo llegar andando hasta el parque del oeste"
    assert T.titulo_valido_del_modelo(largo, "x") == ""
    # Repeating the question adds nothing: that is what the deterministic one is for, and
    # without spending a model call.
    assert T.titulo_valido_del_modelo("Cómo ir a Vallecas", "cómo ir a vallecas") == ""


def test_la_puerta_deja_pasar_un_titulo_de_verdad():
    bueno = T.titulo_valido_del_modelo("«Ruta por Madrid Río».", "dame una ruta")
    assert bueno == "Ruta por Madrid Río"


def test_se_reconoce_un_titulo_roto_para_repararlo():
    assert T.esta_roto("Título de esta conversación en 3 a 5 palabras")
    assert T.esta_roto("[TITULO: algo]")
    assert T.esta_roto("run_id=575 exports=/workspace/")
    # And a legitimate title is NOT touched, which is the half that matters.
    assert not T.esta_roto("Ruta por Madrid Río")
    assert not T.esta_roto("Cómo ir a Vallecas")
    assert not T.esta_roto("")
