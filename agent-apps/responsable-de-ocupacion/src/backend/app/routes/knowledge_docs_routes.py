"""Operator-managed DOCUMENT KNOWLEDGE LIBRARY (Operations → Conocimiento → Documentos).

CRUD over ``KnowledgeDocument`` + the uploaded originals on the app PVC. Uploading
kicks off the extract→index pipeline (``knowledge_library.process_document``) as a
background task; the row shows a live status the SPA polls (Subiendo → Indexando →
Listo/Error). The agent reads the library ONLY through the MCP tools
(``list_knowledge_docs`` / ``search_knowledge`` / ``get_knowledge_doc``); this API is
the operator surface.

v7.88.0 — document reads (list / preview / download) require ``require_admin``:
they expose every document's content, including ``visibility=internal`` and other
people's ``scope=user`` docs, so a rank-and-file member must not see them (the
agent still reaches the library, scope/visibility-filtered, via the MCP tools).
``/summary`` + ``/settings`` reads stay project-access (no document content); every
mutation (upload / edit / reindex / delete) is admin — an admin curates content.
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime
from typing import Any, Optional

from fastapi import (
    APIRouter, BackgroundTasks, Body, Depends, File, Form, UploadFile,
)
from fastapi.responses import FileResponse
from sqlalchemy import delete as sql_delete, func, select, update

from app import config
from app import knowledge_library as klib
from app.api_errors import api_error
from app.app_database import app_session, app_write
from app.app_models.knowledge_library import (
    DOC_SCOPES, DOC_VISIBILITY, SUPPORTED_EXTS, KnowledgeChunk, KnowledgeDocument,
)
from app.auth import require_admin, require_project_access
from app.timeutil import iso_utc

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agent/knowledge", tags=["knowledge"])

# Human-facing extension list for the "supported types" hint + client validation.
_ACCEPT_EXTS = tuple(e for e in SUPPORTED_EXTS if e != "markdown")


def _row_to_dict(r: KnowledgeDocument) -> dict[str, Any]:
    return {
        "id": r.id,
        "title": r.title,
        "filename": r.filename,
        "ext": r.ext,
        "mime": r.mime,
        "size_bytes": int(r.size_bytes or 0),
        "status": r.status,
        "error": r.error,
        "enabled": bool(r.enabled),
        "scope": r.scope,
        "user_email": r.user_email,
        "visibility": r.visibility,
        "tags": r.tags,
        "summary": r.summary,
        "char_count": int(r.char_count or 0),
        "chunk_count": int(r.chunk_count or 0),
        "use_count": int(r.use_count or 0),
        "last_used_at": iso_utc(r.last_used_at) if r.last_used_at else None,
        "valid_until": iso_utc(r.valid_until) if r.valid_until else None,
        "uploaded_by": r.uploaded_by,
        "created_at": iso_utc(r.created_at),
        "updated_at": iso_utc(r.updated_at),
    }


def _parse_date(raw: str) -> Optional[datetime]:
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw[: len(datetime.utcnow().strftime(fmt))], fmt)
        except ValueError:
            continue
    return None


def _clean_scope(scope: str, visibility: str, user_email: str) -> tuple[str, str, Optional[str]]:
    scope = (scope or "all").strip().lower()
    if scope not in DOC_SCOPES:
        scope = "all"
    visibility = (visibility or "shareable").strip().lower()
    if visibility not in DOC_VISIBILITY:
        visibility = "shareable"
    email = (user_email or "").strip().lower() or None
    if scope == "user" and not email:
        raise api_error(400, "knowledge.user_email_required",
                        "A user e-mail is required when the scope is a single person.")
    if scope != "user":
        email = None
    return scope, visibility, email


@router.get("/settings")
async def get_knowledge_settings(user=Depends(require_project_access)):
    """The knowledge-area settings the operator controls in Configuración —
    today the SENSITIVE TOPICS list (topics on which the employee must not
    commit on its own; it routes them through an approval)."""
    from app.knowledge_prompt import sensitive_topics
    return {"sensitive_topics": await sensitive_topics()}


@router.put("/settings")
async def put_knowledge_settings(payload: dict = Body(...), user=Depends(require_admin)):
    """Set the SENSITIVE TOPICS list (admin only). Accepts a list or a
    comma/newline separated string; stored normalised in ``AppSetting``."""
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    from app.app_models.runtime import AppSetting
    from app.knowledge_prompt import SENSITIVE_TOPICS_KEY, sensitive_topics
    raw = payload.get("sensitive_topics", "")
    if isinstance(raw, list):
        items = [str(x).strip() for x in raw]
    else:
        items = [p.strip() for p in str(raw).replace("\n", ",").split(",")]
    seen: list[str] = []
    for it in items:
        if it and it.lower() not in {x.lower() for x in seen}:
            seen.append(it[:80])
    value = ", ".join(seen[:50])
    async with app_write() as s:
        await s.execute(
            sqlite_insert(AppSetting)
            .values(key=SENSITIVE_TOPICS_KEY, value=value)
            .on_conflict_do_update(index_elements=[AppSetting.key], set_={"value": value})
        )
        await s.commit()
    return {"sensitive_topics": await sensitive_topics()}


@router.get("/docs")
async def list_docs(user=Depends(require_admin)):
    """All knowledge documents (metadata only), newest first."""
    if not config.KNOWLEDGE_LIBRARY_ENABLED:
        return {"documents": [], "enabled": False}
    async with app_session() as s:
        rows = (await s.execute(
            select(KnowledgeDocument).order_by(KnowledgeDocument.created_at.desc())
        )).scalars().all()
    return {"documents": [_row_to_dict(r) for r in rows], "enabled": True}


@router.get("/docs/summary")
async def docs_summary(user=Depends(require_project_access)):
    """Usage + limits + a status breakdown, for the Documentos header."""
    if not config.KNOWLEDGE_LIBRARY_ENABLED:
        return {"enabled": False}
    async with app_session() as s:
        usage = await klib.library_usage(s)
        by_status = dict((k, int(v)) for k, v in (await s.execute(
            select(KnowledgeDocument.status, func.count(KnowledgeDocument.id))
            .group_by(KnowledgeDocument.status)
        )).all())
    return {
        "enabled": True,
        "count": usage["count"],
        "total_bytes": usage["total_bytes"],
        "by_status": by_status,
        "limits": {
            "max_docs": config.KNOWLEDGE_MAX_DOCS,
            "max_file_bytes": config.KNOWLEDGE_MAX_FILE_MB * 1024 * 1024,
            "max_total_bytes": config.KNOWLEDGE_MAX_TOTAL_MB * 1024 * 1024,
            "accepted_exts": list(_ACCEPT_EXTS),
        },
    }


@router.post("/docs")
async def upload_doc(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    title: str = Form(""),
    scope: str = Form("all"),
    visibility: str = Form("shareable"),
    tags: str = Form(""),
    user_email: str = Form(""),
    valid_until: str = Form(""),
    user=Depends(require_admin),
):
    """Upload ONE document. Validates type + size + library caps, stores the
    original on the PVC, creates the row (status ``uploading``) and schedules the
    extract→index pipeline. Returns the created document."""
    if not config.KNOWLEDGE_LIBRARY_ENABLED:
        raise api_error(503, "knowledge.disabled", "The knowledge library is disabled in this deployment.")
    orig = (file.filename or "").strip()
    ext = klib.ext_of(orig)
    if ext not in SUPPORTED_EXTS:
        raise api_error(400, "knowledge.unsupported_type",
                        f"Unsupported file type “.{ext or '?'}”. Allowed: {', '.join(_ACCEPT_EXTS)}.")
    scope, visibility, email = _clean_scope(scope, visibility, user_email)

    # Enforce the document-count cap up front.
    async with app_session() as s:
        usage = await klib.library_usage(s)
    if usage["count"] >= config.KNOWLEDGE_MAX_DOCS:
        raise api_error(409, "knowledge.too_many_docs",
                        f"The library is full ({config.KNOWLEDGE_MAX_DOCS} documents). Delete one first.")

    # Stream to a unique on-disk name, enforcing the per-file cap as we go.
    stored = f"{uuid.uuid4().hex}.{ext}"
    abs_path = os.path.join(klib.storage_dir(), stored)
    max_file = config.KNOWLEDGE_MAX_FILE_MB * 1024 * 1024
    max_total = config.KNOWLEDGE_MAX_TOTAL_MB * 1024 * 1024
    size = 0
    over = False
    try:
        with open(abs_path, "wb") as fh:
            while True:
                chunk = await file.read(256 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_file or usage["total_bytes"] + size > max_total:
                    over = True
                    break
                fh.write(chunk)
    except OSError as exc:
        _safe_unlink(abs_path)
        raise api_error(500, "knowledge.write_failed", f"Could not store the file: {exc}")
    finally:
        await file.close()
    if over:
        _safe_unlink(abs_path)
        raise api_error(413, "knowledge.too_large",
                        f"The file exceeds the limit ({config.KNOWLEDGE_MAX_FILE_MB} MB per file, "
                        f"{config.KNOWLEDGE_MAX_TOTAL_MB} MB total).")
    if size == 0:
        _safe_unlink(abs_path)
        raise api_error(400, "knowledge.empty_file", "The uploaded file is empty.")

    display = (title or "").strip() or _display_from_name(orig)
    async with app_write() as s:
        doc = KnowledgeDocument(
            filename=stored, title=display[:300], mime=file.content_type,
            ext=ext, size_bytes=size, workspace_path=f"knowledge/{stored}",
            status="uploading", enabled=1, scope=scope, user_email=email,
            visibility=visibility, tags=(tags or "").strip() or None,
            valid_until=_parse_date(valid_until),
            uploaded_by=(getattr(user, "email", "") or None),
        )
        s.add(doc)
        await s.commit()
        await s.refresh(doc)
        doc_id = doc.id
        out = _row_to_dict(doc)
    # Extract + index off the request path; the SPA polls the status.
    background.add_task(klib.process_document, doc_id)
    return out


@router.patch("/docs/{doc_id}")
async def update_doc(doc_id: int, payload: dict = Body(...), user=Depends(require_admin)):
    """Edit governance/metadata of a document: title, enabled, scope, visibility,
    tags, user_email, valid_until. Content is immutable — re-upload to change it."""
    async with app_write() as s:
        doc = (await s.execute(
            select(KnowledgeDocument).where(KnowledgeDocument.id == doc_id)
        )).scalar_one_or_none()
        if doc is None:
            raise api_error(404, "knowledge.not_found", "Document not found.")
        values: dict[str, Any] = {}
        if "title" in payload:
            t = (payload.get("title") or "").strip()
            if t:
                values["title"] = t[:300]
        if "enabled" in payload:
            values["enabled"] = 1 if payload.get("enabled") else 0
        if "tags" in payload:
            values["tags"] = (payload.get("tags") or "").strip() or None
        if "valid_until" in payload:
            values["valid_until"] = _parse_date(payload.get("valid_until") or "")
        if any(k in payload for k in ("scope", "visibility", "user_email")):
            scope, visibility, email = _clean_scope(
                payload.get("scope", doc.scope),
                payload.get("visibility", doc.visibility),
                payload.get("user_email", doc.user_email or ""),
            )
            values.update(scope=scope, visibility=visibility, user_email=email)
        if values:
            values["updated_at"] = datetime.utcnow()
            await s.execute(update(KnowledgeDocument)
                            .where(KnowledgeDocument.id == doc_id).values(**values))
            await s.commit()
        doc = (await s.execute(
            select(KnowledgeDocument).where(KnowledgeDocument.id == doc_id)
        )).scalar_one()
        return _row_to_dict(doc)


@router.post("/docs/{doc_id}/reindex")
async def reindex_doc(doc_id: int, background: BackgroundTasks, user=Depends(require_admin)):
    """Re-run extraction + indexing for a document (e.g. after an error)."""
    async with app_write() as s:
        doc = (await s.execute(
            select(KnowledgeDocument).where(KnowledgeDocument.id == doc_id)
        )).scalar_one_or_none()
        if doc is None:
            raise api_error(404, "knowledge.not_found", "Document not found.")
        await s.execute(update(KnowledgeDocument)
                        .where(KnowledgeDocument.id == doc_id)
                        .values(status="uploading", error=None))
        await s.commit()
    background.add_task(klib.process_document, doc_id)
    return {"ok": True}


@router.get("/docs/{doc_id}/preview")
async def preview_doc(doc_id: int, user=Depends(require_admin)):
    """The first few extracted chunks — lets the operator confirm the text came
    out clean before trusting the employee to cite it."""
    async with app_session() as s:
        doc = (await s.execute(
            select(KnowledgeDocument).where(KnowledgeDocument.id == doc_id)
        )).scalar_one_or_none()
        if doc is None:
            raise api_error(404, "knowledge.not_found", "Document not found.")
        chunks = (await s.execute(
            select(KnowledgeChunk.text).where(KnowledgeChunk.document_id == doc_id)
            .order_by(KnowledgeChunk.ordinal.asc()).limit(5)
        )).scalars().all()
    return {"id": doc_id, "title": doc.title, "status": doc.status,
            "summary": doc.summary, "preview": "\n\n".join(chunks)}


@router.get("/docs/{doc_id}/download")
async def download_doc(doc_id: int, user=Depends(require_admin)):
    """Download the original uploaded file."""
    async with app_session() as s:
        doc = (await s.execute(
            select(KnowledgeDocument).where(KnowledgeDocument.id == doc_id)
        )).scalar_one_or_none()
        if doc is None:
            raise api_error(404, "knowledge.not_found", "Document not found.")
    abs_path = os.path.join(klib.storage_dir(), doc.filename)
    if not os.path.exists(abs_path):
        raise api_error(404, "knowledge.file_missing", "The stored file is no longer available.")
    return FileResponse(abs_path, filename=_download_name(doc),
                        media_type=doc.mime or "application/octet-stream")


@router.delete("/docs/{doc_id}")
async def delete_doc(doc_id: int, user=Depends(require_admin)):
    """Delete a document: its chunks (FTS follows), the row and the stored file."""
    async with app_write() as s:
        doc = (await s.execute(
            select(KnowledgeDocument).where(KnowledgeDocument.id == doc_id)
        )).scalar_one_or_none()
        if doc is None:
            raise api_error(404, "knowledge.not_found", "Document not found.")
        filename = doc.filename
        # Delete chunks explicitly (SQLite FK cascade is not relied upon); the FTS
        # sync triggers keep the index in lockstep.
        await s.execute(sql_delete(KnowledgeChunk).where(KnowledgeChunk.document_id == doc_id))
        await s.execute(sql_delete(KnowledgeDocument).where(KnowledgeDocument.id == doc_id))
        await s.commit()
    _safe_unlink(os.path.join(klib.storage_dir(), filename))
    return {"ok": True}


# ── helpers ──────────────────────────────────────────────────────────
def _safe_unlink(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        logger.debug("could not unlink %s", path, exc_info=True)


def _display_from_name(name: str) -> str:
    base = os.path.basename((name or "").strip().replace("\\", "/"))
    stem = os.path.splitext(base)[0].replace("_", " ").replace("-", " ").strip()
    return (stem or base or "Documento")[:300]


def _download_name(doc: KnowledgeDocument) -> str:
    from app import workspace_fs as wfs
    base = wfs.sanitize_name(doc.title or doc.filename) or "documento"
    if doc.ext and not base.lower().endswith("." + doc.ext):
        base = f"{base}.{doc.ext}"
    return base
