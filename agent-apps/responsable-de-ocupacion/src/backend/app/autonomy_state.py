"""The employee's AUTONOMY LEVEL — one dial that sets sensible defaults.

The boss decides HOW BOLDLY the employee may act, on a single escalating
scale of THREE levels (propose → act-with-approval → act-autonomous). That one
choice biases three things at once instead of forcing the operator to
reason about each separately:

(v7.77.0 — the old ``observe`` and ``propose`` levels were behaviourally
IDENTICAL, so they are merged into one, ``propose`` = "Observa y propone". A
stored ``observe`` coerces to ``propose`` automatically — see ``_coerce``.)

  * whether autonomous triggers (disparadores) start active,
  * whether new automations are born requiring the boss's aprobación,
  * the posture line injected into the employee's identity preamble, so it
    KNOWS — in business terms — how far it may go before asking.

This is a DEFAULT-setter, not a hard gate: it never overrides an explicit
operator choice (the triggers master switch, a rule's own
``requires_approval``) and it does not replace the platform's tool
permissions (allow/deny/ask) — it complements them. Tool-level enforcement
still lives in ``agent_tool_permissions`` and the ``request_approval`` flow.

Source of truth, in order (mirrors ``triggers_state``):
  1. The operator's stored choice (``app_settings.autonomy_level``), once set.
  2. Otherwise the manifest default (``agent_app.autonomy.level`` in
     ``wai-app.json``), which DEFAULTS TO ``propose`` — a freshly created
     agent proposes and waits for the boss's OK before acting outward.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from app.app_database import app_session
from app.app_models.runtime import AppSetting
from app.config import AGENT_APP

logger = logging.getLogger(__name__)

SETTING_KEY = "autonomy_level"
DEFAULT_LEVEL = "propose"

# Ordered, escalating. ``defaults`` is what each level biases; ``posture`` is
# the first-person business line injected into the identity preamble.
LEVELS: list[dict[str, Any]] = [
    {
        "key": "propose",
        "label": {"es": "Observa y propone", "en": "Observe & propose"},
        "description": {
            "es": "Vigila, te informa y organiza el trabajo interno por su cuenta (avisos, alertas, recomendaciones y encargos), y te PROPONE —esperando tu aprobación— todo lo que toque sistemas externos (CRM, etc.).",
            "en": "Watches, informs you and organises internal work on its own (notices, alerts, recommendations and tasks), and PROPOSES —awaiting your sign-off— anything that touches external systems (CRM, etc.).",
        },
        "posture": {
            "es": (
                "Trabajo en modo OBSERVA Y PROPONE: vigilo los datos, te informo "
                "(avisos, alertas, recomendaciones) y organizo el trabajo interno "
                "(encargos, recordatorios) por mi cuenta, pero NO toco sistemas "
                "externos por mi cuenta: SIEMPRE espero tu aprobación antes de crear, "
                "modificar o enviar algo en un conector o sistema externo."
            ),
            "en": (
                "I work in OBSERVE-AND-PROPOSE mode: I watch the data, keep you "
                "informed (notices, alerts, recommendations) and organise internal "
                "work (tasks, reminders) on my own, but I do NOT touch external "
                "systems on my own: I ALWAYS wait for your sign-off before creating, "
                "modifying or sending anything in a connector or external system."
            ),
        },
        "defaults": {"triggers_start_active": False, "automation_requires_approval": True},
    },
    {
        "key": "act_with_approval",
        "label": {"es": "Actúa con tu aprobación", "en": "Act with approval"},
        "description": {
            "es": "SÍ actúo por mi cuenta en lo rutinario y de bajo riesgo; solo te pido aprobación antes de lo de riesgo o que salga hacia fuera. El trabajo autónomo arranca ACTIVO. (Diferencia con «Propón»: allí no actúo por mi cuenta en nada.)",
            "en": "I DO act on my own for routine, low-risk work; I only ask approval before risky or outward actions. Autonomous work starts ACTIVE. (Difference vs «Propose»: there I act on nothing on my own.)",
        },
        "posture": {
            "es": (
                "Trabajo en modo ACCIÓN CON APROBACIÓN: actúo por mi cuenta en lo "
                "rutinario y de bajo riesgo, pero te pido tu OK antes de cualquier "
                "acción de riesgo o que salga hacia fuera (enviar, publicar, modificar "
                "sistemas externos)."
            ),
            "en": (
                "I work in ACT-WITH-APPROVAL mode: I act on my own for routine, "
                "low-risk work, but I ask for your OK before any risky or outward "
                "action (send, publish, modify external systems)."
            ),
        },
        "defaults": {"triggers_start_active": True, "automation_requires_approval": True},
    },
    {
        "key": "act_autonomous",
        "label": {"es": "Actúa de forma autónoma", "en": "Act autonomously"},
        "description": {
            "es": "Actúo solo en lo de bajo riesgo; solo te consulto lo realmente arriesgado.",
            "en": "I act on my own for low-risk work; I only consult you on genuinely risky calls.",
        },
        "posture": {
            "es": (
                "Trabajo en modo ACCIÓN AUTÓNOMA: actúo por mi cuenta en lo de bajo "
                "riesgo sin molestarte; solo te consulto lo realmente arriesgado o "
                "irreversible."
            ),
            "en": (
                "I work in AUTONOMOUS mode: I act on my own for low-risk work without "
                "bothering you; I only consult you on genuinely risky or irreversible "
                "actions."
            ),
        },
        "defaults": {"triggers_start_active": True, "automation_requires_approval": False},
    },
]

_BY_KEY = {lvl["key"]: lvl for lvl in LEVELS}


def _coerce(level: Any) -> str:
    key = str(level or "").strip().lower()
    return key if key in _BY_KEY else DEFAULT_LEVEL


def manifest_autonomy_level() -> str:
    """The deploy-time default from ``wai-app.json`` (``agent_app.autonomy.
    level`` or the flat ``autonomy_level``). Defaults to ``propose``."""
    a = AGENT_APP.get("autonomy")
    if isinstance(a, dict) and a.get("level"):
        return _coerce(a.get("level"))
    return _coerce(AGENT_APP.get("autonomy_level", DEFAULT_LEVEL))


def autonomy_defaults(level: str) -> dict[str, Any]:
    """The behavioural defaults a level biases. Never raises."""
    return dict(_BY_KEY.get(_coerce(level), _BY_KEY[DEFAULT_LEVEL])["defaults"])


async def autonomy_level() -> str:
    """The effective level right now: operator override → manifest default.
    Never raises (autonomy must never break a run)."""
    try:
        async with app_session() as s:
            raw = (await s.execute(
                select(AppSetting.value).where(AppSetting.key == SETTING_KEY)
            )).scalar_one_or_none()
    except Exception:  # pragma: no cover — never block on a read error
        logger.debug("autonomy_level read failed; using manifest default", exc_info=True)
        return manifest_autonomy_level()
    if raw is None:
        return manifest_autonomy_level()
    return _coerce(raw)
