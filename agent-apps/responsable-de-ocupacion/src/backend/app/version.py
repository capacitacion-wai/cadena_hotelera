"""Single source of truth for the skill version this agent-app was generated
from.

``SKILL_VERSION`` MUST equal the ``version`` field in the skill's ``skill.json``
— the verifier (``verify_generated_app.sh`` §version) enforces byte-equality, so
a generated app always carries the EXACT skill version it was built with.

Why it exists: it lets the platform / code agent tell which skill version an
agent was created or last updated with, and therefore EVOLVE that agent to the
latest skill version by replaying the migration notes in the skill's
``CHANGELOG.md`` for every version in ``(stamped_version, current_version]``.

It is surfaced to the operator (Operations → Estado) via the manifest endpoint
and stamped into ``wai-app.json`` as ``skill_version`` at generation time. See
SKILL.md → "Versioning & evolving an agent".
"""

SKILL_VERSION = "15.14.0"
"""Bump on every SIGNIFICANT change, in lockstep with skill.json and a new
CHANGELOG.md entry. Semver: MAJOR = migration-required, MINOR = additive
feature, PATCH = fix."""
