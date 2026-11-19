"""SQLite FTS5 full-text index over per-user memory (v7.56.0).

Inspired by the FTS5 memory search in NousResearch/hermes-agent (MIT), adapted to
WAI's schema and per-user model. Rationale: the built-in ``memory_embeddings``
provider is a hashed n-gram bag ranked by O(N) Python cosine — fine for dedup,
weak for lexical recall. FTS5 gives proper **BM25 keyword ranking**, indexed in
SQLite, accent-insensitive (Spanish), with **zero extra dependency or model** —
the same "app-native, no external services" principle as the rest of memory.

Kept DB-maintained on purpose: the index is an **external-content FTS5** table
synced by triggers, so it cannot drift from the base tables (the same
"derive from the real photo, don't propagate" invariant we hold elsewhere).

Everything is best-effort: if the SQLite build lacks FTS5, or a query is
malformed, every entry point degrades to the caller's fallback (embedding cosine
/ recency) and NEVER blocks a write or a read.

v7.140.0 — the same machinery now serves BOTH memory axes (per-user memory and
the agent's own learning about its environment). One spec table, one search
implementation: the only difference is whether the layer has an owner column.

Public API:
* ``ensure_fts(conn)`` — create the FTS tables + triggers for every spec
  (idempotent), and backfill existing rows once. Call at boot, after
  ``create_all``.
* ``search(session, key, query, user_email, limit)`` — ``{row_id: relevance}``
  where relevance ∈ (0, 1], 1.0 = best BM25 match. ``{}`` when disabled / no hit.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from sqlalchemy import text

logger = logging.getLogger(__name__)

# key → (fts table, base table, [indexed text columns], optional extra
#        live-filter, owner column)
# The FTS column names MUST equal the base-table column names so external-content
# 'rebuild' can repopulate from the content table.
#
# ``owner`` (v7.140.0) is the column that scopes a search to its owner, and it is
# what lets ONE implementation serve both memory axes: per-user memory scopes by
# ``user_email``, the agent's SELF-memory has no owner at all (its identity is
# the ``subject``, and there is exactly one employee per app). ``owner=None``
# means "no owner filter" — never "any owner", since these tables simply have
# nothing personal in them.
_SPECS: dict[str, dict] = {
    "user_memory": {
        "fts": "fts_user_memory", "base": "user_memory",
        "cols": ["content", "tags"],
        # Only LIVE (non-superseded) facts are relevant.
        "live": "AND b.superseded_by IS NULL",
        "owner": "user_email",
    },
    "user_episodes": {
        "fts": "fts_user_episodes", "base": "user_episodes",
        "cols": ["summary", "outcome"], "live": "",
        "owner": "user_email",
    },
    "user_files": {
        "fts": "fts_user_files", "base": "user_files",
        "cols": ["filename", "description"], "live": "",
        "owner": "user_email",
    },
    # v7.140.0 — the agent's own learning about its environment. ``live``
    # excludes superseded rows AND expired ones: a baseline that stopped being
    # true must not surface in search either, or the viewer would show as
    # current something the injected block already dropped.
    "agent_memory": {
        "fts": "fts_agent_memory", "base": "agent_memory",
        "cols": ["content", "subject", "tags"],
        "live": (
            "AND b.superseded_by IS NULL AND b.review_state <> 'rejected' "
            "AND (b.valid_until IS NULL OR b.valid_until >= datetime('now'))"
        ),
        "owner": None,
    },
    "agent_episodes": {
        "fts": "fts_agent_episodes", "base": "agent_episodes",
        "cols": ["summary", "outcome", "subject"], "live": "",
        "owner": None,
    },
}

# unicode61 + remove_diacritics=2 → "gestión" matches "gestion" (Spanish-friendly).
_TOKENIZE = "unicode61 remove_diacritics 2"
_TOKEN_RE = re.compile(r"[0-9A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+", re.UNICODE)


def _match_expr(query: str) -> Optional[str]:
    """Turn a free-text query into a SAFE FTS5 MATCH expression: alnum tokens
    only (drops FTS operators/quotes/punctuation), each as an OR'd prefix term so
    "gestion" also matches "gestionar". Returns None when there is nothing to
    search. Tokens are alnum so they can never inject FTS5 syntax."""
    toks = _TOKEN_RE.findall(query or "")
    toks = [t for t in toks if len(t) >= 2][:20]  # drop 1-char noise; cap width
    if not toks:
        return None
    return " OR ".join(f"{t}*" for t in toks)


async def ensure_fts(conn) -> None:
    """Create the FTS5 tables + sync triggers (idempotent) and backfill existing
    rows once. ``conn`` is a raw async connection (the boot ``begin()`` block).
    A missing FTS5 build makes this a logged no-op — ``search`` then returns {}."""
    if conn.dialect.name != "sqlite":
        return
    for spec in _SPECS.values():
        fts, base, cols = spec["fts"], spec["base"], spec["cols"]
        collist = ", ".join(cols)
        try:
            await conn.execute(text(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {fts} USING fts5("
                f"{collist}, content='{base}', content_rowid='id', "
                f"tokenize='{_TOKENIZE}')"
            ))
        except Exception as exc:  # pragma: no cover — FTS5 not compiled in
            logger.warning("memory FTS5 unavailable (%s) — search falls back to embeddings", exc)
            return
        # External-content sync triggers: keep the index in lockstep with the base
        # table so it can never drift. The 'delete' command feeds OLD values back.
        new_vals = ", ".join(f"new.{c}" for c in cols)
        old_vals = ", ".join(f"old.{c}" for c in cols)
        stmts = [
            f"CREATE TRIGGER IF NOT EXISTS {base}_ai AFTER INSERT ON {base} BEGIN "
            f"INSERT INTO {fts}(rowid, {collist}) VALUES (new.id, {new_vals}); END",
            f"CREATE TRIGGER IF NOT EXISTS {base}_ad AFTER DELETE ON {base} BEGIN "
            f"INSERT INTO {fts}({fts}, rowid, {collist}) VALUES('delete', old.id, {old_vals}); END",
            f"CREATE TRIGGER IF NOT EXISTS {base}_au AFTER UPDATE ON {base} BEGIN "
            f"INSERT INTO {fts}({fts}, rowid, {collist}) VALUES('delete', old.id, {old_vals}); "
            f"INSERT INTO {fts}(rowid, {collist}) VALUES (new.id, {new_vals}); END",
        ]
        for stmt in stmts:
            try:
                await conn.execute(text(stmt))
            except Exception:  # pragma: no cover
                logger.debug("memory FTS trigger create failed for %s", base, exc_info=True)
        # Backfill ONCE: only when the index is empty but the base table isn't
        # (first boot with FTS, or after an operator dropped it). Ongoing writes
        # are covered by the triggers, so we don't re-index every boot.
        try:
            fts_n = (await conn.execute(text(f"SELECT count(*) FROM {fts}"))).scalar() or 0
            base_n = (await conn.execute(text(f"SELECT count(*) FROM {base}"))).scalar() or 0
            if fts_n == 0 and base_n > 0:
                await conn.execute(text(f"INSERT INTO {fts}({fts}) VALUES('rebuild')"))
                logger.info("memory FTS backfilled %s (%d rows)", fts, base_n)
        except Exception:  # pragma: no cover
            logger.debug("memory FTS backfill failed for %s", base, exc_info=True)


async def search(session, key: str, query: str, user_email: str | None = None,
                 limit: int = 50) -> dict[int, float]:
    """Full-text search one memory layer. Returns ``{row_id: relevance}`` with
    relevance ∈ (0, 1] (1.0 = best BM25 match), ordered by BM25. Empty dict when
    FTS is off/unavailable, the query has no usable tokens, or nothing matches —
    the caller then keeps its own fallback.

    ``user_email`` is REQUIRED for the owned layers (per-user memory) and
    ignored for the unowned ones (the agent's self-memory): a spec with
    ``owner=None`` has nothing personal to scope, so demanding an owner there
    would just make the search unreachable."""
    from app.config import AGENT_MEMORY_FTS
    if not AGENT_MEMORY_FTS:
        return {}
    spec = _SPECS.get(key)
    expr = _match_expr(query)
    if spec is None or expr is None:
        return {}
    owner_col = spec.get("owner")
    if owner_col and not user_email:
        return {}
    fts, base, live = spec["fts"], spec["base"], spec["live"]
    where_owner = f"AND b.{owner_col} = :e " if owner_col else ""
    sql = (
        f"SELECT b.id AS id, bm25({fts}) AS rank "
        f"FROM {fts} JOIN {base} b ON b.id = {fts}.rowid "
        f"WHERE {fts} MATCH :q {where_owner}{live} "
        f"ORDER BY rank LIMIT :k"
    )
    params = {"q": expr, "k": max(1, int(limit))}
    if owner_col:
        params["e"] = user_email
    try:
        rows = (await session.execute(text(sql), params)).all()
    except Exception:  # pragma: no cover — FTS5 absent / transient
        logger.debug("memory FTS search failed for %s", key, exc_info=True)
        return {}
    if not rows:
        return {}
    # bm25() is a COST (more negative = more relevant). Flip to a positive score
    # and normalise by the best hit so relevance lands in (0, 1], 1.0 = top match.
    raw = {int(r._mapping["id"]): -float(r._mapping["rank"]) for r in rows}  # noqa: SLF001
    top = max(raw.values()) or 1.0
    if top <= 0:
        # All-equal / degenerate ranks: fall back to uniform positive relevance.
        return {i: 1.0 for i in raw}
    return {i: max(0.0, v / top) for i, v in raw.items()}
