"""The UNIVERSAL signal policy (v7.41.0) — one runtime source of truth for how
the employee decides WHAT to emit and with WHICH intensity, shared by alerts,
recommendations and decisions.

Why it exists: before this, "how severe is this alert?" / "how urgent is this
recommendation?" lived only in the agent's prompt, so two agents (or two runs)
answered differently and nothing was auditable. This module makes the policy
CODE: deterministic, testable and identical for every deployed employee. The
prompt/skills only decide *whether* a signal fires; this module decides its
*intensity* and *shape*.

Five axes, deliberately SEPARATE and DOMAIN-AGNOSTIC (never "sales/CRM"):

  * impact      — magnitude of the consequence         (low|medium|high|critical)
  * urgency     — how soon action is needed            (low|medium|high|immediate)
  * confidence  — how sure the agent is of the finding (low|medium|high)
  * risk        — the DERIVED band an alert carries (``severity``), and the
                  DERIVED band a recommendation carries (``priority``). Computed
                  from impact × urgency, downgraded when confidence is low.
  * approval    — the operator gate (the unified lifecycle ``status`` +
                  ``approvals``); NOT modelled here.

The intensity is DERIVED so it is a single source of truth: an agent that raises
``impact=high, urgency=immediate`` ALWAYS gets ``severity=critical`` (unless it
overrides explicitly, with a reason). The derivation is two explicit matrices —
readable, monotonic, unit-tested — not a hidden formula.

Per-app PARAMETRISATION (only) lives in ``wai-app.json`` →
``agent_app.signal_policy`` (shifts, confidence toggle, dedupe window, enabled
reason codes). The STRUCTURE (axes, matrices, catalog) stays here in code. A
per-agent business skill layers *qualitative* "when to raise X" rules on top via
the prompt — it never reaches into this module. See ``patterns/signal-policy.md``.
"""
from __future__ import annotations

import re
from typing import Any, Optional

# ── Vocabularies (ordered low → high; domain-agnostic) ───────────────────────
IMPACT: tuple[str, ...] = ("low", "medium", "high", "critical")
URGENCY: tuple[str, ...] = ("low", "medium", "high", "immediate")
CONFIDENCE: tuple[str, ...] = ("low", "medium", "high")
# Derived risk bands:
SEVERITY: tuple[str, ...] = ("info", "low", "medium", "high", "critical")   # alerts
PRIORITY: tuple[str, ...] = ("low", "medium", "high", "urgent")             # recommendations
# Decision DISPOSITION (coarse) is kept separate from the structured reason code.
DECISION_DISPOSITIONS: tuple[str, ...] = ("no_action", "deferred", "escalated")

# ── Structured reason codes for decisions ("why I did NOT act now") ──────────
# The catalog is DOMAIN-AGNOSTIC; a per-app manifest may restrict the enabled set
# (never invent domain codes here). Labels feed the UI/guide/welcome.
REASON_CODES: dict[str, dict[str, str]] = {
    "below_threshold":       {"es": "Por debajo del umbral",     "en": "Below threshold"},
    "insufficient_evidence": {"es": "Evidencia insuficiente",    "en": "Insufficient evidence"},
    "low_confidence":        {"es": "Confianza baja",            "en": "Low confidence"},
    "duplicate":             {"es": "Duplicado",                 "en": "Duplicate"},
    "out_of_scope":          {"es": "Fuera de alcance",          "en": "Out of scope"},
    "needs_human":           {"es": "Requiere decisión humana",  "en": "Needs human decision"},
    "already_handled":       {"es": "Ya gestionado",             "en": "Already handled"},
    "deferred_timing":       {"es": "Aplazado por el momento",   "en": "Deferred (timing)"},
    "awaiting_data":         {"es": "A la espera de datos",      "en": "Awaiting data"},
    "no_action_needed":      {"es": "No requiere acción",        "en": "No action needed"},
}

# ── Axis labels (for the manifest → SPA guide / welcome) ─────────────────────
AXIS_LABELS: dict[str, dict[str, str]] = {
    "impact":     {"es": "Impacto",    "en": "Impact"},
    "urgency":    {"es": "Urgencia",   "en": "Urgency"},
    "confidence": {"es": "Confianza",  "en": "Confidence"},
    "severity":   {"es": "Severidad",  "en": "Severity"},
    "priority":   {"es": "Prioridad",  "en": "Priority"},
    "risk":       {"es": "Riesgo",     "en": "Risk"},
}

# ── Derivation matrices (rows = impact, cols = urgency) ──────────────────────
# Explicit and MONOTONIC (a cell is never lower than the one above or to its
# left), so the policy is legible at a glance and easy to unit-test. Confidence
# is applied AFTER as a one-band downgrade (see ``_apply_confidence``).
#
# severity[impact][urgency]:
_SEVERITY_MATRIX: dict[str, dict[str, str]] = {
    "low":      {"low": "info",   "medium": "low",    "high": "low",      "immediate": "medium"},
    "medium":   {"low": "low",    "medium": "medium", "high": "medium",   "immediate": "high"},
    "high":     {"low": "medium", "medium": "high",   "high": "high",     "immediate": "critical"},
    "critical": {"low": "high",   "medium": "high",   "high": "critical", "immediate": "critical"},
}
# priority[impact][urgency]:
_PRIORITY_MATRIX: dict[str, dict[str, str]] = {
    "low":      {"low": "low",    "medium": "low",    "high": "medium", "immediate": "medium"},
    "medium":   {"low": "low",    "medium": "medium", "high": "high",   "immediate": "high"},
    "high":     {"low": "medium", "medium": "high",   "high": "high",   "immediate": "urgent"},
    "critical": {"low": "high",   "medium": "high",   "high": "urgent", "immediate": "urgent"},
}

# Legacy severity → (impact, urgency) for backfilling pre-7.41.0 rows.
_SEVERITY_TO_IMPACT_URGENCY: dict[str, tuple[str, str]] = {
    "info":     ("low", "low"),
    "low":      ("low", "medium"),
    "medium":   ("medium", "medium"),
    "high":     ("high", "high"),
    "critical": ("critical", "high"),
}
# Legacy decision disposition → a sensible default reason code (backfill).
_DISPOSITION_TO_REASON: dict[str, str] = {
    "no_action": "no_action_needed",
    "deferred":  "deferred_timing",
    "escalated": "needs_human",
}


def _clamp_index(seq: tuple[str, ...], idx: int) -> str:
    return seq[max(0, min(len(seq) - 1, idx))]


def higher_band(a: Any, b: Any, seq: tuple[str, ...]) -> Optional[str]:
    """Return the more intense of two bands on ``seq`` (used when a recurring
    alert is updated — keep the worst reading). Unknown values are ignored."""
    ia = seq.index(a) if a in seq else -1
    ib = seq.index(b) if b in seq else -1
    hi = max(ia, ib)
    return seq[hi] if hi >= 0 else (a or b or None)


def _coerce(value: Any, allowed: tuple[str, ...], default: str) -> str:
    """Return ``value`` lowercased if it is in ``allowed``, else ``default``.
    Never raises — a bad input degrades to the default, keeping the tool call
    alive (the agent gets a best-effort classification, not a 500)."""
    v = str(value or "").strip().lower()
    return v if v in allowed else default


def coerce_impact(value: Any, default: str = "medium") -> str:
    return _coerce(value, IMPACT, default)


def coerce_urgency(value: Any, default: str = "medium") -> str:
    return _coerce(value, URGENCY, default)


def coerce_confidence(value: Any, default: str = "medium") -> str:
    return _coerce(value, CONFIDENCE, default)


def coerce_disposition(value: Any, default: str = "no_action") -> str:
    return _coerce(value, DECISION_DISPOSITIONS, default)


def validate_reason_code(value: Any, params: Optional[dict] = None) -> Optional[str]:
    """Return the canonical reason code if valid AND enabled for this app, else
    ``None`` (the caller decides the fallback). ``params.reason_codes`` may
    restrict the enabled set; absent = all catalog codes are enabled."""
    v = str(value or "").strip().lower()
    if v not in REASON_CODES:
        return None
    enabled = _enabled_reason_codes(params)
    return v if v in enabled else None


def _apply_confidence(band_seq: tuple[str, ...], band: str, confidence: str,
                      params: Optional[dict]) -> str:
    """Downgrade the derived band by one step when confidence is ``low`` — an
    uncertain finding should not shout ``critical``. ``high`` keeps the band;
    ``medium`` keeps it. Toggle off with ``params.confidence_downgrade = false``."""
    if not _param(params, "confidence_downgrade", True):
        return band
    idx = band_seq.index(band)
    if confidence == "low":
        idx -= 1
    return _clamp_index(band_seq, idx)


def _shifted(band_seq: tuple[str, ...], band: str, shift: int) -> str:
    """Apply an integer per-app shift (−N..+N) to a band, clamped in range."""
    if not shift:
        return band
    return _clamp_index(band_seq, band_seq.index(band) + int(shift))


def derive_severity(impact: Any, urgency: Any, confidence: Any = "medium",
                    params: Optional[dict] = None) -> str:
    """The alert's risk band (``severity``) DERIVED from impact × urgency, then
    downgraded for low confidence and shifted by the per-app ``severity_shift``.
    Deterministic and monotonic — the single source of truth for alert intensity."""
    i, u, c = coerce_impact(impact), coerce_urgency(urgency), coerce_confidence(confidence)
    band = _SEVERITY_MATRIX[i][u]
    band = _apply_confidence(SEVERITY, band, c, params)
    return _shifted(SEVERITY, band, _param(params, "severity_shift", 0))


def derive_priority(impact: Any, urgency: Any, confidence: Any = "medium",
                    params: Optional[dict] = None) -> str:
    """The recommendation's ``priority`` band DERIVED from impact × urgency, then
    downgraded for low confidence and shifted by the per-app ``priority_shift``."""
    i, u, c = coerce_impact(impact), coerce_urgency(urgency), coerce_confidence(confidence)
    band = _PRIORITY_MATRIX[i][u]
    band = _apply_confidence(PRIORITY, band, c, params)
    return _shifted(PRIORITY, band, _param(params, "priority_shift", 0))


def severity_to_impact_urgency(severity: Any) -> tuple[str, str]:
    """Best-effort inverse used ONLY to backfill pre-7.41.0 alerts that carry a
    hand-set ``severity`` but no impact/urgency."""
    return _SEVERITY_TO_IMPACT_URGENCY.get(str(severity or "").strip().lower(), ("medium", "medium"))


def reason_code_from_disposition(disposition: Any) -> str:
    """Backfill a legacy decision's ``reason_code`` from its coarse disposition."""
    return _DISPOSITION_TO_REASON.get(str(disposition or "").strip().lower(), "no_action_needed")


# ── Semantic deduplication ───────────────────────────────────────────────────
def canonical_target(target_kind: Any = None, target_id: Any = None) -> Optional[str]:
    """Target in CANONICAL form (``target_kind:target_id``), normalized.

    It is the only accepted way to name the target inside a dedup key: the agent
    wrote it by hand and it drifted between passes (``product.product:21`` once,
    ``warehouse:1:product:21`` the next), so two passes over the SAME problem never
    converged and the dedup was useless by design (duplicates in production, 2026-07).
    """
    parts = [str(target_kind or "").strip(), str(target_id or "").strip()]
    joined = ":".join(p for p in parts if p)
    return _norm_key(joined) if joined else None


def _strip_target_fragments(concern: str, target_kind: Any, target_id: Any) -> str:
    """Strip from the concern the pieces that name the target (in any of its forms),
    so it is not duplicated when composing ``<concern>:<canonical target>``."""
    kind_n = _norm_key(str(target_kind or ""))
    id_n = _norm_key(str(target_id or ""))
    fuera = {t for t in ({kind_n} | set(kind_n.split("."))) if t} | ({id_n} if id_n else set())
    segs = [seg for seg in concern.split(":") if seg and seg not in fuera]
    return ":".join(segs)


def normalize_dedupe_key(explicit: Any = None, root_cause: Any = None,
                         target_kind: Any = None, target_id: Any = None,
                         target_only: bool = False) -> Optional[str]:
    """STABLE dedup key, derived from the TARGET — not from how the agent words it.

    A recurring problem must land on the SAME row (updating it) instead of stacking
    a new one on every pass. The precedence used to be
    ``explicit → root_cause → target``: the key the LLM improvised won and the
    target was ignored, so two wordings of the same problem produced two keys
    and two rows that the operator saw as duplicates.

    Now the target RULES:

    * ``target_only=True`` (recommendations): the key **is** the target. A
      recommendation is the pending ACTION on a target, and there can only be one
      open per target: a new proposal UPDATES the existing one. (If two actions on
      the same target are really needed, the first is closed —the
      ``done``/``dismissed`` cycle allows it— and the next pass creates the new one.)
    * Alerts: ``<concern>:<canonical target>``. Two DIFFERENT risks on the same
      target can coexist, but the same risk always converges because the target
      goes in canonical form, however the agent writes it.
    * No identifiable target: the normalized concern is used (or ``None`` if there is nothing).

    Idempotent: calling it twice with equivalent inputs gives the same key.
    """
    target = canonical_target(target_kind, target_id)
    concern = ""
    for candidate in (explicit, root_cause):
        s = _norm_key(str(candidate or "").strip())
        if s:
            concern = s
            break

    if target:
        if target_only:
            return target
        concern = _strip_target_fragments(concern, target_kind, target_id)
        return f"{concern}:{target}" if concern else target
    return concern or None


def _norm_key(s: str) -> str:
    """Canonicalise cosmetic separators so the SAME problem written with a
    hyphen, an underscore or spaces converges on ONE key. The agent slugifies
    the concern free-hand and drifted between runs (``stock_bajo`` one run,
    ``stock-bajo`` the next), which used to defeat dedup and pile up duplicate
    signals for the same target. ``:`` is preserved — it STRUCTURES the key
    (``concern:target_kind:target_id``)."""
    s = str(s or "").strip().lower()
    s = re.sub(r"\s*:\s*", ":", s)      # tidy the structural separator first
    s = re.sub(r"[\s_-]+", "-", s)      # any run of space/underscore/hyphen → one '-'
    return s.strip("-")[:200]


def dedupe_window_days(params: Optional[dict] = None) -> Optional[int]:
    """How far back a matching OPEN/active alert may be to be updated in place
    instead of inserted. ``None`` = no time bound (match any non-terminal alert
    with the key). Per-app: ``signal_policy.dedupe.window_days``."""
    dd = (params or {}).get("dedupe") if isinstance(params, dict) else None
    if isinstance(dd, dict) and dd.get("window_days") is not None:
        try:
            return int(dd["window_days"])
        except (TypeError, ValueError):
            return None
    return None


# ── Per-app parameters (parametrisation ONLY — never the policy itself) ──────
def _param(params: Optional[dict], key: str, default: Any) -> Any:
    if isinstance(params, dict) and key in params and params[key] is not None:
        return params[key]
    return default


def _enabled_reason_codes(params: Optional[dict]) -> set[str]:
    rc = _param(params, "reason_codes", None)
    if isinstance(rc, list) and rc:
        return {str(x).strip().lower() for x in rc} & set(REASON_CODES)
    return set(REASON_CODES)


def policy_params() -> dict:
    """Load the per-app ``signal_policy`` block from ``wai-app.json``
    (``agent_app.signal_policy``). Pure parametrisation — shifts, toggles, dedupe
    window, enabled reason codes. Absent ⇒ ``{}`` (all defaults). Never raises."""
    try:
        from app.config import AGENT_APP
        sp = (AGENT_APP or {}).get("signal_policy")
        return dict(sp) if isinstance(sp, dict) else {}
    except Exception:
        return {}


def public_policy(lang: str = "es") -> dict:
    """A JSON-serialisable description of the EFFECTIVE policy for the manifest
    (so the SPA guide/welcome can show the real axes, bands and reason codes
    without hardcoding them). Structure from code + the app's parameters."""
    p = policy_params()

    def lbl(d: dict[str, dict[str, str]]) -> dict[str, str]:
        return {k: (v.get(lang) or v.get("es") or k) for k, v in d.items()}

    return {
        "axes": ["impact", "urgency", "confidence", "risk", "approval"],
        "axis_labels": lbl(AXIS_LABELS),
        "impact": list(IMPACT),
        "urgency": list(URGENCY),
        "confidence": list(CONFIDENCE),
        "severity": list(SEVERITY),
        "priority": list(PRIORITY),
        "reason_codes": {k: (REASON_CODES[k].get(lang) or REASON_CODES[k]["es"])
                         for k in sorted(_enabled_reason_codes(p))},
        "derived": {"severity": "impact × urgency (↓ if confidence low)",
                    "priority": "impact × urgency (↓ if confidence low)"},
        "params": {
            "confidence_downgrade": bool(_param(p, "confidence_downgrade", True)),
            "severity_shift": int(_param(p, "severity_shift", 0)),
            "priority_shift": int(_param(p, "priority_shift", 0)),
            "dedupe_window_days": dedupe_window_days(p),
        },
    }
