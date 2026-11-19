"""Document knowledge library — extraction, chunking, indexing + search (v7.84.0).

The operator uploads files; this module turns each into searchable text with NO
external service:

  1. ``extract_text_sync(path, ext)`` — pull plain text out of a PDF / DOCX / TXT /
     MD / CSV / XLSX (lazy-imported ``pypdf`` / ``python-docx`` / ``openpyxl``). A
     scanned PDF (no embedded text layer) raises ``ExtractionError`` — we do NOT
     OCR, so the document lands in status ``error`` with a clear reason.
  2. ``chunk_text`` — split the text into overlapping, paragraph-aware slices.
  3. ``process_document`` — the async pipeline: extract (in a threadpool) → replace
     the document's chunks → flip status ``ready`` / ``error``. Idempotent, so a boot
     reconciler can safely re-run it for anything left mid-flight by a pod restart.
  4. ``search`` (thin wrapper over ``knowledge_fts.search``) — ranked, governance-
     filtered chunk hits + a trimmed citation snippet; bumps each cited document's
     ``use_count`` / ``last_used_at`` so the operator can see what actually gets used.

Everything degrades safely: extraction errors never crash the request, and a
search over a disabled/absent library just returns ``[]``.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime

from sqlalchemy import delete as sql_delete, func, select, update

from app import config
from app.app_database import app_session, app_write
from app.app_models.knowledge_library import KnowledgeChunk, KnowledgeDocument

logger = logging.getLogger(__name__)


class ExtractionError(Exception):
    """Raised when a file cannot be turned into usable text (unsupported,
    corrupt, or a scanned PDF with no text layer). The reason is operator-facing."""


# ── storage ──────────────────────────────────────────────────────────
def storage_dir() -> str:
    """Absolute directory where uploaded originals live, created on demand.

    ``KNOWLEDGE_STORAGE_DIR`` wins; else a ``knowledge`` folder next to the app
    SQLite DB (the app pod's PRIVATE persistent volume). We deliberately do NOT
    use the shared agent workspace: the employee's OpenCode sidecar can browse
    that workspace by filesystem, which would let it read a ``visibility=internal``
    original raw — bypassing the governance the MCP tools enforce. Keeping
    originals on the app-only volume means the agent reaches them ONLY through the
    governed search/get tools; the operator downloads them via authenticated routes."""
    root = config.KNOWLEDGE_STORAGE_DIR
    if not root:
        root = os.path.join(os.path.dirname(config.APP_DB_PATH) or ".", "knowledge")
    os.makedirs(root, exist_ok=True)
    return root


def ext_of(filename: str) -> str:
    """Lower-case extension without the dot (``markdown`` normalised to ``md``)."""
    ext = os.path.splitext(filename or "")[1].lower().lstrip(".")
    return "md" if ext == "markdown" else ext


# ── extraction ───────────────────────────────────────────────────────
def _extract_txt(path: str) -> str:
    with open(path, "rb") as fh:
        return fh.read().decode("utf-8", errors="replace")


def _extract_csv(path: str) -> str:
    # A CSV is already text; keep it verbatim (rows/columns stay legible to the
    # model and to the FTS tokenizer). Decode leniently.
    return _extract_txt(path)


def _extract_pdf(path: str) -> str:
    try:
        from pypdf import PdfReader
    except Exception as exc:  # pragma: no cover — dependency missing
        raise ExtractionError("PDF support is not available in this deployment.") from exc
    try:
        reader = PdfReader(path)
        pages = reader.pages
    except Exception as exc:
        raise ExtractionError("The PDF could not be read (corrupt or encrypted).") from exc
    parts = []
    for pg in pages:
        try:
            parts.append(pg.extract_text() or "")
        except Exception:
            parts.append("")
    text = "\n\n".join(p.strip() for p in parts if p.strip())
    # A scanned PDF yields (almost) no embedded text — we don't OCR, so be explicit.
    if len(text.strip()) < 20 and len(pages) >= 1:
        raise ExtractionError(
            "This looks like a scanned PDF (image only, no text layer). "
            "Upload a text PDF or a Word/Markdown version instead."
        )
    return text


def _extract_docx(path: str) -> str:
    try:
        import docx  # python-docx
    except Exception as exc:  # pragma: no cover — dependency missing
        raise ExtractionError("Word support is not available in this deployment.") from exc
    try:
        d = docx.Document(path)
    except Exception as exc:
        raise ExtractionError("The Word document could not be read (corrupt).") from exc
    parts = [p.text for p in d.paragraphs if (p.text or "").strip()]
    for table in d.tables:
        for row in table.rows:
            cells = [(c.text or "").strip() for c in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _extract_xlsx(path: str) -> str:
    try:
        from openpyxl import load_workbook
    except Exception as exc:  # pragma: no cover — dependency missing
        raise ExtractionError("Excel support is not available in this deployment.") from exc
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:
        raise ExtractionError("The Excel file could not be read (corrupt).") from exc
    parts = []
    try:
        for ws in wb.worksheets:
            parts.append(f"# {ws.title}")
            for row in ws.iter_rows(values_only=True):
                cells = ["" if v is None else str(v) for v in row]
                if any(c.strip() for c in cells):
                    parts.append("\t".join(cells))
    finally:
        try:
            wb.close()
        except Exception:
            pass
    return "\n".join(parts)


_EXTRACTORS = {
    "txt": _extract_txt, "md": _extract_txt, "csv": _extract_csv,
    "pdf": _extract_pdf, "docx": _extract_docx, "xlsx": _extract_xlsx,
}


def extract_text_sync(path: str, ext: str) -> str:
    """Blocking text extraction for one file. Run it in a threadpool. Raises
    ``ExtractionError`` (operator-facing reason) on any unsupported/failed file."""
    fn = _EXTRACTORS.get((ext or "").lower())
    if fn is None:
        raise ExtractionError(f"Unsupported file type: .{ext or '?'}")
    text = fn(path) or ""
    text = text.replace("\x00", " ").strip()
    if not text:
        raise ExtractionError("No readable text was found in the file.")
    cap = config.KNOWLEDGE_MAX_EXTRACT_CHARS
    if cap and len(text) > cap:
        text = text[:cap]
    return text


# ── chunking ─────────────────────────────────────────────────────────
_PARA_RE = re.compile(r"\n\s*\n")
_WS_RE = re.compile(r"[ \t]+")


def chunk_text(text: str) -> list[str]:
    """Split extracted text into overlapping, paragraph-aware chunks of roughly
    ``KNOWLEDGE_CHUNK_CHARS`` with ``KNOWLEDGE_CHUNK_OVERLAP`` carry-over, so a
    fact split across a boundary is still recoverable from a neighbouring chunk."""
    size = max(200, config.KNOWLEDGE_CHUNK_CHARS)
    overlap = max(0, min(config.KNOWLEDGE_CHUNK_OVERLAP, size // 2))
    paras = [_WS_RE.sub(" ", p.strip()) for p in _PARA_RE.split(text or "") if p.strip()]
    chunks: list[str] = []
    buf = ""
    for para in paras:
        # A single oversized paragraph is hard-split so no chunk blows the budget.
        while len(para) > size:
            if buf:
                chunks.append(buf.strip()); buf = ""
            chunks.append(para[:size].strip())
            para = para[max(0, size - overlap):]
        if len(buf) + len(para) + 2 > size and buf:
            chunks.append(buf.strip())
            tail = buf[-overlap:] if overlap else ""
            buf = (tail + "\n" + para).strip()
        else:
            buf = (buf + "\n" + para).strip() if buf else para
    if buf.strip():
        chunks.append(buf.strip())
    return [c for c in chunks if c]


def make_summary(text: str, limit: int = 500) -> str:
    """A short, clean preview (first meaningful text) shown to the operator."""
    flat = _WS_RE.sub(" ", (text or "").replace("\n", " ")).strip()
    if len(flat) <= limit:
        return flat
    cut = flat[:limit]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > limit * 0.6 else cut).rstrip() + "…"


# ── the async extract→index pipeline ─────────────────────────────────
async def process_document(doc_id: int) -> None:
    """Extract + (re)index one document by id, flipping its status to
    ``ready``/``error``. Creates its own session so it can run as a background
    task (or be re-run by the boot reconciler). Never raises to the caller."""
    from fastapi.concurrency import run_in_threadpool
    async with app_write() as s:
        doc = (await s.execute(
            select(KnowledgeDocument).where(KnowledgeDocument.id == doc_id)
        )).scalar_one_or_none()
        if doc is None:
            return
        abs_path = os.path.join(storage_dir(), doc.filename)
        await s.execute(update(KnowledgeDocument)
                        .where(KnowledgeDocument.id == doc_id)
                        .values(status="indexing", error=None))
        await s.commit()
        ext = doc.ext or ext_of(doc.filename)
    try:
        if not os.path.exists(abs_path):
            raise ExtractionError("The uploaded file is no longer on disk.")
        text = await run_in_threadpool(extract_text_sync, abs_path, ext)
        chunks = chunk_text(text)
        if not chunks:
            raise ExtractionError("No readable text was found in the file.")
    except ExtractionError as exc:
        async with app_write() as s:
            await s.execute(update(KnowledgeDocument)
                            .where(KnowledgeDocument.id == doc_id)
                            .values(status="error", error=str(exc)[:500]))
            await s.commit()
        logger.info("knowledge doc %s extraction failed: %s", doc_id, exc)
        return
    except Exception as exc:  # pragma: no cover — unexpected
        async with app_write() as s:
            await s.execute(update(KnowledgeDocument)
                            .where(KnowledgeDocument.id == doc_id)
                            .values(status="error",
                                    error="Unexpected error while reading the file."))
            await s.commit()
        logger.warning("knowledge doc %s unexpected extraction error", doc_id, exc_info=True)
        return
    async with app_write() as s:
        # Replace chunks wholesale (the FTS triggers keep the index in lockstep).
        await s.execute(sql_delete(KnowledgeChunk).where(KnowledgeChunk.document_id == doc_id))
        for i, ch in enumerate(chunks):
            s.add(KnowledgeChunk(document_id=doc_id, ordinal=i, text=ch))
        await s.execute(update(KnowledgeDocument)
                        .where(KnowledgeDocument.id == doc_id)
                        .values(status="ready", error=None,
                                char_count=len(text), chunk_count=len(chunks),
                                summary=make_summary(text)))
        await s.commit()
    logger.info("knowledge doc %s ready (%d chunks, %d chars)", doc_id, len(chunks), len(text))


async def resume_pending() -> None:
    """Boot reconciler: re-run extraction for any document a pod restart left in
    ``uploading``/``indexing`` (the bytes are still on the PVC). Bounded so a boot
    is never blocked; anything beyond the cap is picked up on a later upload."""
    if not config.KNOWLEDGE_LIBRARY_ENABLED:
        return
    async with app_session() as s:
        rows = (await s.execute(
            select(KnowledgeDocument.id)
            .where(KnowledgeDocument.status.in_(("uploading", "indexing")))
            .order_by(KnowledgeDocument.id.asc()).limit(50)
        )).scalars().all()
    for doc_id in rows:
        try:
            await process_document(int(doc_id))
        except Exception:  # pragma: no cover — never block boot
            logger.debug("resume_pending failed for doc %s", doc_id, exc_info=True)


# ── caps ─────────────────────────────────────────────────────────────
async def library_usage(s) -> dict:
    """Current document count + total bytes, for the limit checks + the summary."""
    n = (await s.execute(select(func.count(KnowledgeDocument.id)))).scalar() or 0
    total = (await s.execute(select(func.coalesce(func.sum(KnowledgeDocument.size_bytes), 0)))).scalar() or 0
    return {"count": int(n), "total_bytes": int(total)}


# ── search (citation-bearing) ────────────────────────────────────────
def _snippet(text: str) -> str:
    flat = _WS_RE.sub(" ", (text or "").replace("\n", " ")).strip()
    limit = config.KNOWLEDGE_SNIPPET_CHARS
    if len(flat) <= limit:
        return flat
    cut = flat[:limit]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > limit * 0.6 else cut).rstrip() + "…"


async def search(query: str, *, attending: bool = True, user_email: str = "",
                 shareable_only: bool = False, limit: int | None = None) -> list[dict]:
    """Governance-filtered, BM25-ranked document search. Returns a list of
    ``{document_id, title, snippet, relevance, visibility}`` and records each cited
    document's usage. ``shareable_only`` excludes ``internal`` docs (use it when the
    answer is going out to a person)."""
    if not config.KNOWLEDGE_LIBRARY_ENABLED:
        return []
    from app import knowledge_fts
    k = limit or config.KNOWLEDGE_SEARCH_LIMIT
    async with app_write() as s:
        hits = await knowledge_fts.search(
            s, query, attending=attending, user_email=user_email,
            shareable_only=shareable_only, limit=k,
        )
        if not hits:
            return []
        seen: set[int] = set()
        for h in hits:
            h["snippet"] = _snippet(h.get("snippet", ""))
            seen.add(int(h["document_id"]))
        if seen:
            await s.execute(
                update(KnowledgeDocument)
                .where(KnowledgeDocument.id.in_(seen))
                .values(use_count=KnowledgeDocument.use_count + 1,
                        last_used_at=datetime.utcnow())
            )
            await s.commit()
    return hits
