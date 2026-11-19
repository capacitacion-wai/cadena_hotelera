"""ONE SINGLE ROUND: the answer to a question is not answered with another question (12.52.0)."""
from app.routes.agent_routes import _ya_pregunto


def test_tras_una_pregunta_de_la_recepcion_se_delega():
    historial = [
        {"de": "persona", "texto": "quiero un hotel en Madrid"},
        {"de": "agente", "texto": "¿qué fechas?", "atendido_por": "recepcion",
         "recepcion": "pregunto"},
    ]
    assert _ya_pregunto(historial) is True


def test_si_lo_ultimo_fue_una_respuesta_suya_se_puede_preguntar():
    """Replying to a pleasantry does not use up the round: nobody has left a question hanging."""
    historial = [
        {"de": "persona", "texto": "hola"},
        {"de": "agente", "texto": "¡hola!", "atendido_por": "recepcion",
         "recepcion": "contesto"},
    ]
    assert _ya_pregunto(historial) is False


def test_si_trabajo_el_empleado_la_ronda_se_reinicia():
    """The employee already did something: whatever comes next is a new request, not an answer."""
    historial = [
        {"de": "persona", "texto": "¿qué fechas?"},
        {"de": "agente", "texto": "aquí tienes tres hoteles", "atendido_por": "agente",
         "recepcion": ""},
    ]
    assert _ya_pregunto(historial) is False


def test_sin_historial_se_puede_preguntar():
    assert _ya_pregunto([]) is False
    assert _ya_pregunto([{"de": "persona", "texto": "hola"}]) is False
