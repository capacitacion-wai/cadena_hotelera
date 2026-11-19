"""The default conversation can be DELETED (v12.3.0).

Up to 12.2.1 the server answered 409 "the default conversation cannot be deleted; use
`Limpiar contexto` to start from scratch". It was a program rule disguised as a user rule:
deleting the conversation and starting from scratch are the same thing said two ways, and on
top of that the 409 left the conversation in front of whoever had just asked to remove it.

The REAL reason for the prohibition was the id: the default one was identified by an id
computed from the email (`hilo_semilla_de`), which is the PK, so once that row was deleted
there was no way to create it again. This pins the design that fixes it —the default is the
MARKED row, `is_default`— and the two things that must remain true:

  · deleting it ROTATES it: it leaves the list and another one is born, empty and with NEW
    context;
  · there are never two marked, and the database guarantees that (partial unique index), not
    the code.
"""
from __future__ import annotations

import asyncio

ANA = "ana@ejemplo.com"
BEA = "bea@ejemplo.com"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _con_indice() -> None:
    """The partial unique index is created by `main._ensure_indexes`, not by the models — so
    `create_all`, which is what sets up the DB for these tests, does NOT add it. Without it
    this file would not test the half that matters: that on rotation the mark is removed
    BEFORE the other one is set.
    """
    from sqlalchemy import text
    from app.app_database import app_engine
    async with app_engine.begin() as c:
        await c.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_threads_default "
            "ON chat_threads (user_email) WHERE is_default = 1"
        ))


async def _marcadas(email: str) -> list[str]:
    from sqlalchemy import select
    from app.app_database import app_session
    from app.app_models.runtime import ChatThread
    async with app_session() as s:
        return list((await s.execute(select(ChatThread.id).where(
            ChatThread.user_email == email,
            ChatThread.is_default == 1,
            ChatThread.deleted_at.is_(None),
        ))).scalars().all())


def test_borrar_la_por_defecto_abre_otra_vacia():
    async def caso():
        from app import chat_threads as T
        from app.agent_sessions import hilo_semilla_de
        await _con_indice()

        primera = await T.asegurar_por_defecto(ANA)
        assert primera == hilo_semilla_de(ANA), "la primera nace con el id semilla"

        res = await T.borrar(ANA, primera)
        assert res is not None, "borrar la por defecto ya no es un 409"
        nueva = res["id_por_defecto"]
        assert nueva and nueva != primera, "la nueva no puede reusar el id de la borrada"

        lista = {h["id"]: h for h in await T.listar(ANA)}
        assert primera not in lista, "la borrada se va de la lista"
        assert lista[nueva]["por_defecto"] is True
        assert lista[nueva]["titulo"] == "Conversación"
        assert await _marcadas(ANA) == [nueva], "una sola marcada, siempre"
    _run(caso())


def test_la_nueva_empieza_de_cero_de_verdad():
    """What was asked for was "start from scratch": if the new one inherited the session key
    of the deleted one, it would drag its context along and the conversation would still be
    there even if not visible."""
    async def caso():
        from app import chat_threads as T
        await _con_indice()
        primera = await T.asegurar_por_defecto(ANA)
        nueva = (await T.borrar(ANA, primera))["id_por_defecto"]

        assert T.clave_de(ANA, primera) == f"interactive:{ANA}", "la histórica, sin sufijo"
        assert T.clave_de(ANA, nueva) == f"interactive:{ANA}:{nueva}"
        assert T.clave_de(ANA, nueva) != T.clave_de(ANA, primera)
    _run(caso())


def test_un_cliente_con_el_id_viejo_aterriza_en_la_de_ahora():
    """An open tab or an unrefreshed phone keep sending the id of the deleted one (or the
    alias 'default'). They deserve the current conversation, not a 404 for something they
    did not do."""
    async def caso():
        from app import chat_threads as T
        await _con_indice()
        primera = await T.asegurar_por_defecto(ANA)
        nueva = (await T.borrar(ANA, primera))["id_por_defecto"]

        assert await T.resolver_hilo(ANA, "default") == nueva
        assert await T.resolver_hilo(ANA, primera) == nueva
        assert await T.resolver_hilo(ANA, "") == nueva
    _run(caso())


def test_archivar_la_por_defecto_tambien_rota():
    """Same rule from the other side: if archiving the default were still forbidden, the 409
    would come back through the other door. The archived one stays archived, not deleted."""
    async def caso():
        from app import chat_threads as T
        await _con_indice()
        primera = await T.asegurar_por_defecto(ANA)

        res = await T.archivar(ANA, primera)
        assert res is not None
        nueva = res["id_por_defecto"]
        assert nueva != primera
        assert await _marcadas(ANA) == [nueva]

        # It still exists: archiving does not delete, and it can be restored.
        archivadas = [h for h in await T.listar(ANA, incluir_archivados=True) if h["archivado"]]
        assert [h["id"] for h in archivadas] == [primera]
        assert await T.archivar(ANA, primera, archivar_si=False) is not None
        # And on restoring it, it does NOT stay marked: the default is the current one.
        assert await _marcadas(ANA) == [nueva]
    _run(caso())


def test_recuperar_la_semilla_del_archivo_no_la_confunde_con_la_de_ahora():
    """The case that slipped through when writing the rotation: the seed resolves to the
    current default ONLY if its row no longer exists. If it was archived and someone restored
    it, it is just another conversation — and mapping it would make deleting THAT one delete
    THIS one, silently and without any test noticing."""
    async def caso():
        from app import chat_threads as T
        await _con_indice()
        semilla = await T.asegurar_por_defecto(ANA)
        nueva = (await T.archivar(ANA, semilla))["id_por_defecto"]
        assert await T.archivar(ANA, semilla, archivar_si=False) is not None

        # The seed is alive again and is NOT the default: it resolves to itself.
        assert await T.resolver_hilo(ANA, semilla) == semilla
        # And deleting it deletes the seed, not the default.
        assert await T.borrar(ANA, semilla) is not None
        assert await _marcadas(ANA) == [nueva]
        assert nueva in [h["id"] for h in await T.listar(ANA)]
        # Once deleted, that id may lead to the current one: an old client does not deserve a 404.
        assert await T.resolver_hilo(ANA, semilla) == nueva
    _run(caso())


def test_borrar_una_normal_no_toca_la_por_defecto():
    async def caso():
        from app import chat_threads as T
        await _con_indice()
        por_defecto = await T.asegurar_por_defecto(ANA)
        otra = await T.crear(ANA, titulo="Ruta por Madrid Río")

        res = await T.borrar(ANA, otra.id)
        assert res["id_por_defecto"] == por_defecto, "la por defecto sigue siendo la misma"
        assert await _marcadas(ANA) == [por_defecto]
        assert [h["id"] for h in await T.listar(ANA)] == [por_defecto]
    _run(caso())


def test_borrar_dos_veces_o_lo_que_no_existe_no_inventa_nada():
    async def caso():
        from app import chat_threads as T
        await _con_indice()
        primera = await T.asegurar_por_defecto(ANA)
        nueva = (await T.borrar(ANA, primera))["id_por_defecto"]
        # The already-deleted id does NOT fall into the alias (only the seed does), so it is an
        # honest 404… unless it is precisely the seed, which does resolve to the current one.
        otra = await T.crear(ANA, titulo="Tirar")
        assert await T.borrar(ANA, otra.id) is not None
        assert await T.borrar(ANA, otra.id) is None, "borrar dos veces no es borrar otra cosa"
        assert await T.borrar(ANA, "no-existe") is None
        assert await _marcadas(ANA) == [nueva]
    _run(caso())


def test_borrar_la_mia_no_toca_la_de_otra_persona():
    async def caso():
        from app import chat_threads as T
        await _con_indice()
        de_ana = await T.asegurar_por_defecto(ANA)
        de_bea = await T.asegurar_por_defecto(BEA)
        assert de_ana != de_bea

        # Ana cannot delete Bea's, not even by the exact id.
        assert await T.borrar(ANA, de_bea) is None
        assert await _marcadas(BEA) == [de_bea]

        nueva_de_ana = (await T.borrar(ANA, de_ana))["id_por_defecto"]
        assert await _marcadas(BEA) == [de_bea], "rotar la de Ana no mueve la de Bea"
        assert nueva_de_ana != de_bea
    _run(caso())


def test_asegurar_es_idempotente_y_no_duplica():
    async def caso():
        from app import chat_threads as T
        await _con_indice()
        a = await T.asegurar_por_defecto(ANA)
        b = await T.asegurar_por_defecto(ANA)
        assert a == b
        # In parallel, which is how it really happens: two turns from the same person at once.
        ids = await asyncio.gather(*(T.asegurar_por_defecto(ANA) for _ in range(5)))
        assert set(ids) == {a}
        assert await _marcadas(ANA) == [a]
    _run(caso())
