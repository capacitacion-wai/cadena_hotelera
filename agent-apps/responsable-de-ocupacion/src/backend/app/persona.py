"""The AI-employee IDENTITY, rendered as a prompt preamble.

A generated app is a DIGITAL EMPLOYEE (a persona with a name, a role/area, a
way of speaking) — not a generic coding assistant. Without an explicit
identity the model falls back to the base OpenCode "coding agent" system
prompt and answers things like "I can analyse/edit code, refactor,
debug" — which is exactly wrong for a business employee.

This module is the single place that resolves the persona (manifest +
operator override) and turns it into the identity preamble injected at the
top of EVERY surface (chat prime, channel replies, autonomous runs) so the
employee is the SAME person everywhere and speaks in business terms.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _loc(value: Any, lang: str) -> str:
    """Resolve a locale-aware field (str or {es,en}) to a plain string."""
    if isinstance(value, dict):
        return str(
            value.get(lang) or value.get("es") or value.get("en")
            or next(iter(value.values()), "")
        ).strip()
    return str(value or "").strip() if value is not None else ""


async def resolve_persona() -> Optional[dict]:
    """Resolved persona = manifest default with the operator's DB override
    applied. Never raises (identity must never break a run)."""
    try:
        from app.config import AGENT_APP
        from app.app_database import app_session
        from app.routes.manifest_routes import _resolve_persona
        async with app_session() as s:
            return await _resolve_persona(AGENT_APP, s)
    except Exception:  # pragma: no cover — fall back to manifest-only
        try:
            from app.config import AGENT_APP
            from app.routes.manifest_routes import _persona_block
            return _persona_block(AGENT_APP)
        except Exception:
            logger.debug("persona resolution failed", exc_info=True)
            return None


def identity_preamble(persona: Optional[dict], lang: str = "es", posture: str = "") -> str:
    """Render the employee-identity preamble. Internal (never streamed to the
    user); establishes WHO the agent is, HOW it speaks and HOW BOLDLY it may act
    (``posture`` from the autonomy level). Returns a generic employee identity
    (never the code-agent one) even if the persona is thin."""
    persona = persona or {}
    name = (persona.get("name") or "").strip()
    role = _loc(persona.get("role"), lang)
    pers = persona.get("personality") if isinstance(persona.get("personality"), dict) else {}
    tone = _loc(pers.get("tone"), lang)
    bio = _loc(pers.get("bio"), lang)
    traits = pers.get("traits") if isinstance(pers.get("traits"), list) else []
    traits_txt = ", ".join(_loc(t, lang) for t in traits if _loc(t, lang))[:200]

    who = f"You are {name}" if name else "You are a digital employee"
    if role:
        who += f", {role}"
    who += "."

    lines = [
        # The label «IDENTIDAD DEL EMPLEADO» stays byte-identical: chat_prompt._LEAK_MARKERS
        # cuts the bubble at it if the model ever echoes it.
        "IDENTIDAD DEL EMPLEADO (employee identity — internal instruction; embody it, "
        "do not quote it).",
        who,
    ]
    if bio:
        lines.append(bio)
    if tone or traits_txt:
        lines.append(
            "Your way of being: "
            + "; ".join(x for x in (tone, traits_txt) if x) + "."
        )
    lines.append(
        "You ALWAYS speak in the first person as this employee and in BUSINESS "
        "language, never technical. You are NOT a programming assistant nor a "
        "\"code agent\": NEVER describe yourself that way or offer to \"analyse/edit/"
        "refactor code\", \"debug\" or technical capabilities. When they "
        "ask who you are, what you do or what you specialise in, answer FROM "
        "YOUR ROLE and your area, how you help your manager — do not list programming "
        "capabilities. If a request falls outside your role, help with judgement "
        "but always from your identity. Reply in the language the person writes in."
    )
    posture = (posture or "").strip()
    if posture:
        lines.append("YOUR AUTONOMY LEVEL: " + posture)
    # The rule that turns "with your approval" into something that HAPPENS.
    #
    # Without it the employee reads its own capabilities card —"write to X: yes, but with
    # your approval"— and concludes on its own that it must not try: it refuses, calls no
    # tool, and its manager is left with nothing to approve and the work undone. Measured
    # in the lab: given the SAME order, 1 in 3 times it parked and the other two it answered
    # "I am not executing the write: it is blocked without prior approval" with zero calls
    # (AGO-136). The rule already existed, but ONLY in the prompt of delegated tasks, so
    # through chat and channels nobody saw it — and an employee that behaves differently
    # depending on where you talk to it is not governable. It goes here, which is the only
    # thing ALL surfaces read.
    lines.append(
        "WHEN SOMETHING NEEDS SIGN-OFF, INVOKE IT ANYWAY. If an action of yours requires "
        "approval, CALL the tool normally: the platform holds it "
        "before it goes out, creates the approval for your manager and tells you how to "
        "continue. NEVER decide yourself that \"I can't, it needs approval\" without having "
        "tried: refusing in advance leaves your manager with nothing to approve and the "
        "work undone. Whether it is a test, or the data does not exist, changes nothing: "
        "try anyway and let the system answer."
    )
    return "\n".join(lines)


async def _autonomy_posture(lang: str) -> str:
    """The first-person autonomy posture line for the current level. Never
    raises — autonomy is a posture hint, it must not break a run."""
    try:
        from app.autonomy_state import autonomy_level, LEVELS
        level = await autonomy_level()
        spec = next((l for l in LEVELS if l["key"] == level), None)
        if spec:
            return _loc(spec.get("posture"), lang)
    except Exception:  # pragma: no cover
        logger.debug("autonomy posture resolution failed", exc_info=True)
    return ""


async def employee_identity_preamble(lang: str = "es") -> str:
    """Convenience: resolve the persona + autonomy posture and render the
    identity preamble used at the top of every surface.

    The COLLEAGUES are hooked in here too —who you have and which role you are missing—
    and not in each surface separately. The reason is an expensive lesson: the rule
    "attempt the action even if it needs approval" lived only in the prompt of delegated
    tasks, and through chat the employee behaved differently (AGO-136). This preamble is
    the ONLY thing all surfaces read: chat, channels and autonomous work. Whatever it must
    always know goes here."""
    persona = await resolve_persona()
    posture = await _autonomy_posture(lang)
    bloques = [identity_preamble(persona, lang, posture)]
    try:
        from app.colleague_tools import colleagues_note, missing_colleagues_note
        for nota in (await colleagues_note(), await missing_colleagues_note()):
            if nota:
                bloques.append(nota)
    except Exception:  # pragma: no cover — without colleagues work carries on
        logger.debug("no se pudo componer el bloque de compañeros", exc_info=True)
    # ITS APPLICATION (12.51.0). It goes here for the same reason as the colleagues: it is
    # the only thing the three surfaces read, and an employee that links its application
    # through chat but not through a channel behaves differently depending on where it is
    # talked to. Empty if it has none associated.
    try:
        from app.mi_aplicacion import para_el_prompt
        nota = await para_el_prompt()
        if nota:
            bloques.append(nota)
    except Exception:  # pragma: no cover — without knowing its application work carries on
        logger.debug("no se pudo componer el bloque de la aplicación", exc_info=True)
    from app.agent_locale import memory_language_note
    bloques.append(memory_language_note(lang))
    return "\n\n".join(bloques)
