"""A person's «preferred channel» means the same in all FIVE places (7.158.2).

A value domain copied by hand drifts out of sync — and this one did: when
Telegram became a real channel (7.155.0, notifier and address resolution), the
five places that decide which values are legal stayed at ("email", "whatsapp"):

  · `routes/users_routes._meta_updates`  → returned 422 for «telegram»
  · `mcp_server.create_user`             → silently dropped the preference
  · `mcp_server.find_user`               → did not show it to the agent
  · `memory_prompt`                      → did not tell the model about it
  · `frontend/src/pages/Users.tsx`       → did not even appear in the dropdown

Nobody saw an error: the dropdown simply did not offer Telegram. These tests cross
the single source (`users_directory.REACHABLE_CHANNELS`) with the five consumers, without
starting the app.

The rule that really matters is the last one: **a channel can only be offered as preferred
if we know how to WRITE to that person on it**. That is why Slack is out: the profile does
not store a Slack handle, so that preference would be accepted and then silently lost
because no recipient could be resolved.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from app.users_directory import CHANNEL_LABELS_ES, REACHABLE_CHANNELS

_APP = pathlib.Path(__file__).resolve().parent.parent / "app"
_FRONT = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "src"

# The (file, pattern) pair that gave away each manual copy of the domain.
_SIN_COPIAS = ("routes/users_routes.py", "mcp_server.py", "memory_prompt.py",
               "channels/notifier.py")


def test_telegram_es_un_canal_preferido_valido():
    """The concrete regression the user reported: Telegram was missing."""
    assert "telegram" in REACHABLE_CHANNELS


def test_ningun_modulo_reescribe_el_dominio_a_mano():
    culpables = []
    for rel in _SIN_COPIAS:
        txt = (_APP / rel).read_text(encoding="utf-8")
        for m in re.finditer(r'\("email", "whatsapp"\)|\("whatsapp", "email"\)', txt):
            linea = txt[:m.start()].count("\n") + 1
            culpables.append(f"{rel}:{linea}")
    assert not culpables, (
        "Alguien volvió a escribir el dominio a mano en vez de importar "
        "REACHABLE_CHANNELS; la próxima vía que añadamos se quedará fuera de "
        "ahí sin que nadie lo note:\n  " + "\n  ".join(culpables)
    )


def test_el_desplegable_ofrece_exactamente_las_vias_del_backend():
    """If the backend gains a channel and the dropdown does not, nobody can pick it."""
    txt = (_FRONT / "pages" / "Users.tsx").read_text(encoding="utf-8")
    # Anchor on the field's LABEL, not on the state name: the `useState`
    # appears earlier and the slice swallowed the role dropdown.
    bloque = txt[txt.index("users.fields.preferredChannel"):]
    bloque = bloque[:bloque.index("</TextField>")]
    ofrecidas = set(re.findall(r'<MenuItem value="([a-z_]+)"', bloque))
    assert ofrecidas == set(REACHABLE_CHANNELS), (
        f"El desplegable ofrece {sorted(ofrecidas)} y el backend acepta "
        f"{sorted(REACHABLE_CHANNELS)}."
    )


def test_la_ruta_acepta_todas_las_vias_y_nada_mas():
    """Route validation = single source (otherwise, 422 for a legal value)."""
    txt = (_APP / "routes" / "users_routes.py").read_text(encoding="utf-8")
    assert "REACHABLE_CHANNELS" in txt


def test_cada_via_tiene_etiqueta_para_el_prompt():
    faltan = [k for k in REACHABLE_CHANNELS if k not in CHANNEL_LABELS_ES]
    assert not faltan, f"sin etiqueta humana para el modelo: {faltan}"


@pytest.mark.parametrize("kind", REACHABLE_CHANNELS)
def test_toda_via_ofrecida_se_sabe_DIRECCIONAR(kind):
    """The underlying invariant: offering as preferred a channel whose address we
    cannot resolve creates a preference that is silently lost. That is
    exactly why Slack is NOT on the list."""
    txt = (_APP / "signal_notify.py").read_text(encoding="utf-8")
    cuerpo = txt[txt.index("async def _resolve_address"):]
    cuerpo = cuerpo[:cuerpo.index("\nasync def ", 1)] if "\nasync def " in cuerpo[1:] else cuerpo
    assert f'k == "{kind}"' in cuerpo, (
        f"«{kind}» se puede elegir como canal preferido pero _resolve_address no "
        f"sabe sacar la dirección de esa persona: el aviso se perdería sin ruido."
    )


def test_slack_sigue_fuera_y_por_su_motivo():
    """Documents the exclusion so nobody «fixes» it by adding it."""
    assert "slack" not in REACHABLE_CHANNELS
    txt = (_APP / "signal_notify.py").read_text(encoding="utf-8")
    assert "Slack has" in txt or "Slack" in txt
