"""SQLite FTS5 full-text index over the DOCUMENT KNOWLEDGE LIBRARY (v7.84.0).

Same engine + rationale as ``memory_fts.py`` (BM25 keyword ranking, unicode61
``remove_diacritics 2`` so "gestión" matches "gestion", zero extra dependency, no
embeddings/vectors), but indexed over ``knowledge_chunks.text`` and joined back to
``knowledge_documents`` so a search can honour the per-document governance filters
(enabled, ready, scope, visibility) AND return the source citation.

External-content FTS5 synced by triggers — the index can never drift from the
chunk rows. Best-effort throughout: a SQLite build without FTS5, or a malformed
query, degrades to ``[]`` and NEVER blocks a write or a read.

Public API:
* ``ensure_fts(conn)`` — create the FTS table + triggers (idempotent) + one-shot
  backfill. Call at boot, after ``create_all`` + the memory ``ensure_fts``.
* ``search(session, query, *, attending, user_email, shareable_only, limit)`` —
  ranked list of ``{document_id, title, visibility, ordinal, snippet, relevance}``
  hits, already filtered to LIVE + IN-SCOPE + (optionally) shareable documents.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from sqlalchemy import text

logger = logging.getLogger(__name__)

_FTS = "fts_knowledge_chunks"
_BASE = "knowledge_chunks"
_COLS = ["text"]
_TOKENIZE = "unicode61 remove_diacritics 2"
_TOKEN_RE = re.compile(r"[0-9A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+", re.UNICODE)


def _match_expr(query: str) -> Optional[str]:
    """Free text → a SAFE FTS5 MATCH expression: alnum tokens only (drops FTS
    operators/quotes/punctuation), each OR'd as a prefix term. Tokens are alnum so
    they can never inject FTS5 syntax. ``None`` when there is nothing to search."""
    toks = _TOKEN_RE.findall(query or "")
    toks = [t for t in toks if len(t) >= 2][:20]
    if not toks:
        return None
    return " OR ".join(f"{t}*" for t in toks)


async def ensure_fts(conn) -> None:
    """Create the FTS5 table + sync triggers (idempotent) and backfill once.
    ``conn`` is a raw async connection (the boot ``begin()`` block). A SQLite
    build without FTS5 makes this a logged no-op — ``search`` then returns []."""
    if conn.dialect.name != "sqlite":
        return
    collist = ", ".join(_COLS)
    try:
        await conn.execute(text(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {_FTS} USING fts5("
            f"{collist}, content='{_BASE}', content_rowid='id', "
            f"tokenize='{_TOKENIZE}')"
        ))
    except Exception as exc:  # pragma: no cover — FTS5 not compiled in
        logger.warning("knowledge FTS5 unavailable (%s) — document search disabled", exc)
        return
    new_vals = ", ".join(f"new.{c}" for c in _COLS)
    old_vals = ", ".join(f"old.{c}" for c in _COLS)
    stmts = [
        f"CREATE TRIGGER IF NOT EXISTS {_BASE}_ai AFTER INSERT ON {_BASE} BEGIN "
        f"INSERT INTO {_FTS}(rowid, {collist}) VALUES (new.id, {new_vals}); END",
        f"CREATE TRIGGER IF NOT EXISTS {_BASE}_ad AFTER DELETE ON {_BASE} BEGIN "
        f"INSERT INTO {_FTS}({_FTS}, rowid, {collist}) VALUES('delete', old.id, {old_vals}); END",
        f"CREATE TRIGGER IF NOT EXISTS {_BASE}_au AFTER UPDATE ON {_BASE} BEGIN "
        f"INSERT INTO {_FTS}({_FTS}, rowid, {collist}) VALUES('delete', old.id, {old_vals}); "
        f"INSERT INTO {_FTS}(rowid, {collist}) VALUES (new.id, {new_vals}); END",
    ]
    for stmt in stmts:
        try:
            await conn.execute(text(stmt))
        except Exception:  # pragma: no cover
            logger.debug("knowledge FTS trigger create failed", exc_info=True)
    try:
        fts_n = (await conn.execute(text(f"SELECT count(*) FROM {_FTS}"))).scalar() or 0
        base_n = (await conn.execute(text(f"SELECT count(*) FROM {_BASE}"))).scalar() or 0
        if fts_n == 0 and base_n > 0:
            await conn.execute(text(f"INSERT INTO {_FTS}({_FTS}) VALUES('rebuild')"))
            logger.info("knowledge FTS backfilled (%d chunks)", base_n)
    except Exception:  # pragma: no cover
        logger.debug("knowledge FTS backfill failed", exc_info=True)


def _scope_clause(attending: bool) -> str:
    """The SQL fragment (on alias ``d``) selecting IN-SCOPE documents for the run.

    Mirrors ScopedKnowledge: a background/autonomous run sees ``all`` + ``autonomous``
    docs; a run SERVING a person sees ``all`` + ``attending`` docs plus that person's
    own ``user``-scoped docs (bound with ``:e``)."""
    if attending:
        return "(d.scope IN ('all','attending') OR (d.scope='user' AND d.user_email = :e))"
    return "d.scope IN ('all','autonomous')"


async def search(session, query: str, *, attending: bool = True,
                 user_email: str = "", shareable_only: bool = False,
                 limit: int = 6) -> list[dict]:
    """Full-text search the enabled, ready, in-scope knowledge documents. Returns
    a BM25-ranked list of hits ``{document_id, title, visibility, ordinal, snippet,
    relevance}`` (relevance ∈ (0,1], 1.0 = best match). ``shareable_only`` drops
    ``internal`` documents (use it when the answer will be QUOTED to a person).
    Empty list when FTS is off, the query has no usable tokens, or nothing matches."""
    from app.config import KNOWLEDGE_LIBRARY_ENABLED
    if not KNOWLEDGE_LIBRARY_ENABLED:
        return []
    expr = _match_expr(query)
    if expr is None:
        return []
    vis = "AND d.visibility = 'shareable'" if shareable_only else ""
    sql = (
        f"SELECT d.id AS document_id, d.title AS title, d.visibility AS visibility, "
        f"d.valid_until AS valid_until, "
        f"c.ordinal AS ordinal, c.text AS snippet, bm25({_FTS}) AS rank "
        f"FROM {_FTS} JOIN {_BASE} c ON c.id = {_FTS}.rowid "
        f"JOIN knowledge_documents d ON d.id = c.document_id "
        f"WHERE {_FTS} MATCH :q AND d.enabled = 1 AND d.status = 'ready' "
        f"AND {_scope_clause(attending)} {vis} "
        f"ORDER BY rank LIMIT :k"
    )
    params = {"q": expr, "k": max(1, int(limit))}
    if attending:
        params["e"] = user_email or ""
    try:
        rows = (await session.execute(text(sql), params)).all()
    except Exception:  # pragma: no cover — FTS5 absent / transient
        logger.debug("knowledge FTS search failed", exc_info=True)
        return []
    if not rows:
        return []
    from datetime import datetime
    now = datetime.utcnow()
    hits = []
    for r in rows:
        m = r._mapping  # noqa: SLF001
        raw = -float(m["rank"])  # bm25 is a cost; flip so bigger = better
        # v7.88.0 — surface valid_until + a STALE flag so the model can flag/avoid
        # quoting an out-of-date official figure (valid_until is a hint, not a hard
        # filter — we don't drop the doc, we mark it).
        vu = m["valid_until"]
        stale = False
        if vu is not None:
            try:
                vud = vu if isinstance(vu, datetime) else datetime.fromisoformat(str(vu).replace("Z", "").split("+")[0])
                stale = vud < now
            except Exception:
                stale = False
        hits.append({
            "document_id": int(m["document_id"]),
            "title": m["title"],
            "visibility": m["visibility"],
            "ordinal": int(m["ordinal"] or 0),
            "snippet": (m["snippet"] or "").strip(),
            "stale": stale,
            "_raw": raw,
        })
    top = max((h["_raw"] for h in hits), default=1.0) or 1.0
    for h in hits:
        h["relevance"] = round(max(0.0, h.pop("_raw") / top), 4) if top > 0 else 1.0
    return hits
