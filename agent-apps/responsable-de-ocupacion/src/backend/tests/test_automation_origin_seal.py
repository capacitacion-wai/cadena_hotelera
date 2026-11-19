"""An automation's ORIGIN is read-only; to change it you EVOLVE it (7.151.0).

The autonomous-work templates come from the DEPLOYMENT: they are
``automation_templates.{es,en}.json`` files baked into the image, written by the code agent
when it generates the employee. Nobody edits them (neither the UI nor the agent has a way to
write them), so the missing seal was not on the templates but on each row's PROVENANCE:
until now an `update_automation` could change `template_key` and make an automation claim
to come from a template that is not its own. And since the UI and the agent go through the
SAME service, locking it down here covers both.

The allowed path to "change" something that came from the deployment is `evolve_automation`:
the original stays intact and a copy pointing to it is born.
"""
from __future__ import annotations

import asyncio

import pytest


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _crear_desde_plantilla() -> dict:
    from app import automations_service as svc
    return await svc.create_automation({
        "title": "Vigilar ventas caídas",
        "description": "<p>Cuando bajen las ventas, avisar.</p>",
        "signal_type": "manual",
        "reaction_type": "notify",
        "template_key": "ventas_caidas",
        "template_version": "1.0.0",
    }, by="test")


def test_el_origen_no_se_puede_reescribir():
    from app import automations_service as svc

    a = _run(_crear_desde_plantilla())
    assert a["template_key"] == "ventas_caidas"

    with pytest.raises(ValueError) as exc:
        _run(svc.update_automation(a["id"], {"template_key": "otra_plantilla"}))
    # The message has to show the way: the one who hits it most is the agent.
    assert "evolve_automation" in str(exc.value)

    with pytest.raises(ValueError):
        _run(svc.update_automation(a["id"], {"derived_from_id": 999}))

    vivo = _run(svc.get_automation(a["id"]))
    assert vivo["template_key"] == "ventas_caidas", "el origen sigue siendo el suyo"


def test_reenviar_el_mismo_origen_no_molesta():
    """The UI resends the whole object on save: same value = not a change."""
    from app import automations_service as svc

    a = _run(_crear_desde_plantilla())
    out = _run(svc.update_automation(a["id"], {
        "title": "Vigilar ventas caídas",         # unchanged
        "template_key": "ventas_caidas",          # unchanged
        "template_version": "1.0.0",
        "enabled": True,                          # operational lever: this one is allowed
    }))
    assert out["enabled"] is True


# ── Read-only content in what came from the deployment (7.152.0) ────────

def test_el_contenido_del_despliegue_no_se_edita():
    from app import automations_service as svc

    a = _run(_crear_desde_plantilla())
    for campo, valor in (("title", "Otro nombre"),
                         ("description", "<p>otra cosa</p>"),
                         ("reaction_type", "act")):
        with pytest.raises(ValueError) as exc:
            _run(svc.update_automation(a["id"], {campo: valor}))
        assert "EVOLUCIÓN" in str(exc.value), campo


def test_las_palancas_de_operacion_siguen_libres():
    """Without this you could not pause one that bothers you without duplicating it first."""
    from app import automations_service as svc

    a = _run(_crear_desde_plantilla())
    out = _run(svc.update_automation(a["id"], {"enabled": True, "cooldown_sec": 900,
                                               "priority": 5}))
    assert out["enabled"] is True and out["cooldown_sec"] == 900


def test_la_derivada_si_se_edita_libremente():
    """An evolution belongs to the user, even if it keeps the template's lineage."""
    from app import automations_service as svc

    a = _run(_crear_desde_plantilla())
    hija = _run(svc.evolve_automation(a["id"], {"title": "Mi variante"}, by="test"))
    out = _run(svc.update_automation(hija["id"], {"title": "Mi variante v2"}))
    assert out["title"] == "Mi variante v2"
    assert svc.is_from_deployment(out) is False


def test_evolucionar_deja_intacto_el_original():
    from app import automations_service as svc

    a = _run(_crear_desde_plantilla())
    hija = _run(svc.evolve_automation(a["id"], {
        "title": "Vigilar ventas caídas — solo grandes cuentas",
    }, by="test"))

    assert hija["id"] != a["id"]
    assert hija["derived_from_id"] == a["id"], "la copia apunta a su origen"
    assert hija["template_key"] == "ventas_caidas", "no se pierde el linaje de la plantilla"
    assert hija["enabled"] is False, "una evolución la revisa una persona antes de disparar"

    original = _run(svc.get_automation(a["id"]))
    assert original["title"] == "Vigilar ventas caídas", "el original NO se ha tocado"
    assert original["derived_from_id"] is None


def test_la_evolucion_esquiva_el_guardian_de_duplicados():
    """Sharing the template with the origin is intentional here; the guard exists to
    stop duplicate creations made by mistake, not deliberate evolutions."""
    from app import automations_service as svc

    a = _run(_crear_desde_plantilla())
    # A normal creation with the same template IS rejected…
    with pytest.raises(ValueError) as exc:
        _run(svc.create_automation({
            "title": "Otra igual",
            "description": "<p>x</p>",
            "signal_type": "manual", "reaction_type": "notify",
            "template_key": "ventas_caidas",
        }, by="test"))
    assert "ya está añadida" in str(exc.value)
    # …and the evolution is not.
    hija = _run(svc.evolve_automation(a["id"], {"title": "Variante buena"}, by="test"))
    assert hija["template_key"] == "ventas_caidas"


def test_la_copia_se_renombra_sola_si_no_le_das_titulo():
    from app import automations_service as svc

    a = _run(_crear_desde_plantilla())
    hija = _run(svc.evolve_automation(a["id"], {}, by="test"))
    assert hija["title"] != a["title"], "dos filas con el mismo nombre son indistinguibles"
    assert "evolución" in hija["title"]
