"""«Take me to X» answered by a shortcut carries its PLACE card (14.20.0, 06/10/2026).

Seen with the travel employee: two «Llévame» to the Heidelberg Marriott answered with the
address only, nothing to press to go there. The card is built from the geocoder's DATA, never
by the small model that writes the answer.
"""
import json

from app import tarjetas

# The real shape: the Composio router wraps Google's (v4) result two levels deep.
GEOCODIFICADO = {"data": {"results": [{"response": {"successful": True, "data": {"results": [{
    "formattedAddress": "Vangerowstraße 16, 69115 Heidelberg, Germany",
    "location": {"latitude": 49.4092537, "longitude": 8.6722877},
    "addressComponents": [{"longText": "16", "types": ["street_number"]}]}]}}}]}}


def test_a_geocoding_result_gives_the_place_card():
    t = tarjetas.lugar_de_geocodificacion(GEOCODIFICADO, nombre="hotel Marriott Heidelberg")
    assert t == {"tipo": "lugar", "v": 1, "nombre": "hotel Marriott Heidelberg", "lat": 49.4092537,
                 "lon": 8.6722877, "direccion": "Vangerowstraße 16, 69115 Heidelberg, Germany",
                 "enlaces": {}}


def test_also_as_text_and_with_the_old_shape():
    v3 = json.dumps({"results": [{"formatted_address": "Calle Mayor 1, Madrid",
                                  "geometry": {"location": {"lat": 40.4168, "lng": -3.7038}}}]})
    t = tarjetas.lugar_de_geocodificacion(v3)
    assert (t["nombre"], t["lat"], t["lon"]) == ("Calle Mayor 1, Madrid", 40.4168, -3.7038)


def test_without_coordinates_there_is_no_card():
    assert tarjetas.lugar_de_geocodificacion({"results": [{"formattedAddress": "X"}]}) is None
    assert tarjetas.lugar_de_geocodificacion("not json") is None
    assert tarjetas.lugar_de_geocodificacion({"temp": 12.5, "city": "Madrid"}) is None


def test_a_client_that_cannot_paint_places_gets_it_pruned():
    texto = "El Marriott está en Vangerowstraße 16." + tarjetas.bloque(
        tarjetas.lugar_de_geocodificacion(GEOCODIFICADO, nombre="Marriott"))
    assert "wai-card" in tarjetas.podar(texto, ["lugar"])
    assert "wai-card" not in tarjetas.podar(texto, [])
