import json
import logging
from datetime import datetime, timezone
from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.app_database import app_session, app_write
from app.app_models.runtime import AgentRun, Event
from app.autonomous.event_machine import (
    claim_next_event, defer_event, mark_in_progress, mark_completed,
    schedule_retry, mark_failed,
)
from app.agent_proxy import RecoverableAgentError, get_agent_proxy
from app.agent_locale import language_block, resolve_agent_language
from app.config import BACKOFF_SECONDS, DAILY_BUDGET_USD

# Event type emitted by the channels poller for an inbound message.
# Branched out of ``run_agent_session`` so the adapter's send_reply is
# called after the agent answers (see app/channels/executor.py).
EXTERNAL_MESSAGE_EVENT_TYPE = "external_message"

# Operator-triggered (Operations "Optimizar memoria") agent-driven
# compaction. The deterministic pass already ran in the route; this run
# is the SEMANTIC half (merge/forget/relink with judgement).
MEMORY_MAINTENANCE_EVENT_TYPE = "memory_maintenance"
# v7.140.0 — the SELF-memory counterpart: distil repeated raw observations about
# the environment into a baseline/lesson. A separate type (not a flag on the
# other one) because the two touch different tables with different tools and
# neither should be able to edit the other's memory by accident.
SELF_MEMORY_MAINTENANCE_EVENT_TYPE = "self_memory_maintenance"

# 7.153.0 — CONSUMPTION CATEGORIES. A business label for the ledger: spending while working
# is not the same as spending on keeping itself tidy, and until then it was all summed together.
# Splitting it is what lets us see whether consolidation pays off (or whether it is burning
# tokens for nothing, as happened with 14 rounds that did not merge a single entry).
# The VALUES are ledger enums the platform reads: they stay as they are.
ACTIVITY_WORK = "trabajo"
ACTIVITY_TIDY_MEMORY = "ordenar_lo_aprendido"


def _activity_for(event_type: str) -> str:
    """What the employee was doing, in business terms."""
    return (ACTIVITY_TIDY_MEMORY
            if (event_type or "") == SELF_MEMORY_MAINTENANCE_EVENT_TYPE
            else ACTIVITY_WORK)
AUTOMATION_FIRED_EVENT_TYPE = "automation_fired"
# v7.63.0 — READ-ONLY verification run for check-before-approve: the employee
# verifies an ``agent`` (natural-language) condition BEFORE the approval is
# parked, and returns a structured verdict (no side effects, no write grant).
AUTOMATION_CHECK_EVENT_TYPE = "automation_check"


async def _automation_prompt(event: dict | None) -> str:
    """Build the reaction prompt for a fired automation from its config."""
    try:
        payload = json.loads((event or {}).get("payload_json") or "{}")
    except Exception:
        payload = {}
    aid = payload.get("automation_id")
    ctx = payload.get("context") or {}
    # A manual run (operator Play / boss "run it now") is an explicit command:
    # honour it even for a ``manual`` automation that is intentionally disabled
    # (manual signals never auto-fire, so they're typically enabled == 0).
    is_manual = bool(ctx.get("manual"))
    try:
        from app import automations_service as asvc
        a = await asvc.get_automation(int(aid)) if aid else None
        # Act if the automation still exists AND is enabled — OR it was fired
        # manually. If it was deleted/paused between firing and execution (and
        # NOT manual), instruct a NO-OP instead of an open-ended prompt that
        # could take an undefined action.
        if a and (a.get("enabled") or is_manual):
            return asvc.build_reaction_prompt(a, ctx)
        if a is not None or aid:
            return (
                "The automation that originated this run is no longer active "
                "(it was deleted or disabled). Do NOT take any action or send "
                "anything; state briefly that there is nothing to act on."
            )
    except Exception:  # pragma: no cover — never break the run
        pass
    return f"Automation fired ({aid}). Context: {payload.get('context')}"


async def _condition_check_prompt(event: dict | None) -> str:
    """Build the READ-ONLY verification prompt for an ``automation_check`` event
    (check-before-approve). Always runs the check (even for a disabled/manual
    automation being 'Probado'), because it has no side effects."""
    try:
        payload = json.loads((event or {}).get("payload_json") or "{}")
    except Exception:
        payload = {}
    aid = payload.get("automation_id")
    ctx = payload.get("context") or {}
    try:
        from app import automations_service as asvc
        a = await asvc.get_automation(int(aid)) if aid else None
        if a:
            return asvc.build_condition_check_prompt(a, ctx)
    except Exception:  # pragma: no cover — never break the run
        pass
    return (
        f"Automation check ({aid}). Verify the condition and answer "
        f"in the STRUCTURED_RESPONSE with an `automation_check` object "
        '{"condition_holds": true|false, "evidence": "...", "confidence": "..."}. '
        "Do not act."
    )


async def _settle_failed_check(event: dict, run_id: int | None) -> None:
    """v7.68.0 — a pre-check run that FAILED PERMANENTLY must not silently drop the
    firing (that would defeat ``requires_approval``: no approval, no decision). Park
    the approval FAIL-SAFE as UNCONFIRMED, so the operator still decides and — on
    approval — the reaction RE-VERIFIES the condition (build_reaction_prompt honours
    ``check_unconfirmed``). Skipped for a ``test_only`` check (a failed 'Probar' just
    fails). Best-effort; only for ``automation_check`` events."""
    if event.get("type") != AUTOMATION_CHECK_EVENT_TYPE:
        return
    try:
        payload = json.loads(event.get("payload_json") or "{}")
        ctx = payload.get("context") or {}
        if ctx.get("test_only"):
            return
        from app import automations_service as asvc
        # structured=None → verdict holds=None → parks an UNCONFIRMED approval.
        await asvc.settle_condition_check(
            int(payload.get("automation_id")), str(payload.get("bucket") or ""),
            ctx, None, run_id, test_only=False,
        )
    except Exception:
        logger.exception("fail-safe park for failed check (event %s) errored", event.get("id"))


async def _settle_failed_approval(event: dict) -> None:
    """v7.85.0 — when the run that was to CARRY OUT an already-APPROVED action fails
    permanently, flip the linked Approval to a terminal ``failed`` state. The Event
    retry already exhausted its budget, so without this the approval sits at
    ``approved`` forever — indistinguishable from "approved, still running". The
    ``approval_id`` travels in the APPROVAL_GRANTED / automation_fired payload; the
    update is guarded on ``status == 'approved'`` so it never clobbers an approval a
    later ``mark_approval_executed`` already settled. Best-effort."""
    try:
        payload = json.loads(event.get("payload_json") or "{}")
    except Exception:
        return
    approval_id = payload.get("approval_id")
    if not approval_id:
        return
    try:
        from sqlalchemy import update as _update
        from app.app_models.business import Approval
        async with app_write() as session:
            await session.execute(
                _update(Approval)
                .where(Approval.id == int(approval_id), Approval.status == "approved")
                # The PERSON reads this on the approval card (their UI text, not an
                # instruction to the agent): it stays in the product's language.
                .values(status="failed",
                        execution_result="La ejecución falló tras aprobar; revísala y reintenta.")  # wai:es-ok
            )
            await session.commit()
    except Exception:
        logger.debug("could not settle failed approval %s", approval_id, exc_info=True)


async def _memory_maintenance_prompt(event: dict, run_id: int) -> str:
    """Build the prompt for an agent-driven ``memory_maintenance`` run,
    injecting the target user's current memory + a consolidation brief."""
    from app.agent_sessions import build_memory_block_detail
    try:
        payload = json.loads(event.get("payload_json") or "{}")
    except Exception:
        payload = {}
    target = (payload.get("user_email") or "").strip().lower()
    mem = ""
    if target:
        detail = await build_memory_block_detail(
            target, enqueue_autocompact=False,
        )
        mem = detail.get("text") or ""
    return (
        "MEMORY MAINTENANCE (this is not a conversation with a human; do not "
        "send any message to anyone). Optimise the persistent memory of "
        f"user `{target}`. A DETERMINISTIC compaction already ran "
        "(dedupe of identical facts, dedupe of files, pruning of episodes); "
        "your task now is the SEMANTIC part, with judgement:\n"
        f"1) Read the memory with `list_memory` (run_id={run_id}, "
        f"user_email='{target}').\n"
        "2) Merge facts that say the same thing in different words using "
        "`remember_fact(..., supersedes=<id>)`.\n"
        "3) Delete what is obsolete or clearly wrong with `forget_fact`.\n"
        "4) Make each fact/preference SELF-CONTAINED: if it refers to a "
        "file, link it with `index_file` and `artifacts=[{...}]`.\n"
        "5) Fix badly worded preferences (e.g. 'exact same format' "
        "of a binary → 'clean equivalent based on <file path>').\n"
        "Write every fact you rewrite or merge in the OUTPUT LANGUAGE below, translating one "
        "written in another language (memory search matches words: mixed languages are not found). "
        "Do not invent new facts: only consolidate, correct and link what already "
        f"exists. Always use run_id={run_id} and user_email='{target}' in every "
        "memory tool."
        + mem
    )


def _is_heavy_failure(err: Exception) -> bool:
    """True when a RecoverableAgentError is a *heavy* failure — a gateway
    timeout or a sidecar step-ceiling / no-progress abort — rather than a
    cheap transient (connection reset, 5xx). Heavy failures will recur if we
    just re-run the identical prompt, so we cap them at a single retry; cheap
    transients keep the normal backoff budget."""
    s = str(err).lower()
    needles = (
        "gateway timeout",
        "did not complete",
        "step ceiling",
        "no-progress",
        "stream timeout",
    )
    return any(n in s for n in needles)


def clock_block() -> str:
    """Tell the agent the CURRENT business wall-clock time + timezone so
    it schedules relative times ("tomorrow at 9", "in 2 hours") against
    the operator's clock, not the pod's UTC. Cheap and always safe."""
    try:
        from app.timeutil import local_clock_line
        return (
            "\n\nCURRENT BUSINESS TIME: "
            f"{local_clock_line()}. Interpret any relative date/time "
            "(today, tomorrow, in 2 hours, on Monday — in whatever language it is "
            "written) in THIS time zone. When you "
            "schedule something with `schedule_task`/`assign_task_to_user`, pass the "
            "wall-clock time in this zone."
        )
    except Exception:  # pragma: no cover — never block a run on this
        # …but never swallow it silently either: a broken clock line means the
        # agent misreads "tomorrow at 9" for EVERY run (a v7.89.0 NameError
        # hid here for a whole release).
        logger.warning("clock_block failed — autonomous runs lose the business-time header", exc_info=True)
        return ""


def plan_verify_block() -> str:
    """Cross-cutting Plan -> Verify -> Act + bounded-loop discipline,
    appended to EVERY autonomous run prompt. This mirrors the global
    operating rules injected into AGENTS.md but is repeated per-run because
    autonomous runs must carry the discipline explicitly (AGENTS.md is
    honored loosely). It is the prompt-side guard against the kind of
    runaway loop that timed out a '.docx exact format' request 3x900s — the
    deterministic guard is the sidecar step ceiling."""
    return (
        # The FOUR generic rules (planning, bounded loop, data type, proportional
        # effort) are NOT repeated here: they are already in `AGENTS.md`, which is
        # loaded in EVERY session, and their single source is the AGENTS-MD block
        # of `skill-agent-core.md`. Repeating them was the third copy of the same
        # text and could drift from the other two without anyone noticing.
        #
        # What does live here, because it exists nowhere else, are the guardrails
        # OF THE AUTONOMOUS RUN: the deterministic abort on repetition, the `bash`
        # watchdog and the budget.
        # The trailer shares NOTHING with `AGENTS.md`: the agent already has it
        # loaded in the session, so repeating it —even just naming it— only spends
        # context and opens the door to the two versions drifting apart.
        "\n\nGUARDRAILS OF THIS RUN (mandatory, do not mention them to the user): "
        "the platform ABORTS the run when the same tool is repeated "
        "CONSECUTIVELY with identical arguments (`AGENT_MAX_REPEAT_STEPS`, 8 in a row) or "
        "the step ceiling is exceeded. The counter resets with any other "
        "call, so waiting for a job you launched by alternating `sleep` and looking "
        "does NOT trigger it, and the tools that look at a long job are exempt: "
        "waiting is not a blocker nor a loop without progress. What does trigger it is "
        "re-sending the same failed call: if a tool returns an error, nothing, or the "
        "same result, do NOT call it again the same way — DIAGNOSE first (in "
        "Odoo/MCP, `fields_get`/`search_read` to see what really exists in THIS "
        "version), adapt the call and try ONCE; the third identical time it is a "
        "blocker: record it with `log_decision` and carry on with whatever does not depend on it. "
        "`bash`: if a command fails or comes back empty do not re-send it as is; fix it and "
        "run it once (files with the editor, not with repeated heredocs). "
        "You have a budget of time and steps: if the platform stops you, that is a "
        "blocker and you say so; until then, you keep going."
    )


def delivery_block() -> str:
    """Artifact-delivery rules (skill ``wai-autonomous-delivery``,
    physical files under ``/workspace/exports/``). Only meaningful for
    apps with external channels — a pure monitoring agent without
    channels has nowhere to deliver a file, and unconditionally
    loading the delivery skill on every run (pre-v6.20.0) biased
    every generated agent toward the mail-analyst archetype."""
    try:
        from app.config import CHANNELS_ENABLED
    except Exception:  # pragma: no cover — older configs
        CHANNELS_ENABLED = False
    if not CHANNELS_ENABLED:
        return ""
    return (
        "\n\nMANDATORY RULES: before acting, load and follow the "
        "`wai-autonomous-delivery` skill (delivery of artifacts through a "
        "channel, traceability and security). In particular: "
        "deliverables requested through a channel are generated PHYSICALLY in "
        "`/workspace/exports/` and attached with `workspace_path` — "
        "NEVER links. A deliverable is written in its recipient's language. "
        "If the deliverable is a PRESENTATION, attach "
        "ONLY the `.pptx` (never `slide-NN.jpg` or other visual-validation "
        "images — the platform filters them out anyway)."
    )


async def _self_memory_maintenance_prompt(event: dict, run_id: int) -> str:
    """Prompt for a ``self_memory_maintenance`` run: turn N raw observations
    about ONE subject into a single baseline (or lesson) and retire the rest.

    Deliberately narrow. The deterministic half already ran (expiry + caps in
    ``agent_memory_utils.compact``); what needs judgement is "these five
    durations mean 35-45 min is normal here", which is a summary no rule can
    write. Keeping it to one subject per run keeps the cost bounded and makes the
    result reviewable.
    """
    try:
        payload = json.loads(event.get("payload_json") or "{}")
    except Exception:
        payload = {}
    subject = str(payload.get("subject") or "").strip()
    from app.agent_memory_prompt import build_agent_memory_block
    from app.memory_contract import MODE_MAINTENANCE, two_memories_contract
    block = await build_agent_memory_block(query=subject, consolidate=False)
    # 7.153.0 — it is given the MEASURED WORK, not just the subject: how many repeated pairs
    # there are and which contradictions exist. Before, it arrived blind ("you have several
    # observations") and had no way of knowing whether it had finished.
    pistas = ""
    try:
        from app.agent_memory_utils import contradictions, redundancy_report
        red = [r for r in await redundancy_report(subject) if r["subject"] == subject]
        contra = await contradictions(subject)
        if red:
            pistas += (f"\nMEASURED: {red[0]['entries']} live entries for `{subject}` with "
                       f"{red[0]['redundant_pairs']} pairs that say the same thing. That number "
                       f"must end at 0.")
        if contra:
            pistas += "\nCONTRADICTIONS to resolve (one of the two is superfluous):"
            for c in contra[:5]:
                pistas += (f"\n  · #{c['older_id']} «{c['older'][:90]}» "
                           f"VERSUS #{c['newer_id']} «{c['newer'][:90]}»")
            pistas += ("\n  Keep the one that is true TODAY and retire the other with "
                       "`supersedes`, explaining in the new one why it changed.")
    except Exception:  # pragma: no cover — the hints are a help, not a requirement
        pistas = ""
    return (
        "MAINTENANCE OF YOUR ENVIRONMENT MEMORY (this is not a conversation with a "
        "human; do not send any message to anyone). You have accumulated several "
        f"loose observations about `{subject}` and it is time to distil them.\n"
        f"1) Read them with `search_learnings(run_id={run_id}, "
        f"subject='{subject}')`.\n"
        "2) If they say the same thing, write ONE baseline with "
        "`remember_learning(kind='baseline', ...)` that summarises the normal range and "
        "which evidence it rests on, and pass `supersedes=[ids]` with the "
        "observations it summarises (do not delete them by hand: superseding keeps the "
        "trace of why you know it).\n"
        "   EXIT CRITERION: you are not done while there are entries left that "
        "say the same thing. When you finish, call `search_learnings` again on "
        "the same `subject` and check that there are no repetitions left; if there are, "
        "keep merging. A round that retires NOTHING is a wasted round: "
        "state explicitly in your answer how many you merged and how many you "
        "retired, because that is what is measured to know whether this is worth it.\n"
        "3) If you notice a pattern that should change your behaviour (e.g. "
        "\"this is normal on Sundays\"), do NOT apply it on your own: "
        "propose it with `propose_silence` and wait for approval.\n"
        "4) If something no longer holds, mark it with "
        "`forget_learning(id, reason=...)`.\n"
        "Do not invent new knowledge: only consolidate and correct what you already "
        f"observed. Always use run_id={run_id}."
        + pistas
        + block
        + two_memories_contract(MODE_MAINTENANCE)
    )


def self_memory_traceability_block(run_id: int) -> str:
    """Self-memory-only trailer: the four learning tools and nothing else, so a
    maintenance run cannot drift into acting on the business."""
    return (
        "\n\nLEARNING TRACEABILITY — MCP `app-tools` (run_id="
        f"{run_id}): use `search_learnings`, `remember_learning`, "
        "`forget_learning` and `compact_learnings`. Always pass "
        f"`run_id`: {run_id} in every call."
    )


def memory_traceability_block(run_id: int) -> str:
    """Memory-only trailer for ``memory_maintenance`` runs — no domain
    tools (alerts/recommendations/communications)."""
    return (
        "\n\nMEMORY TRACEABILITY — MCP `app-tools` (run_id="
        f"{run_id}): use `list_memory`, `remember_fact`, `forget_fact`, "
        "`index_file`, `search_memory` and `compact_memory` as you need. "
        f"Always pass `run_id`: {run_id} in every call together with the "
        "`user_email` given above."
    )


def traceability_block(run_id: int) -> str:
    """Per-run trailer with the DYNAMIC essentials only.

    The COMPLETE, always-true catalogue of the agent's local tools and
    what each is for now lives permanently in AGENTS.md (section "Your
    full capabilities", injected by the deployer alongside the
    ``app-tools`` MCP). So this trailer no longer re-lists every tool —
    it only (1) stamps the current ``run_id`` the agent must put on each
    write and (2) reinforces the two run-specific behaviours that matter
    most each time: deliver for real, and don't duplicate."""
    return (
        "\n\nMANDATORY TRACEABILITY — MCP `app-tools` (run_id="
        f"{run_id}): materialise your findings in your local database "
        "(full catalogue of read/write tools in AGENTS.md → "
        "\"Your full capabilities\"). At minimum: `upsert_alert` for "
        "each relevant anomaly, `register_recommendation` for each "
        "suggested action, `log_communication` when you communicate something to "
        "a human and `log_decision` if you decide not to act. "
        f"ALWAYS pass `run_id`: {run_id} in the arguments of every "
        "call."
        "\n\nREAL DELIVERY (not just recording): `log_communication` with "
        "`status='draft'` ONLY leaves a draft (it is NOT sent). For "
        "the message to be REALLY SENT through the channel use `status='ready'` "
        "(the system delivers it to the recipients the operator set "
        "and updates the row to `sent`/`failed`) — or, equivalently, the "
        "`notify` block of the STRUCTURED_RESPONSE. Write each message in its "
        "recipient's language (the output language given at the end of this "
        "prompt), not in the language of these instructions. To attach a file "
        "(PPTX/PDF/CSV) pass it in `attachments` with `workspace_path` to a "
        "real file in `/workspace/exports/` (NEVER a link). "
        "Presentation ⇒ attach the `.pptx`, NOT the `slide-NN.jpg` images "
        "from visual QA (intermediate, not deliverables). Use ONE of the two "
        "mechanisms, not both for the same message. Do NOT claim you have "
        "sent/attached something if you did not deliver it with `status='ready'` "
        "or `notify`: describing the file in the body does NOT send it."
        "\n\nMANDATORY ANTI-DUPLICATES: BEFORE creating, check what "
        "already exists with the read tools of the same MCP: "
        "`list_alerts` before `upsert_alert`, `list_recommendations` "
        "before `register_recommendation` and `list_decisions` before "
        "`log_decision`. If there is already an OPEN alert or a "
        "PENDING recommendation that covers the same problem (same "
        "target and same cause), do NOT create another one: reference its `id` in "
        "your summary. Create a new row only if the finding is "
        "really new or has changed materially (e.g. "
        "higher severity or different evidence)."
        "\n\nDO NOT REPEAT: if you catch yourself generating AGAIN AND AGAIN the same thing a "
        "human already closed (or that is noise for a while), silence it with "
        "`suppress_signal` (a BOUNDED postponement) instead of insisting; use "
        "`list_suppressions` to know what you must NOT raise again. If you try "
        "to create something silenced, the platform SKIPS it and tells you so "
        "(`suppressed: true`): do not retry it."
    )


def notify_block(kinds: list[str]) -> str:
    """Trailer added to NON-channel autonomous prompts when at least one
    channel is configured to notify. Lets the agent decide WHETHER to
    push a proactive notification (and WHAT to say) — never to whom: the
    recipients are fixed by the operator on the channel. Returns ''
    when no channel can notify, so we don't tempt the agent with a tool
    that would silently no-op."""
    if not kinds:
        return ""
    listed = ", ".join(sorted(set(kinds)))
    return (
        "\n\nPROACTIVE NOTIFICATION (optional): there are channels configured "
        f"to alert the people in charge ({listed}). If — and ONLY if — this "
        "run has found something relevant and actionable (an anomaly, a "
        "threshold exceeded, a critical finding a human should "
        "know about now), add a `notify` block to the STRUCTURED_RESPONSE: "
        '{"decision": "send", "severity": "info|warning|critical", '
        '"subject": "...", "body": "clear message for the person", '
        '"attachments": [{"filename": "report.pdf", "workspace_path": '
        '"exports/report.pdf"}], "channels": ["email"]}. Rules: do NOT '
        "choose recipients (the operator sets them on the channel); do NOT "
        "notify routine summaries or valueless 'all fine' messages; "
        "use an honest severity; `subject` and `body` go in the recipients' "
        "language (the output language given at the end of this prompt), not "
        "in the language of these instructions; `channels` is optional (omit it to "
        "use every eligible channel); attachments follow the same "
        "rule as replies (a PHYSICAL file in `/workspace/exports/`, "
        "never links). If there is no reason to alert, omit the `notify` block."
    )


async def _notifiable_channel_kinds() -> list[str]:
    """Kinds of active channels with at least one recipient configured
    and a policy that is not 'never'. Empty ⇒ no notify_block."""
    try:
        from app.app_models.channels import Channel
        from app.channels.notifier import _channel_notify_cfg
        async with app_write() as s:
            channels = (await s.execute(
                select(Channel).where(
                    Channel.status == "active", Channel.is_active == 1,
                )
            )).scalars().all()
        kinds: list[str] = []
        for ch in channels:
            notify_to, policy, _ = _channel_notify_cfg(ch)
            if notify_to and policy != "never":
                kinds.append(ch.kind)
        return kinds
    except Exception:  # pragma: no cover — never block a run on this
        logger.debug("notifiable channel lookup failed", exc_info=True)
        return []


async def run_agent_session(*, run_id: int, idempotency_key: str,
                            prompt: str, event_payload: dict,
                            event: dict | None = None) -> dict:
    """Send ``prompt`` to the sister OpenCode agent pod and return a
    normalised result dict consumed by ``_finish_run``.

    Implementation: open an **autonomous** session via the simple-API
    sidecar (bound to ``AGENT_AUTONOMOUS_USER_EMAIL``),
    ``send_autonomous`` for the assistant's final aggregated message,
    and map the response onto the columns persisted on AgentRun.
    Transient failures are surfaced as ``RecoverableAgentError`` so
    the state machine can schedule a retry.

    The session is deleted on completion (success or failure) so we
    don't leak entries in the simple-API ACL on long-running pods. If
    the delete fails it is logged and ignored — the sidecar cleans up
    stale entries on its own restart.

    External-channel events (``event['type'] == 'external_message'``)
    take a dedicated path that builds a channel-aware prompt, parses
    the agent's ``reply.body`` from the structured response and asks
    the adapter to deliver the reply. See ``app/channels/executor.py``.
    """
    if event is not None and event.get("type") == EXTERNAL_MESSAGE_EVENT_TYPE:
        from app.channels.executor import run_channel_message
        return await run_channel_message(event, run_id=run_id)

    if event is not None and event.get("type") == MEMORY_MAINTENANCE_EVENT_TYPE:
        # Focused memory run: no delivery/notify/domain traceability
        # (it never talks to a human or persists domain rows).
        try:
            payload = json.loads(event.get("payload_json") or "{}")
        except Exception:
            payload = {}
        from app.memory_utils import persist_run_memory_user
        await persist_run_memory_user(run_id, payload.get("user_email"))
        prompt = await _memory_maintenance_prompt(event, run_id)
        prompt = prompt + plan_verify_block()
        prompt = prompt + memory_traceability_block(run_id)
        prompt = prompt + language_block(await resolve_agent_language())
    elif event is not None and event.get("type") == SELF_MEMORY_MAINTENANCE_EVENT_TYPE:
        # Same shape as the per-user maintenance run above, on the environment
        # axis: no domain trailers (it must not raise alerts or notify), only the
        # learning tools.
        prompt = await _self_memory_maintenance_prompt(event, run_id)
        prompt = prompt + plan_verify_block()
        prompt = prompt + self_memory_traceability_block(run_id)
        prompt = prompt + language_block(await resolve_agent_language())
    elif event is not None and event.get("type") == AUTOMATION_CHECK_EVENT_TYPE:
        # READ-ONLY verification (check-before-approve): the agent VERIFIES the
        # natural-language condition and returns a structured verdict — it must
        # NOT act. So it gets the connector READ hint + loop discipline, but NONE
        # of the act-oriented trailers (traceability that tells it to materialize
        # findings, delivery, proactive notify) and — crucially — no write grant
        # is bound to its session below, so consequential connector writes stay
        # gated. The settle step turns the verdict into an approval or a decision.
        from app.scoped_knowledge import build_scoped_knowledge_block
        prompt = await _condition_check_prompt(event)
        prompt = prompt + clock_block()
        prompt = prompt + plan_verify_block()
        prompt = prompt + language_block(await resolve_agent_language())
        from app.mcp_runtime import composio_tools_hint
        prompt = prompt + composio_tools_hint()
        # v7.140.0 — the learnings block, WITHOUT the capture hint: this run only
        # judges whether a condition holds. It is precisely where knowing "this
        # window is normal on Sundays" prevents a false positive, but asking it to
        # also distil new learnings would widen a deliberately read-only run.
        from app.agent_memory_prompt import build_agent_memory_block
        prompt = prompt + await build_agent_memory_block(query=prompt[:400])
        # (The colleagues already travel in `employee_identity_preamble`, which heads this
        # same run: attaching them here too duplicated them in the prompt.)
        # And with it, the boundary in its READ-ONLY variant: this run reads its
        # memory so as not to mistake what is normal for an incident, and saves nothing.
        from app.memory_contract import MODE_READONLY, two_memories_contract
        prompt = prompt + two_memories_contract(MODE_READONLY)
        prompt = prompt + await build_scoped_knowledge_block(attending=False)
    else:
        from app.scoped_knowledge import build_scoped_knowledge_block
        # Boss-authored automation: rebuild the prompt from its reaction config.
        if event is not None and event.get("type") == AUTOMATION_FIRED_EVENT_TYPE:
            prompt = await _automation_prompt(event)
        prompt = prompt + clock_block()
        prompt = prompt + plan_verify_block()
        prompt = prompt + delivery_block()
        prompt = prompt + traceability_block(run_id)
        prompt = prompt + notify_block(await _notifiable_channel_kinds())
        prompt = prompt + language_block(await resolve_agent_language())
        # How to drive the Composio connectors (router meta-tools vs direct).
        from app.mcp_runtime import composio_tools_hint
        prompt = prompt + composio_tools_hint()
        # Write-policy rules (v7.26.0): which connectors are read-only /
        # approval-gated and HOW to park a write for approval.
        from app.mcp_policy import write_policy_block
        prompt = prompt + await write_policy_block()
        # Memory distillation (v7.29.0): emit the structured `memory` block.
        from app.memory_prompt import memory_capture_hint
        prompt = prompt + memory_capture_hint()
        # v7.140.0 — WHAT THE EMPLOYEE LEARNED ABOUT THIS ENVIRONMENT.
        # This is the branch that matters most for the self-memory: an autonomous
        # run has no human to ask, so without its own learnings it re-discovers
        # the same "this backup takes 40 minutes" every night and raises the same
        # false alarm. The block goes in BEFORE the capture hint's counterpart so
        # the agent first READS what it knows, then is told how to record more.
        from app.agent_memory_prompt import (
            agent_memory_capture_hint, build_agent_memory_block_detail,
        )
        # 7.164.0 — the DETAIL is requested (not just the text) to note down which
        # learnings this run carries inside. When it ends, `attribute_outcome`
        # shares the outcome among them: that is what turns "it was injected" into
        # "it helped", and without that nuance the life cycle keeps the most repeated
        # instead of the most useful.
        _mem = await build_agent_memory_block_detail(query=prompt[:400])
        prompt = prompt + (_mem.get("text") or "")
        try:
            from app.agent_memory_utils import record_injection
            await record_injection(run_id, _mem.get("entry_ids") or [])
        except Exception:  # pragma: no cover — never break the run over this
            logger.debug("record_injection failed (ignored)", exc_info=True)
        prompt = prompt + agent_memory_capture_hint()
        # 7.164.0 — and RIGHT AFTER telling it how to record, where NOT to. The
        # warning goes here and not before the block on purpose: first it reads what it
        # knows, then how to write it down, and last where it is already saturated.
        try:
            from app.agent_memory_prompt import saturation_warning
            prompt = prompt + await saturation_warning()
        except Exception:  # pragma: no cover
            logger.debug("saturation_warning failed (ignored)", exc_info=True)
        # …and the BOUNDARY between the two memories, the same on every path
        # (v7.143.0). This is where it matters most: with nobody in front, the
        # `memory` block has no owner and all the learning of the turn must go to
        # `self_memory` — before, nothing said so in this direction.
        from app.memory_contract import MODE_AUTONOMOUS, two_memories_contract
        prompt = prompt + two_memories_contract(MODE_AUTONOMOUS)
        # Background watcher/cron run: the agent acts on its own, no human
        # served → "autonomous" scope (+ "all").
        prompt = prompt + await build_scoped_knowledge_block(attending=False)
    # The autonomous run also acts AS the employee (persona/business voice),
    # not a generic assistant — prepend the identity to whatever branch built.
    try:
        from app.persona import employee_identity_preamble
        _identity = await employee_identity_preamble(await resolve_agent_language())
        if _identity:
            prompt = _identity + "\n\n" + prompt
    except Exception:
        pass
    # Persist the REAL prompt (with the traceability trailer) so the
    # runs UI shows exactly what the agent received.
    try:
        async with app_write() as s:
            await s.execute(
                update(AgentRun).where(AgentRun.id == run_id).values(prompt=prompt)
            )
            await s.commit()
    except Exception:  # pragma: no cover — display-only, never block the run
        logger.warning("could not persist prompt for run %s", run_id)

    proxy = get_agent_proxy()
    from app.agent_sessions import resolve_operator_model, resolve_tool_permissions
    # v7.35.0 COALESCENCE — an approved automation reaction carries a run-scoped
    # write grant; bind it to THIS run's session so its connector writes don't
    # re-raise the write gate (approvals_routes minted it on approve).
    _grant = ((event_payload or {}).get("context") or {}).get("write_grant") if isinstance(event_payload, dict) else None
    session_id = await proxy.create_session(
        role="autonomous", model=await resolve_operator_model(),
        tool_permissions=await resolve_tool_permissions(),
        write_grant=_grant or None,
    )
    # Persist the live OpenCode session id NOW (not only at _finish_run) so
    # an operator who cancels this IN_PROGRESS run can abort the in-flight
    # generation via the kill switch.
    try:
        async with app_write() as s:
            await s.execute(
                update(AgentRun)
                .where(AgentRun.id == run_id)
                .values(opencode_session_id=session_id)
            )
            await s.commit()
    except Exception:  # pragma: no cover — display/abort aid, never block
        logger.debug("could not persist session id for run %s", run_id)
    try:
        raw = await proxy.send_autonomous(
            session_id, prompt, require_structured=True,
        )
    finally:
        # Abort BEFORE delete so a timed-out / step-ceiling run is actually
        # stopped (a bare delete may leave the generation running) — this is
        # what prevents heavy retries from stacking up.
        await proxy.abort_session(session_id)
        await proxy.delete_session(session_id)
    duration = int(raw.get("duration_sec") or 0)
    # What it DID is recorded, not just what it wrote. In autonomous mode there is no stream
    # to watch, so without this the run's card showed one line (the `upsert_alert` on its
    # own database) for a turn with six calls. See `app/tool_usage.py`.
    from app.tool_usage import anotar as _anotar_uso
    for _t in (raw.get("tools") or []):
        await _anotar_uso(
            run_id, call_id=str(_t.get("call_id") or ""),
            tool_name=str(_t.get("tool") or "tool"),
            step_idx=int(_t.get("step") or 0),
            terminada=(_t.get("status") in ("ok", "error")),
            estado=("error" if _t.get("status") == "error" else "ok"),
        )
    # The run's account, in the log and in the same format as the chat's
    # (`event: "turno"`), so the two paths can be compared without translating anything
    # (the `extra` keys are the log contract: they stay as they are). In autonomous mode
    # there is NO stream: the sidecar returns the turn already closed, so the total, the
    # calls and the spend are measured; the model/tools split cannot be measured here
    # and so it is not invented.
    try:
        _por_herramienta: dict[str, int] = {}
        for _t in (raw.get("tools") or []):
            _n = str(_t.get("tool") or "tool")
            _por_herramienta[_n] = _por_herramienta.get(_n, 0) + 1
        logger.info(
            "autonomous run in %d s (%d calls)",
            duration, sum(_por_herramienta.values()),
            extra={
                "event": "turno",
                "estado": "ok",
                "origen": "autonomo",
                "run_id": run_id,
                "ms_total": duration * 1000,
                "llamadas": sum(_por_herramienta.values()),
                "herramientas": [
                    {"nombre": n, "n": c}
                    for n, c in sorted(_por_herramienta.items(), key=lambda kv: -kv[1])
                ][:15],
                "tokens": {
                    "entrada": int(raw.get("tokens_input") or 0),
                    "salida": int(raw.get("tokens_output") or 0),
                },
                "coste": float(raw.get("cost") or 0.0),
                "modelo": raw.get("model") or "",
            },
        )
    except Exception:  # noqa: BLE001 — measuring never breaks a run
        logger.debug("medicion: could not record run %s", run_id, exc_info=True)
    return {
        "text": raw.get("text") or "",
        "structured": raw.get("structured") or {},
        "tokens_in": int(raw.get("tokens_input") or 0),
        "tokens_out": int(raw.get("tokens_output") or 0),
        "model": raw.get("model") or "",
        "duration_sec": duration,
        "cost_usd": str(raw.get("cost") or ""),
        "session_id": session_id,
    }


def _origen_del_evento(source: str) -> tuple[str, int | None]:
    """Where the run came from, derived from the event's `source`.

    The formats are set by whoever creates the events: `channel:<id>` (channels poller),
    `automation:<id>` (automations), `scheduled_item:<id>` (schedule) and, if none
    matches, the watcher's name. It is translated into a STABLE word the screen can
    filter on (`canal`/`automatizacion`/`programado`/`vigia` — enum values the frontend
    matches, they stay as they are), instead of letting each view learn these prefixes
    on its own.
    """
    s = (source or "").strip()
    if s.startswith("channel:"):
        try:
            return "canal", int(s.split(":", 1)[1])
        except (ValueError, IndexError):
            return "canal", None
    if s == "email":                      # old poller format, without id
        return "canal", None
    if s.startswith("automation:"):
        return "automatizacion", None
    if s.startswith("scheduled_item:"):
        return "programado", None
    return "vigia", None


def _prompt_for(event: dict) -> str:
    """Render the system+user prompt for an event using app.prompts,
    falling back to the skill's built-in SYSTEM templates (calendar /
    tasks / approvals) for event types the app didn't declare."""
    from app.prompts import PROMPTS  # late import to avoid cycles
    try:
        from app.prompts_defaults import PROMPTS_DEFAULTS
    except Exception:  # pragma: no cover — older deploys without the file
        PROMPTS_DEFAULTS = {}

    # ``claim_next_event`` returns the column name ``type``; accept the
    # legacy ``event_type`` alias too so older prompt templates keep
    # working when interpolated with **event.
    event_type = event.get("type") or event.get("event_type") or ""
    template = PROMPTS.get(event_type) or PROMPTS_DEFAULTS.get(event_type)
    payload = event.get("payload_json") or "{}"
    if template:
        try:
            return template.format(
                payload=payload, event_type=event_type, **(event or {}),
            )
        except Exception:
            return template
    return f"Process event {event_type or '?'}: {payload}"

logger = logging.getLogger(__name__)

# How often the in-flight event's heartbeat is renewed while the agent
# call runs. Must be comfortably below recovery.STALE_THRESHOLD so a
# live long run (agent calls can take minutes) is never requeued by the
# periodic stale recovery.
HEARTBEAT_RENEW_SECONDS = 60


async def _renew_heartbeat_forever(event_id: int) -> None:
    """Keep ``events.heartbeat_at`` fresh while the agent call is in
    flight. Spawned as a sibling task and cancelled when the call
    returns. Failures are logged and ignored — a missed beat only
    matters if it persists past STALE_THRESHOLD."""
    import asyncio
    from sqlalchemy import update as _update
    from app.app_models.runtime import Event
    while True:
        await asyncio.sleep(HEARTBEAT_RENEW_SECONDS)
        try:
            async with app_write() as session:
                await session.execute(
                    _update(Event).where(Event.id == event_id).values(
                        heartbeat_at=datetime.now(timezone.utc),
                    )
                )
                await session.commit()
        except Exception:
            logger.debug("heartbeat renewal failed (ignored)", exc_info=True)


async def _budget_consumed_24h(session) -> float:
    """R2: sum of cost_usd for autonomous runs in the last 24h.

    Cost is stored as a string (legacy) so we coerce defensively.
    """
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    rows = (await session.execute(
        select(AgentRun.cost_usd).where(
            AgentRun.started_at >= cutoff,
            AgentRun.mode == "autonomous",
        )
    )).scalars().all()
    total = 0.0
    for raw in rows:
        if not raw:
            continue
        try:
            total += float(raw)
        except (TypeError, ValueError):
            continue
    return total


# Conversations with a run IN FLIGHT right now (session scope_keys). The
# worker pool consults it so two messages of the SAME conversation (same
# person, same channel) never run concurrently — they would race on one
# OpenCode session — while different conversations DO run in parallel.
# In-process state is correct here: the pool lives in this process, and
# claims are already atomic across processes at the DB layer.
_BUSY_SCOPES: set[str] = set()


async def _event_guard_key(event: dict) -> str | None:
    """Session scope_key an ``external_message`` event will run under —
    the concurrency-guard identity. Background runs (watcher/cron/
    scheduled) use ephemeral sessions and need no guard. Best-effort:
    on any lookup failure the event just runs unguarded (the pre-v7.25.0
    behaviour for every event)."""
    if event.get("type") != EXTERNAL_MESSAGE_EVENT_TYPE:
        return None
    try:
        payload = json.loads(event.get("payload_json") or "{}")
        channel_id = int(payload.get("channel_id") or 0)
        sender = (payload.get("sender") or "").strip()
        if channel_id and not sender:
            provider_id = str(payload.get("provider_message_id") or "")
            if provider_id:
                from app.app_models.channels import ChannelMessage
                # READ-only lookup — no write queue (see INC-VIG-59).
                async with app_session() as s:
                    sender = (await s.execute(
                        select(ChannelMessage.sender).where(
                            ChannelMessage.channel_id == channel_id,
                            ChannelMessage.provider_message_id == provider_id,
                        )
                    )).scalars().first() or ""
        if not channel_id or not sender:
            return None
        from app.agent_sessions import resolve_inbound_scope
        return (await resolve_inbound_scope(channel_id, sender)).scope_key
    except Exception:  # pragma: no cover — guard is best-effort
        logger.debug("guard-key resolution failed", exc_info=True)
        return None


async def process_once(*, inbound_only: bool = False) -> bool:
    # R2: budget gate. When the running 24-h cost exceeds the
    # configured daily cap, refuse to claim a new event. The watcher
    # keeps inserting events as PENDING; the executor picks them up
    # again as soon as the rolling window slides under the cap.
    if DAILY_BUDGET_USD > 0:
        # READ — must NOT take the write queue (INC-VIG-59). With 8 workers this
        # runs ~4×/second; holding the write slot for a 24 h scan over agent_runs
        # put a crowd of non-writers ahead of every real writer, the kill switch
        # among them. Readers are concurrent under WAL, so app_session is correct.
        async with app_session() as session:
            spent = await _budget_consumed_24h(session)
        if spent >= DAILY_BUDGET_USD:
            logger.warning(
                "daily budget exhausted: spent=$%.4f cap=$%.4f — "
                "skipping this tick", spent, DAILY_BUDGET_USD,
            )
            return False

    # R-BUDGET-1: monthly credit cap. When the rolling-30d consumed credits
    # reach the per-employee budget, the employee stops acting entirely — this
    # gate covers autonomous runs AND channel replies (both flow through
    # process_once). Boss chat is gated separately in agent_routes. The watcher
    # keeps inserting PENDING events; they're claimed once the window slides
    # back under the cap (or a redeploy raises it).
    # 7.122.0: same barrier also fires when the PROJECT balance is exhausted
    # (the account balance, backend V68) — the log says which layer tripped.
    from app.budget import budget_block_reason
    _reason = await budget_block_reason()
    if _reason is not None:
        logger.warning(
            "budget barrier up (%s) — skipping this tick",
            "account balance exhausted (project)" if _reason == "project_budget"
            else "employee's monthly limit",
        )
        return False

    event = await claim_next_event(inbound_only=inbound_only)
    if event is None:
        return False

    # Per-conversation guard: the same session (person+channel) never runs
    # two prompts at once — defer this message a few seconds (attempt rolled
    # back) and keep the worker free for another conversation.
    guard_key = await _event_guard_key(event)
    if guard_key and guard_key in _BUSY_SCOPES:
        async with app_write() as session:
            await defer_event(session, event["id"], event["attempts"])
            await session.commit()
        return True
    if guard_key:
        _BUSY_SCOPES.add(guard_key)
    try:
        return await _process_claimed(event)
    finally:
        if guard_key:
            _BUSY_SCOPES.discard(guard_key)


async def _process_claimed(event: dict) -> bool:
    # The event type is read with the SAME pattern as the rest of the file. It is used
    # further down in `_activity_for(...)` and was not defined in this scope: a NameError
    # that only shows up in the "condition-check settle" branch, i.e. almost never in
    # tests and yes in production.
    event_type = event.get("type") or event.get("event_type") or ""
    async with app_write() as session:
        attempts = event["attempts"]
        max_attempts = event["max_attempts"]
        # B2: idempotency_key MUST NOT include the attempt number.
        # Encoding ``::N`` (the previous behaviour) made every retry
        # write a new AgentRun row, which doubled cost/token totals on
        # /runs/summary and broke the v6.4.0 audit-triplet contract.
        # The retry now UPDATES the same row in place; ``attempts`` is
        # tracked on the Event row, not the AgentRun.
        idem_key = event["fingerprint"]
        # B9: thread a stable correlation id through the chain.
        trace_id = event.get("trace_id") or idem_key

        # Re-entrancy guard: if a run with this idem_key already reached
        # COMPLETED, the event is already done — close the loop.
        existing = (await session.execute(
            select(AgentRun).where(AgentRun.idempotency_key == idem_key)
        )).scalar_one_or_none()
        if existing and existing.status == "COMPLETED":
            await mark_completed(session, event["id"])
            await session.commit()
            return True

        # Fire any standing automations that match this just-arrived event
        # (channel_message on an inbound message, business_event on a watcher/
        # system event). These signals are NOT polled — they're matched here,
        # once per source event. Best-effort + isolated session: a matching
        # failure must never block processing the event itself. The per-source
        # bucket dedups across retries.
        try:
            from app.autonomous.watcher import match_arrival_automations
            async with app_write() as _asess:
                await match_arrival_automations(_asess, event)
                await _asess.commit()
        except Exception:
            logger.exception(
                "arrival automation matching failed for event %s", event.get("id")
            )

        # Create or reuse the run row. INSERT ... ON CONFLICT DO NOTHING
        # is the idempotent path; on a retry the row already exists and
        # we just reset its status/started_at.
        # v12.7.0 — WHERE this came in from. It is derived from the event's `source`, the
        # only place it is recorded, and stored on the run because events get pruned and the
        # runs screen must keep saying where each one came from.
        # `solicitante` stays NULL here on purpose: in an autonomous run NOBODY asked for
        # it, and in a channel run the channel executor fills it in when it loads the
        # message and knows who wrote.
        _origen, _canal_id = _origen_del_evento(event.get("source") or "")
        insert_stmt = sqlite_insert(AgentRun).values(
            event_id=event["id"],
            idempotency_key=idem_key,
            trace_id=trace_id,
            attempts=attempts,
            status="RUNNING",
            prompt=_prompt_for(event),
            origen=_origen,
            canal_id=_canal_id,
            started_at=datetime.now(timezone.utc),
        ).on_conflict_do_nothing(index_elements=[AgentRun.idempotency_key])
        await session.execute(insert_stmt)
        # If the row already existed (retry path), refresh attempts and
        # status to RUNNING so the operator sees the in-flight retry.
        await session.execute(
            update(AgentRun)
            .where(AgentRun.idempotency_key == idem_key)
            .where(AgentRun.status != "COMPLETED")
            .values(
                status="RUNNING",
                attempts=attempts,
                started_at=datetime.now(timezone.utc),
                error=None,
                trace_id=trace_id,
                origen=_origen,
                canal_id=_canal_id,
            )
        )
        await session.commit()

        run = (await session.execute(
            select(AgentRun).where(AgentRun.idempotency_key == idem_key)
        )).scalar_one()

        await mark_in_progress(session, event["id"])
        # Bind the originating ScheduledItem to this run NOW so the board
        # shows a live deep-link and an accurate "in progress" state.
        try:
            await _sync_item_for_run(session, run.id, status="RUNNING")
        except Exception:
            logger.exception("scheduled-item RUNNING sync failed for run %s", run.id)
        await session.commit()

    # Run the agent OUTSIDE the DB transaction — agent calls may take
    # minutes. The heartbeat task below keeps ``events.heartbeat_at``
    # fresh so the periodic stale recovery never requeues a live run.
    import asyncio
    hb_task = asyncio.create_task(_renew_heartbeat_forever(event["id"]))
    try:
        try:
            result = await run_agent_session(
                run_id=run.id,
                idempotency_key=idem_key,
                prompt=run.prompt,
                event_payload=json.loads(event["payload_json"] or "{}"),
                event=event,
            )
        except RecoverableAgentError as err:
            logger.warning("Recoverable agent error on event %s: %s", event["id"], err)
            # Heavy failures (timeout / step-ceiling) recur if we re-run the
            # identical heavy prompt, and stacking 900s runs is exactly what
            # caused the .docx hang. Cap them at ONE retry; cheap transients
            # keep the full max_attempts budget. The in-flight OpenCode
            # session was already aborted in run_agent_session's finally, so
            # the retry starts from a clean slate (no compute pileup).
            heavy = _is_heavy_failure(err)
            retry_cap = min(max_attempts, 2) if heavy else max_attempts
            giving_up = attempts >= retry_cap
            async with app_write() as session:
                if not giving_up:
                    await schedule_retry(session, event["id"], attempts, BACKOFF_SECONDS)
                else:
                    await mark_failed(session, event["id"], str(err))
                await _finish_run(session, run.id, status="FAILED", error=str(err))
                await session.commit()
            # A pre-check that has exhausted its retries must still surface an
            # approval (fail-safe) so the firing isn't lost — see _settle_failed_check.
            if giving_up:
                await _settle_failed_check(event, run.id)
                await _settle_failed_approval(event)
            return True
        except asyncio.CancelledError:
            # Cooperative shutdown — the lifespan cancelled this task.
            # Hand the event back (PENDING) and close the run row so the
            # next boot retries immediately instead of waiting for the
            # stale recovery to notice the orphan.
            try:
                from sqlalchemy import update as _update
                from app.app_models.runtime import Event
                async with app_write() as session:
                    await session.execute(
                        _update(Event).where(Event.id == event["id"]).values(
                            status="PENDING", heartbeat_at=None,
                            next_attempt_at=None,
                        )
                    )
                    await _finish_run(
                        session, run.id, status="FAILED",
                        error="interrupted by shutdown",
                    )
                    await session.commit()
            except Exception:
                logger.warning("shutdown handoff failed for event %s", event["id"])
            raise
        except Exception as err:  # unrecoverable
            logger.exception("Unrecoverable agent error on event %s", event["id"])
            async with app_write() as session:
                await mark_failed(session, event["id"], str(err))
                await _finish_run(session, run.id, status="FAILED", error=str(err))
                await session.commit()
            # Fail-safe: a permanently-failed pre-check still parks an approval.
            await _settle_failed_check(event, run.id)
            await _settle_failed_approval(event)
            return True
    finally:
        hb_task.cancel()

    # v7.63.0/v7.68.0 — check-before-approve: a completed ``automation_check`` run
    # is a READ-ONLY verification, not an action. SETTLE FIRST (turn its verdict
    # into an approval or a decision), THEN finalise — so a settle failure leaves
    # the event UNfinished and retryable (settle is idempotent on the bucket)
    # instead of silently dropping the firing. The run has no memory/notify side
    # effects, so it skips the materialize + dispatch hooks below.
    if event.get("type") == AUTOMATION_CHECK_EVENT_TYPE:
        payload = json.loads(event.get("payload_json") or "{}")
        ctx = payload.get("context") or {}
        try:
            from app import automations_service as asvc
            await asvc.settle_condition_check(
                int(payload.get("automation_id")),
                str(payload.get("bucket") or ""),
                ctx,
                result.get("structured"),
                run.id,
                test_only=bool(ctx.get("test_only")),
            )
        except Exception:
            # Do NOT finalise: leave the event in-flight so stale recovery re-runs
            # it (the run stays RUNNING, so the re-entrancy guard won't skip settle
            # on retry). Re-running a read-only check is safe.
            logger.exception("condition-check settle failed for run %s — leaving event for retry", run.id)
            return True
        async with app_write() as session:
            await _finish_run(session, run.id, status="COMPLETED", result=result,
                              activity=_activity_for(event_type))
            await mark_completed(session, event["id"])
            await session.commit()
        # OUTSIDE the lock: it does NETWORK I/O. See `reportar_coste_del_run`.
        await reportar_coste_del_run(run.id, result, _activity_for(event_type))
        return True

    async with app_write() as session:
        finalised = await _finish_run(session, run.id, status="COMPLETED", result=result,
                                      activity=_activity_for(event_type))
        await mark_completed(session, event["id"])
        await session.commit()
    # OUTSIDE the lock: it does NETWORK I/O. See `reportar_coste_del_run`.
    await reportar_coste_del_run(run.id, result, _activity_for(event_type))

    # v7.29.0 — ingest the agent's DISTILLED memory block (facts/episode/file
    # semantics it wrote itself) for ANY completed run. Trust is capped to the
    # run origin server-side, so an inbound run can only add quarantined facts.
    if finalised:
        try:
            from app.memory_utils import materialize_memory_block, memory_owner_for_run
            owner = await memory_owner_for_run(run.id)
            if owner:
                src = "email" if event.get("type") == EXTERNAL_MESSAGE_EVENT_TYPE else "agent"
                await materialize_memory_block(run.id, owner, result.get("structured"), source=src)
        except Exception:
            logger.debug("memory-block materialize failed run=%s", run.id, exc_info=True)
        # v7.140.0 — the SELF-memory half of the same idea, and the reason the
        # learning costs nothing extra: what the employee learned about the
        # ENVIRONMENT travels in the same structured tail it already writes.
        # Unconditional on an owner: an autonomous run has NO human owner, and
        # that is exactly the run that learns the most about the environment.
        try:
            from app.agent_memory_utils import materialize_self_memory_block
            await materialize_self_memory_block(run.id, result.get("structured"))
        except Exception:
            logger.debug("self-memory materialize failed run=%s", run.id, exc_info=True)

    # Proactive notifications: a NON-channel run (watcher / scheduled)
    # may have asked to push a notify block to the configured channels.
    # Best-effort and AFTER the run is committed COMPLETED — a failed
    # send must never fail the run nor trigger a retry. Channel-origin
    # events already reply to their sender, so they are excluded.
    # Gated on ``finalised``: if the operator cancelled this run mid-flight
    # (so _finish_run did NOT flip it to COMPLETED), we must NOT externally
    # send its staged notify/communications — that would leak email/WhatsApp/
    # Slack past the kill switch.
    if finalised and event.get("type") != EXTERNAL_MESSAGE_EVENT_TYPE:
        try:
            from app.channels.notifier import (
                dispatch_notifications,
                dispatch_ready_communications,
                reconcile_communications,
            )
            outcome = await dispatch_notifications(run.id, result.get("structured"))
            if outcome.intent:
                # The agent delivered (or tried to) via a notify block —
                # bring this run's communications log in line with what
                # actually happened (sent / failed), never leave 'ready'.
                await reconcile_communications(run.id, outcome)
            else:
                # No notify block: deliver any communications the agent
                # staged as 'ready' (the long-missing downstream worker).
                await dispatch_ready_communications(run.id)
        except Exception:
            logger.exception("notification dispatch failed for run %s", run.id)
    return True


async def run_forever(poll_interval: float = 2.0):
    """Supervise the autonomous worker POOL (v7.45.0 — LIVE concurrency).

    The pool is OVER-PROVISIONED to the hard ceiling (8 workers), but each worker
    calls ``execution_limits.acquire`` before claiming an event, so the EFFECTIVE
    concurrency is whatever the operator set in Operations — applied live, no
    redeploy. Two caps: total in-flight (``max_concurrent_runs``) and, of those,
    autonomous in-flight (``max_autonomous_concurrent``); the remainder is always
    free for inbound channel/chat replies. An idle worker holds NO OpenCode
    session (it only polls), so over-provisioning is cheap. The per-conversation
    guard in ``process_once`` still keeps one conversation strictly ordered while
    different conversations run in parallel. A dedicated task runs the periodic
    stale recovery."""
    import asyncio
    from app.autonomous.recovery import recover_stale_events
    from app import execution_limits as _limits

    async def _recovery_loop():
        # Periodic stale recovery — without it an event claimed right
        # before a crash younger than STALE_THRESHOLD at boot time
        # would stay CLAIMED/IN_PROGRESS forever (boot recovery only
        # runs once). Live runs are protected by their heartbeat task.
        while True:
            try:
                await recover_stale_events()
            except Exception:
                logger.exception("periodic stale recovery failed")
            await asyncio.sleep(60)

    async def _worker(idx: int):
        while True:
            role = None
            progressed = False
            try:
                mt, ma = await _limits.effective_limits()
                role = _limits.acquire(mt, ma)  # atomic; None ⇒ at capacity
                if role is None:
                    await asyncio.sleep(poll_interval)
                    continue
                progressed = await process_once(inbound_only=(role == "inbound"))
            except asyncio.CancelledError:
                raise
            except Exception:
                # A worker must survive any single-event explosion — the event
                # itself was already marked FAILED/retried inside process_once
                # wherever possible.
                logger.exception("executor worker %s crashed on an event", idx)
            finally:
                _limits.release(role)
            if not progressed:
                await asyncio.sleep(poll_interval)

    logger.info(
        "autonomous executor pool: %d over-provisioned workers (effective "
        "concurrency is live from Operations)", _limits.CEILING,
    )
    tasks = [asyncio.create_task(_recovery_loop(), name="executor-recovery")]
    tasks += [
        asyncio.create_task(_worker(i), name=f"executor-worker-{i}")
        for i in range(_limits.CEILING)
    ]
    try:
        await asyncio.gather(*tasks)
    finally:
        # Cooperative shutdown: cancel the whole pool when the lifespan
        # cancels this supervisor (or any task dies unexpectedly).
        for t in tasks:
            t.cancel()


import hashlib, json
from datetime import datetime, timezone
from sqlalchemy import update
from app.app_models.runtime import AgentRun

def _collect_run_artifacts(started_at) -> list[dict]:
    """Best-effort listing of deliverable files the run wrote under the
    shared workspace's ``exports/`` folder (mtime within the run window).

    The app pod mounts the same PVC the agent writes to, so on a
    COMPLETED run we can snapshot what landed in ``exports/`` and let the
    SPA offer a direct download from the run, encargo or calendar item.
    Never raises — a capture failure must not break run finalisation.
    """
    try:
        import calendar as _cal
        import os
        from app import workspace_fs as wfs

        root = wfs.workspace_root()
        if not root:
            return []
        exports = os.path.join(root, "exports")
        if not os.path.isdir(exports):
            return []
        # Run-window floor (epoch). ``started_at`` is naive UTC; small
        # slack absorbs clock skew between the pods sharing the PVC.
        floor = 0.0
        if started_at is not None:
            try:
                floor = _cal.timegm(started_at.timetuple()) - 5.0
            except Exception:
                floor = 0.0
        out: list[dict] = []
        for dirpath, dirnames, filenames in os.walk(exports):
            # Skip hidden/QA scaffolding dirs (e.g. exports/.qa/...).
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                if name.startswith("."):
                    continue
                fp = os.path.join(dirpath, name)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                if st.st_mtime < floor:
                    continue
                rel = os.path.relpath(fp, root).replace(os.sep, "/")
                out.append({
                    "filename": name,
                    "workspace_path": rel,
                    "size": int(st.st_size),
                })
        from app.deliverables import filter_run_artifacts

        return filter_run_artifacts(out)
    except Exception:
        logger.debug("artifact collection failed for run", exc_info=True)
        return []


async def reportar_coste_del_run(run_id: int, result, activity=None) -> None:
    """Record this run's spend in the tenant's ledger.

    **It is ALWAYS called outside `app_write`**, and that "outside" is the fix, not a
    style detail. It used to live inside `_finish_run`, i.e. inside the write lock,
    and it makes a NETWORK call —a POST to the platform, plus an opportunistic drain
    of the pending ones—. `app_database` forbids it in writing: "do slow
    I/O (network…) BEFORE/AFTER it, never inside, or you serialize every other
    writer behind that I/O".

    What it cost, measured on the Odoo employee on 16/09/2026: ten holds of the
    lock of **exactly 15.1 s** in 115 minutes —the constancy gives away a network
    timeout, not database work—. With every writer queued behind, the pool and the
    loop saturate, `/api/health` stops answering in time, the pod drops out of the
    Service and nginx returns 503: the employee disappears for ALL its users for
    minutes. INC-AGO-101 uncovered it, failing on a 503 that had nothing to do with
    what it measures.

    The CHAT path (`_finish_chat_run`) already did it right: it closes `app_write`
    and then reports. This only aligns the autonomous path with it.

    Best-effort, as always: a billing failure cannot break a run, and
    `PendingCostReport` keeps whatever does not go out so it can be retried.
    """
    if result is None:
        return
    try:
        from app.agent_proxy import AUTONOMOUS_USER_EMAIL
        from app.cost_reporter import report_run_cost
        await report_run_cost(
            session_id=result.get("session_id") or f"run-{run_id}",
            user_id=AUTONOMOUS_USER_EMAIL or None,
            cost_usd=result.get("cost_usd"),
            tokens_input=int(result.get("tokens_in") or 0),
            tokens_output=int(result.get("tokens_out") or 0),
            model_name=result.get("model") or None,
            # The tokens spent tidying its own memory are counted SEPARATELY, with a
            # business name: Consumption shows how much it costs for the employee to
            # keep itself tidy, apart from what working costs.
            activity=activity,
        )
    except Exception:
        logger.debug("tenant cost report failed for run %s", run_id, exc_info=True)


async def _finish_run(session, run_id: int, *, status: str,
                      result: dict | None = None, error: str | None = None,
                      activity: str = ACTIVITY_WORK):
    values: dict = {
        "status": status,
        "finished_at": datetime.now(timezone.utc),
        "error": error,
    }
    if result is not None:
        values.update({
            "response_md":     result.get("text") or "",
            "structured_json": json.dumps(result.get("structured") or {}),
            "tokens_in":       int(result.get("tokens_in") or 0),
            "tokens_out":      int(result.get("tokens_out") or 0),
            "model":           result.get("model") or "",
            "duration_sec":    int(result.get("duration_sec") or 0),
            "cost_usd":        str(result.get("cost_usd") or ""),
            "opencode_session_id": result.get("session_id") or None,
        })
    # On success, snapshot the files the run produced so they are
    # downloadable from the run / encargo / calendar item.
    artifacts: list[dict] = []
    if status == "COMPLETED":
        started_at = (await session.execute(
            select(AgentRun.started_at).where(AgentRun.id == run_id)
        )).scalar_one_or_none()
        artifacts = _collect_run_artifacts(started_at)
        if artifacts:
            values["artifacts_json"] = json.dumps(artifacts)
    # STATUS GUARD: only finalise a run that is still RUNNING. If the operator
    # cancelled it mid-flight (cancel_run set the row to FAILED — see
    # runs_routes.cancel_run), the in-flight agent call returning COMPLETED must
    # NOT flip it back: that silently undid the kill switch and left the run
    # COMPLETED while its event stayed FAILED. A retried run is reset to RUNNING
    # before re-execution, so the normal finalise path is unaffected.
    _upd = await session.execute(
        update(AgentRun)
        .where(AgentRun.id == run_id, AgentRun.status == "RUNNING")
        .values(**values)
    )
    # rowcount 0 ⇒ the run was no longer RUNNING (operator cancelled it). The
    # downstream item sync must then NOT mark the originating Encargo/Calendar
    # item ``done`` from the in-memory ``status`` — keep the cancel sticky there
    # too.
    finalised = bool(_upd.rowcount)
    # 7.164.0 — CLOSES THE LOOP of learning: shares the outcome among the
    # learnings this run had injected. It is not causality (being present does not
    # prove having helped), which is why there are two counters and not a score:
    # what informs is the PROPORTION across many runs. An entry that only shows up
    # when things go wrong is suspicious even if it is injected daily.
    # Best-effort and off the critical path: it never prevents finalising.
    try:
        from app.agent_memory_utils import attribute_outcome
        await attribute_outcome(run_id, status == "COMPLETED")
    except Exception:
        logger.debug("attribute_outcome failed for run %s", run_id, exc_info=True)
    # The cost is NOT recorded here. See `reportar_coste_del_run`, which is called OUTSIDE
    # `app_write`.
    # Keep the originating ScheduledItem (Encargo / Calendar agent item)
    # in lockstep with the run outcome. Best-effort: a sync failure must
    # never break the run finalisation. Skipped when the run was cancelled
    # (not finalised here) so a late COMPLETED can't mark the item ``done``.
    if finalised:
        try:
            await _sync_item_for_run(
                session, run_id, status=status, result=result, error=error,
                artifacts=artifacts,
            )
        except Exception:
            logger.exception("scheduled-item sync failed for run %s", run_id)
    # Tell the caller whether THIS call actually finalised the run (it was still
    # RUNNING). When False the run was cancelled mid-flight — the caller MUST
    # also skip any external delivery so the kill switch stays sticky.
    return finalised


async def _sync_item_for_run(session, run_id: int, *, status: str,
                             result: dict | None = None,
                             error: str | None = None,
                             artifacts: list[dict] | None = None):
    """Mirror a run's lifecycle onto the ScheduledItem that triggered it.

    DELEGATED_TASK (Encargos) and SCHEDULED_TASK_DUE (Calendar agent
    items) events both carry ``item_id`` in their payload. Without this
    bridge an encargo stays ``in_progress`` forever when the run fails,
    is stopped by the operator, or the agent forgets to call
    ``complete_scheduled_item``. It also wires ``last_run_id`` so the SPA
    can deep-link from the board/calendar to the run (and its emails).
    """
    from app.app_models.business import ScheduledItem
    from app.app_models.runtime import Event

    event_id = (await session.execute(
        select(AgentRun.event_id).where(AgentRun.id == run_id)
    )).scalar_one_or_none()
    if not event_id:
        return
    payload_raw = (await session.execute(
        select(Event.payload_json).where(Event.id == event_id)
    )).scalar_one_or_none()
    if not payload_raw:
        return
    try:
        item_id = (json.loads(payload_raw) or {}).get("item_id")
    except Exception:
        item_id = None
    if not item_id:
        return
    item = (await session.execute(
        select(ScheduledItem).where(ScheduledItem.id == int(item_id))
    )).scalar_one_or_none()
    if item is None:
        return

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    item.last_run_id = run_id
    # Never clobber an explicit close the agent already made via
    # complete_scheduled_item (done / cancelled with a richer note) —
    # but STILL mirror the run's deliverables. The agent typically calls
    # complete_scheduled_item DURING the run (so the item is already
    # "done" here), while artifacts are only collected at _finish_run.
    # Without this the Encargos/Calendar UI shows a finished task with no
    # downloadable file even though the run produced one. See
    # _collect_run_artifacts.
    if item.status in ("done", "cancelled"):
        if status == "COMPLETED" and artifacts:
            existing = (item.artifacts_json or "").strip()
            if existing in ("", "[]", "null"):
                item.artifacts_json = json.dumps(artifacts)
        return
    if status == "RUNNING":
        item.status = "in_progress"
        if item.fired_at is None:
            item.fired_at = now
    elif status == "COMPLETED":
        item.status = "done"
        item.completed_at = now
        if not item.result_md and result:
            item.result_md = (result.get("text") or "")[:4000]
        if artifacts:
            item.artifacts_json = json.dumps(artifacts)
        # NOTE: we deliberately do NOT propagate completion to the source
        # alert/recommendation/decision here. That link is MATERIALIZED from the
        # real photo (encargo + run) when a signal is read
        # (``materialize_signal_statuses``), so it can never drift — no event/hook
        # to miss. See routes/scheduling_routes.py.
    elif status == "FAILED":
        item.status = "failed"
        item.completed_at = now
        if not item.result_md:
            # The PERSON reads this on the task card (their UI text, not an
            # instruction to the agent): it stays in the product's language.
            item.result_md = (
                f"Error: {error}" if error else "La ejecución no se completó."  # wai:es-ok
            )[:4000]


def _request_meta(*, provider_id: str, model_id: str, system_prompt: str,
                  idempotency_key: str, mode: str = "autonomous") -> str:
    """JSON blob persisted on AgentRun.request_meta_json."""
    return json.dumps({
        "provider_id": provider_id,
        "model_id": model_id,
        "system_prompt_hash": hashlib.sha256(system_prompt.encode()).hexdigest()[:16],
        "system_prompt_preview": system_prompt[:280],
        "idempotency_key": idempotency_key,
        "mode": mode,
    })
