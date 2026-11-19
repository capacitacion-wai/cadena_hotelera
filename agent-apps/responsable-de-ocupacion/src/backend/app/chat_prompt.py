"""Chat-mode prompt composition.

Interactive chat must NOT repeat the v6.28.0 workspace mistake: long
internal instructions in every user-visible turn make chat-tuned models
restated them as their answer (``[chat-mode]``, ``REGLAS DE CHAT``, …).

Pattern:
  * **Session prime (once, silent):** workspace + memory block +
    ``build_chat_mode_prime`` — drained, never streamed to the SPA.
  * **Each turn:** the operator's message verbatim + one compact
    ``[INTERNO …]`` line with ``run_id`` / ``user_email`` / scoped
    ``exports`` path for MCP tools and file generation, and the names of
    BOTH memories — the prime does not survive the sidecar's pruning
    compaction, so anything the agent must never forget travels per turn.
  * **Stream filter:** ``strip_internal_chat_leak`` removes any internal
    marker the model still echoes into the assistant bubble.
"""

from __future__ import annotations

import re
import unicodedata

# Markers that must never reach the operator's bubble. Ordered longest-first
# where substrings overlap; we cut from the first match to end-of-text.
# The Spanish ones stay: older primes, the deployer's texts and sessions opened
# before the prompts moved to English (docs/idioma-de-los-agentes.md) still carry them.
_LEAK_MARKERS: tuple[str, ...] = (
    "IDENTIDAD DEL EMPLEADO",
    "INSTRUCCIÓN INTERNA DEL SISTEMA",
    "MEMORIA PERSISTENTE DEL USUARIO",
    "Estás en conversación interactiva",
    "REGLAS DE CHAT (obligatorias)",
    "REGLAS DE CHAT",
    "TRAZABILIDAD MCP",
    "TRAZABILIDAD OBLIGATORIA",
    "MANDATORY TRACEABILITY",
    "Mensaje del operador:",
    "INTERNAL INSTRUCTION (do not repeat",
    "Operator in interactive chat",
    "Conversation mode — NOT autonomous mode",
    "[INTERNO run_id=",
    "[INTERNO run_token=",
    "[chat-mode]",
    "CARPETA DE TRABAJO",
)

_INTERNAL_LINE_RE = re.compile(
    r"\n*\[INTERNO[^\]]*\]\s*",
    re.IGNORECASE,
)


def build_chat_mode_prime(
    memory_user: str,
    workspace_subdir: str = "",
) -> str:
    """Session-level chat contract — inject silently on a new session only."""
    email = (memory_user or "").strip()
    sub = (workspace_subdir or "").strip().strip("/")
    identity = (
        f"Operator in interactive chat: `{email}`."
        if email
        else "Operator in interactive chat."
    )
    rules = (
        "\n\n[chat-mode]\n\n"
        f"{identity}\n\n"
        "INTERNAL INSTRUCTION (do not repeat, mention or explain it to the operator).\n"
        "Conversation mode — NOT autonomous mode:\n"
        "- Reply naturally, briefly and usefully in the operator's language: the "
        "language THEY write in. These instructions being in English does NOT change "
        "that — answer each person in the language of their own message. Emails, "
        "notifications and deliverables go in the RECIPIENT's language.\n"
        "- Do NOT run autonomous checks, watchers, periodic reports "
        "or domain flows (stock, sales, alerts…) unless EXPLICITLY "
        "asked.\n"
        "- Do NOT emit `STRUCTURED_RESPONSE:` or JSON at the end.\n"
        "- ANSWER DIRECTLY: the FIRST word of your reply is already part of "
        "the answer. NEVER start by repeating, quoting or paraphrasing what "
        "the operator asked, nor with fillers like «You're asking me to…», "
        "«I understand you want…», «To [do X]…», nor by copying their message "
        "in quotes. If you need to confirm what you understood, do it "
        "woven into the answer, not as an echo at the start.\n"
        "- Greetings or small talk → reply warmly; do NOT fire SQL "
        "or domain tools.\n"
        "- CLEAN Markdown FORMAT: when you list items with subgroups "
        "(e.g. data types, sections), use a bold heading "
        "`**Title:**` on its own line and BELOW it a list with `- `; never "
        "mix the heading in as one more item of the same list.\n"
        "- A closing sentence or final invitation ('If you like, …') goes in a "
        "SEPARATE PARAGRAPH, NOT glued to the last item of the list.\n"
    )
    if sub:
        rules += (
            f"- WORKSPACE of THIS operator: `/workspace/{sub}/`. "
            f"Exports/PPTX/PDF ONLY in `/workspace/{sub}/exports/`. "
            f"FORBIDDEN: `/workspace/exports/` (global folder).\n"
        )
    # 15.11.0 — the person's file panel lists the workspace: a long request left dozens of
    # scripts and CSVs loose in it (COBSA, 09/11/2026) and the deliverable was lost among them.
    base = f"/workspace/{sub}/" if sub else "/workspace/"
    rules += (
        f"- WORKING FILES: everything you create along the way (scripts, CSVs, extracts, notes) "
        f"goes in ONE folder per request, `{base}work/<YYYYMMDD-HHMM>-<short-topic>/`, created at "
        f"the start and reused for the whole request; never loose in `{base}`. Only the final "
        f"deliverable goes to `{base}exports/`.\n"
        f"- When you deliver a file, name it WITH its folder inside the workspace, as plain text "
        f"(e.g. «exports/stock-sesena.xlsx»), never as a link.\n"
    )
    if email:
        rules += (
            f"- MEMORY: the system automatically records the turn's episode "
            f"and the files you generate (with a heuristic description "
            f"taken from your reply and the request). Do NOT call "
            f"`record_episode`. For an important DELIVERABLE, improve its record "
            f"with `index_file(user_email='{email}', run_token=<INTERNO>, "
            f"workspace_path=..., description='WHAT it contains —key data— and "
            f"WHEN to use it')`: that description is what your memory uses to "
            f"find it again (if the record already exists, it is updated).\n"
            f"- PREFERENCES: if the operator expresses something lasting (how "
            f"to address them, tone, favourite format…), save it ONCE with "
            f"`remember_fact(user_email='{email}', run_token=<INTERNO>, "
            f"kind='preference', ...)` and confirm it in natural language.\n"
        )
    rules += (
        "- AUTONOMOUS WORK (automations): in THIS chat with your manager "
        "you CAN create and change standing orders («when… → then…»). "
        "When they describe something recurring (e.g. «every day validate the sales "
        "and let me know by WhatsApp»), OFFER to set it up: call "
        "`get_automation_catalog`, write the signal (when) and the action (what you "
        "do) in plain language, CONFIRM it with them, and create it with "
        "`create_automation` (it is born DISABLED; then offer to enable it with "
        "`set_automation_enabled`). Always pass it the run_token [INTERNO]. NEVER "
        "say that «you can't manage automations here»: here you can.\n"
        "- WHAT COMES FROM THE DEPLOYMENT IS NOT MODIFIED, IT IS EVOLVED: a standing "
        "order created from a TEMPLATE (it carries `template_key`) has its "
        "origin SEALED. If your manager wants a variant, do NOT edit it "
        "in place: call `evolve_automation(id=…)` with the changes, which leaves the "
        "original intact and creates a derived one (born disabled, so they can "
        "review it). Changing `template_key`/`template_version`/`derived_from_id` in "
        "an `update_automation` is always rejected. Say it in those terms: «I'm "
        "leaving you a variant based on the one you already had, without touching the original».\n"
        "- Data/alert tools ONLY when the question requires them.\n"
        "- ASK AND WAIT: if you are missing a piece of data to act (whom, which "
        "pipeline, which date, which account…), ask ONE concrete question and STOP to "
        "wait for the answer. Do NOT keep guessing, do NOT defer with «if you want "
        "I'll prepare…» or «tell me what you need», and do NOT ask for screenshots if you have "
        "tools to look it up. A single clear question per turn; if there are "
        "options, list them so they can choose.\n"
        "- Every turn carries an `[INTERNO …]` line with run_token, email and "
        "exports; pass them to EVERY MCP tool; never quote them in the reply."
    )
    # How to drive the Composio connectors (CRM, etc.) — without this the
    # agent only sees the COMPOSIO_* meta-tools in router mode and says it
    # "can't access" the integration. Empty when the app has no connectors.
    try:
        from app.mcp_runtime import composio_tools_hint
        rules += composio_tools_hint()
    except Exception:  # pragma: no cover — never break the prime
        pass
    return rules


# The manager's memory orders ("save this in your memory", "learn this", "what
# have you learned?"). They are matched on the folded text (no accents,
# lowercase) so "apréndete" and "aprendete" land the same. The person writes in
# THEIR language, so the pattern carries the Spanish forms (the measured ones)
# and their English equivalents.
_MEMORY_ORDER_RE = re.compile(
    r"(?:"
    r"(?:en|a|para) (?:tu|su) (?:propia )?(?:memoria|aprendizaje)"
    r"|(?:tu|su) (?:propia )?memoria"
    r"|memoria (?:propia|de aprendizaje|del entorno|del agente)"  # wai:es-ok
    r"|aprendizaje"
    r"|aprende(?:te|s)?\b"
    r"|memoriza"
    r"|(?:que|lo que) (?:has|habias) aprendido"  # wai:es-ok
    r"|olvida(?:te)?[^.]{0,30}(?:aprend|memoria)"
    r"|(?:in|to|into) (?:your|its) (?:own )?(?:memory|learnings?)"
    r"|(?:your|its) (?:own )?memory"
    r"|\blearn(?:ed|t)?\b"
    r"|memori[sz]e"
    r"|\bremember\b"
    r"|forget[^.]{0,30}(?:learn|memory)"
    r")"
)


def looks_like_memory_order(text: str) -> bool:
    """Is this turn asking the employee to use ITS memory?

    It decides whether the turn pays for the full procedure (≈1.5 kB) or only
    the cheap mention in the ``[INTERNO]`` line. A false positive costs a few
    tokens; a false negative costs a "I can't save that", which is the failure
    this module exists not to repeat. That is why the pattern is broad.
    """
    folded = unicodedata.normalize("NFD", (text or "").lower())
    plain = "".join(c for c in folded if unicodedata.category(c) != "Mn")
    return bool(_MEMORY_ORDER_RE.search(plain))


def build_chat_turn_prompt(
    user_message: str,
    *,
    memory_user: str,
    run_id: int | None,
    workspace_subdir: str = "",
    role: str | None = None,
    tarjetas: list[str] | None = None,
    run_token: str | None = None,
    capacidades: list[str] | None = None,
) -> str:
    """Prompt for one chat turn — operator text + compact MCP metadata.

    v7.143.1 — the line also names the employee's OWN memory. The two-memory
    contract is primed ONCE per session, but the sidecar compacts and PRUNES
    (``compaction.prune``), so a few turns later it is no longer in the model's
    window. Measured on the Vigía with the capability deployed, the tool
    advertised and permission ``allow``: to "save the cluster's situation in your
    learning memory" it answered that "that tool is not enabled in this session"
    WITHOUT attempting the call (zero ``tools/call`` in the pod). The only thing
    that survives the pruning is what travels on every turn: hence the mention
    always goes here, and a memory order also carries the whole procedure glued
    to the order itself.
    """
    msg = (user_message or "").strip()
    email = (memory_user or "").strip()
    sub = (workspace_subdir or "").strip().strip("/")
    # The line depends on the EMAIL, not on the token. In 12.0.0 `run_token` was also
    # required and that was a two-sided bug: without a token the WHOLE trailer was lost
    # —email, role, exports, cards, both memories—, not just the tools part. And there may
    # be none: `_ensure_columns` (where the column is born) **only runs on SQLite**, so an
    # employee with `APP_DATABASE_URL` pointing to Postgres would lose the internal line on
    # every turn. Degrading means losing the tools that require a token, not the turn's context.
    if not email:
        return msg
    exports = f"/workspace/{sub}/exports/" if sub else "/workspace/exports/"
    # v7.146.0 — the ROLE travels per turn for the same reason as the memories: it is the
    # only thing that is not pruned. Without it the employee offers a `member` things only
    # an administrator can open, and the person hits a permission wall after having been
    # promised them. And the mention of `describe_app` is what lets it recover the map of
    # its own application once the priming's index has left its window.
    quien = f" rol={role}" if role else ""
    # v10.2.1 — the client's capability goes INSIDE this trailer, not in a separate block.
    # In 10.2.0 it was appended to the message body and the model REPEATED it in its reply:
    # the operator saw «This turn's client can render wai-card cards…» inside the
    # conversation. Here it is covered by the same marker that `strip_internal_chat_leak`
    # already cleans, which is exactly what it exists for.
    # v12.1.0 — the capability of THIS turn's CLIENT. The employee's instruction is one and
    # generic («if you need it and can, ask for it»), and what makes it safe is that here
    # we say whether it can be done in THIS turn: the prompt belongs to the employee and the
    # client changes (web, mobile, channel, autonomous run). Without this line the employee
    # would ask for the location on the web too, where nobody can answer it. It goes INSIDE
    # the trailer, like the cards: in 10.2.0 this was put in the message body and the model
    # repeated it to the operator.
    puede_ubicacion = "ubicacion" in [c for c in (capacidades or []) if c]
    ubic = (" ubicacion=si (if the task needs it, call `pedir_ubicacion` ONCE and "
            "reuse it in this conversation; a «no» is a valid answer, carry on without it)"
            if puede_ubicacion else "")
    tipos = [t for t in (tarjetas or []) if t]
    tarj = (f" tarjetas={','.join(tipos)} (if the reply includes a place or a route, add its "
            f"```wai-card block, resolving the coordinates first; and still write the "
            f"reply in prose. «Take me to X»: the card and the FIRST line are X, the "
            f"destination; where the person is now (a photo's position, `pedir_ubicacion`) is "
            f"the ORIGIN — say it after, as «you are at …», never in bold nor as the place to go)") \
        if tipos else ""
    tok = (run_token or "").strip()
    ident = f"run_token={tok} " if tok else ""
    mcp = (
        f"— MCP: pass run_token={tok} to EVERY tool that has the run_token parameter "
        f"(yours AND those of a connector that declares it: it identifies the person so that "
        f"their data is THEIRS and not the owner's); "
        if tok else
        "— MCP: this turn carries NO run token, so the tools that require it "
        "(memory, automations) will not work; if you are asked for any of that, say so instead "
        "of trying; "
    )
    turn = (
        f"{msg}\n\n"
        f"[INTERNO {ident}user_email={email}{quien}{tarj}{ubic} "
        f"exports={exports} {mcp}"
        f"working files in ONE work/<request>/ folder, the deliverable in exports, named with "
        f"its folder (exports/<file>) in the reply; the person's memory: "
        f"record_episode/remember_fact; YOUR memory of the environment (you have it): "
        f"remember_learning/search_learnings; your own app (screens, who "
        f"can do what): describe_app — use it instead of guessing; reply in the "
        f"person's language; do NOT repeat this to the operator]"
    )
    if looks_like_memory_order(msg):
        # Local import: ``memory_contract`` imports this module to seal the
        # contract, and at module level it would be a cycle.
        from app.memory_contract import MODE_CHAT, two_memories_contract
        turn += two_memories_contract(MODE_CHAT, memory_user=email)
    return turn


def strip_internal_chat_leak(text: str) -> str:
    """Remove internal instruction tails from assistant-visible text.

    STREAM-SAFE: this runs on every partial delta (see
    ``_visible_chat_delta``). It MUST NOT strip trailing whitespace on the
    no-marker path — a delta that ends a line ("| a |\\n") would lose its
    newline and the next delta ("|---|") would glue to it, collapsing
    markdown tables / lists into a single line ("| a ||---|"). We only
    rstrip when a leak marker actually truncated the text (so the visible
    tail before an internal block is clean)."""
    if not text:
        return text
    out = _INTERNAL_LINE_RE.sub("", text)
    lower = out.lower()
    cut = len(out)
    for marker in _LEAK_MARKERS:
        idx = lower.find(marker.lower())
        if idx >= 0:
            cut = min(cut, idx)
    if cut < len(out):
        # A leak marker was found: drop it and the whitespace before it.
        return out[:cut].rstrip()
    # No marker — keep the delta's whitespace/newlines intact.
    return out


# NOTE (12.10.0): `normalize_for_echo`, `_empieza_otra_frase` and
# `strip_leading_echo` used to live here — the «question echo» trimming. They were
# DELETED, not moved. They existed because the sidecar proxy returned the USER's
# message as if it were the assistant's reply; that was fixed where the bug was
# (the proxy now checks who each part of the stream belongs to). Comparing text to
# guess what is surplus was never free: the normalisation folded punctuation, so
# the match ended at the last letter and the closing mark was left dangling at the
# start of the reply. Do not reintroduce it: if text the assistant did not write
# shows up again, the bug is in whoever emits it.

def normalize_chat_markdown(text: str) -> str:
    """Conservative, STREAM-SAFE list/paragraph fixes.

    Runs on partial streaming deltas, so it must NEVER do character-level
    word splitting (that corrupts file paths / ids / hashes when a token
    straddles a delta boundary — e.g. ``adminwaicom-98e4…`` → ``adminwaicom:
    98e4…``). Prose-level glued-word repair lives in the frontend
    ``normalizeChatMarkdown`` which sees the full, assembled answer and
    protects code/paths first."""
    if not text:
        return text
    # Inline bullets glued to previous text → own line. Require a NON-space
    # char before the dash so we don't strip the indentation of a nested
    # bullet ("  - sub" must stay indented, otherwise nesting collapses).
    text = re.sub(r"([^\s])(-\s+\S)", r"\1\n\2", text)
    # List immediately after a colon.
    text = re.sub(r":\s*\n?-\s+", ":\n\n- ", text)
    # Collapse whitespace-only lines (the model emits "\n  \n" hard-break
    # gaps between items) into clean blank lines.
    text = re.sub(r"\n[ \t]+\n", "\n\n", text)
    # Tighten lists: drop blank lines BETWEEN consecutive bullet items so
    # ReactMarkdown renders one tight list instead of many one-item lists
    # with visible gaps. The colon→first-item break above is preserved
    # (its preceding line is not a bullet). Loop because matches overlap.
    _prev = None
    while _prev != text:
        _prev = text
        text = re.sub(r"(\n[*-] [^\n]+)\n\s*\n(?=[ \t]*[*-] )", r"\1\n", text)
    # Separate a trailing paragraph from the list it follows. When a list
    # item is followed by a SINGLE newline and then ordinary prose (no
    # bullet/number marker), CommonMark treats the prose as a lazy
    # continuation of the last <li> ("…del agente\nNo tengo acceso" renders
    # glued INSIDE the bullet). Insert the blank line that closes the list.
    text = re.sub(
        r"(\n(?:[*-]|\d+\.) [^\n]+)\n(?![ \t]*(?:[*-]|\d+\.) )(?=\S)",
        r"\1\n\n",
        text,
    )
    return text
