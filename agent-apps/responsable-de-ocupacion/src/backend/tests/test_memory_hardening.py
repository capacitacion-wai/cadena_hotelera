"""Memory hardening + dense-retrieval regression tests (v7.29.0).

Covers the pure, security-critical logic operators rely on: the trust
ceiling (the model can never raise trust above the run origin), the
anti-poisoning supersede/delete guard, the embedding round-trip + dedup
fingerprint, and the multifactor retrieval score. The live poisoning
red-team (an inbound message trying to plant/delete a verified fact)
complements this after deploy.
"""
from app import memory_trust as T
from app import memory_embeddings as E
from app import memory_rank as R


def test_trust_ceiling_never_raises():
    # The model may LOWER trust, never RAISE it above the origin ceiling.
    assert T.clamp_to_ceiling("verified", "untrusted") == "untrusted"
    assert T.clamp_to_ceiling("verified", "inferred") == "inferred"
    assert T.clamp_to_ceiling("inferred", "stated") == "inferred"   # lowering ok
    assert T.clamp_to_ceiling(None, "untrusted") == "untrusted"
    assert T.clamp_to_ceiling("disputed", "verified") == "verified"  # not tool-settable


def test_supersede_and_delete_are_trust_governed():
    assert T.can_supersede("verified", "untrusted") is True
    assert T.can_supersede("untrusted", "verified") is False        # poison delete blocked
    assert T.can_supersede("stated", "stated") is True
    assert T.can_supersede("verified", "stated", old_pinned=True) is True
    assert T.can_supersede("stated", "stated", old_pinned=True) is False  # pinned needs verified
    assert T.weight("verified") > T.weight("stated") > T.weight("inferred") > T.weight("untrusted")


def test_legacy_source_backfill():
    assert T.trust_from_legacy_source("operator") == "verified"
    assert T.trust_from_legacy_source("chat") == "stated"
    assert T.trust_from_legacy_source("email") == "inferred"
    assert T.trust_from_legacy_source(None) == "inferred"


def test_embedding_roundtrip_dedup_and_similarity():
    v_same_a = E.embed("el cliente prefiere correos cortos")
    v_same_b = E.embed("el cliente prefiere correos cortos")
    v_diff = E.embed("la factura de ACME vence el 30")
    assert E.cosine(v_same_a, v_same_b) > 0.99
    assert E.cosine(v_same_a, v_diff) < 0.5
    assert E.cosine(E.unpack(E.pack(v_same_a)), v_same_a) > 0.999
    assert E.cosine(v_same_a, [0.0] * 10) == 0.0                    # dim mismatch = safe 0
    assert E.content_hash("Hola,  MUNDO!") == E.content_hash("hola mundo")
    assert E.content_hash("") == ""


def test_retrieval_score_is_bounded_and_monotone():
    w = dict(w_rel=0.4, w_imp=0.2, w_rec=0.2, w_trust=0.2)
    assert abs(R.score(relevance=1, importance=1, recency=1, trust_weight=1, **w) - 1.0) < 1e-6
    assert abs(R.score(relevance=0, importance=0, recency=0, trust_weight=0, **w)) < 1e-6
    hi = R.score(relevance=1, importance=0.5, recency=0.5, trust_weight=1, **w)
    lo = R.score(relevance=1, importance=0.5, recency=0.5, trust_weight=0, **w)
    assert hi > lo                                                  # trust raises rank
    assert 0.0 <= R.score(relevance=2, importance=None, recency=0.5, trust_weight=0.5, **w) <= 1.0


def test_recency_decay_reinforcement():
    assert abs(R.recency_decay(0, 45) - 1.0) < 1e-6
    assert R.recency_decay(45, 45) < R.recency_decay(10, 45)
    assert abs(R.recency_decay(100, 45, pinned=True) - 1.0) < 1e-6
    assert R.recency_decay(60, 45, access_count=20) > R.recency_decay(60, 45, access_count=0)


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-q"])
