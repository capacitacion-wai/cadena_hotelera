"""Shared per-user memory helpers (v6.40.0).

Centralises human-identity validation and run-scoped memory ownership
so MCP tools cannot read/write another user's store.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from sqlalchemy import select, update

from app.agent_proxy import AUTONOMOUS_USER_EMAIL
from app.app_database import app_session, app_write

logger = logging.getLogger(__name__)

_MEMORY_USER_BLOCKLIST = frozenset({"", "unknown", "unknown@unknown", "none", "null"})
_EXTERNAL_MESSAGE = "external_message"
_MEMORY_MAINTENANCE = "memory_maintenance"


def valid_memory_user(
    value: str | None,
    *,
    allow_autonomous_identity: bool = False,
) -> str | None:
    """Return a normalised human email suitable as a memory owner, or None.

    ``allow_autonomous_identity=True`` is used for **interactive chat** when
    the logged-in operator happens to share the autonomous service email
    (common in demo tenants). Autonomous/watcher runs must keep the default
    ``False`` so the synthetic bot identity never gets a personal memory bucket.
    """
    e = (value or "").strip().lower()
    if not e or "@" not in e or e in _MEMORY_USER_BLOCKLIST:
        return None
    if (
        not allow_autonomous_identity
        and AUTONOMOUS_USER_EMAIL
        and e == AUTONOMOUS_USER_EMAIL.strip().lower()
    ):
        return None
    return e


async def persist_run_memory_user(run_id: int, memory_user: str | None) -> None:
    """Store ``memory_user`` on the run row (``request_meta_json``). Best-effort."""
    # Interactive chat may legitimately use the same address as
    # ``AUTONOMOUS_USER_EMAIL`` (demo tenants). Callers pass the scoped
    # owner — do not re-block that identity here.
    owner = valid_memory_user(memory_user, allow_autonomous_identity=True)
    if not run_id or not owner:
        return
    try:
        from app.app_models.runtime import AgentRun

        async with app_write() as s:
            row = (await s.execute(
                select(AgentRun).where(AgentRun.id == int(run_id))
            )).scalar_one_or_none()
            if row is None:
                return
            meta: dict[str, Any] = {}
            if row.request_meta_json:
                try:
                    meta = json.loads(row.request_meta_json) or {}
                except Exception:
                    meta = {}
            meta["memory_user"] = owner
            await s.execute(
                update(AgentRun)
                .where(AgentRun.id == int(run_id))
                .values(request_meta_json=json.dumps(meta, ensure_ascii=False))
            )
            await s.commit()
    except Exception:  # pragma: no cover — never block the run
        logger.debug("persist_run_memory_user failed for run %s", run_id, exc_info=True)


async def memory_owner_for_run(run_id: int) -> str | None:
    """Resolve the single memory owner authorised for this ``run_id``."""
    if not run_id:
        return None
    try:
        from app.app_models.runtime import AgentRun, AgentSession, Event

        async with app_session() as s:
            run = (await s.execute(
                select(AgentRun).where(AgentRun.id == int(run_id))
            )).scalar_one_or_none()
            if run is None:
                return None
            if run.request_meta_json:
                try:
                    meta = json.loads(run.request_meta_json) or {}
                    cached = valid_memory_user(
                        meta.get("memory_user"),
                        allow_autonomous_identity=True,
                    )
                    if cached:
                        return cached
                except Exception:
                    pass
            if run.event_id:
                event = (await s.execute(
                    select(Event).where(Event.id == run.event_id)
                )).scalar_one_or_none()
                if event is not None:
                    etype = (event.type or "").strip()
                    payload = json.loads(event.payload_json or "{}")
                    if etype == _MEMORY_MAINTENANCE:
                        return valid_memory_user(payload.get("user_email"))
                    if etype == _EXTERNAL_MESSAGE:
                        from app.app_models.channels import ChannelMessage

                        mid = payload.get("channel_message_id") or payload.get("message_id")
                        if mid is not None:
                            msg = (await s.execute(
                                select(ChannelMessage.sender).where(
                                    ChannelMessage.id == int(mid)
                                )
                            )).scalar_one_or_none()
                            return valid_memory_user(msg)
                        pid = payload.get("provider_message_id")
                        cid = payload.get("channel_id")
                        if pid and cid:
                            msg = (await s.execute(
                                select(ChannelMessage.sender).where(
                                    ChannelMessage.channel_id == int(cid),
                                    ChannelMessage.provider_message_id == str(pid),
                                )
                            )).scalar_one_or_none()
                            return valid_memory_user(msg)
            if (run.mode or "") == "chat" and run.opencode_session_id:
                sess = (await s.execute(
                    select(AgentSession.user_email, AgentSession.scope_key).where(
                        AgentSession.session_id == run.opencode_session_id
                    )
                )).first()
                if sess is not None:
                    email, scope_key = sess[0], sess[1] or ""
                    if scope_key.startswith("interactive:"):
                        return valid_memory_user(email) or valid_memory_user(
                            scope_key.split(":", 1)[-1]
                        )
    except Exception:  # pragma: no cover
        logger.debug("memory_owner_for_run failed for %s", run_id, exc_info=True)
    return None


async def authorized_memory_user(run_id: int, requested: str | None) -> str | None:
    """Return the normalised email if ``requested`` matches the run owner."""
    human = valid_memory_user(requested, allow_autonomous_identity=True)
    if not human:
        return None
    owner = await memory_owner_for_run(run_id)
    if owner is None:
        return None
    if human != owner:
        logger.warning(
            "memory access denied run=%s owner=%s requested=%s",
            run_id, owner, human,
        )
        return None
    return human


def reject_workspace_path(path: str) -> bool:
    """True when a workspace path must be rejected (``..`` escape)."""
    p = (path or "").strip()
    if not p or ".." in p.split("/"):
        return True
    return bool(re.match(r"^\.\.", p))


def _heuristic_importance(content: str, kind: str) -> float:
    """0..1 salience floor when the agent gives no hint. Preferences and
    procedures are durable by nature; facts with entities/numbers score
    higher than vague one-liners. Cheap, no LLM."""
    c = (content or "").strip()
    if not c:
        return 0.2
    score = 0.5 if kind in ("preference", "procedure") else 0.35
    if re.search(r"\d", c):
        score += 0.1
    if re.search(r"[A-ZÁÉÍÓÚÑ][\wáéíóúñü]+", c):  # a proper noun / entity
        score += 0.1
    if len(c) > 40:
        score += 0.05
    return max(0.0, min(1.0, score))


async def enrich_memory_write(
    run_id: int | None, content: str, kind: str = "fact",
    requested_trust: str | None = None, requested_importance=None,
    ceiling: str | None = None,
) -> dict:
    """Compute the v7.29.0 write-time fields for a fact: trust (clamped to the
    run-origin ceiling — the model can lower, never raise), importance,
    content_hash and embedding blob. ``ceiling`` may be pre-resolved by the
    caller (e.g. a memory block writing several facts for ONE run) to avoid
    re-deriving it per fact. Never raises."""
    from app import memory_trust as _t
    from app import memory_embeddings as _e
    if ceiling is None:
        try:
            ceiling = await _t.trust_ceiling_for_run(run_id)
        except Exception:  # pragma: no cover
            ceiling = _t.INFERRED
    trust = _t.clamp_to_ceiling(requested_trust, ceiling)
    try:
        imp = float(requested_importance) if requested_importance is not None else None
    except (TypeError, ValueError):
        imp = None
    if imp is None:
        imp = _heuristic_importance(content, kind)
    imp = max(0.0, min(1.0, imp))
    try:
        vec = _e.embed(content)
        blob = _e.pack(vec)
        ch = _e.content_hash(content)
        model = _e.model_id()
    except Exception:  # pragma: no cover
        blob, ch, model = None, "", None
    return {
        "trust": trust, "importance": imp,
        "content_hash": ch or None, "embedding": blob, "embed_model": model,
    }


async def backfill_memory(batch: int = 200) -> dict:
    """One-time, idempotent backfill for pre-v7.29.0 rows: assign ``trust``
    from the existing ``source`` column (operator→verified, chat→stated,
    else→inferred) and compute ``embedding``/``content_hash`` where missing.
    Processes in batches; safe to run repeatedly (only NULL fields touched)."""
    from sqlalchemy import update as _update
    from app.app_models.memory import UserMemory, UserEpisode, UserFile
    from app import memory_trust as _t, memory_embeddings as _e
    done = {"facts": 0, "episodes": 0, "files": 0}
    # READ without the lock → COMPUTE outside → WRITE inside, and only that.
    #
    # Before, the three phases happened INSIDE `app_write()`, which is the process's
    # single write queue. The embedder is a LOCAL model (pure CPU), so a batch of 200
    # chained up to two hundred embeddings with the lock held: meanwhile NOBODY else
    # could write — not the channel poller, not the event claim, not an HTTP request.
    # With the pod limited to 500m of CPU it was measured as 14 s waits
    # (INC-VIG-25 / KOM-44).
    #
    # It is exactly what the `app_write` docstring forbids: "slow I/O goes BEFORE or
    # AFTER, never inside, or you serialize every other writer behind that I/O". The
    # lock protects the WRITE, not the computation.
    try:
        # Facts: trust + embedding + content_hash.
        while True:
            async with app_session() as s:            # read: no lock
                rows = list((await s.execute(
                    select(UserMemory).where(
                        (UserMemory.trust.is_(None)) | (UserMemory.embedding.is_(None))
                    ).limit(batch)
                )).scalars().all())
            if not rows:
                break
            pendientes = []                            # compute: OUTSIDE the lock
            for f in rows:
                vals = {}
                if not f.trust:
                    vals["trust"] = _t.trust_from_legacy_source(f.source)
                if f.embedding is None:
                    vals["embedding"] = _e.pack(_e.embed(f.content or ""))
                    vals["embed_model"] = _e.model_id()
                    vals["content_hash"] = _e.content_hash(f.content or "") or None
                if vals:
                    pendientes.append((f.id, vals))
            if pendientes:
                async with app_write() as s:           # write: only this
                    for fid, vals in pendientes:
                        await s.execute(_update(UserMemory).where(UserMemory.id == fid).values(**vals))
                    await s.commit()
                done["facts"] += len(pendientes)
            if len(rows) < batch:
                break
        # Episodes + files: embeddings only.
        for Model, text_attr, key in (
            (UserEpisode, "summary", "episodes"), (UserFile, "description", "files"),
        ):
            while True:
                async with app_session() as s:
                    rows = list((await s.execute(
                        select(Model).where(Model.embedding.is_(None)).limit(batch)
                    )).scalars().all())
                if not rows:
                    break
                # A NON-NULL embedding is always set (zero vector for empty text)
                # so the `embedding IS NULL` sweep moves forward and does not spin.
                calculados = [(r.id, _e.pack(_e.embed(getattr(r, text_attr, "") or "")),
                               bool((getattr(r, text_attr, "") or "").strip()))
                              for r in rows]
                async with app_write() as s:
                    for rid, blob, con_texto in calculados:
                        await s.execute(_update(Model).where(Model.id == rid).values(
                            embedding=blob, embed_model=_e.model_id()))
                        if con_texto:
                            done[key] += 1
                    await s.commit()
                if len(rows) < batch:
                    break
    except Exception:  # pragma: no cover — backfill must never crash boot
        logger.warning("backfill_memory partial", exc_info=True)
    if any(done.values()):
        logger.info("memory backfill: %s", done)
    return done


async def materialize_memory_block(
    run_id: int | None, memory_user: str | None, structured: dict | None,
    source: str = "agent",
) -> dict:
    """Ingest the agent's distilled ``STRUCTURED_RESPONSE.memory`` block
    (v7.29.0) — facts / an episode / file semantics the agent wrote itself,
    so memory is DISTILLED, not templated. Everything is trust-capped to the
    run-origin ceiling (a poisoned inbound can only add quarantined facts) and
    rate-limited per run. Best-effort; never raises, never blocks the run."""
    owner = valid_memory_user(memory_user, allow_autonomous_identity=True)
    if not run_id or not owner or not isinstance(structured, dict):
        return {"facts": 0, "episode": 0, "files": 0}
    block = structured.get("memory")
    if not isinstance(block, dict):
        return {"facts": 0, "episode": 0, "files": 0}
    from app.config import AGENT_MEMORY_BLOCK_MAX_FACTS
    from app.app_models.memory import UserMemory, UserEpisode, UserFile
    from app import memory_embeddings as _e
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    import hashlib as _h
    added = {"facts": 0, "episode": 0, "files": 0}
    # Resolve the run-origin trust ceiling ONCE for the whole block (all facts
    # share the same run) instead of re-deriving it per fact.
    from app import memory_trust as _mt
    try:
        _ceiling = await _mt.trust_ceiling_for_run(run_id)
    except Exception:  # pragma: no cover
        _ceiling = _mt.INFERRED

    # ── Facts (capped per run so a hostile inbound can't flood memory) ──
    facts = block.get("facts") if isinstance(block.get("facts"), list) else []
    for i, f in enumerate(facts[: max(1, int(AGENT_MEMORY_BLOCK_MAX_FACTS))]):
        if not isinstance(f, dict):
            continue
        content = str(f.get("content") or "").strip()
        if not content:
            continue
        kind = str(f.get("kind") or "fact").strip().lower()
        if kind not in ("fact", "preference", "procedure"):
            kind = "fact"
        enr = await enrich_memory_write(
            run_id, content, kind,
            requested_trust=f.get("trust"), requested_importance=f.get("importance"),
            ceiling=_ceiling,
        )
        if await reinforce_duplicate_fact(owner, enr["content_hash"] or "", enr["trust"]):
            continue
        key = "memblk-" + _h.sha1(f"{run_id}|{i}|{enr['content_hash']}".encode()).hexdigest()[:24]
        try:
            async with app_write() as s:
                await s.execute(sqlite_insert(UserMemory).values(
                    idempotency_key=key, created_by_run_id=int(run_id),
                    user_email=owner, kind=kind, content=content,
                    source=source, provenance=f"run={run_id}",
                    trust=enr["trust"], importance=enr["importance"],
                    content_hash=enr["content_hash"], embedding=enr["embedding"],
                    embed_model=enr["embed_model"],
                ).on_conflict_do_nothing(index_elements=[UserMemory.idempotency_key]))
                await s.commit()
            added["facts"] += 1
        except Exception:  # pragma: no cover
            logger.debug("memory-block fact insert failed", exc_info=True)

    # ── Episode (a real agent-written summary, not the template) ──
    ep = block.get("episode")
    if isinstance(ep, dict) and str(ep.get("summary") or "").strip():
        summary = str(ep["summary"]).strip()[:2000]
        outcome = (str(ep.get("outcome") or "").strip() or None)
        key = f"{source}-memblk-{int(run_id)}"
        try:
            from sqlalchemy import delete as _delete
            blob = _e.pack(_e.embed(summary))
            async with app_write() as s:
                # The channel path already captured a ``raw`` template episode
                # for this same run (auto_record_chat_episode). Replace it with
                # this distilled one so a single interaction = ONE episode.
                await s.execute(_delete(UserEpisode).where(
                    UserEpisode.created_by_run_id == int(run_id),
                    UserEpisode.distill_state == "raw",
                ))
                await s.execute(sqlite_insert(UserEpisode).values(
                    idempotency_key=key, created_by_run_id=int(run_id),
                    user_email=owner, summary=summary, source=source,
                    outcome=(outcome[:2000] if outcome else None),
                    embedding=blob, embed_model=_e.model_id(),
                    distill_state="distilled",
                ).on_conflict_do_nothing(index_elements=[UserEpisode.idempotency_key]))
                await s.commit()
            added["episode"] += 1
        except Exception:  # pragma: no cover
            logger.debug("memory-block episode insert failed", exc_info=True)

    # ── File semantics (the agent describes what it produced/received) ──
    files = block.get("files") if isinstance(block.get("files"), list) else []
    for f in files[:10]:
        if not isinstance(f, dict):
            continue
        wp = str(f.get("workspace_path") or "").strip()
        desc = str(f.get("description") or "").strip()
        if not wp or reject_workspace_path(wp) or not desc:
            continue
        try:
            blob = _e.pack(_e.embed(desc))
            async with app_write() as s:
                # Only enrich the description/embedding of an already-indexed
                # file (the file row itself is created by auto_index).
                from sqlalchemy import update as _update
                await s.execute(_update(UserFile).where(
                    UserFile.user_email == owner, UserFile.workspace_path == wp,
                ).values(description=desc[:2000], embedding=blob, embed_model=_e.model_id()))
                await s.commit()
            added["files"] += 1
        except Exception:  # pragma: no cover
            logger.debug("memory-block file enrich failed", exc_info=True)
    return added


async def reinforce_duplicate_fact(
    user_email: str, content_hash: str, incoming_trust: str | None = None,
) -> int | None:
    """If a LIVE fact with the same content_hash already exists for the user,
    dedupe (return its id) and — only for a TRUSTED corroboration — bump its
    recency/usage. An ``untrusted`` re-assertion (e.g. an attacker repeating a
    planted fact via inbound) is deduped but NOT reinforced, so repetition
    can't inflate a quarantined fact's prominence. Returns None when there is
    no duplicate."""
    if not content_hash:
        return None
    from app import memory_trust as _t
    reinforce = _t.rank(incoming_trust) >= _t.rank(_t.STATED)
    try:
        from datetime import datetime, timezone
        from sqlalchemy import update as _update
        from app.app_models.memory import UserMemory
        async with app_write() as s:
            row = (await s.execute(
                select(UserMemory.id, UserMemory.access_count).where(
                    UserMemory.user_email == user_email,
                    UserMemory.content_hash == content_hash,
                    UserMemory.superseded_by.is_(None),
                ).limit(1)
            )).first()
            if row is None:
                return None
            fid, acc = int(row[0]), int(row[1] or 0)
            if reinforce:
                await s.execute(
                    _update(UserMemory).where(UserMemory.id == fid).values(
                        access_count=acc + 1,
                        last_accessed_at=datetime.now(timezone.utc),
                        updated_at=datetime.now(timezone.utc),
                    )
                )
                await s.commit()
            return fid
    except Exception:  # pragma: no cover
        logger.debug("reinforce_duplicate_fact failed", exc_info=True)
        return None


def _one_line(text: str, limit: int = 240) -> str:
    s = re.sub(r"\s+", " ", (text or "").strip())
    if len(s) <= limit:
        return s
    return s[: limit - 1].rstrip() + "…"


def _clean_for_memory(text: str | None, limit: int = 200) -> str:
    """Strip internal markers, take a clean, human one-liner from agent text."""
    from app.chat_prompt import strip_internal_chat_leak
    raw = strip_internal_chat_leak(text or "")
    one = _one_line(raw, 4000)
    if not one:
        return ""
    # Prefer the final sentence(s): agents close with the actual outcome
    # ("Hecho. Te dejé la presentación…"), while any pre-tool narration
    # that leaked onto the answer channel sits at the front.
    parts = re.split(r"(?<=[.!?])\s+", one)
    tail = " ".join(parts[-2:]).strip() if len(parts) > 1 else one
    return _one_line(tail or one, limit)


# Conversational filler that should NOT spawn an episode on its own.
_TRIVIAL_CHAT = {
    "hola", "buenas", "hey", "ok", "okay", "vale", "si", "sí", "no",
    "gracias", "adios", "adiós", "sigue", "seguir", "continua", "continúa",
    "dale", "perfecto", "genial", "bien", "listo", "ya", "test", "prueba",
}


def _normalize_summary(text: str | None) -> str:
    """Lowercased, punctuation-stripped, space-collapsed form for cheap
    near-duplicate comparison of episode summaries."""
    return re.sub(r"[^\wáéíóúñ ]", "", (text or "").lower()).strip()


def _summaries_similar(a: str, b: str, *, threshold: float = 0.82) -> bool:
    """True when two episode summaries say essentially the same thing.

    No embeddings — we keep the agent image lean. Exact match after
    normalization, or a high Jaccard token overlap, is enough to catch the
    common case: the same routine interaction ("Petición: revisa el correo")
    logged turn after turn, which would otherwise crowd out the episodic
    layer with noise.
    """
    na, nb = _normalize_summary(a), _normalize_summary(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    ta, tb = set(na.split()), set(nb.split())
    if not ta or not tb:
        return False
    inter = len(ta & tb)
    union = len(ta | tb)
    return union > 0 and (inter / union) >= threshold


def _is_trivial_prompt(prompt: str) -> bool:
    p = re.sub(r"[^\wáéíóúñ ]", "", (prompt or "").lower()).strip()
    if not p:
        return True
    if p in _TRIVIAL_CHAT:
        return True
    # A single short word with no other signal (e.g. "okok", "vaa").
    return len(p) < 6 and " " not in p


_FILE_PATH_RE = re.compile(
    r"(?:/workspace/)?"
    r"((?:[\w.\-]+/)*[\w.\-]+\."
    r"(?:pptx|pdf|docx|xlsx|csv|png|jpe?g|gif|webp|txt|md|json|html|zip))",
    re.IGNORECASE,
)


def _detect_workspace_paths(text: str | None) -> list[str]:
    """Extract workspace-relative file paths mentioned in agent text."""
    out: list[str] = []
    for m in _FILE_PATH_RE.finditer(text or ""):
        rel = m.group(1).lstrip("/")
        if rel.startswith("workspace/"):
            rel = rel[len("workspace/"):]
        if rel and ".." not in rel.split("/") and "/" in rel:
            out.append(rel)
    # De-dup preserving order.
    return list(dict.fromkeys(out))


def _scan_recent_exports(workspace_subdir: str, since_ts: float | None) -> list[str]:
    """List files freshly written to the scope's ``exports/`` folder."""
    import os
    sub = (workspace_subdir or "").strip().strip("/")
    if not sub or ".." in sub.split("/") or since_ts is None:
        return []
    try:
        from app.workspace_fs import workspace_root
        root = workspace_root()
        if not root:
            return []
        exports = os.path.join(root, sub, "exports")
        if not os.path.isdir(exports):
            return []
        found: list[str] = []
        for name in os.listdir(exports):
            fp = os.path.join(exports, name)
            try:
                if os.path.isfile(fp) and os.path.getmtime(fp) >= since_ts - 1:
                    found.append(os.path.relpath(fp, root))
            except OSError:
                continue
        return found
    except Exception:  # pragma: no cover — best-effort
        logger.debug("scan exports failed for %s", workspace_subdir, exc_info=True)
        return []


_SNIFF_MAX_BYTES = 256 * 1024


def _sniff_file_content(root: str, rel: str) -> str:
    """Cheap, dependency-free content summary for TEXT-like formats — the
    heuristic floor of a file's semantics when nobody described it. CSV/TSV →
    delimiter-sniffed columns + row count; md/txt → first heading/line; JSON →
    top-level shape. Binary formats return '' (the agent's own description or
    the turn context must carry those)."""
    import json
    import os
    ext = rel.rsplit(".", 1)[-1].lower() if "." in rel else ""
    if ext not in ("csv", "tsv", "txt", "md", "json"):
        return ""
    try:
        fp = os.path.join(root, rel)
        size = os.path.getsize(fp)
        with open(fp, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read(_SNIFF_MAX_BYTES)
    except OSError:
        return ""
    if not raw.strip():
        return ""
    truncated = size > len(raw.encode("utf-8", errors="replace"))
    if ext in ("csv", "tsv"):
        header, _, _rest = raw.partition("\n")
        delim = "\t" if ext == "tsv" else max(",;|", key=header.count)
        cols = [c.strip().strip('"') for c in header.split(delim) if c.strip()]
        # Data rows: non-empty lines minus the header itself.
        rows = max(0, sum(1 for ln in raw.splitlines() if ln.strip()) - 1)
        approx = "≥" if truncated else ""
        head = ", ".join(cols[:12]) + ("…" if len(cols) > 12 else "")
        return f"Tabla {ext.upper()} con {approx}{rows} filas. Columnas: {head}."
    if ext == "json":
        try:
            data = json.loads(raw) if not truncated else None
        except ValueError:
            data = None
        if isinstance(data, dict):
            keys = ", ".join(list(data.keys())[:10])
            return f"JSON con claves: {keys}."
        if isinstance(data, list):
            return f"JSON con {len(data)} elementos."
        return ""
    # md / txt — first heading, else first non-empty line.
    for line in raw.splitlines():
        t = line.strip().lstrip("#").strip()
        if t:
            return f"Empieza: «{t[:160]}»."
    return ""


def _mention_snippet(response_text: str | None, filename: str) -> str:
    """The sentence in the agent's answer that mentions ``filename`` — in a
    chat turn that sentence usually SAYS what the file is («He generado el
    plan adaptado a EUR en `x.pdf`»), making it free semantics."""
    text = response_text or ""
    idx = text.find(filename)
    if idx < 0:
        return ""
    start = max(text.rfind("\n", 0, idx), text.rfind(". ", 0, idx) + 1, 0)
    end_candidates = [p for p in (text.find("\n", idx), text.find(". ", idx)) if p >= 0]
    end = min(end_candidates) if end_candidates else len(text)
    snippet = " ".join(text[start:end].split()).strip(" -*·#>")
    # Markdown table/code rows are noise, not semantics.
    if snippet.startswith("|") or snippet.startswith("```"):
        return ""
    return snippet[:240]


def build_file_semantics(
    rel: str,
    root: str = "",
    origin: str = "",
    response_text: str | None = None,
    user_message: str | None = None,
    lang: str = "es",
) -> str:
    """Compose the best zero-LLM-cost semantic description available for a
    file row: origin («Generado por chat» / «Adjuntado…»), the REQUEST that
    produced it (the strongest \"when to use it\" anchor), the answer sentence
    that mentions it, and a content sniff. The agent can still overwrite this
    with a better description (``index_file`` / STRUCTURED_RESPONSE
    ``memory.files``); this floor guarantees a file is never remembered as an
    opaque name."""
    fname = rel.rsplit("/", 1)[-1]
    parts: list[str] = [p for p in [(origin or "").strip()] if p]
    req = " ".join((user_message or "").split()).strip()
    if req and not _is_trivial_prompt(req):
        from app.agent_locale import rotulo
        parts.append(rotulo(lang, "a_raiz", x=req[:180]))
    snippet = _mention_snippet(response_text, fname)
    if snippet:
        parts.append(snippet)
    if root:
        sniff = _sniff_file_content(root, rel)
        if sniff:
            parts.append(sniff)
    return " · ".join(parts)[:600]


async def auto_index_generated_files(
    run_id: int | None,
    memory_user: str | None,
    response_text: str | None,
    workspace_subdir: str = "",
    since_ts: float | None = None,
    source: str = "chat",
    description: str | None = None,
    user_message: str | None = None,
) -> list[dict]:
    """Index files the agent produced/mentioned this turn into ``user_files``.

    Detects workspace paths in the agent's answer AND scans the scope's
    ``exports/`` folder for fresh files, so deliverables are remembered even
    when the agent forgets to call ``index_file``. Idempotent on
    ``(user_email, workspace_path)``. Returns the newly-indexed artifacts
    as ``[{filename, workspace_path}]`` for episode linkage.

    ``source`` tags the channel that produced the file (``chat``, ``email``,
    ``whatsapp``…) so the Memoria panel can tell apart deliverables sent by
    chat from those delivered through an external channel.

    v7.96.0 — every row gets SEMANTICS, not just a channel label: the stored
    description is composed by ``build_file_semantics`` (``description`` as
    the origin line + the user request + the answer sentence naming the file
    + a content sniff) and embedded for dense recall, so the memory can tell
    WHAT a file holds and WHEN to use it."""
    import hashlib
    import mimetypes
    import os

    owner = valid_memory_user(memory_user, allow_autonomous_identity=True)
    if not run_id or not owner:
        return []
    from app.agent_locale import resolve_agent_language, rotulo
    lang = await resolve_agent_language()
    candidates = set(_detect_workspace_paths(response_text))
    candidates |= set(_scan_recent_exports(workspace_subdir, since_ts))
    if not candidates:
        return []
    new_artifacts: list[dict] = []
    try:
        from app.app_models.memory import UserFile

        root = ""
        try:
            from app.workspace_fs import workspace_root
            root = workspace_root() or ""
        except Exception:
            root = ""
        # This block had INSIDE the lock, for every file: a `getsize` to disk, the
        # building of the semantics and an embedding (local model, CPU). Indexing ten
        # files blocked every other writer all that time — the same cause of
        # INC-VIG-25 as the backfill. Now: read without the lock, PREPARE outside,
        # and the lock only for the insert.
        async with app_session() as s:
            existing = set((await s.execute(
                select(UserFile.workspace_path).where(UserFile.user_email == owner)
            )).scalars().all())
        from app import memory_embeddings as _e
        filas = []
        for rel in sorted(candidates):
            if rel in existing:
                continue
            fname = rel.rsplit("/", 1)[-1]
            size = None
            if root:
                try:
                    size = os.path.getsize(os.path.join(root, rel))
                except OSError:
                    size = None
            key = "file-" + hashlib.sha1(
                f"{owner}|{rel}".encode("utf-8")
            ).hexdigest()[:24]
            origen = description or rotulo(lang, "generado_por", x=source)
            desc = build_file_semantics(
                rel, root=root, origin=origen,
                response_text=response_text, user_message=user_message, lang=lang,
            ) or origen
            try:
                blob = _e.pack(_e.embed(desc))
                emodel = _e.model_id()
            except Exception:  # pragma: no cover — embedding is best-effort
                blob, emodel = None, None
            filas.append(UserFile(
                idempotency_key=key,
                created_by_run_id=int(run_id),
                user_email=owner,
                filename=fname,
                workspace_path=rel,
                mime=mimetypes.guess_type(fname)[0],
                size=size,
                source=source,
                source_ref=f"run={run_id}",
                description=desc,
                embedding=blob,
                embed_model=emodel,
            ))
            new_artifacts.append({"filename": fname, "workspace_path": rel})
        if filas:
            async with app_write() as s:
                for fila in filas:
                    s.add(fila)
                await s.commit()
    except Exception:  # pragma: no cover — memory must never block chat
        logger.debug("auto_index_generated_files failed run=%s", run_id, exc_info=True)
        return []
    return new_artifacts


async def auto_record_chat_episode(
    run_id: int | None,
    memory_user: str | None,
    user_message: str,
    response_text: str | None = None,
    artifacts: list[dict] | None = None,
    source: str = "chat",
) -> None:
    """Best-effort episodic memory for turns the agent did not summarize.

    Idempotent on ``{source}-auto-{run_id}``. Skips conversational filler.
    ``summary`` = what the operator/sender asked; ``outcome`` = what was
    delivered (generated files, else a clean tail of the final answer) — so
    the Memoria panel reads "Resumen / Resultado" sensibly. ``source`` tags
    the channel (``chat``, ``email``, ``whatsapp``…) so an interaction held
    over email is recorded just like a chat turn.
    """
    import json as _json

    owner = valid_memory_user(memory_user, allow_autonomous_identity=True)
    if not run_id or not owner:
        return
    prompt = (user_message or "").strip()
    files = artifacts or []
    # Record a turn only when it carries signal (a real ask or a deliverable).
    if _is_trivial_prompt(prompt) and not files:
        return

    from app.agent_locale import resolve_agent_language, rotulo
    lang = await resolve_agent_language()
    summary = rotulo(lang, "peticion", x=_one_line(prompt, 160)) if prompt else rotulo(lang, "interaccion")
    if files:
        names = ", ".join(a.get("filename", "") for a in files if a.get("filename"))
        outcome = rotulo(lang, "genero", x=names) if names else rotulo(lang, "genero_ficheros")
    else:
        outcome = _clean_for_memory(response_text, 200) or None
    source_ref = _json.dumps(files, ensure_ascii=False)[:1500] if files else f"run={run_id}"

    try:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        from app.app_models.memory import UserEpisode
        from app.config import AGENT_MEMORY_EPISODE_KEEP

        idem = f"{source}-auto-{int(run_id)}"
        # The two checks that can ABORT the record (there is already an episode for
        # this run / it repeats a recent one) go with `app_session`: they are READS
        # and under WAL they run concurrently, without touching the write queue.
        # Doing them inside the lock paid the toll even in the most common case,
        # which is writing nothing.
        async with app_session() as s:
            # If the agent already recorded an episode for this run via the
            # MCP tool, its summary is richer than ours — don't duplicate it.
            already = (await s.execute(
                select(UserEpisode.id)
                .where(
                    UserEpisode.created_by_run_id == int(run_id),
                    UserEpisode.idempotency_key != idem,
                )
                .limit(1)
            )).first()
            if already is not None:
                return
            # SEMANTIC DEDUP: skip logging an episode that just repeats a very
            # recent one (same routine interaction turn after turn). Compare
            # against the latest few so the episodic layer keeps signal, not
            # noise. Only auto-episodes are deduped — explicit agent-recorded
            # ones (richer) go through the MCP tool, not here.
            recent = list((await s.execute(
                select(UserEpisode.summary)
                .where(UserEpisode.user_email == owner)
                .order_by(UserEpisode.created_at.desc())
                .limit(5)
            )).scalars().all())
        if any(_summaries_similar(summary, prev) for prev in recent):
            return
        # v7.29.0 — embed the summary for dense recall, and mark the
        # episode ``raw`` so the background job re-distills this template
        # capture into a real summary (chat has no structured memory block).
        #
        # The embedder is a LOCAL model (CPU). Computing it INSIDE the lock blocked
        # every other writer on EVERY chat turn, and with the pod at 500m of CPU that
        # piled up (INC-VIG-25). Outside: the lock only covers the INSERT and the
        # pruning.
        from app import memory_embeddings as _emb
        _blob = _emb.pack(_emb.embed(summary))
        async with app_write() as s:
            stmt = sqlite_insert(UserEpisode).values(
                idempotency_key=idem,
                created_by_run_id=int(run_id),
                user_email=owner,
                summary=summary[:2000],
                source=source,
                source_ref=source_ref,
                outcome=(outcome[:2000] if outcome else None),
                embedding=_blob,
                embed_model=_emb.model_id(),
                distill_state="raw",
            ).on_conflict_do_nothing(index_elements=[UserEpisode.idempotency_key])
            await s.execute(stmt)
            await s.commit()
            keep = max(int(AGENT_MEMORY_EPISODE_KEEP), 1)
            stale = list((await s.execute(
                select(UserEpisode.id)
                .where(UserEpisode.user_email == owner)
                .order_by(UserEpisode.created_at.desc())
                .offset(keep)
            )).scalars().all())
            if stale:
                from sqlalchemy import delete as sql_delete

                await s.execute(
                    sql_delete(UserEpisode).where(UserEpisode.id.in_(stale))
                )
                await s.commit()
    except Exception:  # pragma: no cover — memory must never block chat
        logger.debug("auto_record_chat_episode failed run=%s", run_id, exc_info=True)
