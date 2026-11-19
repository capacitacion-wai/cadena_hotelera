"""Deliverable vs QA artifact policy for autonomous agent-apps.

Generic export skills (wai-pptx-export, wai-docx-export, …) stay
domain-only. THIS module owns the agent-app layer: what counts as an
operator-facing deliverable in Encargos/Calendar/run detail, and which
files may be attached to outbound communications.

Typical failure mode without this filter: a PPTX run renders
``slide-NN.jpg`` for visual QA, the agent attaches those JPGs instead of
the ``.pptx``, and Encargos lists slides instead of the presentation.
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional

# Visual-QA intermediates from wai-pptx-export (pptx_to_images.sh, etc.).
_QA_SLIDE_JPG = re.compile(r"^slide-\d+\.jpg$", re.I)
_QA_SLIDE_FIXED = re.compile(r"^slide-fixed-\d+\.jpg$", re.I)
_QA_THUMB = re.compile(r"-thumbs?\.jpg$", re.I)

# Primary deliverable extensions — lower sort key = shown first.
_PRIMARY_EXT: dict[str, int] = {
    ".pptx": 0,
    ".pdf": 1,
    ".docx": 2,
    ".xlsx": 3,
    ".csv": 4,
}

_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

# Allowlist of extensions an agent may attach to an OUTBOUND message. The
# agent works in a writable workspace next to its own SQLite DB, configs and
# helper scripts — without this guard a confused or prompt-injected run could
# attach ``app.db``, a ``.env`` or a shell script to a customer email. URLs
# are exempt (they're plain links, materialised by the recipient, not us).
_DELIVERABLE_EXT = frozenset({
    ".pdf", ".pptx", ".ppt", ".docx", ".doc", ".xlsx", ".xls", ".csv", ".tsv",
    ".txt", ".md", ".json", ".xml", ".html", ".htm", ".rtf",
    ".odt", ".ods", ".odp", ".ics", ".vcf",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".tiff",
    ".zip",
})


def is_allowed_deliverable_ext(filename: str = "", workspace_path: str = "") -> bool:
    """True when the file's extension is a safe outbound deliverable type.
    A name with no extension is rejected (could be a binary/secret)."""
    name = _basename(filename, workspace_path)
    ext = os.path.splitext(name)[1].lower()
    return bool(ext) and ext in _DELIVERABLE_EXT


def _basename(filename: str, workspace_path: str = "") -> str:
    if filename:
        return filename.strip()
    path = (workspace_path or "").replace("\\", "/").rstrip("/")
    return path.rsplit("/", 1)[-1] if path else ""


def is_qa_artifact(filename: str = "", workspace_path: str = "") -> bool:
    """True for visual-QA intermediates that must NOT be operator deliverables."""
    name = _basename(filename, workspace_path)
    if not name:
        return False
    if _QA_SLIDE_JPG.match(name):
        return True
    if _QA_SLIDE_FIXED.match(name):
        return True
    if _QA_THUMB.search(name):
        return True
    norm = ("/" + (workspace_path or "").replace("\\", "/")).lower()
    if "/.qa/" in norm or norm.endswith("/.qa"):
        return True
    return False


def artifact_kind(filename: str = "", workspace_path: str = "") -> str:
    """Classify a workspace file for UI / filtering."""
    name = _basename(filename, workspace_path)
    ext = os.path.splitext(name)[1].lower()
    if is_qa_artifact(name, workspace_path):
        return "qa"
    if ext in _PRIMARY_EXT:
        return "deliverable"
    norm = (workspace_path or "").replace("\\", "/").lower()
    if ext in _IMAGE_EXT and ("_assets" in norm or "/_assets_" in norm):
        return "auxiliary"
    if ext in _IMAGE_EXT:
        return "image"
    return "other"


def _sort_key(item: dict[str, Any]) -> tuple:
    kind = item.get("kind") or artifact_kind(
        item.get("filename") or "", item.get("workspace_path") or ""
    )
    name = _basename(item.get("filename") or "", item.get("workspace_path") or "")
    ext = os.path.splitext(name)[1].lower()
    kind_order = {
        "deliverable": 0,
        "other": 1,
        "image": 2,
        "auxiliary": 3,
        "qa": 9,
    }.get(kind, 5)
    ext_order = _PRIMARY_EXT.get(ext, 50)
    return (kind_order, ext_order, (item.get("workspace_path") or name).lower())


def filter_run_artifacts(raw: list[dict]) -> list[dict]:
    """Drop QA intermediates; when a primary deliverable exists, hide chart
    assets and other auxiliary images so Encargos shows the ``.pptx`` (or
    PDF/CSV…) instead of ``slide-01.jpg`` screenshots."""
    if not raw:
        return []

    enriched: list[dict] = []
    for item in raw:
        fn = str(item.get("filename") or "")
        path = str(item.get("workspace_path") or "")
        if is_qa_artifact(fn, path):
            continue
        kind = artifact_kind(fn, path)
        enriched.append({**item, "kind": kind})

    has_primary = any(x.get("kind") == "deliverable" for x in enriched)
    if has_primary:
        enriched = [
            x for x in enriched
            if x.get("kind") not in ("auxiliary", "qa")
            and not (
                x.get("kind") == "image"
                and "_assets" in (x.get("workspace_path") or "").lower()
            )
        ]

    enriched.sort(key=_sort_key)
    return enriched[:30]


def sanitize_attachment_dict(item: dict) -> Optional[dict]:
    """Return *item* if it is a valid outbound attachment, else ``None``."""
    if not isinstance(item, dict):
        return None
    filename = str(item.get("filename") or item.get("name") or "").strip()
    ws_path = str(
        item.get("workspace_path") or item.get("path") or ""
    ).strip()
    if is_qa_artifact(filename, ws_path):
        return None
    # Reject slide JPGs even when filename was omitted (derive from path).
    if ws_path and is_qa_artifact("", ws_path):
        return None
    # Deliverable allowlist: a URL is a plain link (recipient fetches it), so
    # it's exempt; a workspace file or inline blob we materialise ourselves
    # must be a known-safe deliverable type — never the app's DB, a secret or
    # a script that happens to live in the workspace.
    is_url = bool(str(item.get("url") or "").strip())
    if not is_url and not is_allowed_deliverable_ext(filename, ws_path):
        return None
    return item


def sanitize_attachment_list(raw: Any) -> list[dict]:
    """Filter QA intermediates out of ``attachments`` / ``notify.attachments``."""
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        cleaned = sanitize_attachment_dict(item)
        if cleaned is not None:
            out.append(cleaned)
    return out
