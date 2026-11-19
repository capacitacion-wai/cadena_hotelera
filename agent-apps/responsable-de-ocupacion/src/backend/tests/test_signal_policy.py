"""Unit tests for the UNIVERSAL signal policy (v7.41.0).

The whole point of ``app.signal_policy`` is that alert severity / recommendation
priority are DERIVED deterministically instead of living in the prompt. These
tests pin that determinism down: the derivation matrices are monotonic, low
confidence only ever downgrades, the dedupe key is stable/idempotent, reason
codes validate against the catalog, and legacy coercions round-trip — so an
upgraded app behaves identically to a fresh one and nobody can silently change
"how severe is this?" without a failing test.
"""
from __future__ import annotations

import itertools

from app import signal_policy as sp


# ── derivation matrices ──────────────────────────────────────────────────────
def _idx(seq, band):
    return seq.index(band)


def test_derive_extremes():
    assert sp.derive_severity("critical", "immediate", "high") == "critical"
    assert sp.derive_severity("low", "low", "high") == "info"
    assert sp.derive_priority("critical", "immediate", "high") == "urgent"
    assert sp.derive_priority("low", "low", "high") == "low"


def test_severity_monotonic_in_impact_and_urgency():
    # Fixing confidence high (no downgrade), raising impact or urgency must never
    # LOWER the derived severity band.
    for u in sp.URGENCY:
        prev = -1
        for i in sp.IMPACT:
            cur = _idx(sp.SEVERITY, sp.derive_severity(i, u, "high"))
            assert cur >= prev, (i, u, cur, prev)
            prev = cur
    for i in sp.IMPACT:
        prev = -1
        for u in sp.URGENCY:
            cur = _idx(sp.SEVERITY, sp.derive_severity(i, u, "high"))
            assert cur >= prev, (i, u, cur, prev)
            prev = cur


def test_priority_monotonic_in_impact_and_urgency():
    for u in sp.URGENCY:
        prev = -1
        for i in sp.IMPACT:
            cur = _idx(sp.PRIORITY, sp.derive_priority(i, u, "high"))
            assert cur >= prev, (i, u)
            prev = cur
    for i in sp.IMPACT:
        prev = -1
        for u in sp.URGENCY:
            cur = _idx(sp.PRIORITY, sp.derive_priority(i, u, "high"))
            assert cur >= prev, (i, u)
            prev = cur


def test_low_confidence_only_downgrades():
    # For every impact×urgency, low confidence yields a band <= the high-confidence
    # one (never higher), and at most one step lower.
    for i, u in itertools.product(sp.IMPACT, sp.URGENCY):
        hi = _idx(sp.SEVERITY, sp.derive_severity(i, u, "high"))
        lo = _idx(sp.SEVERITY, sp.derive_severity(i, u, "low"))
        assert lo <= hi
        assert hi - lo <= 1


def test_confidence_downgrade_toggle():
    params = {"confidence_downgrade": False}
    assert sp.derive_severity("high", "high", "low", params) == \
        sp.derive_severity("high", "high", "high", params)


def test_severity_shift_param():
    base = sp.derive_severity("medium", "medium", "high")
    up = sp.derive_severity("medium", "medium", "high", {"severity_shift": 1})
    assert _idx(sp.SEVERITY, up) == min(len(sp.SEVERITY) - 1, _idx(sp.SEVERITY, base) + 1)


def test_garbage_inputs_default_to_medium():
    # An unknown axis value must not crash; it degrades to the medium band.
    assert sp.coerce_impact("banana") == "medium"
    assert sp.coerce_urgency(None) == "medium"
    assert sp.coerce_confidence(42) == "medium"
    # derive still returns a valid band for junk inputs
    assert sp.derive_severity("banana", "banana", "banana") in sp.SEVERITY


# ── dedupe key ───────────────────────────────────────────────────────────────
def test_dedupe_key_idempotent_and_normalised():
    a = sp.normalize_dedupe_key(root_cause="  Impago   RECURRENTE ")
    b = sp.normalize_dedupe_key(root_cause="impago recurrente")
    assert a == b == "impago-recurrente"
    # applying normalisation again is a no-op
    assert sp.normalize_dedupe_key(root_cause=a) == a


def test_dedupe_key_cosmetic_separators_converge():
    # v7.100.0 regression: the agent slugified the SAME problem with a hyphen one
    # run and an underscore (or spaces) the next, which used to yield DIFFERENT
    # keys and pile up duplicate "Stock bajo…" signals. All variants must collapse
    # onto ONE key; the structural ':' between concern and target is preserved.
    variants = [
        "stock_bajo:warehouse:1:product:21",
        "stock-bajo:warehouse:1:product:21",
        "stock bajo : warehouse : 1 : product : 21",
        "  Stock--Bajo :warehouse:1:product:21 ",
    ]
    keys = {sp.normalize_dedupe_key(v) for v in variants}
    assert keys == {"stock-bajo:warehouse:1:product:21"}


def test_dedupe_key_precedence_and_none():
    """v7.107.0 — the concern (explicit → root_cause) is still chosen by precedence,
    but it NO LONGER WINS over the target: the target is ALWAYS APPENDED in canonical form.

    Up to 7.106.0 the explicit key won whole and the target was ignored — which is
    exactly the bug: the agent worded the concern (and the target inside it) differently
    on each pass, so the same problem produced different keys and duplicate rows.
    This test asserted that behaviour; now it asserts the correct one.
    """
    assert sp.normalize_dedupe_key("KEY-1", "cause", "order", "9") == "key-1:order:9"
    assert sp.normalize_dedupe_key(None, "cause", "order", "9") == "cause:order:9"
    assert sp.normalize_dedupe_key(None, None, "order", "9") == "order:9"
    assert sp.normalize_dedupe_key(None, None, None, None) is None
    # without a target, the concern rules as-is (behaviour unchanged)
    assert sp.normalize_dedupe_key("KEY-1", "cause") == "key-1"


def test_higher_band_keeps_worst():
    assert sp.higher_band("low", "high", sp.IMPACT) == "high"
    assert sp.higher_band("critical", "medium", sp.IMPACT) == "critical"
    assert sp.higher_band(None, "medium", sp.IMPACT) == "medium"


# ── reason codes ─────────────────────────────────────────────────────────────
def test_reason_code_validation():
    assert sp.validate_reason_code("below_threshold") == "below_threshold"
    assert sp.validate_reason_code("BELOW_THRESHOLD") == "below_threshold"
    assert sp.validate_reason_code("not_a_code") is None
    # a per-app restricted set disables codes outside it
    params = {"reason_codes": ["duplicate", "out_of_scope"]}
    assert sp.validate_reason_code("duplicate", params) == "duplicate"
    assert sp.validate_reason_code("below_threshold", params) is None


# ── legacy coercions (backfill parity) ───────────────────────────────────────
def test_legacy_severity_inverse():
    assert sp.severity_to_impact_urgency("critical") == ("critical", "high")
    assert sp.severity_to_impact_urgency("info") == ("low", "low")
    # unknown → medium/medium (never crashes)
    assert sp.severity_to_impact_urgency("weird") == ("medium", "medium")


def test_reason_code_from_disposition():
    assert sp.reason_code_from_disposition("deferred") == "deferred_timing"
    assert sp.reason_code_from_disposition("escalated") == "needs_human"
    assert sp.reason_code_from_disposition("no_action") == "no_action_needed"
    assert sp.reason_code_from_disposition("???") == "no_action_needed"


# ── public policy (manifest surface) ─────────────────────────────────────────
def test_public_policy_shape():
    pub = sp.public_policy("es")
    assert pub["impact"] == list(sp.IMPACT)
    assert pub["urgency"] == list(sp.URGENCY)
    assert set(pub["axes"]) == {"impact", "urgency", "confidence", "risk", "approval"}
    assert "reason_codes" in pub and pub["reason_codes"]
    assert pub["params"]["confidence_downgrade"] is True


# ── v7.107.0: the TARGET drives the key, not the agent's wording ──────────────
def test_dedupe_key_always_carries_the_canonical_target():
    """Real bug (2026-07): the agent wrote the target in two different ways
    between passes, so the same problem never converged and duplicates piled up.
    Now the target ALWAYS goes in canonical form at the end of the key."""
    a = sp.normalize_dedupe_key("reposicion:product.product:21:warehouse:1",
                                None, "product.product", "21")
    b = sp.normalize_dedupe_key("reposicion:warehouse:1:product:21",
                                None, "product.product", "21")
    assert a.endswith("product.product:21")
    assert b.endswith("product.product:21")
    # The target is not duplicated inside the key just because the concern already contains it
    assert a.count("product.product:21") == 1


def test_dedupe_key_target_only_collapses_the_free_hand_concern():
    """Recommendations: one pending ACTION per target. `'Revisar reposición de X'` and
    `'Definir la reposición de X'` on the SAME product are the same card."""
    revisar = sp.normalize_dedupe_key("reposicion:product.product:21:warehouse:1",
                                      None, "product.product", "21", target_only=True)
    definir = sp.normalize_dedupe_key("reabastecimiento-sin-regla:warehouse:1:product:21",
                                      None, "product.product", "21", target_only=True)
    assert revisar == definir == "product.product:21"


def test_dedupe_key_alerts_keep_distinct_concerns_on_the_same_target():
    """Alerts: two different RISKS on the same target can coexist (they are not
    collapsed), but the same risk converges because the target is canonical."""
    stock = sp.normalize_dedupe_key("stock-bajo", None, "product.product", "21")
    precio = sp.normalize_dedupe_key("margen-negativo", None, "product.product", "21")
    assert stock != precio
    assert stock == "stock-bajo:product.product:21"
    assert precio == "margen-negativo:product.product:21"


def test_dedupe_key_without_target_falls_back_to_the_concern():
    assert sp.normalize_dedupe_key(root_cause="impago recurrente") == "impago-recurrente"
    assert sp.normalize_dedupe_key() is None
