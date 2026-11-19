"""Read-only proxy to the WAI platform FUNCTIONAL (global) skill catalog.

The operator attaches these skills to make the autonomous agent smarter EN
CALIENTE (Operations → Skills). This module is the only place the app pod talks
to the platform backend's skill catalog, and it does so READ-ONLY:

  * ``list_functional_skills()`` — the slim catalog for the picker.
  * ``fetch_skill_payload(id)`` — one skill's file tree, embedded into the
    ``agent_extra_skills`` AppSetting so ``/agent-runtime`` is self-contained
    (no platform dependency at agent boot) and the sidecar's ``resolve_mcp.py``
    can write the files into ``/workspace/.opencode/skills`` on the next refresh.

Auth: ``X-API-Secret: BACKEND_API_SECRET`` against ``WAI_BACKEND_URL`` (both
injected by the deployer). When either is missing (local dev) every call returns
empty so the feature degrades quietly instead of erroring.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from app.config import WAI_BACKEND_URL, BACKEND_API_SECRET

logger = logging.getLogger("wai.skills_catalog")

# Bound the number of files / total bytes we embed per skill so a pathological
# skill can't bloat the AppSetting row (functional skills are text: SKILL.md +
# a handful of patterns/assets).
_MAX_FILES = 200
_MAX_TOTAL_BYTES = 2_000_000


def _configured() -> bool:
    return bool(WAI_BACKEND_URL and BACKEND_API_SECRET)


# Reachability probe for the health blocker (``platform_degraded``): env vars
# being SET says nothing about the URL resolving or the secret being accepted,
# so the blocker probes the catalog itself. TTL-cached — health is polled by
# the SPA and must not hammer the platform backend.
_PROBE_TTL_S = 300.0
_probe_cache: dict[str, Any] = {"at": 0.0, "status": "unknown"}


async def platform_status() -> str:
    """One of ``ok`` / ``missing`` (env not injected — old deployer) /
    ``unreachable`` (env set but the catalog endpoint errors or rejects the
    secret). Cached for ``_PROBE_TTL_S`` seconds."""
    if not _configured():
        return "missing"
    import time
    now = time.monotonic()
    if now - _probe_cache["at"] < _PROBE_TTL_S and _probe_cache["status"] != "unknown":
        return _probe_cache["status"]
    status = "unreachable"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                f"{_base()}/backend/skills/",
                headers=_headers(),
                params={"kind": "functional", "status_filter": "active"},
            )
        if resp.status_code == 200:
            status = "ok"
        else:
            logger.warning("platform probe: catalog returned HTTP %s", resp.status_code)
    except Exception as e:  # noqa: BLE001 — probe is best-effort
        logger.warning("platform probe failed: %s", e)
    _probe_cache["at"] = now
    _probe_cache["status"] = status
    return status


def _base() -> str:
    return WAI_BACKEND_URL.rstrip("/")


def _headers() -> dict[str, str]:
    return {"X-API-Secret": BACKEND_API_SECRET}


async def list_functional_skills() -> list[dict[str, Any]]:
    """Return the active FUNCTIONAL skills as a slim list for the picker.

    Shape: ``[{id, name, display_name, description, category}]``. Non-fatal on
    any error (returns ``[]``) — the picker just shows nothing."""
    if not _configured():
        return []
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{_base()}/backend/skills/",
                headers=_headers(),
                params={
                    "kind": "functional",
                    "status_filter": "active",
                    "sort_by": "display_name",
                },
            )
        if resp.status_code != 200:
            logger.warning("functional skills list failed: HTTP %s", resp.status_code)
            return []
        data = resp.json()
        rows = data if isinstance(data, list) else (data.get("items") or [])
        out: list[dict[str, Any]] = []
        for s in rows:
            if (s.get("status") or "active") != "active":
                continue
            out.append({
                "id": s.get("id"),
                "name": s.get("name"),
                "display_name": s.get("display_name") or s.get("name"),
                "description": s.get("description"),
                "category": s.get("category"),
            })
        return out
    except Exception as e:  # noqa: BLE001 — catalog is best-effort
        logger.warning("functional skills list error: %s", e)
        return []


async def fetch_skill_payload(skill_id: int) -> Optional[dict[str, Any]]:
    """Fetch one skill and return ``{id, name, display_name, files:{path:content}}``.

    ``files`` maps each text file's path (relative to the skill root, e.g.
    ``SKILL.md`` / ``patterns/x.md``) to its content. Binary / storage-backed
    files are skipped (functional skills authored in the UI are text). Returns
    ``None`` on failure."""
    if not _configured():
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_base()}/backend/skills/{skill_id}", headers=_headers(),
            )
        if resp.status_code != 200:
            logger.warning("skill %s fetch failed: HTTP %s", skill_id, resp.status_code)
            return None
        skill = resp.json()
        detail = skill.get("current_version_detail") or {}
        raw_files = detail.get("files") or []
        files: dict[str, str] = {}
        total = 0
        for f in raw_files:
            if len(files) >= _MAX_FILES or total >= _MAX_TOTAL_BYTES:
                break
            content = f.get("content")
            rel = (f.get("file_path") or f.get("filename") or "").lstrip("/")
            if content is None or not rel:
                continue
            files[rel] = content
            total += len(content)
        if not files:
            return None
        return {
            "id": skill.get("id"),
            "name": (skill.get("name") or f"skill-{skill_id}").strip("/"),
            "display_name": skill.get("display_name") or skill.get("name"),
            "files": files,
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("skill %s fetch error: %s", skill_id, e)
        return None
