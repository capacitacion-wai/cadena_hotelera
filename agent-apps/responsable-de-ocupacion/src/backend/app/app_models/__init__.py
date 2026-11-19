"""App-local SQLite models.

Importing this package has the side-effect of loading all model
modules so SQLAlchemy registers them on `AppBase.metadata`. Add a
new line below for every business-specific module you create under
`app_models/`.

Three layers ship by default:

* ``runtime``   — operational tables the platform itself owns
                  (events, agent_runs, tool_call_log, …). NEVER
                  declared as ``domain_entities``.
* ``business``  — canonical OUTPUT shapes the agent produces
                  (alerts, recommendations, communications, scoring,
                  decisions). Declared as ``domain_entities`` with
                  ``source: "local"`` so an operator can browse them
                  alongside the project-side reads.
* ``channels``  — external-channel state (inbound buzon configuration
                  and message log). Driven by the watcher and the
                  executor; managed through the SPA's "Canales" section.

Add a fourth module (``app_models/<domain>.py``) and import it here
when the domain needs extra business tables (e.g. ``welcome_queue``,
``customer_segments``).
"""

from app.app_models import runtime as _runtime  # noqa: F401
from app.app_models import business as _business  # noqa: F401
from app.app_models import channels as _channels  # noqa: F401
from app.app_models import memory as _memory  # noqa: F401
from app.app_models import mcp_connectors as _mcp_connectors  # noqa: F401
from app.app_models import automations as _automations  # noqa: F401
from app.app_models import users as _users  # noqa: F401
from app.app_models import notify_rules as _notify_rules  # noqa: F401
from app.app_models import signal_suppressions as _signal_suppressions  # noqa: F401
from app.app_models import knowledge_library as _knowledge_library  # noqa: F401
# v7.170.0 — dismissals of the status notices (the bell in the top bar).
from app.app_models import health_dismissals as _health_dismissals  # noqa: F401
# v7.140.0 — the employee's OWN memory (environment axis), the traceability of
# the rules born from it, the non-action log and the automation-day rollup.
from app.app_models import agent_memory as _agent_memory  # noqa: F401
from app.app_models import learning_effects as _learning_effects  # noqa: F401
from app.app_models import signal_silence_log as _signal_silence_log  # noqa: F401
from app.app_models import automation_daily_stats as _automation_daily_stats  # noqa: F401
