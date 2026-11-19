"""Helpers for the agent's SELF-memory (v7.140.0) — writes, reinforcement,
expiry, consolidation and the in-process block cache.

Mirrors ``memory_utils.py`` for the environment axis. The important differences
are the three that keep this honest:

* **Reinforce, never duplicate.** The same content on the same ``subject``
  bumps ``observations`` instead of inserting a near-copy — which is what turns
  "it took 38 min" repeated 40 times into evidence rather than noise.
* **Expiry is first-class.** A baseline is only true while the environment
  holds, so ``valid_until`` drops it out of the injected block automatically.
* **Writing never grants behaviour.** Everything lands ``review_state='new'``.
  Changing what the employee DOES goes through ``learning_effects_service``.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete as sql_delete, func, or_, select, update as sql_update
from sqlalchemy.orm import aliased
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.app_database import app_session, app_write

logger = logging.getLogger(__name__)

KINDS = ("observation", "baseline", "lesson", "procedure", "entity")
REVIEW_STATES = ("new", "validated", "rejected")

# ── Block cache ────────────────────────────────────────────────────────────
# The block is built on EVERY run (autonomous tick, channel message, chat
# prime), so it is the only genuinely hot path of this feature. The queries are
# indexed and LIMITed, but re-rendering the same text several times a minute is
# pure waste, so we cache the rendered text in-process and invalidate it with a
# version stamp bumped by every write. Deliberately in-process: the app is one
# pod per employee, and a stale block after a restart is simply a rebuild.
_CACHE: dict[str, tuple[int, dict]] = {}
_VERSION = {"n": 0}


def bump_memory_version() -> None:
    """Invalidate the block cache. Called by every write path."""
    _VERSION["n"] += 1
    _CACHE.clear()


def cache_get(key: str) -> dict | None:
    hit = _CACHE.get(key)
    if hit is None:
        return None
    ver, payload = hit
    return payload if ver == _VERSION["n"] else None


def cache_put(key: str, payload: dict) -> None:
    # Bounded: a handful of (subject, query) shapes per employee.
    if len(_CACHE) > 32:
        _CACHE.clear()
    _CACHE[key] = (_VERSION["n"], payload)


# ── Normalization ─────────────────────────────────────────────────────────
_SUBJECT_RE = re.compile(r"[^a-z0-9:_\-./]+")


def normalize_subject(value: str | None) -> str:
    """Canonical identity of what was observed, e.g. ``job:indexing``,
    ``deployment:wai-engine-api``, ``tenant:pabisan``.

    Same discipline as ``dedupe_key``: lowercase, collapse separators, keep the
    ``prefix:name`` shape. Without this, "Job Indexing" and "job:indexing"
    become two subjects and reinforcement silently stops working — which is the
    difference between evidence and a pile of near-duplicates.
    """
    s = (value or "").strip().lower()
    if not s:
        return ""
    s = s.replace(" ", "-")
    s = _SUBJECT_RE.sub("-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s[:120]


def valid_kind(value: str | None) -> str:
    k = (value or "").strip().lower()
    return k if k in KINDS else "observation"


def content_fingerprint(subject: str, content: str) -> str:
    """Fingerprint used for reinforcement. Includes the subject on purpose: the
    same sentence about two different jobs is two different learnings."""
    norm = re.sub(r"\s+", " ", (content or "").strip().lower())
    return hashlib.sha1(f"{subject}|{norm}".encode("utf-8")).hexdigest()[:32]


# ── Content discipline (7.149.0) ──────────────────────────────────────────
# Why this exists: the fingerprint above is what turns an anecdote into evidence, and it is
# enough for the text to carry the day's date for it to NEVER match. On the Vigía, the 12
# live entries had "1 observation" each and 0 baselines: the content was of the kind "On 28
# July 2026 the aggregated control covered 18 of 18 tenants, but the gate used up the 25
# seconds". That text cannot be reinforced tomorrow, cannot be consolidated and does not
# expire — it is dead learning as soon as the incident is fixed.
# The rule, therefore: the content is a TIMELESS and atomic fact. The WHEN lives in
# `first_seen_at`/`last_confirmed_at`, the HOW MANY TIMES in `observations` and the FROM
# WHERE in `provenance`.
_MESES = ("enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|"
          "setiembre|octubre|noviembre|diciembre")
# Absolute date in any of the forms the model writes (Spanish phrasing, as the facts are).
_DATE = (rf"(?:\d{{1,2}}\s+de\s+(?:{_MESES})(?:\s+de\s+\d{{4}})?"
         r"|\d{4}-\d{2}-\d{2}"
         r"|\d{1,2}/\d{1,2}/\d{2,4})")
_ABS_DATE_RE = re.compile(_DATE, re.IGNORECASE)
# INITIAL time phrase: it is the canonical form of the problem ("On 28 July 2026, …") and it
# can be removed without losing the fact, so it is removed instead of rejected. A date in
# the middle of the sentence is part of the fact → rejected.
_LEADING_DATE_RE = re.compile(
    rf"^(?:el\s+|durante\s+el\s+|a\s+fecha\s+de\s+|on\s+)?{_DATE}\s*[,:;]?\s+",
    re.IGNORECASE)
_LEADING_RELATIVE_RE = re.compile(
    r"^(?:hoy|ayer|anoche|esta\s+(?:mañana|tarde|noche)|"
    r"esta\s+semana|hace\s+\w+\s+(?:minutos?|horas?|días?))\s*[,:;]?\s+",
    re.IGNORECASE)


# 7.165.0 — CITING ITS OWN MEMORY. Measured on the Vigía: 7 entries of the kind "observations
# 27, 35, 36, 40, 41, 47… consistently show four or more workers". That is the MAINTENANCE
# run summarising its own memory inside a NEW entry instead of merging the originals: the
# cleanup fattened what it came to clean (it fits the `superseded_by = 0` we measured). And
# as a summary it is no use either: the numbers it cites stop meaning anything as soon as
# those rows are merged or retired.
#
# A fact of the environment NEVER needs to cite the memory. If it does, what it wanted was
# to consolidate, and that is what `supersedes` is for.
_SELF_REF_RE = re.compile(
    r"\b(?:observaci[oó]n(?:es)?|aprendizaje?s?|entradas?|memorias?)\s*"
    r"(?:n[uú]mero\s*)?#?\d+(?:\s*[,y]\s*#?\d+)*",
    re.IGNORECASE)


# 7.182.0 — CONFIGURATION IS NOT LEARNED. Measured on the Kommo employee: with the three
# dials open (maximum autonomy, write policy, allow permission) it did NOT touch the CRM and
# answered "I could not rename it because Kommo is in read-only mode". There was no veto nor
# attempt: it believed it from its own memory, where four days earlier it had stored "the
# composio-kommo connector is configured in read-only mode and blocks write operations",
# with a month of validity.
#
# The damage for a customer is worse than for the test: whoever OPENS their connector's
# writing finds an employee that keeps refusing for weeks —and explains it confidently,
# citing something that was true once.
#
# The write policy, the autonomy level and the tool permissions are STATE the operator
# changes whenever they want, and they travel in the prompt of EVERY run: the current
# posture is already in front of the employee. Memorizing them turns a turn of a dial into
# a month of wrong behaviour, so they are rejected just like dates.
_CONFIG_STATE_RE = re.compile(
    r"(?:"
    r"solo\s+(?:lectura|consulta)|s[oó]lo\s+(?:lectura|consulta)|read[_\s-]?only"
    r"|modo\s+(?:lectura|consulta)"
    r"|bloquea\s+(?:las\s+)?(?:operaciones\s+de\s+)?escritura"
    r"|no\s+(?:permite|deja|puede)\s+escribir"
    r"|(?:requiere|exige|pide)\s+(?:mi\s+|una\s+)?aprobaci[oó]n"
    r"|(?:permiso|escritura|acci[oó]n)\s+(?:denegad[oa]|vetad[oa]|bloquead[oa])"
    r"|nivel\s+de\s+autonom[ií]a"
    r"|write[_\s-]?policy|write[_\s-]?grant"
    r")",
    re.IGNORECASE)


def normalize_content(text: str) -> tuple[str, str | None]:
    """Returns ``(clean_content, rejection_reason)``.

    It removes the initial time phrase (recoverable) and rejects what cannot be fixed
    without inventing: an absolute date embedded in the fact or a text so long it is no
    longer an atomic fact. Rejecting is deliberate — truncating would store half a sentence
    and the half learning pollutes the block injected into every run.
    """
    clean = re.sub(r"\s+", " ", (text or "").strip())
    if not clean:
        return "", "empty content"
    for rx in (_LEADING_DATE_RE, _LEADING_RELATIVE_RE):
        stripped = rx.sub("", clean, count=1)
        if stripped != clean and stripped.strip():
            # After removing a leading date ("On 28 July 2026 "), the sentence starts in lowercase:
            # it is recapitalized so it still reads as a fact.
            clean = stripped[0].upper() + stripped[1:]
    try:
        from app.config import (AGENT_SELF_MEMORY_CONTENT_MAX,
                                AGENT_SELF_MEMORY_REJECT_DATED)
        limit = max(int(AGENT_SELF_MEMORY_CONTENT_MAX), 80)
        reject_dated = bool(AGENT_SELF_MEMORY_REJECT_DATED)
    except Exception:  # pragma: no cover — config always present at runtime
        limit, reject_dated = 240, True
    if reject_dated and _ABS_DATE_RE.search(clean):
        return clean, (
            "the content carries a specific date. A learning describes how this environment "
            "behaves, not what happened one day: rewrite it without the date (the when is "
            "already recorded) or store it as an episode if it really is the account of an event")
    if _CONFIG_STATE_RE.search(clean):
        return clean, (
            "that is not a learning, it is the CONFIGURATION of this moment (write policy, "
            "permissions or autonomy level). The operator changes it whenever they want and "
            "the current one travels in your prompt on every turn: storing it would make you "
            "keep believing it weeks after it is changed. If an action is blocked, say so in "
            "your answer or record it with `log_decision`, but do not memorize it: next time "
            "TRY IT and look at what the platform answers")
    if _SELF_REF_RE.search(clean):
        return clean, (
            "the content cites other entries of your memory by their number. A fact of the "
            "environment stands on its own: if what you wanted was to join several observations "
            "into a conclusion, do NOT write a new summary — write the distilled fact and pass "
            "the ids in `supersedes`, which is what retires the originals. A loose summary "
            "leaves the originals alive and adds one more row")
    if len(clean) > limit:
        return clean, (
            f"the content has {len(clean)} characters (maximum {limit}). Split it into atomic "
            f"facts: one per learning, so each can be reinforced and expire on its own")
    return clean, None


def _naive_utc(value: datetime | None) -> datetime | None:
    """Model timestamps are naive UTC (``datetime.utcnow``); mixing aware and
    naive values breaks every comparison in expiry and consolidation."""
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def parse_valid_until(value) -> datetime | None:
    """Accept an ISO stamp or a number of days ("30")."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return _naive_utc(value)
    raw = str(value).strip()
    if raw.isdigit():
        return datetime.utcnow() + timedelta(days=int(raw))
    try:
        return _naive_utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except ValueError:
        return None


# ── Vigencia ──────────────────────────────────────────────────────────────
def ttl_days(kind: str) -> int:
    """Default validity days of the type (0 = never expires).

    It is applied on the server on purpose: on the Vigía, NONE of the 12 real entries
    carried `valid_until` even though the prompt asked for it, so trusting the model for
    this does not work.
    """
    try:
        from app import config as _c
        return max(0, int({
            "observation": _c.AGENT_SELF_MEMORY_TTL_OBSERVATION_DAYS,
            "baseline": _c.AGENT_SELF_MEMORY_TTL_BASELINE_DAYS,
            "lesson": _c.AGENT_SELF_MEMORY_TTL_LESSON_DAYS,
            "procedure": _c.AGENT_SELF_MEMORY_TTL_PROCEDURE_DAYS,
            "entity": _c.AGENT_SELF_MEMORY_TTL_ENTITY_DAYS,
        }.get(kind, _c.AGENT_SELF_MEMORY_TTL_OBSERVATION_DAYS)))
    except Exception:  # pragma: no cover
        return 14


def _renewed_valid_until(kind: str, current: datetime | None,
                         now: datetime) -> datetime | None:
    """Validity after RECONFIRMING: a sliding window from today.

    It is the half that makes dead knowledge die on its own without cron or LLM: what keeps
    being observed renews its date, what stops being observed reaches `valid_until` and
    leaves the block. It never SHORTENS a validity that was already longer (a baseline with
    a hand-set date rules).
    """
    days = ttl_days(kind)
    if days <= 0:
        return current
    fresh = now + timedelta(days=days)
    cur = _naive_utc(current)
    return fresh if cur is None or cur < fresh else cur


# ── Writes ────────────────────────────────────────────────────────────────
async def _bump_evidence(session, row_id: int, observations, confidence,
                         kind: str, valid_until, now: datetime) -> None:
    """Raises an existing row's evidence and RENEWS its validity. The only place where
    "reconfirm" materializes: the two reinforcement paths (exact hash and similarity) share
    exactly the same effect."""
    from app.app_models.agent_memory import AgentMemory
    # Confidence grows with evidence but saturates: 40 sightings of the
    # same thing are not 40x more certain than 10.
    new_conf = min(0.99, float(confidence if confidence is not None else 0.5) + 0.05)
    await session.execute(
        sql_update(AgentMemory).where(AgentMemory.id == row_id).values(
            observations=int(observations or 1) + 1,
            last_confirmed_at=now,
            updated_at=now,
            confidence=new_conf,
            valid_until=_renewed_valid_until(kind, valid_until, now),
        )
    )


async def reinforce(subject: str, fingerprint: str, *, kind: str = "observation") -> int | None:
    """If a LIVE learning with the same fingerprint exists, bump its evidence
    (``observations`` +1, ``last_confirmed_at``, confidence nudged up, vigencia
    renovada) and return its id. Returns None when there is nothing to reinforce.

    This is what makes the count trustworthy: the employee re-asserting "the
    sweep took ~40 min" every night builds ONE baseline with 40 observations,
    not 40 rows.
    """
    if not fingerprint:
        return None
    try:
        from app.app_models.agent_memory import AgentMemory
        now = datetime.utcnow()
        async with app_write() as s:
            row = (await s.execute(
                select(AgentMemory.id, AgentMemory.observations, AgentMemory.confidence,
                       AgentMemory.kind, AgentMemory.valid_until)
                .where(
                    AgentMemory.content_hash == fingerprint,
                    AgentMemory.subject == subject,
                    AgentMemory.superseded_by.is_(None),
                )
                .limit(1)
            )).first()
            if row is None:
                return None
            await _bump_evidence(s, int(row[0]), row[1], row[2],
                                 str(row[3] or kind), row[4], now)
            await s.commit()
        bump_memory_version()
        return int(row[0])
    except Exception:  # pragma: no cover — never block a run
        logger.debug("agent-memory reinforce failed", exc_info=True)
        return None


async def supersede(ids, by_id: int, *, reason: str = "") -> int:
    """Retires `ids` marking them as superseded by `by_id`. Returns how many.

    It is the missing piece because of which consolidation cleaned NOTHING: the maintenance
    run was told to write a baseline "with `supersedes=[ids]`", but `remember_learning`
    silently ignored the parameter, so the run completed green without merging. 14 paid LLM
    runs and `superseded_by = 0` in the database.

    Superseding does NOT delete: it keeps the trace of why something is known (where the
    baseline came from) and takes the old ones out of the injected block, which is what matters.
    """
    try:
        limpio = [int(i) for i in (ids or []) if str(i).strip().isdigit() or isinstance(i, int)]
    except (TypeError, ValueError):
        return 0
    limpio = [i for i in limpio if i != int(by_id)]
    if not limpio:
        return 0
    try:
        from app.app_models.agent_memory import AgentMemory
        now = datetime.utcnow()
        async with app_write() as s:
            res = await s.execute(
                sql_update(AgentMemory)
                .where(AgentMemory.id.in_(limpio),
                       AgentMemory.superseded_by.is_(None),
                       AgentMemory.id != int(by_id),
                       # What backs an approved rule is not retired by consolidation: removing
                       # it is the operator's decision (Aprendizaje → Revertir).
                       AgentMemory.behavior_effect.is_(None))
                .values(superseded_by=int(by_id), updated_at=now,
                        provenance=(reason or "consolidado")[:400])
            )
            await s.commit()
        n = int(res.rowcount or 0)
        if n:
            bump_memory_version()
            logger.info("consolidación: %d aprendizaje(s) retirados por %s (%s)",
                        n, by_id, reason or "sin motivo")
        return n
    except Exception:  # pragma: no cover — never break a run over this
        logger.debug("supersede falló", exc_info=True)
        return 0


async def refine_neighbour(subject: str, kind: str, vector, texto: str) -> int | None:
    """LAYER 1 — the neighbour is UPDATED instead of opening a new row.

    Between "it is the same fact" (reinforcement) and "it is a different fact" (insert) there
    is an intermediate band that until now always ended up as an INSERT: the MORE COMPLETE
    version of something we already knew. Always appending is what produces heaps like the
    23 entries of `cluster:tenant-error-coverage`, which are the same subject told with more
    or less detail.

    Deterministic rule, without LLM: same subject and kind, similarity in the band
    [refine_min, dedupe_sim) and the new text EXTENDS the old one (longer and sharing most of
    the words). Then the neighbour's content is rewritten keeping its evidence — like a Mem0
    UPDATE — instead of leaving two versions living together.
    """
    if not vector or not texto:
        return None
    try:
        from app.app_models.agent_memory import AgentMemory
        from app import memory_embeddings as _e
        from app.config import (AGENT_SELF_MEMORY_DEDUPE_SIM,
                                AGENT_SELF_MEMORY_REFINE_SIM)
        alto, bajo = float(AGENT_SELF_MEMORY_DEDUPE_SIM), float(AGENT_SELF_MEMORY_REFINE_SIM)
        if bajo >= alto:
            return None
        pal_nuevo = set(re.findall(r"\w+", texto.lower()))
        now = datetime.utcnow()
        async with app_write() as s:
            filas = (await s.execute(
                select(AgentMemory.id, AgentMemory.content, AgentMemory.embedding,
                       AgentMemory.observations, AgentMemory.valid_until)
                .where(AgentMemory.subject == subject, AgentMemory.kind == kind,
                       AgentMemory.superseded_by.is_(None),
                       AgentMemory.review_state != "rejected",
                       AgentMemory.pinned.is_(False),
                       AgentMemory.embedding.isnot(None))
                .order_by(AgentMemory.updated_at.desc()).limit(40)
            )).all()
            mejor, mejor_sim = None, 0.0
            for f in filas:
                sim = _e.cosine(vector, _e.unpack(f[2]))
                if bajo <= sim < alto and sim > mejor_sim:
                    viejo = str(f[1] or "")
                    if _e.is_affirmative(texto) != _e.is_affirmative(viejo):
                        continue  # extending a negation with an affirmation would be lying
                    pal_viejo = set(re.findall(r"\w+", viejo.lower()))
                    # EXTENDS: it says more, and what the old one said is still there.
                    if len(texto) > len(viejo) and pal_viejo and \
                            len(pal_viejo & pal_nuevo) / len(pal_viejo) >= 0.7:
                        mejor, mejor_sim = f, sim
            if mejor is None:
                return None
            await s.execute(
                sql_update(AgentMemory).where(AgentMemory.id == int(mejor[0])).values(
                    content=texto[:4000],
                    content_hash=content_fingerprint(subject, texto),
                    embedding=_e.pack(vector), embed_model=_e.model_id(),
                    observations=int(mejor[3] or 1) + 1,
                    last_confirmed_at=now, updated_at=now,
                    valid_until=_renewed_valid_until(kind, mejor[4], now),
                )
            )
            await s.commit()
        bump_memory_version()
        logger.info("aprendizaje AMPLIADO (sim %.2f, id=%s, subject=%s)",
                    mejor_sim, mejor[0], subject)
        return int(mejor[0])
    except Exception:  # pragma: no cover
        logger.debug("refine_neighbour falló", exc_info=True)
        return None


async def reinforce_similar(subject: str, kind: str, vector,
                            texto: str | None = None) -> int | None:
    """SEMANTIC reinforcement: the same fact REWRITTEN must not open a new row.

    The exact hash only catches literal repetition; in practice the model rephrases ("the
    synchronization returns 404 persistently" / "Metabase answers Not found when
    synchronizing"). Cosine against the embeddings that were ALREADY stored without being
    used, bounded to the SAME subject (comparing across different subjects would mix things
    that only look alike when written). Returns the reinforced id or None.
    """
    if not vector:
        return None
    try:
        from app.app_models.agent_memory import AgentMemory
        from app import memory_embeddings as _e
        from app.config import AGENT_SELF_MEMORY_DEDUPE_SIM
        threshold = float(AGENT_SELF_MEMORY_DEDUPE_SIM)
        now = datetime.utcnow()
        async with app_write() as s:
            rows = (await s.execute(
                select(AgentMemory.id, AgentMemory.observations, AgentMemory.confidence,
                       AgentMemory.kind, AgentMemory.valid_until, AgentMemory.embedding,
                       AgentMemory.content)
                .where(
                    AgentMemory.subject == subject,
                    AgentMemory.superseded_by.is_(None),
                    AgentMemory.review_state != "rejected",
                    AgentMemory.embedding.isnot(None),
                )
                .order_by(AgentMemory.observations.desc(),
                          AgentMemory.updated_at.desc())
                .limit(60)
            )).all()
            best, best_sim = None, 0.0
            for r in rows:
                sim = _e.cosine(vector, _e.unpack(r[5]))
                # NEVER merge across polarity. Without this guard, "the sweep covers the 18
                # tenants" and "the sweep does NOT cover the 18 tenants" score 0.77 and were
                # taken as the same fact: the new one reinforced the old one and the WRONG
                # version won, silently. If they contradict each other, both go in and the
                # contradiction shows (layer 4).
                if texto is not None and _e.is_affirmative(texto) != _e.is_affirmative(r[6]):
                    continue
                if sim > best_sim:
                    best, best_sim = r, sim
            if best is None or best_sim < threshold:
                return None
            await _bump_evidence(s, int(best[0]), best[1], best[2],
                                 str(best[3] or kind), best[4], now)
            await s.commit()
        bump_memory_version()
        logger.info("self-memory: reforzado por similitud %.2f (id=%s, subject=%s)",
                    best_sim, best[0], subject)
        return int(best[0])
    except Exception:  # pragma: no cover — never block a run
        logger.debug("agent-memory semantic reinforce failed", exc_info=True)
        return None


async def remember(
    *,
    content: str,
    subject: str,
    kind: str = "observation",
    run_id: int | None = None,
    source: str = "agent",
    importance=None,
    confidence=None,
    valid_until=None,
    tags: str | None = None,
    supersedes=None,
) -> dict:
    """Store (or reinforce) one learning about the environment.

    Always lands ``review_state='new'``: a fresh learning INFORMS judgment and
    can change nothing on its own. Returns ``{"id", "reinforced": bool}`` — or
    ``{"id": None, "rejected": reason}`` if the content does not meet the 7.149.0
    discipline (a timeless and atomic fact). Never raises.

    Three gates, in this order: (1) content normalized/validated, (2) reinforcement by
    exact hash, (3) reinforcement by SIMILARITY. Only if all three fail is a new row
    opened — which is what prevents the same fact rewritten every night from turning into
    40 learnings of "1 observation".
    """
    subj = normalize_subject(subject)
    text, rejection = normalize_content(content)
    if not subj:
        return {"id": None, "reinforced": False, "error": "subject required"}
    if rejection:
        # The COMPLETE reason is returned: the agent sees the tool's error and can rewrite
        # it in the same turn. Storing it badly would be worse.
        logger.info("self-memory: aprendizaje rechazado (%s) subject=%s", rejection, subj)
        return {"id": None, "reinforced": False, "rejected": rejection,
                "error": rejection}
    k = valid_kind(kind)
    fp = content_fingerprint(subj, text)

    existing = await reinforce(subj, fp, kind=k)
    if existing is not None:
        return {"id": existing, "reinforced": True}

    try:
        from app.app_models.agent_memory import AgentMemory
        from app import memory_embeddings as _e
        try:
            vector = _e.embed(text)
            blob, emodel = _e.pack(vector), _e.model_id()
        except Exception:  # pragma: no cover — embedding is best-effort
            vector, blob, emodel = None, None, None
        # LAYER 1 — writing is DECIDING, not appending. In order from least to most change:
        #   1. same text  → reinforce (above, by hash)
        #   2. same fact rewritten → reinforce by similarity
        #   3. more complete version of the same → EXTEND the neighbour
        #   4. new fact → insert
        similar = await reinforce_similar(subj, k, vector, text)
        if similar is not None:
            return {"id": similar, "reinforced": True, "matched_by": "similarity"}
        ampliado = await refine_neighbour(subj, k, vector, text)
        if ampliado is not None:
            return {"id": ampliado, "reinforced": True, "matched_by": "refine",
                    "note": "I extended what I already knew instead of opening another entry"}
        try:
            imp = float(importance) if importance is not None else None
        except (TypeError, ValueError):
            imp = None
        try:
            conf = float(confidence) if confidence is not None else None
        except (TypeError, ValueError):
            conf = None
        now = datetime.utcnow()
        key = "selfmem-" + hashlib.sha1(f"{run_id}|{fp}".encode()).hexdigest()[:24]
        # Validity: whatever the caller asks for and, if it asks for none, the type's.
        # Without this default 100% of the real learnings were born without expiry
        # (measured) and were only pruned by capacity, 120 rows later.
        expires = parse_valid_until(valid_until) or _renewed_valid_until(k, None, now)
        async with app_write() as s:
            await s.execute(
                sqlite_insert(AgentMemory).values(
                    idempotency_key=key,
                    created_by_run_id=int(run_id) if run_id else None,
                    kind=k, subject=subj, content=text[:4000], tags=tags,
                    observations=1, first_seen_at=now, last_confirmed_at=now,
                    valid_until=expires,
                    confidence=conf, importance=imp,
                    source=source, provenance=f"run={run_id}" if run_id else None,
                    review_state="new",
                    content_hash=fp, embedding=blob, embed_model=emodel,
                ).on_conflict_do_nothing(index_elements=[AgentMemory.idempotency_key])
            )
            await s.commit()
            new_id = (await s.execute(
                select(AgentMemory.id).where(AgentMemory.idempotency_key == key)
            )).scalar()
        bump_memory_version()
        out = {"id": int(new_id) if new_id else None, "reinforced": False}
        # LAYER 0 — the distilled baseline RETIRES the observations it summarises. Without
        # this the consolidation completed green without merging anything (measured: 14 runs, 0 rows).
        if supersedes and out["id"]:
            n = await supersede(supersedes, out["id"], reason=f"consolidado en {out['id']}")
            if n:
                out["superseded"] = n
        return out
    except Exception:  # pragma: no cover
        logger.debug("agent-memory remember failed", exc_info=True)
        return {"id": None, "reinforced": False, "error": "write failed"}


async def record_episode(
    *, summary: str, subject: str = "", outcome: str | None = None,
    run_id: int | None = None, source: str = "autonomous",
) -> int | None:
    """Record what happened in one of the employee's OWN runs."""
    text = (summary or "").strip()
    if not text:
        return None
    try:
        from app.app_models.agent_memory import AgentEpisode
        from app import memory_embeddings as _e
        try:
            blob, emodel = _e.pack(_e.embed(text)), _e.model_id()
        except Exception:  # pragma: no cover
            blob, emodel = None, None
        key = f"selfep-{source}-{int(run_id or 0)}-" + hashlib.sha1(
            text.encode()).hexdigest()[:12]
        async with app_write() as s:
            await s.execute(
                sqlite_insert(AgentEpisode).values(
                    idempotency_key=key,
                    created_by_run_id=int(run_id) if run_id else None,
                    subject=normalize_subject(subject),
                    summary=text[:2000],
                    outcome=(outcome or "").strip()[:2000] or None,
                    source=source, source_ref=f"run={run_id}" if run_id else None,
                    embedding=blob, embed_model=emodel, distill_state="distilled",
                ).on_conflict_do_nothing(index_elements=[AgentEpisode.idempotency_key])
            )
            await s.commit()
            eid = (await s.execute(
                select(AgentEpisode.id).where(AgentEpisode.idempotency_key == key)
            )).scalar()
        bump_memory_version()
        return int(eid) if eid else None
    except Exception:  # pragma: no cover
        logger.debug("agent-memory episode failed", exc_info=True)
        return None


async def materialize_self_memory_block(
    run_id: int | None, structured: dict | None,
) -> dict:
    """Ingest the ``self_memory`` block of a STRUCTURED_RESPONSE — the zero-cost
    path where the agent distils its own learnings at the end of a turn (the
    same trick as ``materialize_memory_block`` for per-user memory).

    Shape::

        "self_memory": {
          "learnings": [{"subject": "job:indexing", "content": "...",
                         "kind": "baseline", "valid_until": 30}],
          "episode": {"subject": "...", "summary": "...", "outcome": "..."}
        }
    """
    out = {"learnings": 0, "reinforced": 0, "episode": 0}
    if not isinstance(structured, dict):
        return out
    block = structured.get("self_memory")
    if not isinstance(block, dict):
        # It omitted the whole block. It is NOT the same as "there was nothing to learn": an
        # explicit decision is asked for (`nothing_new`), so the absence is recorded as such
        # — it is the only way to see from the panel that the instruction is being ignored
        # rather than the environment being calm.
        out["absent"] = True
        await record_self_memory_verdict(run_id, out)
        return out
    from app.config import AGENT_SELF_MEMORY_BLOCK_MAX_ITEMS
    items = block.get("learnings") if isinstance(block.get("learnings"), list) else []
    for item in items[: max(1, int(AGENT_SELF_MEMORY_BLOCK_MAX_ITEMS))]:
        if not isinstance(item, dict):
            continue
        res = await remember(
            content=str(item.get("content") or ""),
            subject=str(item.get("subject") or ""),
            kind=str(item.get("kind") or "observation"),
            run_id=run_id, source="agent",
            importance=item.get("importance"),
            confidence=item.get("confidence"),
            valid_until=item.get("valid_until"),
        )
        if res.get("reinforced"):
            out["reinforced"] += 1
        elif res.get("id"):
            out["learnings"] += 1
    ep = block.get("episode")
    if isinstance(ep, dict) and str(ep.get("summary") or "").strip():
        if await record_episode(
            summary=str(ep.get("summary")), subject=str(ep.get("subject") or ""),
            outcome=(str(ep.get("outcome")) if ep.get("outcome") else None),
            run_id=run_id, source="autonomous",
        ):
            out["episode"] = 1
    # v7.143.0 — the turn's VERDICT, which is what makes "my employee learns nothing"
    # debuggable: without this, "it did not learn" and "it was not asked" look the same
    # (empty memory) and the operator cannot tell them apart.
    out["nothing_new"] = str(block.get("nothing_new") or "").strip()[:400]
    await record_self_memory_verdict(run_id, out)
    return out


# One single row overwritten per turn: the last decision about learning. It is deliberately
# a setting and not a table — the full trace is already in the run's answer; here it only
# has to answer "did the last round learn anything and, if not, why?" without going through
# runs.
SELF_MEMORY_VERDICT_KEY = "self_memory_last_verdict"


async def record_self_memory_verdict(run_id: int | None, out: dict) -> None:
    """Stores the learning verdict of the last turn. Best-effort: the employee cannot stop
    working because it could not note how it went."""
    import json as _json
    payload = {
        "run_id": int(run_id or 0),
        "at": datetime.now(timezone.utc).isoformat(),
        "learnings": int(out.get("learnings") or 0),
        "reinforced": int(out.get("reinforced") or 0),
        "nothing_new": out.get("nothing_new") or "",
        "absent": bool(out.get("absent")),
    }
    try:
        from app.app_models.runtime import AppSetting
        blob = _json.dumps(payload, ensure_ascii=False)
        async with app_write() as s:
            await s.execute(
                sqlite_insert(AppSetting)
                .values(key=SELF_MEMORY_VERDICT_KEY, value=blob,
                        updated_by="agent",
                        updated_at=datetime.now(timezone.utc))
                .on_conflict_do_update(
                    index_elements=[AppSetting.key],
                    set_={"value": blob, "updated_by": "agent",
                          "updated_at": datetime.now(timezone.utc)},
                )
            )
            await s.commit()
    except Exception:  # pragma: no cover — best-effort
        logger.debug("self-memory verdict not recorded (ignored)", exc_info=True)


async def last_verdict() -> dict | None:
    """The last round's verdict, for the Learning panel."""
    import json as _json
    try:
        from app.app_models.runtime import AppSetting
        async with app_session() as s:
            raw = (await s.execute(
                select(AppSetting.value)
                .where(AppSetting.key == SELF_MEMORY_VERDICT_KEY)
            )).scalar_one_or_none()
        return _json.loads(raw) if raw else None
    except Exception:  # pragma: no cover
        return None


# ── Maintenance ───────────────────────────────────────────────────────────
async def stats() -> dict:
    """Health header for the Aprendizaje tab: how much it knows, how much is
    still unvalidated, how much has expired."""
    from app.app_models.agent_memory import AgentMemory, AgentEpisode
    now = datetime.utcnow()
    async with app_session() as s:
        # 7.162.0 — "live" means the same here as in the block injected into the employee:
        # neither merged nor RETIRED. Before it only excluded the merged ones, so the pruning
        # was not visible: the Vigía retired 39 entries at 22:05 and the header kept showing
        # 107. The operator read "it cleans nothing" and was right about what they saw — the
        # cleanup had happened and the only number that counted it ignored it.
        base = select(func.count(AgentMemory.id)).where(
            AgentMemory.superseded_by.is_(None))
        live = base.where(AgentMemory.review_state != "rejected")
        total = (await s.execute(live)).scalar() or 0
        # And what was retired is published, because an invisible pruning cannot be told
        # apart from a broken pruning. It is the proof that the lifecycle works.
        retired = (await s.execute(
            base.where(AgentMemory.review_state == "rejected"))).scalar() or 0
        merged = (await s.execute(
            select(func.count(AgentMemory.id))
            .where(AgentMemory.superseded_by.isnot(None)))).scalar() or 0
        by_kind = {}
        for k in KINDS:
            by_kind[k] = int((await s.execute(
                live.where(AgentMemory.kind == k))).scalar() or 0)
        unvalidated = (await s.execute(
            live.where(AgentMemory.review_state == "new"))).scalar() or 0
        low_trust = (await s.execute(
            live.where(AgentMemory.review_state == "new",
                       AgentMemory.observations <= 1,
                       AgentMemory.access_count == 0))).scalar() or 0
        # Expired ones are counted over `base`: the pruning marks them "rejected", so
        # measuring them over `live` would always give 0 and we would lose the figure.
        expired = (await s.execute(
            base.where(AgentMemory.valid_until.isnot(None),
                       AgentMemory.valid_until < now))).scalar() or 0
        applied = (await s.execute(
            live.where(AgentMemory.behavior_effect.isnot(None)))).scalar() or 0
        subjects = (await s.execute(
            select(func.count(func.distinct(AgentMemory.subject)))
            .where(AgentMemory.superseded_by.is_(None))
        )).scalar() or 0
        episodes = (await s.execute(
            select(func.count(AgentEpisode.id)))).scalar() or 0
    return {
        "total": int(total), "by_kind": by_kind,
        "unvalidated": int(unvalidated), "expired": int(expired),
        "applied": int(applied), "subjects": int(subjects),
        "episodes": int(episodes),
        # Aliases the Learning tab consumes (`LearningViewer.tsx` reads
        # `entries_live`/`baselines`/`lessons`/`observations`). Without them the two header
        # counters ALWAYS showed 0 even with dozens of learnings in the list — the panel
        # contradicted its own data.
        "entries_live": int(total), "entries_total": int(total),
        # What the lifecycle has already taken — the visible proof that it works.
        "retired": int(retired), "merged": int(merged),
        # 7.154.0 — "unvalidated" stopped being a useful metric (everything was). What
        # matters is how much evidence holds up and how much needs a person.
        "low_trust": int(low_trust),
        "baselines": int(by_kind.get("baseline", 0)),
        "lessons": int(by_kind.get("lesson", 0)),
        "observations": int(by_kind.get("observation", 0)),
    }


async def redundancy_report(subject: str | None = None) -> list[dict]:
    """LAYER 2 — how much real REDUNDANCY there is per subject, not how much volume.

    The previous trigger was the raw volume ("N observations of the same subject"), which
    confuses "knows many things about this" with "repeats the same thing in ten ways". Here
    semantically close PAIRS are counted: it is what really has to be merged, and it also
    serves as the exit criterion — the consolidation has not finished while pairs remain.
    """
    from app.app_models.agent_memory import AgentMemory
    from app import memory_embeddings as _e
    from app.config import AGENT_SELF_MEMORY_REFINE_SIM
    umbral = float(AGENT_SELF_MEMORY_REFINE_SIM)
    q = (select(AgentMemory.id, AgentMemory.subject, AgentMemory.kind,
                AgentMemory.embedding)
         .where(AgentMemory.superseded_by.is_(None),
                AgentMemory.review_state != "rejected",
                AgentMemory.embedding.isnot(None)))
    if subject:
        q = q.where(AgentMemory.subject == subject)
    async with app_session() as s:
        filas = (await s.execute(q.limit(500))).all()
    por_subject: dict[str, list] = {}
    for f in filas:
        por_subject.setdefault(f[1], []).append(f)
    out = []
    for subj, grupo in por_subject.items():
        pares = _e.redundant_pairs([_e.unpack(f[3]) for f in grupo], umbral)
        if pares:
            out.append({"subject": subj, "entries": len(grupo), "redundant_pairs": pares})
    out.sort(key=lambda r: r["redundant_pairs"], reverse=True)
    return out


# LAYER 4 — NEGATION markers. Two entries of the same subject that talk about the same thing
# (high similarity) but one affirms and the other denies are not two learnings: they are a
# contradiction, and accumulating them is worse than knowing nothing. It does not try to do
# logical inference —that would require a model—, but to detect the frequent and OBVIOUS
# case: the polarity.
async def contradictions(subject: str | None = None) -> list[dict]:
    """LAYER 4 — pairs of the same subject that say the same thing with OPPOSITE polarity.

    It does not resolve: it brings them to light so the consolidation decides which one
    survives (superseding the old one with a reason) and so the block can warn there is tension.
    """
    from app.app_models.agent_memory import AgentMemory
    from app import memory_embeddings as _e
    from app.config import AGENT_SELF_MEMORY_CONTRADICTION_SIM
    umbral = float(AGENT_SELF_MEMORY_CONTRADICTION_SIM)
    q = (select(AgentMemory.id, AgentMemory.subject, AgentMemory.content,
                AgentMemory.embedding, AgentMemory.updated_at)
         .where(AgentMemory.superseded_by.is_(None),
                AgentMemory.review_state != "rejected",
                AgentMemory.embedding.isnot(None)))
    if subject:
        q = q.where(AgentMemory.subject == subject)
    async with app_session() as s:
        filas = (await s.execute(q.limit(400))).all()
    por_subject: dict[str, list] = {}
    for f in filas:
        por_subject.setdefault(f[1], []).append(f)
    out = []
    for subj, grupo in por_subject.items():
        items = [(int(f[0]), f[2], _e.unpack(f[3]), f[4] or datetime.min) for f in grupo]
        for par in _e.contradiction_pairs(items, umbral):
            out.append({"subject": subj, **par})
    return out


async def note_used(ids) -> None:
    """LAYER 3 — records that these entries were USED (they entered the block).

    It is the missing signal to tell apart what the employee consults from what only takes
    up space. `access_count`/`last_accessed_at` existed in the table from the start and
    nobody wrote them, so expiry could only look at the birth date.
    """
    try:
        ids = [int(i) for i in (ids or [])]
    except (TypeError, ValueError):
        return
    if not ids:
        return
    try:
        from app.app_models.agent_memory import AgentMemory
        now = datetime.utcnow()
        async with app_write() as s:
            await s.execute(
                sql_update(AgentMemory).where(AgentMemory.id.in_(ids)).values(
                    access_count=AgentMemory.access_count + 1, last_accessed_at=now)
            )
            await s.commit()
    except Exception:  # pragma: no cover — counting uses can never break a run
        logger.debug("note_used falló (ignorado)", exc_info=True)


async def saturation_report(top: int = 3) -> list[dict]:
    """SATURATED subjects: many live facts and many pairs that say the same thing.

    7.164.0 — write brake. Cleaning afterwards costs a model call; not writing is free.
    Until now nothing warned the agent BEFORE writing that the subject was already
    saturated, and the result measured on the Vigía was 31 entries on
    `cluster:cpu-scheduling` with 21 redundant pairs: the merge engine would see them, but
    only when its turn came and paying.
    """
    try:
        from app.app_models.agent_memory import AgentMemory
        from app.config import AGENT_SELF_MEMORY_SATURATED_AT
        limite = max(int(AGENT_SELF_MEMORY_SATURATED_AT), 3)
        redundancia = {r["subject"]: int(r.get("redundant_pairs") or 0)
                       for r in await redundancy_report()}
        async with app_session() as s:
            filas = (await s.execute(
                select(AgentMemory.subject, func.count(AgentMemory.id))
                .where(AgentMemory.superseded_by.is_(None),
                       AgentMemory.review_state != "rejected")
                .group_by(AgentMemory.subject)
                .having(func.count(AgentMemory.id) >= limite)
                .order_by(func.count(AgentMemory.id).desc())
                .limit(max(int(top), 1))
            )).all()
        return [{"subject": f[0], "entries": int(f[1]),
                 "redundant_pairs": redundancia.get(f[0], 0)} for f in filas]
    except Exception:  # pragma: no cover
        logger.debug("saturation_report falló", exc_info=True)
        return []


async def record_injection(run_id: int | None, ids) -> None:
    """Records WHICH learnings this run carried inside (7.164.0).

    It is the cheap half of the loop: recording when injecting. The other half
    (`attribute_outcome`) is closed by the end of the run. Without this, "used" would keep
    meaning "was selected for the block", which is a measure of RANK, not of usefulness —
    and the lifecycle would end up keeping what is injected most instead of what helps most.
    """
    try:
        rid = int(run_id or 0)
        ids = [int(i) for i in (ids or [])]
    except (TypeError, ValueError):
        return
    if not rid or not ids:
        return
    try:
        from app.app_models.agent_memory import AgentMemoryUse
        now = datetime.utcnow()
        async with app_write() as s:
            for eid in ids:
                await s.execute(
                    sqlite_insert(AgentMemoryUse)
                    .values(run_id=rid, entry_id=eid, created_at=now)
                    .prefix_with("OR IGNORE"))
            await s.commit()
    except Exception:  # pragma: no cover — never break a run over this
        logger.debug("record_injection falló (ignorado)", exc_info=True)


async def attribute_outcome(run_id: int | None, ok: bool) -> int:
    """The run finished: it distributes the outcome among what it carried inside.

    It is not causality —an entry being present does not prove it helped— and that is why
    they are TWO counters and not a mark: what matters is the proportion across many runs.
    An entry that only shows up when things go wrong is suspicious even if it is injected
    daily.

    Returns how many entries were attributed.
    """
    try:
        rid = int(run_id or 0)
    except (TypeError, ValueError):
        return 0
    if not rid:
        return 0
    try:
        from app.app_models.agent_memory import AgentMemory, AgentMemoryUse
        campo = AgentMemory.used_ok if ok else AgentMemory.used_ko
        async with app_write() as s:
            ids = list((await s.execute(
                select(AgentMemoryUse.entry_id).where(AgentMemoryUse.run_id == rid)
            )).scalars().all())
            if not ids:
                return 0
            await s.execute(
                sql_update(AgentMemory).where(AgentMemory.id.in_(ids))
                .values({campo: campo + 1}))
            # The link has done its job: the accumulated truth lives in the counters.
            await s.execute(
                sql_delete(AgentMemoryUse).where(AgentMemoryUse.run_id == rid))
            await s.commit()
            return len(ids)
    except Exception:  # pragma: no cover
        logger.debug("attribute_outcome falló (ignorado)", exc_info=True)
        return 0


async def maybe_enqueue_consolidation() -> None:
    """Enqueue a deduped ``self_memory_maintenance`` run when there are enough
    raw observations on one subject to be worth distilling into a baseline.

    Same "no cron" discipline as ``maybe_enqueue_autocompact``: called from the
    block build, which is the natural low-frequency moment.
    """
    try:
        from app.config import (
            AGENT_SELF_MEMORY_AUTOCOMPACT,
            AGENT_SELF_MEMORY_CONSOLIDATE_AFTER,
        )
        if not AGENT_SELF_MEMORY_AUTOCOMPACT:
            return
        from app.config import AGENT_SELF_MEMORY_CONSOLIDATE_STALE_DAYS
        after = max(int(AGENT_SELF_MEMORY_CONSOLIDATE_AFTER), 2)
        from app.app_models.agent_memory import AgentMemory
        live_obs = (
            AgentMemory.kind == "observation",
            AgentMemory.superseded_by.is_(None),
            AgentMemory.review_state != "rejected",
        )
        # LAYER 2 — first by measured REDUNDANCY: it is what really has to be merged. The raw
        # volume (below) stays as a net in case the embedder does not see the resemblance.
        redundantes = await redundancy_report()
        if redundantes:
            hot = (redundantes[0]["subject"], redundantes[0]["redundant_pairs"])
        else:
            hot = None
        async with app_session() as s:
            if hot is None:
                hot = (await s.execute(
                    select(AgentMemory.subject, func.count(AgentMemory.id).label("n"))
                    .where(*live_obs)
                    .group_by(AgentMemory.subject)
                    .having(func.count(AgentMemory.id) >= after)
                    .limit(1)
                )).first()
            if hot is None:
                # LONG TAIL: a subject with 2 observations that no longer grows will never
                # reach the threshold by volume — and it is exactly the one that rots
                # (observations of something already solved). If it has not moved for days,
                # it is also time to distil or retire it.
                stale_days = max(int(AGENT_SELF_MEMORY_CONSOLIDATE_STALE_DAYS), 1)
                cutoff = datetime.utcnow() - timedelta(days=stale_days)
                hot = (await s.execute(
                    select(AgentMemory.subject,
                           func.count(AgentMemory.id).label("n"))
                    .where(*live_obs)
                    .group_by(AgentMemory.subject)
                    .having(func.max(func.coalesce(
                        AgentMemory.last_confirmed_at,
                        AgentMemory.created_at)) < cutoff)
                    .limit(1)
                )).first()
        if hot is None:
            return
        import json as _json
        import time as _time
        from app.app_models.runtime import Event
        bucket = int(_time.time()) // 3600
        async with app_write() as s:
            await s.execute(
                sqlite_insert(Event).values(
                    fingerprint=f"selfmemmaint:{bucket}",
                    type="self_memory_maintenance",
                    source="auto:self-memory",
                    payload_json=_json.dumps({"subject": hot[0]}),
                    status="PENDING", attempts=0, max_attempts=1,
                    detected_at=datetime.now(timezone.utc),
                ).on_conflict_do_nothing(index_elements=[Event.fingerprint])
            )
            await s.commit()
    except Exception:  # pragma: no cover — best-effort
        logger.debug("self-memory consolidation enqueue skipped", exc_info=True)


async def force_consolidation(subject: str | None = None) -> bool:
    """Queues the semantic consolidation NOW, at a human's request (7.164.0).

    It shares the fingerprint with the automatic queueing —`selfmemmaint:{hour}`— so it
    respects the same brake of one per hour. That is deliberate: the button exists to
    unstick a stalled employee, not to be able to pay for the same merge ten times in a row
    by clicking.

    Returns True if this call created the run, False if there already was one in that hour
    (the UI says so, instead of pretending it did something).
    """
    import json as _json
    import time as _time
    try:
        from app.app_models.agent_memory import AgentMemory
        from app.app_models.runtime import Event
        if not subject:
            # The most redundant subject; if there is no measurable redundancy, the most populated.
            informe = await redundancy_report()
            if informe:
                subject = informe[0]["subject"]
            else:
                async with app_session() as s:
                    fila = (await s.execute(
                        select(AgentMemory.subject)
                        .where(AgentMemory.superseded_by.is_(None),
                               AgentMemory.review_state != "rejected")
                        .group_by(AgentMemory.subject)
                        .order_by(func.count(AgentMemory.id).desc())
                        .limit(1))).first()
                subject = fila[0] if fila else ""
        bucket = int(_time.time()) // 3600
        async with app_write() as s:
            res = await s.execute(
                sqlite_insert(Event).values(
                    fingerprint=f"selfmemmaint:{bucket}",
                    type="self_memory_maintenance",
                    source="operator:self-memory",
                    payload_json=_json.dumps({"subject": subject or ""}),
                    status="PENDING", attempts=0, max_attempts=1,
                    detected_at=datetime.now(timezone.utc),
                ).on_conflict_do_nothing(index_elements=[Event.fingerprint])
            )
            await s.commit()
            return bool(res.rowcount)
    except Exception:  # pragma: no cover
        logger.debug("force_consolidation falló", exc_info=True)
        return False


async def resolve_by_evidence() -> int:
    """Closes the contradictions that do NOT need anyone (7.164.0).

    When one of the two versions is both more recent and better backed, the evidence has
    already decided: the loser is retired stating why. Asking a person about that wastes
    their time, and it is what filled the queue.

    It never touches what a human confirmed nor what backs a behaviour rule: there the last
    word does not belong to a heuristic. Returns how many it retired.
    """
    try:
        from app.app_models.agent_memory import AgentMemory
        pendientes = (await needs_attention(limit=50)).get("resolvable") or []
        if not pendientes:
            return 0
        now = datetime.utcnow()
        n = 0
        async with app_write() as s:
            for caso in pendientes:
                res = await s.execute(
                    sql_update(AgentMemory)
                    .where(AgentMemory.id == int(caso["drop_id"]),
                           AgentMemory.review_state != "validated",
                           AgentMemory.pinned.is_(False),
                           AgentMemory.behavior_effect.is_(None))
                    .values(review_state="rejected",
                            reviewed_by="system:contradicción-resuelta",
                            reviewed_at=now, updated_at=now,
                            superseded_by=int(caso["keep_id"])))
                n += int(res.rowcount or 0)
            await s.commit()
        if n:
            bump_memory_version()
        return n
    except Exception:  # pragma: no cover
        logger.debug("resolve_by_evidence falló", exc_info=True)
        return 0


async def compact() -> dict:
    """Deterministic housekeeping (no LLM): expire what is past
    ``valid_until``, drop observations beyond the cap, and report what is ripe
    for consolidation. The semantic part (merging observations into a baseline)
    is the agent's job in the ``self_memory_maintenance`` run."""
    from sqlalchemy import delete as sql_delete
    from app.app_models.agent_memory import AgentMemory, AgentEpisode
    from app.config import (
        AGENT_SELF_MEMORY_OBSERVATIONS_KEEP,
        AGENT_SELF_MEMORY_EPISODE_KEEP,
    )
    out = {"expired": 0, "observations_pruned": 0, "episodes_pruned": 0}
    # Before pruning: close what the evidence has already resolved. It is deterministic and
    # free, and it lowers the judgement queue without spending a model call.
    resueltas = await resolve_by_evidence()
    if resueltas:
        out["contradictions_resolved"] = resueltas
    now = datetime.utcnow()
    try:
        async with app_write() as s:
            # Expired rows stay as history but must not look live to the viewer.
            res = await s.execute(
                sql_update(AgentMemory)
                .where(
                    AgentMemory.valid_until.isnot(None),
                    AgentMemory.valid_until < now,
                    AgentMemory.review_state != "rejected",
                    AgentMemory.behavior_effect.is_(None),
                    # Pinning = the human says this does not expire. Since 7.149.0 EVERY
                    # learning is born with a validity, so without this exception what is
                    # pinned would also expire behind their back.
                    AgentMemory.pinned.is_(False),
                )
                .values(review_state="rejected", reviewed_by="system:expired",
                        reviewed_at=now)
            )
            out["expired"] = int(res.rowcount or 0)
            # LAYER 3 — retire what has NEVER been used and already had its chance. Before,
            # the pruning was only by capacity (cap of 120): it came late and deleted by age,
            # not by uselessness. Now what nobody ever consulted despite being available for
            # days goes first; what is used survives even if it is old.
            from app.config import AGENT_SELF_MEMORY_UNUSED_GRACE_DAYS
            gracia = max(int(AGENT_SELF_MEMORY_UNUSED_GRACE_DAYS), 1)
            corte = now - timedelta(days=gracia)
            # 7.164.0 — three exclusions that were missing before and did harm:
            #
            #  · IT DOES NOT TOUCH WHAT IS IMPORTANT. The queue asks you about the "important
            #    seen only once" while the reaper counted its days to delete it. It was left
            #    at the mercy of you pinning in time something the system itself had marked
            #    as worthy of your judgement.
            #  · IT DOES NOT TOUCH WHAT HELPED. If it was inside runs that went well, its
            #    value is not "how many times it was injected".
            #
            # What is NOT done, and was tried: conditioning this rule on the entry having
            # already expired. It seemed to fix the mismatch "born with 14 days, the pruning
            # takes it at 7", but it leaves the rule USELESS — since 7.149.0 everything is born
            # with a validity, so it would never hold. And the mismatch was not one: expiring
            # and not being used measure different things ("it is no longer true" / "nobody
            # ever needed it"), and retiring the second one earlier is deliberate.
            from app.config import AGENT_SELF_MEMORY_IMPORTANT_FLOOR
            sin_uso = list((await s.execute(
                select(AgentMemory.id)
                .where(AgentMemory.kind == "observation",
                       AgentMemory.pinned.is_(False),
                       AgentMemory.behavior_effect.is_(None),
                       AgentMemory.access_count == 0,
                       AgentMemory.used_ok == 0,
                       func.coalesce(AgentMemory.importance, 0.0)
                           < float(AGENT_SELF_MEMORY_IMPORTANT_FLOOR),
                       AgentMemory.created_at < corte)
            )).scalars().all())
            if sin_uso:
                await s.execute(
                    sql_update(AgentMemory).where(AgentMemory.id.in_(sin_uso)).values(
                        review_state="rejected", reviewed_by="system:sin-uso",
                        reviewed_at=now))
                out["unused_retired"] = len(sin_uso)
            # 7.165.0 — IRREINFORCEABLE BY EVIDENCE. The 7.157.0 rule detected the same by
            # regex (date in the text) and fell short: with the suffix removed, the content
            # still carried the figures of the moment ("maximum of 92.34% on usewai-wk-2")
            # and could never be reinforced either. Measured: 50 of 107 entries of the Vigía
            # were still unrepeatable after cleaning the date.
            #
            # Instead of guessing the cause —forbidding numbers would be worse, "the sweep
            # takes 30-45 min" is a legitimate fact— the RESULT is measured: an observation
            # that has had ONE single sighting for days while its subject kept receiving
            # writes is not new, it just never matches itself.
            from app.config import AGENT_SELF_MEMORY_IRREINFORCEABLE_DAYS
            corte_ref = now - timedelta(
                days=max(int(AGENT_SELF_MEMORY_IRREINFORCEABLE_DAYS), 1))
            M2 = aliased(AgentMemory)
            # "Its subject stayed alive": there is something NEWER in the same subject.
            # Without this condition the observations of a quiet subject would fall, and they
            # are not irreinforceable — nothing has happened again.
            asunto_activo = (
                select(M2.id)
                .where(M2.subject == AgentMemory.subject,
                       M2.id != AgentMemory.id,
                       M2.superseded_by.is_(None),
                       M2.created_at > AgentMemory.created_at)
                .exists())
            irreforzables = list((await s.execute(
                select(AgentMemory.id)
                .where(AgentMemory.kind == "observation",
                       AgentMemory.review_state == "new",
                       AgentMemory.pinned.is_(False),
                       AgentMemory.behavior_effect.is_(None),
                       AgentMemory.superseded_by.is_(None),
                       AgentMemory.observations <= 1,
                       AgentMemory.used_ok == 0,
                       func.coalesce(AgentMemory.importance, 0.0)
                           < float(AGENT_SELF_MEMORY_IMPORTANT_FLOOR),
                       AgentMemory.created_at < corte_ref,
                       asunto_activo)
            )).scalars().all())
            if irreforzables:
                await s.execute(
                    sql_update(AgentMemory).where(AgentMemory.id.in_(irreforzables))
                    .values(review_state="rejected",
                            reviewed_by="system:irreforzable",
                            reviewed_at=now, updated_at=now))
                out["irreinforceable_retired"] = len(irreforzables)
            # 7.157.0 — INHERITED BALLAST: entries written before the content discipline,
            # with the date put in the text. It is not that they are old: they will NEVER be
            # reinforceable (the fingerprint changes every day), so they will never gain
            # confidence nor be consolidated. Measured on the Vigía: 63 of 107. They are
            # retired (they stay in the database, they leave the block and the queue) because
            # they also pollute the contradiction detection: they are the same story told
            # twenty times, and the operator ended up with 40 cards to decide.
            from app.agent_memory_utils import _ABS_DATE_RE as _RX
            candidatas = list((await s.execute(
                select(AgentMemory.id, AgentMemory.content)
                .where(AgentMemory.kind == "observation",
                       AgentMemory.pinned.is_(False),
                       AgentMemory.behavior_effect.is_(None),
                       AgentMemory.review_state == "new",
                       AgentMemory.observations <= 1)
            )).all())
            fechadas = [int(r[0]) for r in candidatas if _RX.search(str(r[1] or ""))]
            if fechadas:
                await s.execute(
                    sql_update(AgentMemory).where(AgentMemory.id.in_(fechadas)).values(
                        review_state="rejected", reviewed_by="system:no-reforzable",
                        reviewed_at=now))
                out["unreinforceable_retired"] = len(fechadas)
            keep_obs = max(int(AGENT_SELF_MEMORY_OBSERVATIONS_KEEP), 10)
            stale = list((await s.execute(
                select(AgentMemory.id)
                .where(AgentMemory.kind == "observation",
                       AgentMemory.pinned.is_(False),
                       AgentMemory.behavior_effect.is_(None))
                .order_by(AgentMemory.access_count.desc(),
                          AgentMemory.last_confirmed_at.desc().nullslast(),
                          AgentMemory.created_at.desc())
                .offset(keep_obs)
            )).scalars().all())
            if stale:
                await s.execute(sql_delete(AgentMemory).where(AgentMemory.id.in_(stale)))
                out["observations_pruned"] = len(stale)
            keep_eps = max(int(AGENT_SELF_MEMORY_EPISODE_KEEP), 10)
            old_eps = list((await s.execute(
                select(AgentEpisode.id)
                .order_by(AgentEpisode.created_at.desc())
                .offset(keep_eps)
            )).scalars().all())
            if old_eps:
                await s.execute(sql_delete(AgentEpisode).where(AgentEpisode.id.in_(old_eps)))
                out["episodes_pruned"] = len(old_eps)
            await s.commit()
        bump_memory_version()
    except Exception:  # pragma: no cover
        logger.warning("self-memory compact partial", exc_info=True)
    return out


# ── Confidence and human judgement (7.154.0) ─────────────────────────────
# Why it is redesigned: "validating" asked the operator to confirm EACH learning. With 107
# entries that is work nobody does —measured: 103 not validated, 4 validated—, so the mark
# «⚠ SIN VALIDAR» ended up on everything and stopped meaning anything. A review queue
# without triage is not a guarantee: it is noise that looks like a guarantee.
#
# The redesign separates the two things that were mixed in `review_state`:
#   · Is it TRUE?  → CONFIDENCE, which is earned with evidence (times seen, reconfirmed,
#     used). It needs no human and computes itself.
#   · Can it CHANGE what I do? → that is human, and it already had its path
#     (`propose_silence` → approved effects). It is not touched.
# And the human is taken ONLY where their judgement is decisive and scarce: contradictions
# the machine cannot resolve, and important things backed by a single observation.

TRUST_HIGH, TRUST_MEDIUM, TRUST_LOW = "alta", "media", "baja"


def trust_level(entry: dict) -> str:
    """Confidence DERIVED from the evidence, not from someone having signed it.

    High: confirmed by a person, or seen many times and also used.
    Medium: repeated or used at some point. Low: a single observation without use — which is
    exactly what the block must mark as "it deduced this, take it with a pinch of salt".
    """
    if (entry.get("review_state") or "") == "validated":
        return TRUST_HIGH
    obs = int(entry.get("observations") or 1)
    usos = int(entry.get("access_count") or 0)
    ok = int(entry.get("used_ok") or 0)
    origen = (entry.get("source") or "agent").strip().lower()
    # 7.164.0 — WHERE it comes from also counts, not only how many times it has been seen.
    # Before, the three provenances scored the same: a fact the employee OBSERVED in the
    # environment and one someone DROPPED on it through a channel went up the same path.
    # That is the door through which a memory gets poisoned: it is enough to repeat
    # something to it three times. What a person says counts, but it does not crown itself:
    # it needs the employee itself to have corroborated it (or an operator to confirm it,
    # which is the branch above).
    if origen == "chat":
        return TRUST_MEDIUM if (obs >= 2 or usos >= 1) else TRUST_LOW
    # Having been inside runs that went WELL is evidence of usefulness, not just of
    # repetition: it counts as one more observation for confidence.
    if obs >= 5 or (obs >= 3 and usos >= 1) or (obs >= 2 and ok >= 2):
        return TRUST_HIGH
    if obs >= 2 or usos >= 1:
        return TRUST_MEDIUM
    return TRUST_LOW


async def needs_attention(limit: int = 5) -> dict:
    """THE SHORT QUEUE: only what really needs a person's judgement.

    7.157.0 — the first version produced 40 cards and was again a long list with another
    name. Two corrections, learned from the Vigía's real panel (107 entries, 28 on the same
    subject, 63 with the date put in the text):

    · **One card per SUBJECT, not per pair.** Twenty pairs of the same subject are ONE
      problem ("it contradicts itself here"), not twenty decisions.
    · **Only what has WEIGHT arrives.** A contradiction between two loose observations is
      not the operator's work: it is exactly what the consolidation will merge on its own. At
      least one side is required to be backed (seen more than once, confirmed, or backing a
      rule). If nobody has seen it again, we bother nobody.

    The queue coming out empty is a good result, not a failure: it means the evidence is
    enough and there is nothing to decide.
    """
    from app.app_models.agent_memory import AgentMemory
    out: dict = {"contradictions": [], "thin_but_important": [], "resolvable": [], "total": 0}
    try:
        async with app_session() as s:
            # Weight of each entry, to decide what deserves a person.
            filas_peso = (await s.execute(
                select(AgentMemory.id, AgentMemory.observations,
                       AgentMemory.review_state, AgentMemory.behavior_effect,
                       AgentMemory.last_confirmed_at, AgentMemory.created_at)
                .where(AgentMemory.superseded_by.is_(None))
            )).all()
            peso = {
                int(r[0]): (int(r[1] or 1), (r[2] or "") == "validated", bool(r[3]))
                for r in filas_peso
            }
            # Freshness and evidence, to decide whether one version DOMINATES the other.
            detalle = {
                int(r[0]): {"obs": int(r[1] or 1),
                            "confirmada": (r[2] or "") == "validated",
                            "cuando": _naive_utc(r[4]) or _naive_utc(r[5])}
                for r in filas_peso
            }

        def respaldada(mid: int) -> bool:
            obs, confirmada, con_regla = peso.get(int(mid), (1, False, False))
            return obs > 1 or confirmada or con_regla

        # ── Contradictions: grouped by subject and only if there is weight ──
        # 7.164.0 — not every contradiction needs a person. If one of the two versions
        # DOMINATES the other —it is more recent AND better backed— the decision has already
        # been taken by the evidence, and asking wastes time. The Vigía's queue came out
        # with 6 cards and several were of this kind.
        #
        # Dominating requires BOTH things at once. "Newer" alone is not enough: a loose
        # observation from last night does not overturn a baseline seen twenty times.
        def domina(a: int, b: int) -> bool:
            da, db = detalle.get(int(a)), detalle.get(int(b))
            if not da or not db or da["cuando"] is None or db["cuando"] is None:
                return False
            if db["confirmada"] and not da["confirmada"]:
                return False   # what a person confirmed is not overturned by a heuristic
            return da["cuando"] > db["cuando"] and da["obs"] > db["obs"]

        por_asunto: dict[str, list] = {}
        for c in await contradictions():
            if not (respaldada(c["older_id"]) or respaldada(c["newer_id"])):
                continue  # two loose anecdotes → the consolidation resolves it
            gana = (c["newer_id"] if domina(c["newer_id"], c["older_id"])
                    else c["older_id"] if domina(c["older_id"], c["newer_id"]) else None)
            if gana is not None:
                out.setdefault("resolvable", []).append({
                    "subject": c["subject"], "keep_id": gana,
                    "drop_id": c["older_id"] if gana == c["newer_id"] else c["newer_id"],
                    "keep": c["newer"] if gana == c["newer_id"] else c["older"],
                    "drop": c["older"] if gana == c["newer_id"] else c["newer"],
                })
                continue
            por_asunto.setdefault(c["subject"], []).append(c)
        for subject, pares in sorted(por_asunto.items(),
                                     key=lambda kv: len(kv[1]), reverse=True)[:limit]:
            muestra = pares[0]
            out["contradictions"].append({
                "subject": subject, "pairs": len(pares),
                "older_id": muestra["older_id"], "newer_id": muestra["newer_id"],
                "older": muestra["older"], "newer": muestra["newer"],
            })
        # Trimmed like the rest: reporting 40 auto-resolved ones is another wall.
        if out.get("resolvable"):
            out["resolvable"] = out["resolvable"][:limit]

        # ── Important with little evidence: out with what is already in the merge queue ──
        # The SAME floor the reaper uses NOT to retire them (7.164.0): if they are asked about
        # here, there they cannot be deleted for disuse.
        from app.config import AGENT_SELF_MEMORY_IMPORTANT_FLOOR as _IMPORTANT_FLOOR
        saturados = {r["subject"] for r in await redundancy_report()}
        async with app_session() as s:
            filas = (await s.execute(
                select(AgentMemory.id, AgentMemory.subject, AgentMemory.content,
                       AgentMemory.importance)
                .where(AgentMemory.superseded_by.is_(None),
                       AgentMemory.review_state == "new",
                       AgentMemory.observations <= 1,
                       AgentMemory.access_count == 0,
                       AgentMemory.importance.isnot(None),
                       AgentMemory.importance >= float(_IMPORTANT_FLOOR))
                .order_by(AgentMemory.importance.desc())
                .limit(limit * 4)
            )).all()
        vistos: set[str] = set()
        for f in filas:
            subj = f[1]
            # If the subject is saturated with repetitions, this is NOT a decision: it is
            # material to distil. And one card per subject, not one per entry.
            if subj in saturados or subj in vistos:
                continue
            vistos.add(subj)
            out["thin_but_important"].append(
                {"id": int(f[0]), "subject": subj, "content": str(f[2])[:200],
                 "importance": float(f[3] or 0)})
            if len(out["thin_but_important"]) >= limit:
                break
    except Exception:  # pragma: no cover — the queue can never break the panel
        logger.debug("needs_attention falló", exc_info=True)
    out["total"] = len(out["contradictions"]) + len(out["thin_but_important"])
    return out


async def expire_config_state_memories(motivo: str) -> int:
    """Expires what the employee learned about the CONFIGURATION when it changes.

    The content rule (``_CONFIG_STATE_RE``) prevents it from being learned from 7.182.0 on,
    but the employees that ALREADY have it stored would keep refusing to act citing
    something that stopped being true — which is exactly the failure seen: with writing
    open, the employee answered "I could not rename it because Kommo is in read-only mode",
    without even trying, because four days earlier it had memorized it with a month of
    validity.

    `valid_until` is used (the model's honest mechanism): the row IS still there as history
    and for audit, but it stops being injected into every run's block. Nothing is deleted:
    deleting would hide that the employee believed that for a while, and that is exactly
    what an operator wants to be able to look at when something did not add up.

    Returns how many it expired (0 is the norm, and it is cheap: the table is small by
    design). It never raises: changing the dial cannot fail because of the memory.
    """
    from app.app_models.agent_memory import AgentMemory

    ahora = datetime.utcnow()
    # One second back, not "now": the prompt block filters by `valid_until >= now` and the
    # run can start at the same instant (in the test, 180 ms later). With the margin,
    # expired is expired.
    caducada_en = ahora - timedelta(seconds=1)
    caducadas = 0
    try:
        async with app_write() as s:
            filas = (await s.execute(
                select(AgentMemory.id, AgentMemory.subject, AgentMemory.content)
                .where(AgentMemory.superseded_by.is_(None))
                .where(or_(AgentMemory.valid_until.is_(None), AgentMemory.valid_until > ahora))
            )).all()
            ids = [int(f[0]) for f in filas if _CONFIG_STATE_RE.search(str(f[2] or ""))]
            if ids:
                await s.execute(
                    sql_update(AgentMemory).where(AgentMemory.id.in_(ids))
                    .values(valid_until=caducada_en, updated_at=ahora)
                )
                await s.commit()
                caducadas = len(ids)
                for f in filas:
                    if int(f[0]) in ids:
                        logger.info("memoria de configuración caducada (%s): #%s %s — %s",
                                    motivo, f[0], f[1], str(f[2])[:120])
        if caducadas:
            bump_memory_version()
    except Exception:  # pragma: no cover — it can never break the dial change
        logger.warning("no se pudo caducar la memoria de configuración", exc_info=True)
    return caducadas
