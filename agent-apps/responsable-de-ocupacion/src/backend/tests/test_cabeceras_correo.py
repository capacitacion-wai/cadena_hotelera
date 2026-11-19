"""An incoming email must not stop the employee from replying.

Regression measured on 18/08/2026 on the Odoo employee: replying to an email with a
LONG subject, the run died with «ValueError: Header values may not contain linefeed or
carriage return characters» and the sender got no reply. The cause was neither the
agent nor SMTP: RFC 5322 FOLDS a long header over several lines, `msg.get()` returns
it with the line breaks inside and `.strip()` only cleans the ends.
"""
from email.message import EmailMessage

import email as _email
import pytest

from app.channels.base import texto_de_cabecera, valor_de_cabecera

# Exactly as a mail client sends it: the subject does not fit in 78 columns and is split.
CORREO_PLEGADO = (
    b"From: cliente@ejemplo.com\r\n"
    b"To: alex.odoo@waiworking.com\r\n"
    b"Subject: Consulta sobre las facturas vencidas del trimestre y el estado de los\r\n"
    b" pedidos pendientes de entrega\r\n"
    b"Message-ID: <abc@ejemplo.com>\r\n"
    b"\r\nHola\r\n"
)


def test_el_asunto_plegado_se_lee_en_una_linea():
    crudo = _email.message_from_bytes(CORREO_PLEGADO).get("Subject")
    assert "\n" in crudo, "el correo de prueba ya no reproduce el plegado"
    leido = texto_de_cabecera(crudo)
    assert leido == ("Consulta sobre las facturas vencidas del trimestre y el estado "
                     "de los pedidos pendientes de entrega")


def test_responder_a_ese_asunto_no_revienta():
    asunto = texto_de_cabecera(_email.message_from_bytes(CORREO_PLEGADO).get("Subject"))
    msg = EmailMessage()
    msg["Subject"] = valor_de_cabecera(f"Re: {asunto}")     # antes: ValueError
    assert msg["Subject"].startswith("Re: Consulta sobre las facturas")


def test_los_acentos_llegan_legibles():
    crudo = _email.message_from_bytes(
        b"Subject: =?UTF-8?Q?Facturaci=C3=B3n_de_agosto?=\r\n\r\nx\r\n").get("Subject")
    assert texto_de_cabecera(crudo) == "Facturación de agosto"


@pytest.mark.parametrize("sucio", [
    "Asunto\r\nBcc: espia@ejemplo.com",   # header injection through the subject
    "Asunto\ncon salto",
    "  espacios   de   sobra  ",
])
def test_ninguna_cabecera_saliente_lleva_saltos(sucio):
    msg = EmailMessage()
    msg["Subject"] = valor_de_cabecera(sucio)               # no debe lanzar
    assert "\n" not in msg["Subject"] and "\r" not in msg["Subject"]


def test_una_cabecera_rota_no_impide_contestar():
    # Invalid encoding: degrades to plain text without line breaks, never raises.
    assert texto_de_cabecera("=?UTF-8?B?no-es-base64!!?=") != ""
