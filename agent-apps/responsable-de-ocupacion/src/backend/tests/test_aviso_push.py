"""The push notification to the phone when a turn finishes (12.55.0).

What is pinned here is what no screen shows until it fails in someone's pocket:

  · it is only sent to a real Expo token — the field is filled in by a client;
  · what the person STOPPED is not notified: they already know it finished;
  · a pending question and an error are said as such, not as «your answer is ready»;
  · the text never carries the answer: it is seen with the phone locked;
  · an Expo failure does not raise: a lost notification must not spoil the turn;
  · the turn notifies when it finishes, and does not notify without a token.
"""
from __future__ import annotations

import asyncio

import pytest

from app import aviso_push as ap

TOKEN = "ExponentPushToken[abcdefghijklmnop12345]"


def test_solo_tokens_de_expo():
    assert ap.token_valido(TOKEN) == TOKEN
    assert ap.token_valido("ExpoPushToken[abcdefghijklmnop12345]")
    for malo in ("", None, "https://otra.example.com", "ExponentPushToken[a]",
                 "ExponentPushToken[abc def ghij klmn]", TOKEN + "\nX: y"):
        assert ap.token_valido(malo) is None


def test_que_se_dice_segun_como_acabo():
    fin = [{"type": "text_delta", "content": "hotel X, 90 €"}, {"type": "complete"}]
    texto = ap.texto_del_aviso(fin, "complete")
    assert texto and "respuesta" in texto and "hotel" not in texto
    assert "pregunta" in ap.texto_del_aviso([{"type": "question", "questions": []}], "complete")
    assert "No ha podido" in ap.texto_del_aviso([{"type": "error"}], "error")
    assert ap.texto_del_aviso([{"type": "aborted"}], "error") is None


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {"data": {"status": "ok"}}
        self.text = str(self._body)

    def json(self):
        return self._body


def _http(monkeypatch, resp=None, lanza=False):
    enviados: list = []

    class _Cli:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, headers=None):
            if lanza:
                raise RuntimeError("expo caído")
            enviados.append({"url": url, "json": json})
            return resp or _Resp()
    monkeypatch.setattr(ap.httpx, "AsyncClient", _Cli)
    monkeypatch.setattr(ap, "_nombre_del_empleado", lambda: "Asistente de Viajes")
    return enviados


@pytest.mark.asyncio
async def test_manda_a_expo_con_titulo_y_sin_contenido(monkeypatch):
    enviados = _http(monkeypatch)
    ok = await ap.avisar(TOKEN, [{"type": "text_delta", "content": "secreto"}], "complete", "h1")
    assert ok is True
    m = enviados[0]["json"]
    assert enviados[0]["url"].startswith("https://exp.host/")
    assert m["to"] == TOKEN and m["title"] == "Asistente de Viajes"
    assert "secreto" not in str(m) and m["data"] == {"que": "turno_terminado", "hilo": "h1"}


@pytest.mark.asyncio
async def test_ni_token_malo_ni_parado_llegan_a_expo(monkeypatch):
    enviados = _http(monkeypatch)
    assert await ap.avisar("https://otra.example.com", [], "complete") is False
    assert await ap.avisar(TOKEN, [{"type": "aborted"}], "error") is False
    assert enviados == []


@pytest.mark.asyncio
async def test_si_expo_falla_no_lanza(monkeypatch):
    _http(monkeypatch, lanza=True)
    assert await ap.avisar(TOKEN, [], "complete") is False
    _http(monkeypatch, resp=_Resp(200, {"data": {"status": "error", "message": "DeviceNotRegistered"}}))
    assert await ap.avisar(TOKEN, [], "complete") is False


@pytest.mark.asyncio
async def test_el_turno_avisa_al_terminar_y_solo_con_token(monkeypatch):
    from app import chat_runtime as cr
    llamadas: list = []

    async def falso(token, eventos, estado, hilo=None):
        llamadas.append((token, estado, hilo))
        return True
    monkeypatch.setattr(ap, "avisar", falso)

    async def runner(turn):
        await turn.append({"type": "text_delta", "content": "hola"})
        return "complete"

    t = await cr.start_turn("s:con", "ses", None, runner, aviso_push=TOKEN, hilo="h9")
    await t.task
    await asyncio.sleep(0)
    t2 = await cr.start_turn("s:sin", "ses", None, runner)
    await t2.task
    await asyncio.sleep(0)
    assert llamadas == [(TOKEN, "complete", "h9")]
