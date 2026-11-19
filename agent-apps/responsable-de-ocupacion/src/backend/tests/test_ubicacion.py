"""The location request: what fails SILENTLY if nobody watches it.

These three are not here for completeness. They are exactly the ways this feature breaks
without showing itself, and the ones a harness with a single user and no phone cannot see:

1. That the tool REFUSES when the client did not declare the capability. If one day it stops
   filtering, the employee starts asking for the location on the web —where nobody can
   answer it— and the only visible sign is that answers take 8 seconds longer.
2. That the CAP closes the wait. Without it, a client that does not answer leaves the turn
   blocked and billing, and the symptom ("it got stuck") does not point here at all.
3. That the permission is PER OWNER. The request identifier travels to the client, so if it
   were valid on its own, anyone in the same pod could inject a position into another
   person's turn.
"""
from __future__ import annotations

import asyncio

import pytest

from app import ubicacion as ubic


@pytest.mark.asyncio
async def test_el_tope_cierra_la_espera_aunque_nadie_conteste() -> None:
    """Nobody answers → "not available" is returned, it does not wait forever."""
    _id, futuro = ubic.abrir("ana@wai.com")
    # Tiny cap: what is checked is that it EXISTS, not its value.
    assert await ubic.esperar(_id, futuro, espera_s=0.05) is None


@pytest.mark.asyncio
async def test_la_respuesta_del_dueno_llega_entera() -> None:
    peticion_id, futuro = ubic.abrir("ana@wai.com")

    async def contesta() -> None:
        await asyncio.sleep(0.01)
        assert ubic.resolver(peticion_id, "ana@wai.com",
                             {"lat": 40.4, "lon": -3.7, "precision": 25.0,
                              "antiguedad_s": 0}) is True

    tarea = asyncio.create_task(contesta())
    datos = await ubic.esperar(peticion_id, futuro, espera_s=1.0)
    await tarea
    assert datos is not None
    assert datos["lat"] == 40.4 and datos["lon"] == -3.7
    # The accuracy ALWAYS travels: it is what separates "you are near X" from asserting a
    # street with 800 m of error.
    assert datos["precision"] == 25.0


@pytest.mark.asyncio
async def test_otra_persona_no_puede_contestar_por_ti() -> None:
    """The identifier travels to the client: on its own it cannot be a permission."""
    peticion_id, futuro = ubic.abrir("ana@wai.com")
    assert ubic.resolver(peticion_id, "otro@wai.com", {"lat": 0.0, "lon": 0.0}) is False
    # And the request stays alive for its owner: the other person's attempt does not consume it.
    assert ubic.resolver(peticion_id, "ana@wai.com", {"lat": 1.0, "lon": 2.0}) is True
    assert (await ubic.esperar(peticion_id, futuro, espera_s=1.0))["lat"] == 1.0


@pytest.mark.asyncio
async def test_una_peticion_caducada_ya_no_se_contesta() -> None:
    """After the cap, the request is withdrawn: a late answer cannot sneak into a turn
    that already moved on."""
    peticion_id, futuro = ubic.abrir("ana@wai.com")
    assert await ubic.esperar(peticion_id, futuro, espera_s=0.05) is None
    assert ubic.resolver(peticion_id, "ana@wai.com", {"lat": 1.0, "lon": 2.0}) is False


def test_el_rastro_dice_la_verdad_en_los_dos_casos() -> None:
    """The line left in the conversation. It goes into the saved TEXT —not as a tool
    call— because the history does not carry tools: when repainting on another client
    an "I know where you are" would be left with no explanation."""
    compartida = ubic.describir({"lat": 40.4, "lon": -3.7, "precision": 30})
    assert "ubicación" in compartida.lower() and "30" in compartida
    # And never the coordinates: the trace says THAT it was shared, not the position.
    assert "40.4" not in compartida and "-3.7" not in compartida
    assert "no estaba disponible" in ubic.describir(None)


# ── 12.58.0: enough time, and the ATTACHED location is not lost ─────────────────────────

def test_la_espera_cubre_gps_en_frio_y_el_dialogo_de_permiso() -> None:
    """With 8 s the phone often arrived late (cold GPS, "allow once" asking again every
    session) and the employee still had no location (measured on 24/09/2026)."""
    assert ubic.ESPERA_S >= 20


@pytest.mark.asyncio
async def test_un_mensaje_con_ubicacion_no_lo_contesta_la_recepcion() -> None:
    """The reception does not receive the location: it answered "which city are you in?" to
    someone who had just sent it. With an attached location, as with an attachment, it is
    handed to the employee."""
    from types import SimpleNamespace
    from app.routes import agent_routes

    payload = SimpleNamespace(attachments=[], request_id=None, canal=None, capacidad=None,
                              location={"lat": 40.41, "lon": -3.69, "precision_m": 15})
    d = await agent_routes._recepcion_decide(payload=payload, mensaje="Estoy aquí",
                                             chat_email="ana@wai.com", hilo_id=None,
                                             medicion=SimpleNamespace(recepcion=""))
    assert d == {"accion": "delego"}
