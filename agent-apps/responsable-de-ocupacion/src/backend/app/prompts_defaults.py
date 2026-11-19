"""Built-in prompt templates for SYSTEM event types every agent-app
gets for free (calendar / tasks / approvals).

These are kept SEPARATE from the per-app generated ``app/prompts.py``
so deploying the skill never clobbers an app's hand-tuned templates.
``executor._prompt_for`` consults ``PROMPTS`` first and falls back here.

Each template is rendered with ``str.format(payload=<json str>,
event_type=<str>, **event_row)`` exactly like ``app.prompts``.
"""
from __future__ import annotations

# Operator delegated a task to the AI employee from the SPA ("Encargos").
DELEGATED_TASK = (
    "TASK FROM AN OPERATOR. A person on the team has delegated this task to you "
    "from the panel. Treat it as real work you must complete now, not as a "
    "conversation.\n\n"
    "Task data (JSON):\n{payload}\n\n"
    "How to proceed:\n"
    "1) Understand the goal and check it is feasible with your data, tools and "
    "permissions.\n"
    "2) Do it from start to finish. If you produce a deliverable (report, "
    "file), generate it physically in `/workspace/exports/`; working files (scripts, "
    "CSVs, extracts) go in ONE folder for this task, `/workspace/work/<YYYYMMDD-HHMM>-<topic>/`, "
    "never loose in `/workspace/`.\n"
    "3) If you need to WRITE to a connector, invoke it normally: if that write "
    "needs sign-off, the platform parks it and tells you how to record the "
    "approval. Never decide yourself that \"approval is needed\" without having "
    "tried. For what is NOT a tool (a decision outside your authority), use "
    "`request_approval`.\n"
    "4) If the task is for later or recurring, schedule it with "
    "`schedule_task`.\n"
    "5) When you finish, leave a clear summary of what you did and, if appropriate, "
    "record alerts/recommendations/communications with your tools."
)

# A ScheduledItem assigned to the agent reached its due time.
SCHEDULED_TASK_DUE = (
    "SCHEDULED TASK DUE. A task that was in your calendar has reached its time. "
    "Execute it now.\n\n"
    "Task data (JSON):\n{payload}\n\n"
    "How to proceed:\n"
    "1) Do the described work (use your data and tools).\n"
    "2) If you generate a deliverable, create it in `/workspace/exports/` (working "
    "files in ONE `/workspace/work/<YYYYMMDD-HHMM>-<topic>/` folder) and "
    "deliver it through the corresponding channel if applicable.\n"
    "3) ESSENTIAL: when you finish, close the task with "
    "`complete_scheduled_item(item_id=<the one in the JSON>, result='<what you did>')`. "
    "If the task was recurring, schedule the next occurrence with "
    "`schedule_task` before closing it.\n"
    "4) If you could not complete it, close it anyway with a `result` that "
    "explains the blocker and, if appropriate, record an alert."
)

# Operator approved a parked action. IMPORTANT: some approvals were ALREADY
# EXECUTED by the platform (a connector-write parked by the proxy is replayed
# deterministically on approve → payload.executed=true). Repeating those would
# re-park and LOOP, so the template BRANCHES on ``action_payload.executed``.
APPROVAL_GRANTED = (
    "APPROVAL GRANTED. The operator has APPROVED an action you left pending their "
    "approval.\n\n"
    "Approval data (JSON, includes action_type and action_payload):\n"
    "{payload}\n\n"
    "VERY IMPORTANT — look at `action_payload.executed`:\n"
    "• If `executed` is true: the PLATFORM HAS ALREADY EXECUTED this action for you (its "
    "result is in `execution_result`). Do NOT repeat it or call any write tool to redo "
    "it — if you do, approval would be requested again IN A LOOP. It is already marked "
    "as executed. Just CARRY ON with whatever was left of your task using that result; "
    "if nothing is left, confirm it briefly and finish.\n"
    "• If `executed` does NOT appear or is false: **YOU execute it, now**. The platform "
    "does NOT read or interpret your `action_payload` — it is the note you left "
    "yourself, not an order for anyone else: even if it contains a file path, a count or "
    "a 'scope', nothing is executed there. Carry out the action with the right tool "
    "(e.g. a communication with `status='ready'`, or the write on the corresponding "
    "connector). Do not ask again for approval of what is already approved, record the "
    "result, and CLOSE the loop with "
    "`mark_approval_executed(approval_id=<the one in the JSON>, result='<what you did>')`. "
    "If the action fails (e.g. Odoo does not connect), do NOT mark it executed: "
    "record the blocker with `log_decision`."
)


PROMPTS_DEFAULTS: dict[str, str] = {
    "DELEGATED_TASK": DELEGATED_TASK,
    "SCHEDULED_TASK_DUE": SCHEDULED_TASK_DUE,
    "APPROVAL_GRANTED": APPROVAL_GRANTED,
}
