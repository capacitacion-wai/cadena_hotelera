"""Local text embeddings for dense memory retrieval + dedup (v7.29.0).

Design (ties to the "ad-hoc substrate + library, NOT framework" decision):
the app pod stays self-contained. We expose a stable ``embed(text)`` API and
a pluggable provider. The DEFAULT provider is fully local and dependency-free
— a hashed character/word n-gram bag projected to a fixed dimension and L2
normalised. It is deterministic, offline, multilingual-agnostic and good
enough for near-duplicate detection and coarse lexical relevance.

To upgrade to a real dense model (e.g. a small multilingual sentence
transformer via ``fastembed``), set ``AGENT_MEMORY_EMBED_MODEL`` and add the
provider in ``_provider()`` — every stored vector is tagged with
``embed_model`` so a model change re-embeds instead of comparing incompatible
vectors. Callers never change.

Vectors are stored as packed float32 blobs (``pack``/``unpack``).
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import struct
from typing import Optional

_DIM = 256  # local provider dimension; a real model overrides via its tag.
_TOKEN_RE = re.compile(r"[\wáéíóúñü]+", re.UNICODE)


def model_id() -> str:
    """Identifier stored alongside each vector. ``local-hash-vN`` for the
    built-in provider; the configured model name when one is set."""
    configured = (os.environ.get("AGENT_MEMORY_EMBED_MODEL") or "").strip()
    return configured or f"local-hash-v1-d{_DIM}"


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


def _local_embed(text: str) -> list[float]:
    """Hashed n-gram bag → L2-normalised fixed vector. Unigrams + bigrams so
    short factual phrases get a stable, comparable signature."""
    vec = [0.0] * _DIM
    toks = _tokens(text)
    if not toks:
        return vec
    grams = list(toks)
    grams += [f"{a}_{b}" for a, b in zip(toks, toks[1:])]
    for g in grams:
        h = int.from_bytes(hashlib.blake2b(g.encode("utf-8"), digest_size=8).digest(), "little")
        idx = h % _DIM
        sign = 1.0 if (h >> 63) & 1 else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


def embed(text: str) -> list[float]:
    """Embed one text with the active provider. Never raises — returns a zero
    vector on failure so a bad embed degrades ranking, never blocks a write."""
    try:
        # Extension point: when AGENT_MEMORY_EMBED_MODEL names a real model,
        # dispatch here (e.g. fastembed). Default = local, dependency-free.
        return _local_embed(text)
    except Exception:
        return [0.0] * _DIM


def pack(vec: Optional[list[float]]) -> Optional[bytes]:
    if not vec:
        return None
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack(blob: Optional[bytes]) -> Optional[list[float]]:
    if not blob:
        return None
    n = len(blob) // 4
    if n == 0:
        return None
    try:
        return list(struct.unpack(f"<{n}f", blob[: n * 4]))
    except Exception:
        return None


def cosine(a: Optional[list[float]], b: Optional[list[float]]) -> float:
    """Cosine similarity in [-1, 1]; 0 when either is missing or a different
    dimension (e.g. a stale vector from another model — safe, just no signal)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    # Vectors are stored L2-normalised, but a real model's may not be — be safe.
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def content_hash(text: str) -> str:
    """Normalized-content fingerprint for cross-run exact/near dedup: lowercase,
    strip punctuation, collapse spaces. Two pool workers asserting the same
    fact collide here and reinforce instead of duplicating."""
    norm = " ".join(_tokens(text))
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:20] if norm else ""


# ── Redundancy and contradiction analysis (7.153.0) ──────────────────
# Shared ON PURPOSE by BOTH of the employee's memories —the people one
# (``memory_utils``) and the environment one (``agent_memory_utils``)—: the criterion
# of "I already know this" and "this contradicts what I know" has to be the SAME in
# both, or you end up with two definitions of redundancy that diverge over time.
# They are PURE functions over already-loaded rows: no SQL, no async, testable.

# Two lessons of this detector, both from our own mistakes:
#  · `sin` ("without") was removed: it is ambiguous to the point of inverting the meaning (`sin problemas` AFFIRMS).
#  · The comparison is by WHOLE WORD. With substrings, «no» matched inside
#    «ninguno», «bueno» or «terreno», and clearly affirmative sentences were read as
#    negations — hiding exactly the contradictions this is meant to uncover.
_NEGATION_MARKERS = ("no", "nunca", "jamás", "jamas", "tampoco", "ningún", "ninguno",
                     "ninguna", "falla", "fallo", "falló", "incorrecto", "incorrecta")
_NEGATION_PHRASES = ("deja de", "ya no", "dejó de", "no puede")
_WORD_RE = re.compile(r"[\wáéíóúñü]+", re.UNICODE)


def is_affirmative(text: Optional[str]) -> bool:
    """Does the sentence AFFIRM (True) or DENY (False)?

    It does not try to do logical inference —that requires a model—, but to catch the
    frequent and obvious case: two entries about the same thing where one says something
    happens and the other that it does not. Accumulating that pair is worse than knowing nothing.

    KNOWN LIMIT: it misreads Spanish double negation («sin dejarse ninguno»
    AFFIRMS, and here it comes out as a negation). It is the price of not putting a model
    in a path that runs on every write; and the cost of being wrong is low, because
    this resolves nothing on its own: it flags the pair for consolidation to review.
    """
    t = (text or "").lower()
    if any(f in t for f in _NEGATION_PHRASES):
        return False
    return not (set(_WORD_RE.findall(t)) & set(_NEGATION_MARKERS))


def redundant_pairs(vectors: list, threshold: float) -> int:
    """How many PAIRS of the given vectors are similar above the threshold.

    It is the honest measure of "how much it repeats", as opposed to counting rows: ten
    different facts on the same subject are not redundancy; three ways of saying the same thing are.
    """
    n = 0
    for i in range(len(vectors)):
        for j in range(i + 1, len(vectors)):
            if cosine(vectors[i], vectors[j]) >= threshold:
                n += 1
    return n


def contradiction_pairs(items: list, threshold: float) -> list:
    """Pairs ``(a, b)`` that talk about the same thing with OPPOSITE polarity.

    ``items``: iterables ``(id, text, vector, updated_at)``. Returns dicts with
    the oldest and the newest, so whoever resolves knows which one is the candidate
    to be retired. It does NOT resolve: bringing it to light is already half the work.
    """
    out = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            a, b = items[i], items[j]
            if is_affirmative(a[1]) == is_affirmative(b[1]):
                continue
            if cosine(a[2], b[2]) < threshold:
                continue
            older, newer = sorted((a, b), key=lambda r: r[3] or 0)
            out.append({"older_id": older[0], "newer_id": newer[0],
                        "older": str(older[1])[:160], "newer": str(newer[1])[:160]})
    return out
