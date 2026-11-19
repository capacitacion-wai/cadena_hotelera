"""Shared agent-workspace filesystem access for the Operations page.

The autonomous agent-app pod mounts the shared workspace PVC at
``AGENT_WORKSPACE_DIR`` (``/agent-workspace``) READ-WRITE (v6.34.0+).
This module gives the Operations "Workspace" tab a safe, confined view of
those files — mirroring (adapted) what the interactive code-agent file
browser offers: list, preview, download, ZIP download, upload, mkdir,
move, unzip and delete.

EVERY path coming from the client is resolved with ``realpath`` and
checked to stay inside the workspace root, so ``..`` and symlinks cannot
escape. The agent pod is the only writer of the live ``/workspace``; here
the operator manages those artefacts after the fact.
"""

from __future__ import annotations

import mimetypes
import os
from datetime import datetime, timezone
from typing import Any, Optional

from app.api_errors import api_error

# Directories hidden from the browser to cut noise (caches / vcs / build
# output the operator never needs to manage).
_EXCLUDED_DIRS = {
    ".git", "__pycache__", "node_modules", "venv", ".venv", "dist",
    ".cache", ".mypy_cache", ".pytest_cache",
}

# Preview policy: text files up to this size are returned inline; larger
# (or binary) files are offered as a download instead.
MAX_PREVIEW_BYTES = 5 * 1024 * 1024

# Upload policy (per file). Operator-tunable via env.
try:
    MAX_UPLOAD_BYTES = int(os.environ.get("AGENT_MAX_UPLOAD_MB", "100")) * 1024 * 1024
except (TypeError, ValueError):
    MAX_UPLOAD_BYTES = 100 * 1024 * 1024

# Extension → coarse "kind" so the SPA knows how to render a preview.
_TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".xml", ".html", ".htm", ".css", ".js", ".ts", ".tsx", ".jsx", ".py",
    ".sql", ".sh", ".log", ".ini", ".cfg", ".toml", ".env", ".rst",
}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
_PDF_EXTS = {".pdf"}
# HTML gets its own coarse kind (a subset of text) so the SPA can RENDER
# it as a web page in a sandboxed iframe — matching the code-agent file
# preview — instead of dumping the raw markup as source text.
_HTML_EXTS = {".html", ".htm"}

_LANG_BY_EXT = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".tsx": "tsx", ".jsx": "jsx", ".json": "json", ".md": "markdown",
    ".markdown": "markdown", ".html": "html", ".htm": "html",
    ".css": "css", ".sql": "sql", ".sh": "bash", ".yaml": "yaml",
    ".yml": "yaml", ".xml": "xml", ".toml": "toml", ".ini": "ini",
    ".csv": "csv", ".tsv": "csv",
}


def workspace_root() -> Optional[str]:
    """Absolute realpath of the workspace mount, or ``None`` when the
    feature is off (no PVC / running locally without the mount)."""
    root = (os.environ.get("AGENT_WORKSPACE_DIR") or "").strip()
    if not root or not os.path.isdir(root):
        return None
    return os.path.realpath(root)


def require_root() -> str:
    root = workspace_root()
    if not root:
        raise api_error(
            503,
            "workspace.unavailable",
            "Shared workspace is not available in this deployment.",
        )
    return root


def resolve(rel: str, must_exist: bool = True) -> str:
    """Resolve a workspace-relative path to an absolute one, confined to
    the workspace root. Raises ``HTTPException`` on traversal / missing."""
    root = require_root()
    r = (rel or "").strip().lstrip("/")
    if ".." in r.split("/"):
        raise api_error(400, "workspace.invalid_path", "Invalid workspace path.")
    target = os.path.realpath(os.path.join(root, r))
    if target != root and not target.startswith(root + os.sep):
        raise api_error(
            403, "workspace.outside_root", "Path is outside the workspace.",
        )
    if must_exist and not os.path.exists(target):
        raise api_error(404, "workspace.not_found", "Path not found.")
    return target


def _rel(root: str, abs_path: str) -> str:
    return os.path.relpath(abs_path, root).replace(os.sep, "/")


def sanitize_name(name: str) -> str:
    """Strip path components and unsafe characters from an uploaded
    filename so it can only land directly inside the target folder."""
    import re
    base = os.path.basename((name or "").strip().replace("\\", "/"))
    base = base.lstrip(".")
    base = re.sub(r"[^A-Za-z0-9._ -]", "_", base)
    return base[:180]


def kind_for(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _PDF_EXTS:
        return "pdf"
    if ext in _HTML_EXTS:
        return "html"
    if ext in _TEXT_EXTS:
        return "text"
    return "binary"


def language_for(name: str) -> Optional[str]:
    return _LANG_BY_EXT.get(os.path.splitext(name)[1].lower())


def guess_mime(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _entry(root: str, abs_path: str, name: str) -> dict[str, Any]:
    is_dir = os.path.isdir(abs_path)
    try:
        st = os.stat(abs_path)
        size = None if is_dir else int(st.st_size)
        modified = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        size, modified = None, None
    return {
        "name": name,
        "path": _rel(root, abs_path),
        "is_dir": is_dir,
        "size": size,
        "modified": modified,
        "kind": "dir" if is_dir else kind_for(name),
    }


def list_dir(rel: str = "") -> dict[str, Any]:
    """One level of the workspace. Folders first, then files, both
    alphabetical. Returns ``available=False`` (never raises) when the
    workspace mount is absent so the SPA can show a friendly state."""
    root = workspace_root()
    if not root:
        return {"available": False, "path": "", "entries": []}
    target = resolve(rel, must_exist=True)
    if not os.path.isdir(target):
        raise api_error(400, "workspace.not_directory", "Path is not a directory.")
    dirs: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    try:
        names = os.listdir(target)
    except OSError as exc:
        raise api_error(
            500, "workspace.list_failed", f"Could not list directory: {exc}",
        )
    for name in names:
        if name in _EXCLUDED_DIRS:
            continue
        abs_path = os.path.join(target, name)
        entry = _entry(root, abs_path, name)
        (dirs if entry["is_dir"] else files).append(entry)
    dirs.sort(key=lambda e: e["name"].lower())
    files.sort(key=lambda e: e["name"].lower())
    return {
        "available": True,
        "path": _rel(root, target) if target != root else "",
        "entries": dirs + files,
    }


def read_text_preview(rel: str) -> dict[str, Any]:
    """Return text content for previewable files; for large or binary
    files return metadata only (``too_large`` / ``binary``)."""
    target = resolve(rel, must_exist=True)
    if not os.path.isfile(target):
        raise api_error(400, "workspace.not_file", "Path is not a file.")
    name = os.path.basename(target)
    size = os.path.getsize(target)
    kind = kind_for(name)
    base = {
        "name": name,
        "size": size,
        "kind": kind,
        "mime": guess_mime(name),
        "language": language_for(name),
    }
    # HTML is read as text too: the SPA needs the raw markup to rewrite
    # workspace-relative assets and render it in a sandboxed iframe.
    if kind not in ("text", "html"):
        return {**base, "content": None, "too_large": False, "binary": True}
    if size > MAX_PREVIEW_BYTES:
        return {**base, "content": None, "too_large": True, "binary": False}
    try:
        with open(target, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except OSError as exc:
        raise api_error(
            500, "workspace.read_failed", f"Could not read file: {exc}",
        )
    return {**base, "content": content, "too_large": False, "binary": False}


def volume_stats(path: str) -> dict[str, Any]:
    """Free / total / used bytes of the filesystem backing ``path`` (the
    PVC). Returns ``None`` values when ``statvfs`` is unavailable (e.g.
    Windows) so the caller can still render the logical size."""
    try:
        st = os.statvfs(path)
        total = int(st.f_blocks * st.f_frsize)
        free = int(st.f_bavail * st.f_frsize)
        used = int(total - st.f_bfree * st.f_frsize)
        return {
            "volume_total_bytes": total,
            "volume_free_bytes": free,
            "volume_used_bytes": used,
        }
    except (OSError, AttributeError, ValueError):
        return {
            "volume_total_bytes": None,
            "volume_free_bytes": None,
            "volume_used_bytes": None,
        }


def disk_usage() -> dict[str, Any]:
    """Logical size of the workspace tree (sum of file sizes, excluding
    noise dirs) plus the free/total of the underlying PVC. Returns
    ``available=False`` (never raises) when no workspace mount exists so
    the SPA can show a friendly state.

    This walks the whole tree; it's a blocking call and should be run in
    a threadpool by the route handler so it never stalls the event loop.
    """
    root = workspace_root()
    if not root:
        return {"available": False}
    total_bytes = 0
    file_count = 0
    dir_count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIRS]
        dir_count += len(dirnames)
        for fn in filenames:
            abs_path = os.path.join(dirpath, fn)
            try:
                # ``lstat`` so a broken symlink can't blow up the walk.
                total_bytes += int(os.lstat(abs_path).st_size)
                file_count += 1
            except OSError:
                continue
    return {
        "available": True,
        "path": root,
        "total_bytes": total_bytes,
        "file_count": file_count,
        "dir_count": dir_count,
        **volume_stats(root),
    }


def iter_zip_members(rel: str = ""):
    """Yield ``(abs_path, arcname)`` for a folder (or the whole
    workspace) excluding noise dirs — used to build a ZIP download."""
    target = resolve(rel, must_exist=True)
    if not os.path.isdir(target):
        raise api_error(400, "workspace.not_directory", "Path is not a directory.")
    base = os.path.basename(target.rstrip(os.sep)) or "workspace"
    for dirpath, dirnames, filenames in os.walk(target):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIRS]
        for fn in filenames:
            abs_path = os.path.join(dirpath, fn)
            arc = os.path.join(base, os.path.relpath(abs_path, target))
            yield abs_path, arc
