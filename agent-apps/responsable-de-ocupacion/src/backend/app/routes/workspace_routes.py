"""Operations → Workspace tab API.

Lets an operator browse and manage the shared agent workspace from the
Operations page, mirroring (adapted) the interactive code-agent file
browser: list a folder, preview/download files, download a folder as
ZIP, upload, create folders, move, unzip and delete.

v7.88.0 — browsing/managing the SHARED workspace requires ``require_admin``:
the workspace holds every operator's chat uploads, exports, memory and channel
inbox under ``sessions/users/<token>/``, so a rank-and-file member must not list
or download it. The ONE member-facing endpoint is ``/chat-upload`` (its
destination is derived server-side from the caller's identity, so it can only
write into that member's own folder). Every path is still confined to the
workspace root by ``app.workspace_fs`` (realpath, no ``..``/symlink escape).
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import zipfile

from fastapi import (
    APIRouter, Depends, File, Form, HTTPException, Query, UploadFile,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from app import config
from app import workspace_fs as wfs
from app.api_errors import api_error
from app.auth import require_admin, require_project_access

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agent/workspace", tags=["workspace"])


@router.get("/usage")
async def workspace_usage(user=Depends(require_admin)):
    """Storage footprint of the shared workspace: logical size (sum of
    file bytes), file count and the free/total of the backing PVC. Used
    by the Operations → Workspace tab to monitor disk pressure. The tree
    walk runs in a threadpool so a large workspace never blocks the loop.
    """
    return await run_in_threadpool(wfs.disk_usage)


def _db_usage() -> dict:
    """Size of the local (writable) SQLite database — the main file plus
    its ``-wal`` / ``-shm`` sidecars — and the free/total of the volume
    that backs it. Blocking ``stat`` calls; run in a threadpool."""
    db_path = config.APP_DB_PATH
    files: list[dict] = []
    total = 0
    for suffix in ("", "-wal", "-shm"):
        p = f"{db_path}{suffix}"
        try:
            size = int(os.path.getsize(p))
        except OSError:
            continue
        files.append({"name": os.path.basename(p), "bytes": size})
        total += size
    available = bool(files) or os.path.exists(db_path)
    vol_dir = os.path.dirname(db_path) or "/"
    return {
        "available": available,
        "path": db_path,
        "total_bytes": total,
        "files": files,
        **wfs.volume_stats(vol_dir),
    }


@router.get("/db-usage")
async def database_usage(user=Depends(require_admin)):
    """Storage footprint of the agent's local application database
    (alerts, communications, decisions, scheduled items, run history…).
    Powers the Operations → Database monitoring tab."""
    return await run_in_threadpool(_db_usage)


def _normalizar_rel(path: str) -> list[str]:
    """Segments of a path relative to ``/workspace``, sanitized.

    Strips a stray ``workspace/`` prefix: paths are relative to ``/workspace`` by contract,
    so that prefix is always a caller error — and it really arrived (the connector asked for
    ``path=workspace/sessions/users/…``, which resolved to ``/workspace/workspace/…`` and did
    not exist). ``..`` segments are kept so the validator rejects them: normalizing them
    here would hide a traversal attempt."""
    rel = (path or "").strip().lstrip("/")
    partes = [p for p in rel.split("/") if p not in ("", ".")]
    if partes and partes[0] == "workspace":
        partes = partes[1:]
    return partes


async def _ruta_de_esta_persona(path: str, user, *, vacio_es_mi_carpeta: bool = False) -> str:
    """The path this user MAY touch, or 403 (v12.0.0).

    Browsing the SHARED workspace is still for administrators. But each person's folder
    (``sessions/users/<token>/…``) is theirs: it holds the files they upload in the chat and
    what the employee generates for them in ``exports/``. Keeping it admin-only made the whole
    mechanism useless for a member: the ``X-WAI-Workspace-Url`` header the proxy announces to
    connectors points at ``/download``, so a member who attached a photo saw the connector
    crash into a 403 — and it looked as if the header was not arriving. Same rule and same
    folder computation as ``/chat-upload-to``: derived from the authenticated identity, never
    from the client. A ``..`` is ALWAYS rejected (even if the prefix matched)."""
    partes = _normalizar_rel(path)
    if ".." in partes:
        raise api_error(403, "workspace.outside_scope", "Ruta no válida.")
    rel = "/".join(partes)
    from app.agent_sessions import interactive_scope
    email = (getattr(user, "email", "") or "").strip().lower()
    alcance = interactive_scope(email).workspace_subdir if email else ""
    if not rel and vacio_es_mi_carpeta and alcance:
        return alcance
    if alcance and (rel == alcance or rel.startswith(alcance + "/")):
        return rel
    from app.users_directory import is_admin
    if await is_admin(email):
        return rel
    raise api_error(
        403, "workspace.outside_scope",
        "Solo puedes acceder a los ficheros de tu propia carpeta.",
    )


@router.get("/list")
async def list_workspace(
    path: str = Query("", description="Folder relative to /workspace"),
    user=Depends(require_project_access),
):
    """One level of the workspace (folders first). Returns
    ``available=false`` when no workspace mount is configured.

    An administrator browses everything; a member, THEIR folder (and with no path, theirs)."""
    return wfs.list_dir(await _ruta_de_esta_persona(path, user, vacio_es_mi_carpeta=True))


@router.get("/preview")
async def preview_file(
    path: str = Query(..., description="File relative to /workspace"),
    user=Depends(require_project_access),
):
    """Inline preview for text files (<=5MB); metadata only for binary
    or oversized files so the SPA falls back to download."""
    return wfs.read_text_preview(await _ruta_de_esta_persona(path, user))


@router.get("/download")
async def download_file(
    path: str = Query(..., description="File relative to /workspace"),
    user=Depends(require_project_access),
):
    target = wfs.resolve(await _ruta_de_esta_persona(path, user), must_exist=True)
    if not os.path.isfile(target):
        raise api_error(400, "workspace.not_file", "Path is not a file.")
    return FileResponse(
        target,
        media_type=wfs.guess_mime(target),
        filename=os.path.basename(target),
    )


@router.get("/download-all")
async def download_zip(
    path: str = Query("", description="Folder relative to /workspace"),
    user=Depends(require_admin),
):
    """Stream a ZIP of a folder (or the whole workspace when path empty)."""
    members = list(wfs.iter_zip_members(path))
    base = os.path.basename((path or "").rstrip("/")) or "workspace"

    def _gen():
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for abs_path, arc in members:
                try:
                    zf.write(abs_path, arc)
                except OSError:
                    continue
        buf.seek(0)
        yield from buf

    return StreamingResponse(
        _gen(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{base}.zip"'},
    )


async def _stage_uploads(dest_dir: str, files: list[UploadFile]) -> dict:
    """Write uploaded files into ``dest_dir`` (already resolved + confined),
    enforcing the per-file size cap. Returns uploaded (workspace-relative
    paths) + rejected lists."""
    if not os.path.isdir(dest_dir):
        raise api_error(
            400, "workspace.dest_not_directory", "Destination is not a directory.",
        )
    uploaded: list[str] = []
    rejected: list[dict[str, str]] = []
    root = wfs.require_root()
    for up in files:
        name = wfs.sanitize_name(up.filename or "")
        if not name:
            rejected.append({"filename": up.filename or "", "reason": "invalid_name"})
            continue
        target = os.path.join(dest_dir, name)
        # Confinement re-check on the final resolved path.
        real = os.path.realpath(target)
        if real != root and not real.startswith(root + os.sep):
            rejected.append({"filename": name, "reason": "outside_workspace"})
            continue
        size = 0
        too_big = False
        try:
            with open(target, "wb") as fh:
                while True:
                    chunk = await up.read(256 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > wfs.MAX_UPLOAD_BYTES:
                        too_big = True
                        break
                    fh.write(chunk)
            if too_big:
                os.remove(target)
                rejected.append({"filename": name, "reason": "too_large"})
            else:
                uploaded.append(wfs._rel(root, real))
        except OSError as exc:
            logger.warning("workspace upload failed (%s): %s", name, exc)
            rejected.append({"filename": name, "reason": "write_failed"})
        finally:
            await up.close()
    return {"uploaded": uploaded, "rejected": rejected}


@router.post("/upload")
async def upload_files(
    path: str = Form("", description="Target folder relative to /workspace"),
    files: list[UploadFile] = File(...),
    user=Depends(require_admin),
):
    """Upload one or more files into ``path``. Per-file size cap
    (``AGENT_MAX_UPLOAD_MB``). Returns uploaded + rejected lists."""
    dest_dir = wfs.resolve(path or "", must_exist=False)
    os.makedirs(dest_dir, exist_ok=True)
    return await _stage_uploads(dest_dir, files)


@router.post("/chat-upload")
async def chat_upload(
    files: list[UploadFile] = File(...),
    user=Depends(require_project_access),
):
    """Stage chat attachments into the WORKING USER's OWN workspace folder
    (``sessions/users/<token>/uploads/``) — not a shared, global
    ``uploads/chat/``. This keeps a boss's chat files under the SAME per-user
    scope as their memory, exports and channel inbox (the agent's chat session
    for that operator is rooted there), and isolates them from other operators.
    The caller sends only the files; the destination is derived server-side
    from the authenticated identity, never trusted from the client."""
    from app.agent_sessions import interactive_scope
    email = (getattr(user, "email", "") or "").strip().lower()
    # Fall back to the legacy shared folder only if we somehow lack an identity
    # (should not happen behind require_project_access).
    subdir = interactive_scope(email).workspace_subdir if email else "uploads/chat"
    dest_dir = wfs.resolve(f"{subdir}/uploads", must_exist=False)
    os.makedirs(dest_dir, exist_ok=True)
    return await _stage_uploads(dest_dir, files)


@router.post("/chat-upload-to")
async def chat_upload_to(
    path: str = Form("", description="Target folder relative to /workspace — must live inside the caller's own scope"),
    files: list[UploadFile] = File(...),
    user=Depends(require_project_access),
):
    """Upload files into a FOLDER of the caller's OWN chat workspace (v7.97.0).

    Backs the "Subir" button of the chat "Mi workspace" panel: the operator
    drops files into the folder they are browsing. Two contracts the admin
    ``/upload`` doesn't give:

    * CONFINEMENT: the target must live under the AUTHENTICATED caller's own
      scope folder (``sessions/users/<token>/``, derived server-side exactly
      like ``/chat-upload``) — any other path is rejected, so a member can
      never write into another user's folder even knowing its path.
    * MEMORY SYNC: every stored file is indexed into ``user_files`` with the
      semantic floor (origin + content sniff, embedded — v7.96.0), mirroring
      chat attachments, so the employee's memory knows the file exists and
      what it holds; deleting it from the same panel purges the row back.
    """
    from app.agent_sessions import interactive_scope
    email = (getattr(user, "email", "") or "").strip().lower()
    if not email:
        raise api_error(401, "workspace.no_identity", "Cannot resolve caller identity.")
    scope = interactive_scope(email)
    sub = scope.workspace_subdir
    rel = (path or "").strip().strip("/") or sub
    if rel != sub and not rel.startswith(sub + "/"):
        raise api_error(
            403, "workspace.outside_scope",
            "Target folder is outside your own workspace scope.",
        )
    dest_dir = wfs.resolve(rel, must_exist=False)
    os.makedirs(dest_dir, exist_ok=True)
    out = await _stage_uploads(dest_dir, files)
    # Memory sync — reuse the chat-attachment indexer (semantics + embedding
    # + idempotency) with a workspace-panel origin line. Best-effort: an
    # indexing failure must not undo a successful upload.
    indexed = 0
    try:
        from app.routes.agent_routes import ChatAttachment, _index_chat_attachments
        atts = [
            ChatAttachment(
                workspace_path=p,
                filename=p.rsplit("/", 1)[-1],
                mime=wfs.guess_mime(p),
                size=_safe_size(p),
            )
            for p in (out.get("uploaded") or [])
        ]
        if atts and scope.memory_user:
            indexed = await _index_chat_attachments(
                scope.memory_user, atts, None,
                origin="Subido al workspace desde el chat",
            )
    except Exception:
        logger.warning("chat-upload-to: memory indexing failed", exc_info=True)
    out["memory_indexed"] = indexed
    return out


def _safe_size(rel: str) -> int | None:
    try:
        root = wfs.workspace_root()
        return int(os.path.getsize(os.path.join(root, rel))) if root else None
    except OSError:
        return None


class MkdirRequest(BaseModel):
    path: str


@router.post("/mkdir")
async def make_dir(payload: MkdirRequest, user=Depends(require_admin)):
    target = wfs.resolve(payload.path, must_exist=False)
    if os.path.exists(target):
        raise api_error(409, "workspace.already_exists", "Path already exists.")
    try:
        os.makedirs(target, exist_ok=False)
    except OSError as exc:
        raise api_error(
            500, "workspace.mkdir_failed", f"Could not create directory: {exc}",
        )
    return {"created": True}


class MoveRequest(BaseModel):
    src: str
    dest: str  # destination FOLDER (relative to /workspace)


@router.post("/move")
async def move_entry(payload: MoveRequest, user=Depends(require_admin)):
    src = wfs.resolve(payload.src, must_exist=True)
    root = wfs.require_root()
    if src == root:
        raise api_error(
            400, "workspace.cannot_move_root", "Cannot move the workspace root.",
        )
    dest_dir = wfs.resolve(payload.dest or "", must_exist=False)
    os.makedirs(dest_dir, exist_ok=True)
    target = os.path.join(dest_dir, os.path.basename(src))
    real = os.path.realpath(target)
    if real != root and not real.startswith(root + os.sep):
        raise api_error(
            403, "workspace.dest_outside", "Destination is outside the workspace.",
        )
    if os.path.exists(target):
        raise api_error(
            409, "workspace.dest_exists", "Destination already exists.",
        )
    try:
        shutil.move(src, target)
    except OSError as exc:
        raise api_error(
            500, "workspace.move_failed", f"Could not move entry: {exc}",
        )
    return {"moved": True, "path": wfs._rel(root, real)}


class UnzipRequest(BaseModel):
    path: str
    keep: bool = True


@router.post("/unzip")
async def unzip_entry(payload: UnzipRequest, user=Depends(require_admin)):
    target = wfs.resolve(payload.path, must_exist=True)
    root = wfs.require_root()
    if not zipfile.is_zipfile(target):
        raise api_error(400, "workspace.invalid_zip", "File is not a valid ZIP archive.")
    dest_dir = os.path.dirname(target)
    extracted, skipped = 0, 0
    try:
        with zipfile.ZipFile(target) as zf:
            for member in zf.namelist():
                out = os.path.realpath(os.path.join(dest_dir, member))
                if out != root and not out.startswith(root + os.sep):
                    skipped += 1
                    continue
                zf.extract(member, dest_dir)
                extracted += 1
    except (OSError, zipfile.BadZipFile) as exc:
        raise api_error(
            500, "workspace.unzip_failed", f"Could not extract archive: {exc}",
        )
    if not payload.keep:
        try:
            os.remove(target)
        except OSError:
            pass
    return {"extracted": extracted, "skipped": skipped}


@router.delete("/file")
async def delete_entry(
    path: str = Query(..., description="File or folder relative to /workspace"),
    user=Depends(require_admin),
):
    target = wfs.resolve(path, must_exist=True)
    root = wfs.require_root()
    if target == root:
        raise api_error(
            400, "workspace.cannot_delete_root", "Cannot delete the workspace root.",
        )
    was_dir = os.path.isdir(target)
    try:
        if was_dir:
            shutil.rmtree(target)
        else:
            os.remove(target)
    except OSError as exc:
        raise api_error(
            500, "workspace.delete_failed", f"Could not delete entry: {exc}",
        )
    # Memory sync (v7.93.0): the FILES layer of user memory (``user_files``)
    # indexes workspace paths — a deleted file must disappear from memory too,
    # or the employee keeps "remembering" (and trying to read) a ghost file.
    # Purge every matching row (any user — this is the admin surface and a
    # folder may hold several users' files); the FTS index follows via the
    # sync triggers. Best-effort: an index miss must not undo the FS delete.
    memory_purged = 0
    try:
        from sqlalchemy import delete as _sql_delete, or_
        from app.app_database import app_write
        from app.app_models.memory import UserFile
        rel = (path or "").strip().lstrip("/")
        conds = [UserFile.workspace_path == rel, UserFile.workspace_path == f"/{rel}"]
        if was_dir:
            conds += [
                UserFile.workspace_path.like(f"{rel}/%"),
                UserFile.workspace_path.like(f"/{rel}/%"),
            ]
        async with app_write() as s:
            res = await s.execute(_sql_delete(UserFile).where(or_(*conds)))
            await s.commit()
            memory_purged = int(res.rowcount or 0)
    except Exception:
        logger.warning("workspace delete: user_files memory purge failed for %s", path, exc_info=True)
    return {"deleted": True, "memory_purged": memory_purged}
