"""v7.84.0 — DOCUMENT KNOWLEDGE LIBRARY: extraction, chunking + FTS governance.

Covers the invariants that keep the library trustworthy:
  * text extraction for the no-dependency formats (txt/md/csv) + explicit errors
    for empty / unsupported files (a scanned PDF must NOT silently look "ready").
  * chunking stays bounded and loses nothing (overlap carry-over).
  * FTS search honours the governance filters — a DISABLED, non-``ready``,
    out-of-scope, or (when shareable_only) ``internal`` document is never returned.

Uses its OWN engine/session (not the global ``app_session``) so it is hermetic.
"""
from __future__ import annotations

import os
import tempfile

import pytest

pytestmark = pytest.mark.asyncio


def test_chunking_is_bounded_and_lossless() -> None:
    from app import config
    from app.knowledge_library import chunk_text
    # 40 paragraphs of ~200 chars ⇒ several chunks, each within the size budget.
    paras = [f"Parrafo {i} " + ("dato " * 40) for i in range(40)]
    text = "\n\n".join(paras)
    chunks = chunk_text(text)
    assert len(chunks) >= 3
    budget = max(200, config.KNOWLEDGE_CHUNK_CHARS)
    assert all(len(c) <= budget + 5 for c in chunks)
    # A distinctive token from the last paragraph survives somewhere.
    assert any("Parrafo 39" in c for c in chunks)


def test_extract_txt_md_csv_and_errors() -> None:
    from app.knowledge_library import ExtractionError, extract_text_sync, ext_of
    assert ext_of("Tarifas 2026.PDF") == "pdf"
    assert ext_of("notas.markdown") == "md"

    def _write(suffix: str, data: bytes) -> str:
        fd, path = tempfile.mkstemp(suffix=suffix)
        os.write(fd, data)
        os.close(fd)
        return path

    txt = _write(".txt", "El precio del plan Pro es 49 euros al mes.".encode())
    assert "plan Pro" in extract_text_sync(txt, "txt")

    csv = _write(".csv", b"producto,precio\nPro,49\nBasic,19\n")
    out = extract_text_sync(csv, "csv")
    assert "Pro" in out and "49" in out

    empty = _write(".txt", b"   \n  ")
    with pytest.raises(ExtractionError):
        extract_text_sync(empty, "txt")

    with pytest.raises(ExtractionError):
        extract_text_sync(txt, "exe")


def test_make_summary_trims() -> None:
    from app.knowledge_library import make_summary
    s = make_summary("hola " * 500, limit=100)
    assert len(s) <= 101 and s.endswith("…")


async def _fresh_db():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.app_database import AppBase
    from app import knowledge_fts
    from app.app_models import knowledge_library  # noqa: F401 — register tables
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as conn:
        await conn.run_sync(AppBase.metadata.create_all)
        await knowledge_fts.ensure_fts(conn)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _doc(**kw):
    from app.app_models.knowledge_library import KnowledgeDocument
    base = dict(filename="f.txt", title="Tarifas", ext="txt", size_bytes=10,
                workspace_path="knowledge/f.txt", status="ready", enabled=1,
                scope="all", visibility="shareable", char_count=10, chunk_count=1)
    base.update(kw)
    return KnowledgeDocument(**base)


async def test_fts_search_respects_governance() -> None:
    from app.app_models.knowledge_library import KnowledgeChunk
    from app import knowledge_fts
    engine, Session = await _fresh_db()
    async with Session() as s:
        # shareable, all-scope, ready → visible
        d1 = _doc(title="Tarifas Pro", scope="all", visibility="shareable")
        # internal → excluded when shareable_only
        d2 = _doc(title="Margenes", scope="all", visibility="internal")
        # disabled → never
        d3 = _doc(title="Viejo", enabled=0)
        # not ready → never
        d4 = _doc(title="Procesando", status="indexing")
        # autonomous-only scope → hidden while attending
        d5 = _doc(title="Ronda nocturna", scope="autonomous")
        for d in (d1, d2, d3, d4, d5):
            s.add(d)
        await s.flush()
        s.add(KnowledgeChunk(document_id=d1.id, ordinal=0, text="El precio del plan Pro es 49 euros"))
        s.add(KnowledgeChunk(document_id=d2.id, ordinal=0, text="El margen del plan Pro es del 30 por ciento"))
        s.add(KnowledgeChunk(document_id=d3.id, ordinal=0, text="precio antiguo del plan Pro"))
        s.add(KnowledgeChunk(document_id=d4.id, ordinal=0, text="precio del plan Pro pendiente"))
        s.add(KnowledgeChunk(document_id=d5.id, ordinal=0, text="precio del plan Pro nocturno"))
        await s.commit()

        # attending, include internal
        hits = await knowledge_fts.search(s, "precio plan Pro", attending=True,
                                          user_email="", shareable_only=False)
        titles = {h["title"] for h in hits}
        assert "Tarifas Pro" in titles
        assert "Margenes" in titles          # internal returned for reasoning
        assert "Viejo" not in titles         # disabled
        assert "Procesando" not in titles    # not ready
        assert "Ronda nocturna" not in titles  # autonomous-only, not while attending

        # shareable_only drops the internal doc
        hits_share = await knowledge_fts.search(s, "plan Pro", attending=True,
                                               user_email="", shareable_only=True)
        assert "Margenes" not in {h["title"] for h in hits_share}

        # background run sees the autonomous-scoped doc, not the attending-only ones
        hits_bg = await knowledge_fts.search(s, "plan Pro", attending=False,
                                            user_email="", shareable_only=False)
        assert "Ronda nocturna" in {h["title"] for h in hits_bg}
    await engine.dispose()


async def test_user_scope_matches_only_that_person() -> None:
    from app.app_models.knowledge_library import KnowledgeChunk
    from app import knowledge_fts
    engine, Session = await _fresh_db()
    async with Session() as s:
        d = _doc(title="Notas de Ana", scope="user", user_email="ana@acme.com")
        s.add(d)
        await s.flush()
        s.add(KnowledgeChunk(document_id=d.id, ordinal=0, text="condiciones especiales para el plan Pro"))
        await s.commit()

        mine = await knowledge_fts.search(s, "plan Pro", attending=True,
                                         user_email="ana@acme.com", shareable_only=False)
        assert "Notas de Ana" in {h["title"] for h in mine}

        other = await knowledge_fts.search(s, "plan Pro", attending=True,
                                          user_email="bob@acme.com", shareable_only=False)
        assert "Notas de Ana" not in {h["title"] for h in other}
    await engine.dispose()
