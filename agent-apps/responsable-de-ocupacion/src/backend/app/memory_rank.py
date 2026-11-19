"""Multifactor memory retrieval scoring (v7.29.0).

The canonical Generative-Agents formula (adopted by Mem0/Letta/LangMem),
extended with a TRUST term so poisoned/low-provenance memories rank below
verified ones:

    score = w_rel·relevance + w_imp·importance + w_rec·recency + w_trust·trust

* relevance — embedding cosine to the CURRENT query (0 when there is no
  query, e.g. the chat prime "core" block, which then ranks on the rest).
* importance — the stored 0..1 salience.
* recency — exponential soft-decay of age, REINFORCED (reset toward 1.0)
  by pinning / high access count, so a stale never-used fact fades but a
  leaned-on one stays (OWASP: decay + reinforcement).
* trust — the provenance weight (verified > stated > inferred > untrusted).

Pure + dependency-free so it is unit-tested in isolation.
"""
from __future__ import annotations

import math
from typing import Optional


def recency_decay(age_days: float, half_life_days: float,
                  *, pinned: bool = False, access_count: int = 0) -> float:
    """0..1 recency term. Pinned facts never decay; each access effectively
    rejuvenates the memory (caps at no-decay). ``half_life_days<=0`` disables
    decay (returns 1.0)."""
    if pinned or half_life_days <= 0:
        return 1.0
    # Reinforcement: every access is worth a fraction of the half-life, so a
    # frequently-used old fact behaves "younger".
    effective_age = max(0.0, age_days - min(access_count, 20) * (half_life_days / 10.0))
    return math.pow(0.5, effective_age / half_life_days)


def score(*, relevance: float, importance: Optional[float], recency: float,
          trust_weight: float,
          w_rel: float, w_imp: float, w_rec: float, w_trust: float) -> float:
    """Weighted, bounded combination. Each factor is already in [0,1] (or
    [-1,1] for relevance → clamped to [0,1])."""
    rel = max(0.0, min(1.0, relevance))
    imp = 0.5 if importance is None else max(0.0, min(1.0, importance))
    rec = max(0.0, min(1.0, recency))
    tw = max(0.0, min(1.0, trust_weight))
    total_w = (w_rel + w_imp + w_rec + w_trust) or 1.0
    raw = w_rel * rel + w_imp * imp + w_rec * rec + w_trust * tw
    return raw / total_w
