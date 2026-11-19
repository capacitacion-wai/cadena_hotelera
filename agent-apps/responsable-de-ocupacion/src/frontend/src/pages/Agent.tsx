/**
 * Canonical chat page for every wai-agent-app (v7.1.0 rewrite).
 *
 * Composes the chat primitives under ``components/chat/`` into a
 * single screen built around the new turn-centric model:
 *
 *   UserBubble  →  AssistantTurn { steps[], finalAnswer }
 *
 * Each ``AssistantTurn`` represents one full reply: the agent's
 * reasoning + tool calls are nested inside step cards (collapsible
 * "Paso N" sections inspired by the OpenCode chat panel) and the
 * customer-facing answer is rendered once at the bottom as a clean
 * Markdown bubble. This eliminates the v6.x pattern of interleaving
 * a fresh assistant bubble after every tool group, which produced
 * the visual artefact of the user prompt being echoed inside the
 * conversation.
 *
 * Streaming contract (matches ``agent_routes.py`` R-CHAT-2):
 *   * ``session``     — proxy session id
 *   * ``text_delta``  — appended to the active step's reasoning
 *                       (or the final answer once the agent has
 *                       stopped calling tools)
 *   * ``tool_call``   — opens a tool row in the active step
 *   * ``tool_result`` — closes the matching row
 *   * ``step_done``   — closes the active step; the next text /
 *                       tool delta starts a fresh one
 *   * ``complete``    — turn finished; carries totals
 *   * ``error``       — surfaced as ``ErrorBubble`` with a retry
 *
 * The page also persists ``sessionId`` and the input draft in
 * ``localStorage`` so refreshes keep context.
 */

import { formatApiError } from '../utils/apiError'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import {
  Alert, Box, Button, Card, Chip, CircularProgress,
  Dialog, DialogActions, DialogContent, DialogContentText, DialogTitle,
  IconButton, Menu, MenuItem, Snackbar, Stack, Tab, Tabs, TextField, Tooltip, Typography,
} from '@mui/material'
import SendIcon from '@mui/icons-material/Send'
import StopIcon from '@mui/icons-material/Stop'
import KeyboardArrowDownIcon from '@mui/icons-material/KeyboardArrowDown'
import RestartAltIcon from '@mui/icons-material/RestartAlt'
import DataUsageIcon from '@mui/icons-material/DataUsage'
import CompressIcon from '@mui/icons-material/Compress'
import UnfoldMoreIcon from '@mui/icons-material/UnfoldMore'
import ViewSidebarIcon from '@mui/icons-material/ViewSidebarOutlined'
import ScreenshotMonitorIcon from '@mui/icons-material/ScreenshotMonitor'
import CloseIcon from '@mui/icons-material/Close'
import DownloadIcon from '@mui/icons-material/FileDownloadOutlined'
import MicIcon from '@mui/icons-material/Mic'
import StopCircleIcon from '@mui/icons-material/StopCircle'
import GraphicEqIcon from '@mui/icons-material/GraphicEq'
import AttachFileIcon from '@mui/icons-material/AttachFile'
import PhotoCameraIcon from '@mui/icons-material/PhotoCamera'
import MyLocationIcon from '@mui/icons-material/MyLocation'
import AddCircleOutlineIcon from '@mui/icons-material/AddCircleOutline'
import OpenInFullIcon from '@mui/icons-material/OpenInFull'
import CloseFullscreenIcon from '@mui/icons-material/CloseFullscreen'
import { useEsMovil } from '../hooks/useEsMovil'
import InsertDriveFileIcon from '@mui/icons-material/InsertDriveFileOutlined'

import {
  AssistantTurn, EmptyChat, ErrorBubble, NivelDeRazonamiento, UserBubble,
} from '../components/chat'
import type { TurnStep, TurnTool } from '../components/chat'
import { TIPOS_QUE_PINTO, partirEnTrozos } from '../components/chat/tarjetas'
// VOICE mode. It is not another screen: it is another way of writing in THIS conversation,
// so that the request made to the employee and everything that led to it stay in view
// here, as always. As a separate screen, every chat capability had to be added again by
// hand and voice was always lagging behind.
import VozEnVivo from '../components/chat/VozEnVivo'
// DICTATING is not CONVERSING: the first writes into the field and the browser does it (free
// and instant), the second opens a spoken conversation with the employee. They were the same
// button, so dictating did not exist and talking started the whole machinery unintentionally.
import { dictar, soportaDictado, type Dictado } from '../components/chat/dictado'
import type { TurnoHablado } from '../components/chat/UserBubble'
import type { SubStep } from '../components/chat/ToolStepGroup'
import { useManifest } from '../hooks/useManifest'
import type { AgentPersona } from '../hooks/useManifest'
import { PersonaAvatar } from '../components/PersonaAvatar'
import { apiFetch } from '../services/apiClient'
import SelectorDeHilos from '../components/chat/SelectorDeHilos'
import WorkspaceViewer, { WORKSPACE_DRAG_TYPE } from '../components/WorkspaceViewer'
import ScreenCaptureHelper, { screenCaptureSupported } from '../components/ScreenCaptureHelper'
import MemoryViewer from '../components/MemoryViewer'
// WHAT IS TOLD WHILE IT WORKS, in pure functions with tests (12.47.0). They used to live in
// here and that is why they could not be tested: bugs only showed up while talking.
import { dondeMira, esFontaneria, queSalio, queHace, contarTarjetas } from '../conversacion/avances'
import type { TFunction } from 'i18next'

// ─────────────────────────────────────────────────────────────────────
// Types — one entry per "turn item" so we can interleave user and
// assistant turns naturally in the timeline.
// ─────────────────────────────────────────────────────────────────────

// A file the operator attached to a turn, once it's safely in the workspace.
// Sent to the backend (workspace_path) AND kept on the user item so the bubble
// shows what was attached.
export interface ChatAttachmentMeta {
  filename: string
  workspace_path: string
  size?: number
  mime?: string
}

interface UserItem { kind: 'user'; id: string; text: string; attachments?: ChatAttachmentMeta[]
  /** The spoken conversation that led to this request (voice mode). */
  hablado?: TurnoHablado[] }

// Composer-side state for a file being/already uploaded. ``ready`` ones are
// turned into ``ChatAttachmentMeta`` and sent with the next message.
interface PendingAttachment {
  id: string
  filename: string
  size: number
  mime: string
  status: 'uploading' | 'ready' | 'error'
  workspace_path?: string
  error?: string
}

// Chat attachments are staged by the backend under the WORKING user's own
// workspace folder (sessions/users/<token>/uploads/) via the scoped
// /api/agent/workspace/chat-upload endpoint — the destination is decided
// server-side, so no client-side path constant is needed here.
// Client-side soft cap (the backend enforces the real, deploy-configured
// ceiling — this just gives instant feedback).
const CHAT_MAX_UPLOAD_MB = 25
const CHAT_MAX_UPLOAD_BYTES = CHAT_MAX_UPLOAD_MB * 1024 * 1024

function fmtBytes(n?: number): string {
  if (!n || n <= 0) return ''
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`
  return `${(n / (1024 * 1024)).toFixed(1)} MB`
}

// One streamed text fragment, tagged with the step it belongs to and
// whether the backend marked it as reasoning. Mirrors the proven "code
// agent" model: we store fragments flat and classify reasoning-vs-answer
// at RENDER time (see ``buildTurnView``) instead of guessing mid-stream.
// ``partId`` = the message part the text comes from. Grouping by ``stepIndex`` is NOT
// enough: the tail of a part —what the proxy's marker buffer held back, and in Spanish
// almost every sentence ends in «s», which is the start of `STRUCTURED_…`— can arrive
// after a ``step_done``. Then it opened a new block and the `join('\n\n')` below painted
// it alone: «…con tus viajes\n\n.».
interface TextBlock { stepIndex: number; isReasoning: boolean; text: string; partId?: string }

interface ToolRec {
  id: string
  stepIndex: number
  name: string
  status: 'pending' | 'ok' | 'error'
  args?: unknown
  result?: unknown
  startedAt: number
  finishedAt?: number
  // v7.167.0 — activity of the subagent this row spawned (``task`` only).
  // Arrives as ``subagent`` events carrying the parent's ``call_id``.
  sub?: SubStep[]
}

interface AssistantItem {
  kind: 'assistant'
  id: string
  textBlocks: TextBlock[]        // flat, in arrival order
  toolCalls: ToolRec[]           // flat, in arrival order
  maxStepIndex: number           // highest step seen (for "active" flag)
  streaming: boolean
  startedAt: number
  finishedAt?: number
  modelId?: string
  totalCost?: number
  totalCredits?: number
  /** HOW this turn was resolved, when it was not through the normal path: `atajo` with the
   *  declared capability that resolved it, or `recepcion` if the written conversation
   *  handled it without starting the agent. Empty = normal turn. Without this, «it went
   *  direct» could only be DEDUCED from the time and the credits, which is guessing. */
  resuelto?: { atajo?: string; recepcion?: string }
  /** 15.12.0 — the server's heartbeat while a step is taking long: which tool is running and
   *  for how long. Cleared by the next real event: it only says «still working on this». */
  latido?: { herramienta: string; segundos: number } | null
}

// True when a tool-arg payload actually carries content. Used to dedupe the
// pending→running re-emissions of one tool call without ever overwriting real
// args with a later empty payload.
function hasArgs(v: unknown): boolean {
  if (v == null) return false
  if (typeof v === 'string') return v.trim().length > 0
  if (typeof v === 'object') return Object.keys(v as Record<string, unknown>).length > 0
  return true
}

// Render-time classifier (the code-agent pattern). Reasoning is ONLY what the
// backend flagged as such. Plain text written at/under the last step that ran a
// tool is something the employee SAID to the person mid-work («the export
// finished and found a problem…»): it is shown in order, outside the collapsed
// steps, before the final answer (`progressTexts`). It used to count as
// reasoning and the person only saw «N steps» and never the message (30/09/2026).
// Everything after the last tool is the customer-facing final answer.
/** «Sigo trabajando · consultando el ERP (3 min)». The tool is said the way a person would
 *  (`dondeMira`), never by its technical name; with no tool running it is thinking. */
function textoDelLatido(l: { herramienta: string; segundos: number }, t: TFunction): string {
  const min = Math.max(1, Math.round(l.segundos / 60))
  const donde = l.herramienta ? dondeMira(l.herramienta) : ''
  const que = donde
    ? t('agent.chat.latido.consultando', 'consultando {{donde}}', { donde })
    : l.herramienta
      ? t('agent.chat.latido.herramienta', 'esperando el resultado de un paso')
      : t('agent.chat.latido.pensando', 'pensando')
  return t('agent.chat.latido.texto', 'Sigo trabajando · {{que}} ({{min}} min)', { que, min }) as string
}

function buildTurnView(item: AssistantItem): { steps: TurnStep[]; progressTexts: string[]; finalAnswer: string } {
  const hasTools = item.toolCalls.length > 0
  const maxToolStep = item.toolCalls.reduce((mx, t) => Math.max(mx, t.stepIndex), -1)
  const isReasoningBlock = (b: TextBlock): boolean => b.isReasoning
  const isBeforeLastTool = (b: TextBlock): boolean => hasTools && b.stepIndex <= maxToolStep

  const progressTexts = item.textBlocks
    .filter((b) => b.text.trim() && !isReasoningBlock(b) && isBeforeLastTool(b))
    .map((b) => b.text.trim())

  const answerText = item.textBlocks
    .filter((b) => b.text.trim() && !isReasoningBlock(b) && !isBeforeLastTool(b))
    .map((b) => b.text.trim())
    .join('\n\n')

  // Gather every step index that owns reasoning or a tool.
  const idxs = new Set<number>()
  item.textBlocks.forEach((b) => { if (b.text.trim() && isReasoningBlock(b)) idxs.add(b.stepIndex) })
  item.toolCalls.forEach((t) => idxs.add(t.stepIndex))
  const sorted = [...idxs].sort((a, b) => a - b)

  const steps: TurnStep[] = sorted.map((stepIndex) => ({
    id: `step-${stepIndex}`,
    index: stepIndex,
    reasoning: item.textBlocks
      .filter((b) => b.stepIndex === stepIndex && isReasoningBlock(b))
      .map((b) => b.text)
      .join(''),
    tools: item.toolCalls
      .filter((t) => t.stepIndex === stepIndex)
      .map((t): TurnTool => ({
        id: t.id,
        name: t.name,
        status: t.status,
        args: t.args,
        result: t.result,
        startedAt: t.startedAt,
        finishedAt: t.finishedAt,
        sub: t.sub,
      })),
    closed: !item.streaming || stepIndex < item.maxStepIndex,
    startedAt: item.startedAt,
  }))

  return { steps, progressTexts, finalAnswer: answerText }
}
interface ErrorItem {
  kind: 'error'; id: string; message: string;
  retryPrompt?: string;
  // v7.88.0 — when the failed turn was ANSWERING a clarifying question, keep its
  // request_id so Retry RESUMES the paused server run instead of starting a fresh
  // prompt (which would leave the paused run dangling forever).
  retryRequestId?: string;
}

// A clarifying question the agent asked (native OpenCode /question). The
// operator answers inline; the chosen option is sent back as a follow-up turn
// carrying ``requestId`` so the paused run resumes (see agent_routes).
interface QChoice {
  id: string; question: string;
  type: 'single_choice' | 'multi_choice' | 'text';
  options: string[]; header?: string; placeholder?: string; default?: string;
}
interface QuestionItem {
  kind: 'question'; id: string; requestId: string;
  questions: QChoice[];
  answered?: string;   // the answer once given (locks the widget)
}

type TurnItem = UserItem | AssistantItem | ErrorItem | QuestionItem

/** Inline widget for a clarifying question the agent asked. Renders the
 *  option(s) as buttons (single), toggle-buttons + send (multi) or a text
 *  field (free text); once answered it locks and shows the chosen answer. */
function QuestionBubble({
  item, disabled, onAnswer,
}: {
  item: QuestionItem
  disabled: boolean
  onAnswer: (requestId: string, answer: string) => void
}) {
  const { t } = useTranslation()
  const [multi, setMulti] = useState<string[]>([])
  const [text, setText] = useState('')
  const q = item.questions[0]
  if (!q) return null
  const locked = !!item.answered || disabled
  const toggle = (opt: string) =>
    setMulti((cur) => (cur.includes(opt) ? cur.filter((x) => x !== opt) : [...cur, opt]))
  const hasOptions = q.options.length > 0
  return (
    <Card variant="outlined" sx={{ p: 2, my: 1, borderColor: 'primary.light' }}>
      {q.header && <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>{q.header}</Typography>}
      <Typography variant="body2" sx={{ mb: 1.5, whiteSpace: 'pre-line' }}>{q.question}</Typography>
      {item.answered ? (
        <Chip size="small" color="primary"
          label={t('agent.question.answered', { answer: item.answered, defaultValue: 'Respondido: {{answer}}' })} />
      ) : !hasOptions || q.type === 'text' ? (
        <Stack direction="row" spacing={1}>
          <TextField
            size="small" fullWidth disabled={locked}
            placeholder={q.placeholder || t('agent.question.placeholder', 'Escribe tu respuesta…')}
            value={text} onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter' && text.trim()) onAnswer(item.requestId, text.trim()) }}
          />
          <Button variant="contained" size="small" disabled={locked || !text.trim()}
            onClick={() => onAnswer(item.requestId, text.trim())}>
            {t('agent.question.send', 'Enviar')}
          </Button>
        </Stack>
      ) : q.type === 'multi_choice' ? (
        <Stack spacing={1}>
          <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
            {q.options.map((opt) => (
              <Button key={opt} size="small" disabled={locked}
                variant={multi.includes(opt) ? 'contained' : 'outlined'}
                onClick={() => toggle(opt)} sx={{ textTransform: 'none' }}>{opt}</Button>
            ))}
          </Stack>
          <Box>
            <Button variant="contained" size="small" disabled={locked || multi.length === 0}
              onClick={() => onAnswer(item.requestId, multi.join(', '))}>
              {t('agent.question.send', 'Enviar')}
            </Button>
          </Box>
        </Stack>
      ) : (
        <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
          {q.options.map((opt) => (
            <Button key={opt} size="small" variant="outlined" disabled={locked}
              onClick={() => onAnswer(item.requestId, opt)} sx={{ textTransform: 'none' }}>{opt}</Button>
          ))}
        </Stack>
      )}
    </Card>
  )
}

// Pointer to the in-flight server-side turn (see STORAGE_ACTIVE_KEY).
interface ActiveRun { turnId: string; runId?: number | null; savedAt: number }

// Compact token count for the context-usage chip ("12.3k", "850").
function fmtTokens(n: number): string {
  if (!n || n <= 0) return '0'
  if (n < 1000) return String(n)
  return `${(n / 1000).toFixed(n < 10000 ? 1 : 0)}k`
}

const STORAGE_SESSION_KEY = 'wai-agent.session_id'
const STORAGE_INPUT_KEY = 'wai-agent.input_draft'
const STORAGE_TIMELINE_KEY = 'wai-agent.timeline'
// R-CHAT-4 (v6.66.0): pointer to the turn currently executing
// server-side. Persisted so a tab switch / refresh can re-attach to the
// detached run via GET /api/agent/chat/stream and keep streaming the
// answer in place instead of losing it. Cleared on complete / error.
const STORAGE_ACTIVE_KEY = 'wai-agent.active_run'
// V1 — bump whenever ``TurnItem`` shape changes so stale browsers
// drop incompatible payloads instead of trying to render them.
const TIMELINE_VERSION = 3
// Drop persisted timelines older than this so the chat doesn't
// resurrect day-old context the operator never expected to see.
const TIMELINE_TTL_MS = 12 * 60 * 60 * 1000  // 12h
// Hard cap so a long-running operator doesn't blow past the 5 MB
// localStorage quota — keep the most recent N turn items.
const TIMELINE_MAX_ITEMS = 80

// ─────────────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────────────

function makeId(prefix: string): string {
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`
}

// v7.88.0 — namespace the chat keys by the authenticated email so a browser
// SHARED by two operators never cross-reads history / drafts / session / active
// run. Computed on EACH access (not at module load) so a logout→login in the same
// tab switches namespace. Logout + 401 also clear every ``wai-agent.*`` key
// (AuthContext / apiClient) so nothing lingers.
function _nsSuffix(): string {
  try { return (window.localStorage.getItem('email') || 'anon').toLowerCase() } catch { return 'anon' }
}
function nsKey(base: string): string {
  return `${base}::${_nsSuffix()}`
}

function readSession(): string | null {
  try { return window.localStorage.getItem(nsKey(STORAGE_SESSION_KEY)) } catch { return null }
}
function persistSession(value: string | null) {
  try {
    if (value) window.localStorage.setItem(nsKey(STORAGE_SESSION_KEY), value)
    else window.localStorage.removeItem(nsKey(STORAGE_SESSION_KEY))
  } catch { /* ignore */ }
}
function readDraft(): string {
  try { return window.localStorage.getItem(nsKey(STORAGE_INPUT_KEY)) || '' } catch { return '' }
}
function persistDraft(value: string) {
  try {
    if (value) window.localStorage.setItem(nsKey(STORAGE_INPUT_KEY), value)
    else window.localStorage.removeItem(nsKey(STORAGE_INPUT_KEY))
  } catch { /* ignore */ }
}

/**
 * Read the persisted conversation timeline. Returns an empty array
 * (and silently discards the entry) if:
 *   • parsing fails,
 *   • the version differs from ``TIMELINE_VERSION``,
 *   • the timestamp is older than ``TIMELINE_TTL_MS``.
 *
 * R-CHAT-4 (v6.66.0): a turn that was streaming when the tab closed is
 * kept ``streaming=true`` (no longer force-finalised). The component's
 * mount effect re-attaches to the detached server-side run via
 * ``GET /api/agent/chat/stream`` and either resumes the live answer in
 * place OR finalises the bubble when the backend reports no live turn
 * (``no_active_turn``). This is what makes a refresh/tab-switch keep the
 * in-flight conversation instead of dropping it.
 */
// v12.0.0 — WHICH conversation was open, per user. Before, only the timeline was persisted
// (without a thread) and `hiloId` was reborn as 'default' on every reload: the messages of
// thread X were shown under «Conversación» and the next message went to the default thread.
// v12.1.0 — is this employee allowed to resolve a location request? Off by default and per
// user (the key is namespaced by email, like the rest). On the web the REAL permission is
// governed by the browser per origin; this is the product's decision.
const STORAGE_UBIC_KEY = 'wai-agent.ubicacion'
function readCompartirUbicacion(): boolean {
  try { return window.localStorage.getItem(nsKey(STORAGE_UBIC_KEY)) === '1' } catch { return false }
}
function persistCompartirUbicacion(v: boolean) {
  try {
    if (v) window.localStorage.setItem(nsKey(STORAGE_UBIC_KEY), '1')
    else window.localStorage.removeItem(nsKey(STORAGE_UBIC_KEY))
  } catch { /* ignore */ }
}

const STORAGE_HILO_KEY = 'wai-agent.hilo'
function readHilo(): string {
  try { return window.localStorage.getItem(nsKey(STORAGE_HILO_KEY)) || 'default' } catch { return 'default' }
}
function persistHilo(id: string) {
  try {
    if (id && id !== 'default') window.localStorage.setItem(nsKey(STORAGE_HILO_KEY), id)
    else window.localStorage.removeItem(nsKey(STORAGE_HILO_KEY))
  } catch { /* ignore */ }
}

function readTimeline(): TurnItem[] {
  try {
    const raw = window.localStorage.getItem(nsKey(STORAGE_TIMELINE_KEY))
    if (!raw) return []
    const blob = JSON.parse(raw) as { version: number; savedAt: number; items: TurnItem[] }
    if (!blob || blob.version !== TIMELINE_VERSION) return []
    if (typeof blob.savedAt !== 'number' || Date.now() - blob.savedAt > TIMELINE_TTL_MS) return []
    if (!Array.isArray(blob.items)) return []
    return blob.items
  } catch {
    return []
  }
}

function readActive(): ActiveRun | null {
  try {
    const raw = window.localStorage.getItem(nsKey(STORAGE_ACTIVE_KEY))
    if (!raw) return null
    const a = JSON.parse(raw) as ActiveRun
    if (!a || typeof a.turnId !== 'string') return null
    // Ignore a stale pointer older than the timeline TTL.
    if (typeof a.savedAt !== 'number' || Date.now() - a.savedAt > TIMELINE_TTL_MS) return null
    return a
  } catch {
    return null
  }
}
function persistActive(value: ActiveRun | null) {
  try {
    if (value) window.localStorage.setItem(nsKey(STORAGE_ACTIVE_KEY), JSON.stringify(value))
    else window.localStorage.removeItem(nsKey(STORAGE_ACTIVE_KEY))
  } catch { /* ignore */ }
}

// Append a streamed text fragment to its (stepIndex, channel) block. We
// merge contiguous same-channel deltas so the renderer sees whole
// sentences. No echo/recover heuristics — classification happens in
// buildTurnView. Pure: returns a new AssistantItem.
function appendTextBlock(
  turn: AssistantItem,
  stepIndex: number,
  delta: string,
  isReasoning: boolean,
  partId?: string,
): AssistantItem {
  const blocks = turn.textBlocks.slice()
  const last = blocks[blocks.length - 1]
  // The text of ONE part is ONE block, even if its tail arrives in another step: identity
  // rules over the counter. Without `partId` (old client or server) it is grouped as
  // always, by step.
  const misma: boolean = Boolean(last) && last.isReasoning === isReasoning && (
    (partId && last.partId) ? last.partId === partId : last.stepIndex === stepIndex
  )
  if (misma) {
    blocks[blocks.length - 1] = { ...last, text: last.text + delta }
  } else {
    blocks.push({ stepIndex, isReasoning, text: delta, partId })
  }
  return { ...turn, textBlocks: blocks, maxStepIndex: Math.max(turn.maxStepIndex, stepIndex) }
}

// Pump a fetch SSE body, parsing ``data: {json}`` lines and dispatching
// each event to ``onEvent``. Shared by the live POST /chat stream and the
// GET /chat/stream reconnect so both speak the exact same vocabulary.
// Includes the R1 client-side silence guard (throws on a stalled socket).
async function pumpReader(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  onEvent: (event: any) => void,
) {
  const decoder = new TextDecoder()
  let buffer = ''
  const SILENCE_MS = 60_000
  while (true) {
    let timer: number | undefined
    const silenceTimeout = new Promise<{ silent: true }>((resolve) => {
      timer = window.setTimeout(() => resolve({ silent: true }), SILENCE_MS)
    })
    const readPromise = reader.read().then((r) => ({ silent: false, ...r }))
    const winner: any = await Promise.race([readPromise, silenceTimeout])
    if (timer !== undefined) window.clearTimeout(timer)
    if (winner.silent) {
      try { await reader.cancel() } catch { /* ignore */ }
      throw new Error('network timeout')
    }
    const { done, value } = winner
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let nl = buffer.indexOf('\n')
    while (nl !== -1) {
      const line = buffer.slice(0, nl)
      buffer = buffer.slice(nl + 1)
      nl = buffer.indexOf('\n')
      if (!line.startsWith('data: ')) continue
      let event: any
      try { event = JSON.parse(line.slice(6)) } catch { continue }
      onEvent(event)
    }
  }
}

/** The local copy without the bulk of the tool calls (15.10.0). A conversation with large
 *  results (ERP batches, documents) went over the ~5 MB of localStorage, the write failed in
 *  silence and the conversation did not survive leaving and coming back. The text the person
 *  reads is kept whole; only arguments/results longer than this are cut. */
const TOOL_PAYLOAD_MAX = 2000
function recortar(v: unknown): unknown {
  if (v == null) return v
  const t = typeof v === 'string' ? v : JSON.stringify(v)
  return t.length > TOOL_PAYLOAD_MAX ? `${t.slice(0, TOOL_PAYLOAD_MAX)}…` : v
}
function aligerar(it: TurnItem): TurnItem {
  if (it.kind !== 'assistant') return it
  const a = it as AssistantItem
  return { ...a, toolCalls: a.toolCalls.map((tc) => ({ ...tc, args: recortar(tc.args), result: recortar(tc.result) })) }
}

function persistTimeline(items: TurnItem[]) {
  try {
    if (items.length === 0) {
      window.localStorage.removeItem(nsKey(STORAGE_TIMELINE_KEY))
      return
    }
    const trimmed = items.length > TIMELINE_MAX_ITEMS
      ? items.slice(items.length - TIMELINE_MAX_ITEMS)
      : items
    const payload = JSON.stringify({
      version: TIMELINE_VERSION,
      savedAt: Date.now(),
      items: trimmed.map(aligerar),
    })
    window.localStorage.setItem(nsKey(STORAGE_TIMELINE_KEY), payload)
  } catch {
    // Quota exceeded or storage disabled — best-effort, the
    // conversation simply won't survive a refresh.
  }
}

/** The turn still running at the end of the conversation: its question and the answer being
 *  written (15.10.0). Empty if nothing is running. A turn counts as running if its bubble is
 *  still streaming, or —without a bubble yet— if the active-run pointer is still alive. */
function turnoEnCurso(items: TurnItem[]): TurnItem[] {
  let i = items.length - 1
  while (i >= 0 && items[i].kind !== 'user') i--
  if (i < 0) return []
  const cola = items.slice(i)
  const corriendo = cola.some((it) => it.kind === 'assistant' && (it as AssistantItem).streaming)
  const sinBurbuja = !cola.some((it) => it.kind === 'assistant') && readActive() !== null
  return corriendo || sinBurbuja ? cola : []
}

/** The server's history plus the local turn in progress, without painting a question twice.
 *  If the question is already in the history WITH its answer, the turn finished meanwhile and
 *  the history wins; if it is there without an answer, the local copy (which has the answer
 *  being written) replaces it. */
function unirConEnCurso(historial: TurnItem[], enCurso: TurnItem[]): TurnItem[] {
  const pregunta = enCurso[0]
  if (!pregunta || pregunta.kind !== 'user') return historial
  let j = historial.length - 1
  while (j >= 0 && historial[j].kind !== 'user') j--
  const ultima = j >= 0 ? historial[j] : null
  if (!ultima || ultima.kind !== 'user' || ultima.text.trim() !== pregunta.text.trim()) {
    return [...historial, ...enCurso]
  }
  const respondida = historial.slice(j + 1).some((it) => it.kind === 'assistant')
  return respondida ? historial : [...historial.slice(0, j), ...enCurso]
}

// ─────────────────────────────────────────────────────────────────────
// Page
// ─────────────────────────────────────────────────────────────────────

export default function Agent() {
  const { t, i18n } = useTranslation()
  const { manifest } = useManifest()
  // Conversation threads (v9.1.0). THE KEY'S ABSENCE MEANS «NO»: one frontend talks to
  // employees of many versions, and one that has not been rebuilt has neither the table nor
  // the endpoints. Without the flag the selector is not painted and `thread_id` is not sent,
  // so the chat behaves exactly as before.
  const soportaHilos = Boolean((manifest as any)?.hilos?.soportado)
  // Does this employee HAVE a conversation module? It is decided at deploy time and it is in
  // beta, so normally it does NOT. The flag's absence means «no», just like with cards and
  // threads: painting a button that errors when pressed is worse than not offering it.
  const tieneVoz = Boolean((manifest as any)?.conversacion?.voz)
  // v12.0.0 — it starts on the last thread this person had open (persisted by email), and on
  // mount it is corrected with the «current thread» the SERVER reports, so that mobile and
  // web open the same conversation. 'default' is an alias the server resolves to the
  // caller's default conversation.
  const [hiloId, setHiloId] = useState<string>(() => readHilo())
  const [compartirUbicacion, setCompartirUbicacion] = useState<boolean>(() => readCompartirUbicacion())

  // Manifest values may be a plain string or a {es,en} locale dict
  // (e.g. agent.tagline). Resolve to the active language so we never
  // render an object as a React child (React #31).
  const pickLoc = (v: any): string => {
    if (v == null) return ''
    if (typeof v === 'string') return v
    if (typeof v === 'object') {
      const lang = (i18n.language || 'es').slice(0, 2)
      return v[lang] || v.es || v.en || (Object.values(v)[0] as string) || ''
    }
    return String(v)
  }

  const persona = ((manifest as any)?.agent?.persona ?? null) as AgentPersona | null
  const functionalName = pickLoc((manifest as any)?.agent?.display_name)
    || (manifest as any)?.app_name
    || t('agent.chat.fallbackName')
  // Headline name = persona first name when present, else the agent name.
  const agentName = (persona?.name || '').trim() || functionalName
  const agentTagline =
    pickLoc(persona?.role)
    || pickLoc((manifest as any)?.agent?.tagline)
    || pickLoc((manifest as any)?.agent?.description)
    || ''
  const agentGreeting = pickLoc(persona?.greeting)
  const modelId: string | undefined = (manifest as any)?.agent?.model

  const capabilityCards = useMemo(() => {
    const caps = (manifest as any)?.agent?.capabilities
    if (Array.isArray(caps) && caps.length > 0) {
      return caps.slice(0, 4).map((c: any) => ({
        title: c.title || c.label || c.name || '',
        description: c.description || c.summary || '',
        prompt: c.prompt || c.example || c.title || '',
      })).filter((c: any) => c.title && c.prompt)
    }
    const ex = (manifest as any)?.agent?.example_prompts
    if (Array.isArray(ex) && ex.length > 0) {
      return ex.slice(0, 4).map((p: string) => ({
        title: p, description: '', prompt: p,
      }))
    }
    return [
      { title: t('agent.chat.example1'), description: '', prompt: t('agent.chat.example1') },
      { title: t('agent.chat.example2'), description: '', prompt: t('agent.chat.example2') },
      { title: t('agent.chat.example3'), description: '', prompt: t('agent.chat.example3') },
      { title: t('agent.chat.example4'), description: '', prompt: t('agent.chat.example4') },
    ]
  }, [manifest, t])

  // U7: items[] is now persisted to localStorage so the chat
  // survives tab navigations and refreshes. Restored timelines
  // have their ``streaming`` flag force-cleared (see readTimeline)
  // so a turn that was mid-stream when the tab closed doesn't
  // resurrect as a permanent spinner.
  const [items, setItems] = useState<TurnItem[]>(() => readTimeline())
  // The most recent conversation, for the reattach listeners: they are registered once
  // and with `items` in the closure they would always see the list from the first render —
  // exactly the one that is useless for knowing whether something was left half-done.
  const itemsRef = useRef(items)
  useEffect(() => { itemsRef.current = items }, [items])
  const [input, setInput] = useState<string>(readDraft())
  // DICTATING. While it lasts, the button is a square and what is recognised keeps falling
  // into the field; on stopping it stays there to be read and corrected. It is not sent by
  // itself: dictated text is read before sending, especially if proper names are involved.
  const [dictando, setDictando] = useState(false)
  const dictadoVivo = useRef<Dictado | null>(null)
  // What was written BEFORE starting: dictation APPENDS, it does not overwrite. Whoever
  // dictates in the middle of a message does not want to lose what they had.
  const textoPrevio = useRef('')
  // Resolved ONCE: asking on every render does not change the answer and the button would
  // flicker on the first paint.
  const puedeDictar = useMemo(() => soportaDictado(), [])
  const esMovil = useEsMovil()
  // The camera and the location are MOBILE things: on a desktop there is no rear camera to
  // open nor a position worth anything, so there they would only get in the way.
  const camaraRef = useRef<HTMLInputElement | null>(null)
  const [ubicando, setUbicando] = useState(false)
  // A SHORT NOTICE, the one in the bottom bar. It was born for the location and was named
  // after it; now dictation shares it, since it has the same two failures to tell
  // —permission denied and «this does not work here»— and deserves the same spot, not another piece of screen.
  const [avisoBreve, setAvisoBreve] = useState<string>('')
  const [menuAdjuntar, setMenuAdjuntar] = useState<HTMLElement | null>(null)
  // Write comfortably: enlarges the composer without changing screen or losing what was
  // written. Useful on mobile (where three lines are nothing) and on desktop (where a long
  // task is written through a slit).
  const [compositorGrande, setCompositorGrande] = useState(false)
  // 12.22.0 — how much the employee thinks on the NEXT message. It starts at the level the
  // person has set (the control itself reads it) and can be changed before each send.
  // Empty = the employee's own: nothing is sent and it runs with whatever it has configured.
  const [nivelRazonamiento, setNivelRazonamiento] = useState('')

  /** Puts the current position into the message, as text and with a Maps link.
   *
   *  It goes to the COMPOSER and not straight to the agent on purpose: that way you see
   *  what is going to be sent before sending it. Sharing where you are is one of the few
   *  things that should not happen without you reading it first.
   *
   *  The Maps link is added because the employee already knows how to use it —it
   *  recommends with Maps— so it does not have to rebuild it from two numbers. */
  const anadirUbicacion = useCallback(() => {
    if (!navigator.geolocation) {
      setAvisoBreve(t('agent.chat.location.unsupported',
        'Este navegador no da la ubicación.') as string)
      return
    }
    setUbicando(true)
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        setUbicando(false)
        const { latitude: la, longitude: lo, accuracy: pr } = pos.coords
        const maps = `https://www.google.com/maps/search/?api=1&query=${la},${lo}`
        const txt = t('agent.chat.location.text',
          'Mi ubicación actual: {{lat}}, {{lon}} (±{{m}} m) — {{maps}}',
          { lat: la.toFixed(6), lon: lo.toFixed(6), m: Math.round(pr || 0), maps }) as string
        setInput((prev) => (prev ? `${prev.trimEnd()}\n${txt}` : txt))
      },
      (err) => {
        setUbicando(false)
        // A denied permission is told apart from a GPS failure: the person fixes the first
        // in their browser, the second is fixed by waiting.
        setAvisoBreve((err.code === err.PERMISSION_DENIED
          ? t('agent.chat.location.denied',
              'No has dado permiso de ubicación. Se activa en los ajustes del navegador.')
          : t('agent.chat.location.failed',
              'No se ha podido obtener la ubicación. Inténtalo de nuevo en unos segundos.')) as string)
      },
      { enableHighAccuracy: true, timeout: 15000, maximumAge: 30000 },
    )
  }, [t])
  const [loading, setLoading] = useState(false)
  const [sessionId, setSessionId] = useState<string | null>(readSession())
  // Files the operator is attaching to the NEXT message (uploaded to the
  // workspace as they're picked; ``ready`` ones ride along on send).
  const [attachments, setAttachments] = useState<PendingAttachment[]>([])
  const fileInputRef = useRef<HTMLInputElement | null>(null)
  // Drag a workspace entry (panel "Mi workspace") onto the input to reference
  // it — same gesture as the code agent. The drop inserts the agent-side
  // ``/workspace/…`` path at the cursor, the convention the agent already
  // resolves for chat attachments.
  const chatInputRef = useRef<HTMLTextAreaElement | null>(null)
  const [dropHot, setDropHot] = useState(false)
  // "Help with your application": capture screens of ANOTHER window with the Screen
  // Capture API and attach them with a question. Generic on purpose (no system names):
  // it works for any employee.
  const [captureOpen, setCaptureOpen] = useState(false)
  const onShotsReady = (files: File[]) => {
    if (!files.length) return
    void handleFiles(files)
    setInput((prev) => prev || t('agent.chat.capture.prefill', 'Sobre estas pantallas de mi aplicación: '))
    requestAnimationFrame(() => chatInputRef.current?.focus())
  }
  /** DICTATE: start, or stop if it was already running. One button with two faces, like the
   *  record button of any app — two buttons for the same thing force you to check which one is active. */
  const alternarDictado = useCallback(() => {
    if (dictadoVivo.current) {
      dictadoVivo.current.cancelar()
      return
    }
    textoPrevio.current = input.trim()
    setDictando(true)
    dictadoVivo.current = dictar({
      onTexto: (t2) => setInput(textoPrevio.current ? `${textoPrevio.current} ${t2}` : t2),
      onFin: (t2) => {
        dictadoVivo.current = null
        setDictando(false)
        if (t2) setInput(textoPrevio.current ? `${textoPrevio.current} ${t2}` : t2)
        // Focus goes back to the field: dictating almost always ends in «and now I add this».
        requestAnimationFrame(() => chatInputRef.current?.focus())
      },
      onFallo: (motivo) => setAvisoBreve(motivo),
    })
  }, [input])

  // Leaving the chat with the microphone open would leave it listening.
  useEffect(() => () => { dictadoVivo.current?.cancelar() }, [])

  const insertWorkspaceRef = useCallback((agentPath: string) => {
    const el = chatInputRef.current
    setInput((prev) => {
      const start = el?.selectionStart ?? prev.length
      const end = el?.selectionEnd ?? start
      const before = prev.slice(0, start)
      const after = prev.slice(end)
      const sep = before && !/\s$/.test(before) ? ' ' : ''
      const next = `${before}${sep}${agentPath} ${after}`
      requestAnimationFrame(() => {
        if (!el) return
        const pos = (before + sep + agentPath + ' ').length
        el.setSelectionRange(pos, pos)
        el.focus()
      })
      return next
    })
  }, [])
  // U8: "Limpiar contexto" — modeled on the OpenCode chat panel
  // (see frontend-react/src/i18n/es/opencode.ts → clearContext).
  // Opens a confirmation dialog so an accidental click doesn't
  // wipe the operator's working transcript.
  const [clearOpen, setClearOpen] = useState(false)
  const [clearing, setClearing] = useState(false)
  // «Compactar» (compact) — mirror of the code agent (v7.95.0): summarises the session IN
  // PLACE in the OpenCode pod to make the next messages cheaper without deleting the
  // visible conversation. The warning threshold is in ABSOLUTE tokens: the cost per
  // message grows with the re-sent context, not with the % of the window.
  const CTX_EXPENSIVE_TOKENS = 150_000
  const [compacting, setCompacting] = useState(false)
  const [compactResult, setCompactResult] = useState<'success' | 'stale' | 'error' | null>(null)
  // v12.4.0 — how many messages at the top were left BEFORE the last compaction. They are
  // folded into one line, as in the code agent: compacting summarised the thread for the
  // model and left the screen just as long, so the conversation became heavy to look at
  // right when you had just lightened it inside. And the fold marks where the employee lost
  // the detail, which until now was not visible anywhere.
  //
  // Nothing is lost: it is one click away, and behind it is the server's history.
  const [plegados, setPlegados] = useState(0)
  const [verAnteriores, setVerAnteriores] = useState(false)
  /**
   * Switch conversation.
   *
   * What is shown is emptied and the new thread's history is fetched FROM THE SERVER: that
   * is what makes a thread started on mobile continue here. What arrives is the text, not
   * the process —tools and reasoning only exist in the live channel—, so a reopened thread
   * looks thinner than when it happened, and it is better not to pretend otherwise.
   */
  // `forzar` (v12.0.0): reload from the server EVEN IF it is the current thread. It is what
  // makes what was said on mobile show up on mount: before, `id === hiloId` was a no-op and
  // the open thread was never requested from the server — only this browser's local cache was painted.
  const cambiarDeHilo = async (id: string, forzar = false) => {
    if (!forzar && id === hiloId) return
    // Reloading the SAME thread (the mount, `forzar`) must not lose the turn that is still
    // running (15.10.0). The server's history only has FINISHED turns, so emptying the screen
    // here and painting the history dropped the question in progress and its half-written
    // answer, and the reattach had no bubble left to write into: leaving and coming back
    // mid-answer lost the conversation. That tail is kept and painted after the history.
    const mismo = id === hiloId
    const enCurso = mismo ? turnoEnCurso(itemsRef.current) : []
    setHiloId(id)
    persistHilo(id)
    setItems(enCurso)
    if (!mismo) setSessionId(null)
    try {
      // What this interface can paint is declared: the server stores the RAW text and prunes
      // on emitting, so without this the reopened conversation would lose its cards.
      const r = await apiFetch(
        `/api/agent/threads/${encodeURIComponent(id)}/history`
        + `?tarjetas=${encodeURIComponent(TIPOS_QUE_PINTO.join(','))}`,
      )
      if (!r.ok) return
      const d = await r.json()
      const turnos: any[] = Array.isArray(d?.turnos) ? d.turnos : []
      // Up to where the employee only remembers a summary. The SERVER says it (it travels with
      // the history), so the fold survives a reload and appears the same even if the
      // compaction was done from mobile.
      const compactadoEn = typeof d?.compactado_en === 'string' ? Date.parse(d.compactado_en) : NaN
      // TYPED ON PURPOSE. This used to be `any[]` and the rebuilt turn carried an INVENTED
      // shape (`steps: [{stepIndex, blocks}]`) that does not exist in `AssistantItem`: when
      // painting it, `AssistantTurn` did `step.tools.filter(...)` on `undefined` and the whole
      // screen crashed with «Cannot read properties of undefined». The `any` is what let that
      // through to production, so the fix is not just the shape: it is removing the `any`.
      const recuperados: TurnItem[] = []
      turnos.forEach((tu, i) => {
        if (tu?.pregunta) recuperados.push({ kind: 'user', id: `h-u-${i}`, text: String(tu.pregunta) })
        if (tu?.respuesta) {
          recuperados.push({
            kind: 'assistant',
            id: `h-a-${i}`,
            textBlocks: [{ stepIndex: 0, isReasoning: false, text: String(tu.respuesta) }],
            toolCalls: [],
            maxStepIndex: 0,
            streaming: false,
            startedAt: Date.now(),
          })
        }
      })
      // The question in progress may already be in the history (asked, not yet answered):
      // it is painted once, from the local tail, which also has the answer being written.
      // If it finished while the person was away, the history already has it whole and the
      // local tail is dropped (the server's copy is the complete one).
      // With the CURRENT screen, not the copy taken at the start: while the history was loading
      // the reattach kept writing the answer, and that text must not be overwritten. The tail is
      // located by the id of its question, so it is kept even if it finished meanwhile.
      setItems((prev) => {
        const k = enCurso.length ? prev.findIndex((it) => it.id === enCurso[0].id) : -1
        return unirConEnCurso(recuperados, k >= 0 ? prev.slice(k) : [])
      })
      // It folds up to the LAST turn before the mark. It is counted over the painted items
      // (question and answer are two), not over the turns, or the cut would fall
      // mid-conversation.
      if (!Number.isNaN(compactadoEn)) {
        let corte = 0
        turnos.forEach((tu, i) => {
          const cuando = Date.parse(String(tu?.cuando || ''))
          const cuantos = (tu?.pregunta ? 1 : 0) + (tu?.respuesta ? 1 : 0)
          void i
          if (!Number.isNaN(cuando) && cuando < compactadoEn) corte += cuantos
        })
        setPlegados(Math.min(corte, recuperados.length))
      } else {
        setPlegados(0)
      }
      setVerAnteriores(false)
    } catch {
      // Without history you can still talk: the thread exists anyway and the next turn starts
      // its session. Emptying and not being able to refill beats mixing two conversations.
    }
  }

  const compactContext = async () => {
    if (compacting) return
    setCompacting(true)
    try {
      const r = await apiFetch(
        `/api/agent/chat/compact${soportaHilos ? `?thread_id=${encodeURIComponent(hiloId)}` : ''}`,
        { method: 'POST' })
      if (!r.ok) throw new Error(String(r.status))
      // `stale`: the native session died with a restart of the agent pod — the counter was
      // stale and there was nothing to compact; the next message starts a fresh session by
      // itself. It is not an error.
      const data = await r.json().catch(() => ({ compacted: true }))
      setCtxTokens(0)
      // What is on screen NOW is exactly what the employee has just summarised.
      // Only if it really compacted: with `stale` nothing was summarised (its session no
      // longer existed), and folding then would say it lost a detail nobody lost.
      if (data.compacted) {
        setPlegados(items.length)
        setVerAnteriores(false)
      }
      setCompactResult(data.compacted ? 'success' : 'stale')
    } catch {
      setCompactResult('error')
    } finally {
      setCompacting(false)
    }
  }

  // Context-window usage for THIS operator's session. Shown in the
  // header so the operator knows how full the session is (and can reset
  // it before it grows too large). Seeded from the backend on mount and
  // refreshed live from every ``complete`` event's token totals.
  const [ctxTokens, setCtxTokens] = useState<number>(0)
  const [ctxMessages, setCtxMessages] = useState<number>(0)
  const [ctxRotateMax, setCtxRotateMax] = useState<number>(40)

  // Per-user side panel ("Mi workspace" / "Mi memoria"). The caller's
  // hashed workspace folder + memory key come from session/current
  // (the frontend cannot derive the hashed token itself).
  const [panelOpen, setPanelOpen] = useState(false)
  const [panelTab, setPanelTab] = useState(0)
  const [workspaceSubdir, setWorkspaceSubdir] = useState<string>('')
  const [memoryUser, setMemoryUser] = useState<string>('')

  useEffect(() => persistDraft(input), [input])
  useEffect(() => persistHilo(hiloId), [hiloId])
  useEffect(() => persistCompartirUbicacion(compartirUbicacion), [compartirUbicacion])
  useEffect(() => persistSession(sessionId), [sessionId])
  useEffect(() => persistTimeline(items), [items])

  useEffect(() => {
    let alive = true
    // v12.0.0 — the SERVER decides which conversation opens (the last one used, or the
    // default, which always exists) and its history is fetched from it. Before, the web only
    // restored its local cache and never asked for the open thread: what was said on mobile
    // did not appear here, and each reload went back to 'default' with another thread's timeline.
    ;(async () => {
      let id = hiloId
      try {
        const r = await apiFetch('/api/agent/threads/current')
        if (r.ok) {
          const d = await r.json()
          if (typeof d?.id === 'string' && d.id) id = d.id
        }
      } catch { /* without the server we carry on with the local state */ }
      if (!alive) return
      // Reload from the server EVEN IF it matches the current thread (`forzar`): the local
      // state is only the first paint; what is on the server rules.
      void cambiarDeHilo(id, true)
      try {
        const q = soportaHilos ? `?thread_id=${encodeURIComponent(id)}` : ''
        const r = await apiFetch(`/api/agent/session/current${q}`)
        if (!r.ok) return
        const d = await r.json()
        if (!alive) return
        setCtxTokens(Number(d.context_tokens) || 0)
        setCtxMessages(Number(d.message_count) || 0)
        if (d.rotate_max_messages) setCtxRotateMax(Number(d.rotate_max_messages))
        if (typeof d.workspace_subdir === 'string') setWorkspaceSubdir(d.workspace_subdir)
        if (typeof d.memory_user === 'string') setMemoryUser(d.memory_user || '')
      } catch { /* non-fatal: header just shows "new session" */ }
    })()
    return () => { alive = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // ── Auto-scroll that respects manual scroll-up ───────────────────
  const scrollRef = useRef<HTMLDivElement>(null)
  const stickToBottomRef = useRef<boolean>(true)
  const [showJump, setShowJump] = useState(false)

  const onScroll = useCallback(() => {
    const el = scrollRef.current
    if (!el) return
    const distance = el.scrollHeight - (el.scrollTop + el.clientHeight)
    const atBottom = distance < 40
    stickToBottomRef.current = atBottom
    setShowJump(!atBottom)
  }, [])

  useEffect(() => {
    if (!stickToBottomRef.current) return
    const el = scrollRef.current
    if (!el) return
    el.scrollTop = el.scrollHeight
  }, [items])

  const jumpToBottom = () => {
    const el = scrollRef.current
    if (!el) return
    el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
    stickToBottomRef.current = true
    setShowJump(false)
  }

  // ── State helpers ────────────────────────────────────────────────
  const updateItem = useCallback(<T extends TurnItem>(id: string, updater: (it: T) => T) => {
    setItems((prev) => prev.map((it) => (it.id === id ? updater(it as T) : it)))
  }, [])

  // VOICE mode. While it is open it takes over the screen and the chat stays behind as the
  // record: each request the voice decides to make enters here as a real turn, with the
  // spoken conversation that led to it hanging from the request.
  const [modoVoz, setModoVoz] = useState(false)
  // What the employee is using NOW, in the person's language, for the orbit.
  const [enMarcha, setEnMarcha] = useState<string[]>([])
  // How the voice learns that the turn finished: `sendMessage` returns nothing, so the turn
  // leaves its promise here and `complete` resolves it with the text.
  const esperaVoz = useRef<((r: { texto: string; requestId?: string }) => void) | null>(null)
  /** The employee's turn that was left PAUSED waiting for an answer. */
  const preguntaEnPausa = useRef('')
  // The turn's FINAL ANSWER, to say it out loud. It keeps the LAST part of the message, not
  // all of them: concatenating produced the agent's process («Voy a verificar la fecha
  // local… Compararé transporte público…») and that is what was passed to the voice to
  // read, instead of the conclusion. Each new part resets the accumulator, which is exactly
  // how the chat groups the text blocks.
  const textoDelTurnoRef = useRef('')
  const parteDelTurnoRef = useRef<string | null>(null)
  // The PROGRESS UPDATE said out loud while the employee works.
  //
  // I do not make it up: the employee already narrates what it does, in Spanish and about
  // the real work («the official lookup found two notices for 18 September: European
  // Mobility Week and works on Line 6»). That arrives as intermediate
  // parts of the message, and until now they were discarded —only the last one survived,
  // which is the final answer— and the gap was filled with an «I am looking on the
  // internet» that says nothing. Each part that closes without being the last is a real
  // progress update, told by whoever is doing it.
  // Three sources, a single channel, in this order of preference:
  //   1. what the employee WRITES while it works (the person's language, about the real work);
  //   2. the TOOL it has just called, with its arguments — it arrives on START, so it is the
  //      only one that gives a sense of work at the moment it happens;
  //   3. what it is THINKING, which comes in English and the voice itself translates.
  // The minimum gap between sentences is set by the voice; here it is only given material.
  /** What the employee asks from the device while you talk, for the machine to carry. */
  const [pideAlAparato, setPideAlAparato] = useState<
    { n: number; que: string; paraQue: string } | null>(null)
  const peticionDelAparato = useRef('')
  /** What was read from the device, tied to the id of the request that asked for it. It goes
   *  with an id because the machine may close on a deadline while the browser keeps looking
   *  for the signal: without this, that late reading would answer the NEXT request with a
   *  position nobody has just authorised. */
  const datoDelAparato = useRef<{ id: string; datos: Record<string, unknown> } | null>(null)
  const [avanceVoz, setAvanceVoz] = useState<
    { n: number; texto: string; fuente: 'dice' | 'herramienta' } | null>(null)
  const nAvance = useRef(0)
  // What each tool was asked, waiting for its result so the two halves can be told
  // together.
  const enCursoRef = useRef(new Map<string, string>())
  const cuenta = (texto: string, fuente: 'dice' | 'herramienta') => {
    // ONLY what the employee WRITES for the person is said aloud while it works (01/10/2026).
    // Narrating its tools («I am looking for X… it returned three results») is plumbing: it
    // stays visible in the chat, never in the speaker. The still-alive notice is the machine's.
    if (fuente !== 'dice') return
    if (!esperaVoz.current || texto.trim().length < 12) return
    nAvance.current += 1
    setAvanceVoz({ n: nAvance.current, texto: texto.trim(), fuente })
  }

  const appendItem = useCallback((it: TurnItem) => {
    setItems((prev) => [...prev, it])
  }, [])

  // ── Send ─────────────────────────────────────────────────────────
  // Abort the LOCAL SSE reader when the operator navigates away. Since
  // v6.66.0 the run executes in a DETACHED server-side task (see
  // ``chat_runtime.py``), so aborting the reader only stops us pumping
  // into unmounted state — it does NOT cancel the agent. The turn keeps
  // going and we re-attach to it on the next mount (see the reconnect
  // effect below), which is what stops a tab switch / refresh from
  // losing the in-flight answer.
  const abortRef = useRef<AbortController | null>(null)
  // The reattach is defined further down (it needs `makeHandleEvent`), but `sendMessage`
  // has to be able to call it. It is passed by reference instead of reordering half the
  // file.
  const reengancharRef = useRef<((turnId: string) => void) | null>(null)
  // When the last piece of data arrived through the stream. It is the only thing that tells
  // a LIVE stream from a HUNG one, and that difference is what was missing: when the page is
  // suspended, the phone leaves the request up in the air —it neither answers nor fails— and
  // `abortRef` stays set forever.
  const ultimoDatoRef = useRef<number>(0)
  // v7.86.0 — distinguishes a USER "Stop" (finalise the turn) from an unmount /
  // tab-switch abort (leave it streaming so the reconnect effect re-attaches).
  const stoppedByUserRef = useRef(false)
  useEffect(() => () => { abortRef.current?.abort() }, [])

  // Server-side sessions: the operator's OpenCode session is owned by
  // the backend (keyed by email), so a page reload keeps context.
  // "Limpiar contexto" therefore can't just drop the local id — it must
  // tell the backend to ROTATE the session on the next send. This ref
  // carries that one-shot intent into the next /api/agent/chat call.
  const pendingResetRef = useRef(false)

  // Event-handler factory shared by the live POST /chat stream and the
  // GET /chat/stream reconnect. ``rebuild=true`` (reconnect path) clears
  // the optimistic partial restored from localStorage on the first real
  // event so the server buffer becomes the single source of truth.
  /** READS THE BROWSER. Nothing else: it neither asks anyone for permission nor answers anyone.
   *
   *  It always returns something —the data or the reason— and with its OWN cap below the
   *  server's (8 s): a browser that never resolves would leave the promise hanging and the
   *  employee would run out its wait without knowing why. */
  const leerUbicacion = useCallback(() => new Promise<Record<string, unknown>>((listo) => {
    if (!('geolocation' in navigator)) { listo({ no_disponible: 'sin_senal' }); return }
    let contestado = false
    const unaVez = (cuerpo: Record<string, unknown>) => {
      if (contestado) return
      contestado = true
      listo(cuerpo)
    }
    window.setTimeout(() => unaVez({ no_disponible: 'sin_senal' }), 6000)
    navigator.geolocation.getCurrentPosition(
      (pos) => unaVez({
        lat: pos.coords.latitude,
        lon: pos.coords.longitude,
        precision: pos.coords.accuracy ?? undefined,
        antiguedad_s: Math.max(0, Math.round((Date.now() - pos.timestamp) / 1000)),
      }),
      (err) => unaVez({
        // The browser tells «you won't let me» from «I can't», and the employee deserves to know which.
        no_disponible: err.code === err.PERMISSION_DENIED ? 'sin_permiso' : 'sin_senal',
      }),
      { enableHighAccuracy: false, timeout: 5500, maximumAge: 60000 },
    )
  }), [])

  /** ANSWERS the employee, which waits inside its call. It is the ONLY gate: in writing it
   *  is used by `resolverUbicacion`, and when talking by `onAparato` when the machine closes
   *  the request. An id is valid only once. */
  const contestarUbicacion = useCallback((peticionId: string, cuerpo: Record<string, unknown>) => {
    if (!peticionId) return
    void apiFetch(`/api/agent/location/${encodeURIComponent(peticionId)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(cuerpo),
    }).catch(() => { /* if not even answering is possible, the server will run out its wait */ })
  }, [])

  /** The WRITTEN gate: with nobody talking there is nobody to ask at the moment, so the
   *  setting rules. It NEVER throws: whatever happens, something is answered. */
  const resolverUbicacion = useCallback(async (peticionId: string) => {
    if (!peticionId) return
    if (!compartirUbicacion) { contestarUbicacion(peticionId, { no_disponible: 'rechazado' }); return }
    contestarUbicacion(peticionId, await leerUbicacion())
  }, [compartirUbicacion, contestarUbicacion, leerUbicacion])

  const makeHandleEvent = useCallback(
    (turnId: string, userMsg: string, rebuild: boolean) => {
      const updateTurn = (updater: (a: AssistantItem) => AssistantItem) => {
        updateItem<AssistantItem>(turnId, updater)
      }
      let fallbackStep = 0
      let conLatido = false
      let didRebuild = !rebuild
      const ensureRebuild = () => {
        if (didRebuild) return
        didRebuild = true
        updateTurn((m) => ({
          ...m, textBlocks: [], toolCalls: [], maxStepIndex: 0, streaming: true,
        }))
      }

      return (event: any) => {
        // Stream sign of life: stamped on EVERY event. It is what makes it possible to tell
        // «it is slow» from «it is hung» when coming back from the background.
        ultimoDatoRef.current = Date.now()
        // First real event of a reconnect wipes the stale partial before
        // we replay the authoritative server buffer.
        if (event.type !== 'session' && event.type !== 'no_active_turn') {
          ensureRebuild()
        }
        // Any real event means it is no longer «waiting»: the heartbeat line goes away.
        if (conLatido && event.type !== 'latido') {
          conLatido = false
          updateTurn((m) => ({ ...m, latido: null }))
        }
        switch (event.type) {
          case 'latido': {
            conLatido = true
            updateTurn((m) => ({ ...m, latido: {
              herramienta: String(event.herramienta || ''), segundos: Number(event.segundos) || 0 } }))
            break
          }
          case 'session': {
            if (event.session_id) setSessionId(event.session_id)
            break
          }
          case 'no_active_turn': {
            // Reconnect found no live run (it finished and aged out, or
            // never existed). Finalise the bubble keeping whatever partial
            // localStorage had, so we never spin forever.
            updateTurn((m) => ({ ...m, streaming: false, finishedAt: m.finishedAt ?? Date.now() }))
            persistActive(null)
            break
          }
          case 'text_delta': {
            const delta: string = event.content || ''
            if (!delta) break
            const stepIdx: number = Number.isFinite(event.stepIndex) ? event.stepIndex : fallbackStep
            const isReasoning = Boolean(event.isReasoning)
            const partId: string | undefined = event.partId || undefined
            if (!isReasoning) {
              if (partId !== parteDelTurnoRef.current) {
                // The previous part has closed. If it had something and we are talking,
                // that is a REAL progress update and it is said out loud.
                const previo = textoDelTurnoRef.current.trim()
                if (previo.length > 30) cuenta(previo, 'dice')
                parteDelTurnoRef.current = partId ?? null
                textoDelTurnoRef.current = ''
              }
              textoDelTurnoRef.current += delta
            }
            // REASONING IS NOT SAID OUT LOUD, and until 20/09/2026 it was.
            //
            // It was sent to the voice with the instruction «it may come in English: tell it
            // in Spanish, in ONE short sentence». It read it as is, header and all:
            // «Clarifying route request — I need to interpret "desorganizar" as a likely
            // typo for "organizar"…». Out loud, in English, in the middle of the wait.
            //
            // The way out is not reinforcing the warning —a rule that has to be repeated to
            // be obeyed is not a rule—: it is that of the three progress sources this was the
            // ONLY one that is neither in the person's language nor about the work. What the
            // employee WRITES and what it ASKS a tool for are, and with that there is plenty of
            // material to keep company. The reasoning is still visible in the chat, which is
            // where it can be skimmed and nobody shouts it at you.
            updateTurn((m) => appendTextBlock(m, stepIdx, delta, isReasoning, partId))
            break
          }
          case 'tool_call': {
            const stepIdx: number = Number.isFinite(event.stepIndex) ? event.stepIndex : fallbackStep
            const realId: string | undefined = event.partId || event.call_id
            const toolName: string = event.name || 'tool'
            const incomingArgs = event.args ?? event.arguments
            // For the voice-mode orbit: the place where it is looking, said the way a person
            // would say it. A tool name spinning around the orb tells nobody anything.
            setEnMarcha((v) => {
              const d = dondeMira(toolName)
              return d && !v.includes(d) ? [...v, d].slice(-4) : v
            })
            // And what it is looking for, with its arguments: «the route from X to Y on the
            // map» says much more than «I am consulting». It arrives on the tool's START, so
            // saying it in the present tense is true.
            // What GOES IN is kept to pair it with what COMES OUT: announcing the call says
            // «I am looking for X»; with the result the whole story can be told —«I looked for
            // X and there are three options»—, which is what really looks like someone
            // working and telling about it.
            if (incomingArgs && !esFontaneria(toolName)) {
              const pide = queHace(toolName, incomingArgs)
              if (pide) enCursoRef.current.set(String(realId || toolName), pide)
              cuenta(pide, 'herramienta')
            }
            updateTurn((m) => {
              // Dedupe: OpenCode re-emits ONE invocation as its state advances
              // (pending→running) on the SAME call id, the first time often
              // with empty args. Merge those repeats into a single row instead
              // of stacking a ghost (the "tool printed twice, one empty" bug).
              // Match by id when present, else the most recent still-pending
              // call with the same name+step (backends without a call id).
              const tools = m.toolCalls.slice()
              let idx = -1
              if (realId) idx = tools.findIndex((t) => t.id === realId)
              if (idx < 0) {
                for (let i = tools.length - 1; i >= 0; i -= 1) {
                  if (tools[i].status === 'pending' && tools[i].name === toolName && tools[i].stepIndex === stepIdx) { idx = i; break }
                }
              }
              if (idx >= 0) {
                const prev = tools[idx]
                tools[idx] = {
                  ...prev,
                  id: realId || prev.id,
                  // The later "running" event fills the args in; never let a
                  // subsequent empty payload blank a value we already have.
                  args: hasArgs(incomingArgs) ? incomingArgs : prev.args,
                }
                return { ...m, maxStepIndex: Math.max(m.maxStepIndex, stepIdx), toolCalls: tools }
              }
              return {
                ...m,
                maxStepIndex: Math.max(m.maxStepIndex, stepIdx),
                toolCalls: [...tools, {
                  id: realId || makeId('tool'),
                  stepIndex: stepIdx,
                  name: toolName,
                  status: 'pending' as const,
                  args: incomingArgs,
                  startedAt: Date.now(),
                }],
              }
            })
            break
          }
          case 'tool_result': {
            const stepIdx: number = Number.isFinite(event.stepIndex) ? event.stepIndex : fallbackStep
            const callId = event.partId || event.call_id
            {
              // The other half of the story. The voice is given the whole pair —what it asked
              // for and what it got back— and it tells it in one sentence. The result is
              // trimmed hard: it can bring thousands of lines, and what is needed is just
              // enough to know whether something came out and how big.
              const clave = String(callId || event.name || '')
              const pidio = enCursoRef.current.get(clave)
              if (pidio) {
                enCursoRef.current.delete(clave)
                const salida = event.result ?? event.content
                // Not one line of JSON in the material: only whether something came out and how much.
                if (!esFontaneria(String(event.name || ''), salida)) {
                  cuenta(`${pidio.replace(/\.$/, '')} ${queSalio(salida, Boolean(event.error))}.`, 'herramienta')
                }
              }
            }
            updateTurn((m) => {
              // Match the most recent still-pending call (backend sends no
              // id, so fall back to name+step). Mutate that one in place.
              const tools = m.toolCalls.slice()
              for (let i = tools.length - 1; i >= 0; i -= 1) {
                const tc = tools[i]
                const matches = callId
                  ? tc.id === callId
                  : (tc.status === 'pending' && tc.name === event.name && tc.stepIndex === stepIdx)
                if (!matches) continue
                tools[i] = {
                  ...tc,
                  status: event.error ? 'error' : 'ok',
                  result: event.result ?? event.content,
                  finishedAt: Date.now(),
                }
                break
              }
              return { ...m, toolCalls: tools }
            })
            break
          }
          case 'subagent': {
            // Nest under the ``task`` row that spawned it. The backend sends
            // the parent's call id; if that row hasn't been created yet
            // (events can interleave) we attach to the most recent pending
            // ``task`` instead of dropping the update.
            const parentId: string = event.call_id || ''
            const kind = event.kind === 'text' ? 'text' : 'tool'
            updateTurn((m) => {
              const tools = m.toolCalls.slice()
              let idx = parentId ? tools.findIndex((tc) => tc.id === parentId) : -1
              if (idx < 0) {
                for (let i = tools.length - 1; i >= 0; i -= 1) {
                  if (tools[i].name === 'task' && tools[i].status === 'pending') { idx = i; break }
                }
              }
              if (idx < 0) return m
              const prev = tools[idx]
              const sub = (prev.sub ?? []).slice()
              const stepId: string = event.call || ''
              const status: SubStep['status'] =
                event.status === 'completed' ? 'ok'
                  : event.status === 'error' ? 'error'
                    : 'pending'
              // A subagent part is RE-EMITTED as its state advances
              // (pending→running→completed), the first time usually with empty
              // args. Match by part id; if the backend sent none, fall back to
              // the most recent still-pending row of the same kind+name. Not
              // doing this stacked a ghost row per state change — the chat
              // showed `bash` three times, one of them argument-less.
              let at = stepId ? sub.findIndex((s) => s.id === stepId && s.kind === kind) : -1
              if (at < 0 && !stepId) {
                for (let i = sub.length - 1; i >= 0; i -= 1) {
                  if (sub[i].kind === kind && sub[i].name === event.name
                      && sub[i].status === 'pending') { at = i; break }
                }
              }
              const hasSubArgs = event.args && Object.keys(event.args).length > 0
              const next: SubStep = {
                id: stepId || `${kind}-${sub.length}`,
                kind,
                name: event.name || undefined,
                status: kind === 'tool' ? status : undefined,
                // Never let a later empty payload blank args we already have.
                args: hasSubArgs ? event.args : (at >= 0 ? sub[at].args : undefined),
                text: event.text || undefined,
                at: Date.now(),
              }
              if (at >= 0) sub[at] = { ...sub[at], ...next, id: sub[at].id }
              else sub.push(next)
              tools[idx] = { ...prev, sub }
              return { ...m, toolCalls: tools }
            })
            break
          }
          case 'step_done': {
            const stepIdx: number = Number.isFinite(event.stepIndex) ? event.stepIndex : fallbackStep
            fallbackStep = stepIdx + 1
            updateTurn((m) => ({
              ...m,
              maxStepIndex: Math.max(m.maxStepIndex, stepIdx + 1),
              modelId: event.model || m.modelId,
            }))
            break
          }
          case 'location_request': {
            // v12.1.0 — the employee asks WHERE you are. It does NOT stop the turn: it waits
            // inside its call with a short cap, so an answer must ALWAYS be given —with
            // coordinates or with a «no»—; staying silent only makes it run out of time without knowing why.
            //
            // On the web the permission is governed by the BROWSER (per origin) and cannot be
            // pre-granted from a setting: the first time its dialog appears. That is why here it
            // is automatic from the second time on, and on mobile from the start.
            // BY VOICE IT IS NOT RESOLVED AUTOMATICALLY: there you have to be told first and
            // your answer awaited, which is what the machine does. In writing it stays the same
            // —it resolves by itself if you have allowed it—. They are not two paths for the
            // same thing: they are two gates with different rules, and the difference is consent.
            if (modoVoz && tieneVoz) {
              peticionDelAparato.current = String(event.request_id || '')
              setPideAlAparato((p) => ({ n: (p?.n || 0) + 1, que: 'ubicacion',
                paraQue: String(event.motivo || 'saber dónde estás') }))
              break
            }
            void resolverUbicacion(String(event.request_id || ''))
            break
          }
          case 'question': {
            // The agent asked a clarifying question — render an interactive
            // widget after the (possibly empty) reply bubble. The turn ends
            // right after (the run is paused until the operator answers).
            const qs = Array.isArray(event.questions) ? event.questions : []
            if (qs.length) {
              appendItem({
                kind: 'question',
                id: makeId('q'),
                requestId: event.request_id || '',
                questions: qs,
              })
              // And IF WE WERE TALKING, the voice has to be told this is NOT the answer: it is
              // a question with the employee's turn waiting behind it. Answering it as a new
              // turn left it hanging forever — measured on 20/09/2026 with «what kind of
              // dinner do you prefer?».
              preguntaEnPausa.current = String(event.request_id || '')
            }
            break
          }
          case 'complete': {
            // If you are talking, the answer is said. The text stays whole in the chat:
            // what is heard accompanies it, it does not replace it.
            // If the request came from talking, the answer is returned here to the voice so
            // it says it. The text stays whole in the chat: what is heard accompanies it, it
            // does not replace it.
            if (esperaVoz.current) {
              // The answer is split with the SAME function the chat uses: the prose on one
              // side and the cards on the other. Two reasons, and both have been suffered:
              // reading the block out loud would be dictating a JSON, and keeping quiet about
              // a map leaves the listener not knowing it exists.
              const trozos = partirEnTrozos((textoDelTurnoRef.current || '').trim())
              const prosa = trozos.filter((x) => x.que === 'texto')
                .map((x) => (x as { texto: string }).texto).join('\n').trim()
              // FIVE CARDS ARE NOT FIVE SENTENCES. They were listed one by one and joined with
              // «and», so with five places in the answer the speaker said «a map with the
              // address and a map with the address and a map with the address and a map with
              // the address and a map with the address» (21/09/2026). What has to be said is
              // HOW MANY there are, not repeating the same sentence: they are counted by type
              // and said in one.
              const tarjetas = trozos.filter((x) => x.que === 'tarjeta')
                .map((x) => ((x as { tarjeta: { tipo: string } }).tarjeta.tipo === 'ruta'
                  ? 'ruta' as const : 'lugar' as const))
              const coletilla = contarTarjetas(tarjetas, {
                ruta: t('voz.tarjetaRuta', 'un mapa con la ruta'),
                rutas: t('voz.tarjetasRuta', '{{n}} mapas con las rutas'),
                lugar: t('voz.tarjetaLugar', 'un mapa con la dirección'),
                lugares: t('voz.tarjetasLugar', '{{n}} mapas con las direcciones'),
              })
              const enPausa = preguntaEnPausa.current
              preguntaEnPausa.current = ''
              // WHAT IS ON THE SCREEN IS A FACT, NOT AN ORDER FOR IT.
              //
              // A bracket used to go here telling it «Say it naturally —"I've left you a
              // map…"— and do NOT read coordinates out loud». It read it out loud, whole, and
              // that was inevitable: the response opened with «Say EXACTLY this, without adding
              // or removing anything», so we were giving it an order inside a text we had told
              // it to read word for word. Two kinds of thing through the same channel, and the
              // model has no way to tell them apart.
              //
              // Now it is one more plain sentence of what has to be told. How it is told —and
              // that a bracket is never an order— lives in the server's contract, which is
              // where the rules live.
              esperaVoz.current({
                texto: prosa + (coletilla
                  ? '\n\n' + t('voz.hayEnPantalla', 'Además, en el chat le has dejado {{que}}.')
                      .replace(/\{\{que\}\}/g, coletilla)
                  : ''),
                ...(enPausa ? { requestId: enPausa } : {}),
              })
              esperaVoz.current = null
            }
            setEnMarcha([])
            updateTurn((m) => ({
              ...m,
              streaming: false,
              finishedAt: Date.now(),
              modelId: event.model || m.modelId,
              totalCost: typeof event.cost === 'number' ? event.cost : m.totalCost,
              totalCredits: typeof event.credits === 'number' ? event.credits : m.totalCredits,
              resuelto: (event?.medicion?.atajo || event?.medicion?.recepcion)
                ? { atajo: event.medicion.atajo || undefined,
                    recepcion: event.medicion.recepcion || undefined }
                : m.resuelto,
            }))
            persistActive(null)
            // Refresh the context-usage indicator: the last turn's input
            // tokens already include the replayed history, so input+output
            // ≈ the context currently held by the session.
            {
              const tin = Number(event?.tokens?.input) || 0
              const tout = Number(event?.tokens?.output) || 0
              if (tin + tout > 0) setCtxTokens(tin + tout)
              setCtxMessages((n) => n + 1)
            }
            break
          }
          case 'error': {
            // The agent's pod did not come back in time (it restarts to apply a new connector):
            // say that, not «stream transport: All connection attempts failed».
            // 15.6.0 — the turn reached its ceiling (`chat_turn_max_minutes`): the backend
            // already STOPPED the run and says so with a stable code, so the text is keyed on
            // the code, never on the message.
            const msg: string = event.code === 'TURN_TIME_LIMIT'
              ? t('agent.chat.turnTimeLimit', {
                  min: event.minutes,
                  defaultValue: 'He detenido esta tarea porque superó el tiempo máximo de respuesta ({{min}} min). Un administrador del empleado puede subirlo en Comportamiento; también puedes dividir la tarea en partes.',
                })
              : /stream transport/i.test(event.message || '')
              ? t('agent.chat.agentRestarting', 'El empleado se está actualizando para usar un conector que acabas de conectar. Vuelve a intentarlo en un minuto.')
              : (event.message || 'Unknown error')
            // R-CHAT-3 (v7.2): defensive belt-and-braces. The
            // backend self-heals session-404s transparently, but
            // if the SPA ever sees one bubble through (e.g. the
            // sidecar is permanently gone, or this is an older
            // backend without the auto-retry) we still wipe the
            // cached session id so the next Retry click starts a
            // fresh conversation rather than re-sending the
            // ghost id.
            if (/unknown to simple API|session.*not found|stream 404/i.test(msg)) {
              setSessionId(null)
              pendingResetRef.current = true
              try { window.localStorage.removeItem(nsKey(STORAGE_SESSION_KEY)) } catch { /* ignore */ }
            }
            updateTurn((m) => ({ ...m, streaming: false, finishedAt: m.finishedAt ?? Date.now() }))
            persistActive(null)
            appendItem({
              kind: 'error', id: makeId('err'),
              message: msg,
              retryPrompt: userMsg || undefined,
            })
            break
          }
          default:
            break
        }
      }
    },
    [updateItem, appendItem, resolverUbicacion],
  )

  // Upload each picked file into the workspace as it's selected, tracking
  // per-file status so the composer can show progress / errors before send.
  const handleFiles = async (fileList: FileList | File[] | null) => {
    const files = fileList ? Array.from(fileList) : []
    for (const file of files) {
      const id = makeId('att')
      setAttachments((cur) => [...cur, {
        id, filename: file.name, size: file.size, mime: file.type || '', status: 'uploading',
      }])
      if (file.size > CHAT_MAX_UPLOAD_BYTES) {
        setAttachments((cur) => cur.map((a) => (a.id === id
          ? { ...a, status: 'error', error: t('agent.chat.attach.tooLarge', { mb: CHAT_MAX_UPLOAD_MB }) }
          : a)))
        continue
      }
      try {
        const fd = new FormData()
        fd.append('files', file, file.name)
        // Scoped endpoint: the backend stages the file under the WORKING
        // user's OWN workspace folder (sessions/users/<token>/uploads/), not a
        // shared uploads/chat/. The destination is derived server-side from the
        // authenticated identity, so we send only the file.
        const resp = await apiFetch('/api/agent/workspace/chat-upload', { method: 'POST', body: fd })
        const data = resp.ok ? await resp.json().catch(() => null) : null
        const rel: string | undefined = Array.isArray(data?.uploaded) ? data.uploaded[0] : undefined
        if (!resp.ok || !rel) {
          const reason = data?.rejected?.[0]?.reason
          setAttachments((cur) => cur.map((a) => (a.id === id
            ? { ...a, status: 'error', error: reason === 'too_large'
                ? t('agent.chat.attach.tooLarge', { mb: CHAT_MAX_UPLOAD_MB })
                : t('agent.chat.attach.failed') }
            : a)))
          continue
        }
        setAttachments((cur) => cur.map((a) => (a.id === id
          ? { ...a, status: 'ready', workspace_path: rel }
          : a)))
      } catch {
        setAttachments((cur) => cur.map((a) => (a.id === id
          ? { ...a, status: 'error', error: t('agent.chat.attach.failed') }
          : a)))
      }
    }
  }

  const removeAttachment = (id: string) =>
    setAttachments((cur) => cur.filter((a) => a.id !== id))

  // v7.86.0 — Stop button: cancel the in-flight turn. Tells the SERVER to stop
  // (mark the detached turn terminal + abort the OpenCode sidecar so compute is
  // freed and billing stops), THEN aborts this tab's SSE reader.
  //
  // v7.88.0 — only finalise the turn when the server CONFIRMS the stop
  // (``{ok:true}``). If the abort fails (network / 5xx) or returns ``{ok:false}``
  // the run is STILL alive server-side, so we must NOT cut the SSE + clear the
  // active pointer (that would hide a running, still-billing turn as "done").
  // Instead we surface the error and leave the turn streaming (reconnectable).
  const stopMessage = async () => {
    if (!loading) return
    let confirmed = false
    try {
      // v12.0.0 — WITH the thread: without it, «Parar» (stop) aborted the default scope even if
      // you were in another thread — the real turn kept running (and billing) and the button «did nothing».
      const resp = await apiFetch(
        `/api/agent/chat/abort${soportaHilos ? `?thread_id=${encodeURIComponent(hiloId)}` : ''}`,
        { method: 'POST' },
      )
      const body = resp.ok ? await resp.json().catch(() => null) : null
      confirmed = !!(resp.ok && body && body.ok === true)
    } catch {
      confirmed = false
    }
    if (confirmed) {
      // Server marked the turn terminal — now finalise this tab (the AbortError
      // branch finalises because stoppedByUserRef is set).
      stoppedByUserRef.current = true
      abortRef.current?.abort()
    } else {
      appendItem({
        kind: 'error', id: makeId('err'),
        message: t('agent.chat.stopFailed', 'No se pudo detener; el agente sigue trabajando. Reinténtalo.'),
      })
    }
  }

  /** The person-friendly name of the place where it is looking. '' = not worth painting. */
  // dondeMira, esFontaneria, esDeLaMaquina, queSalio and queHace moved to
  // `conversacion/avances.ts` (12.47.0). What DECIDES what comes out of the speaker has to be
  // testable, and in here there was no way: bugs only showed up while talking.

  const sendMessage = async (raw: string, opts: {
    requestId?: string; hablado?: TurnoHablado[]
    /** Where the conversation happens: it decides how much it writes (by voice, briefly) and who drafts. */
    canal?: string
    /** What the conversationalist recognised on reading: capability marked [DIRECTO], its
     *  data and how much thought it believes it needs. It names; the server executes. */
    reconocido?: { capacidad?: string; argumentos?: Record<string, unknown>; razonamiento?: string }
  } = {}) => {
    const userMsg = (raw ?? '').trim()
    // Ride-along attachments: only the ones that finished uploading.
    const metas: ChatAttachmentMeta[] = attachments
      .filter((a) => a.status === 'ready' && a.workspace_path)
      .map((a) => ({
        filename: a.filename, workspace_path: a.workspace_path as string,
        size: a.size, mime: a.mime || undefined,
      }))
    // A turn needs either text or at least one ready attachment.
    if ((!userMsg && metas.length === 0) || loading) return
    setInput('')
    persistDraft('')
    setAttachments([])
    const userId = makeId('user')
    appendItem({ kind: 'user', id: userId, text: userMsg,
                 attachments: metas.length ? metas : undefined, hablado: opts.hablado })
    setLoading(true)
    stickToBottomRef.current = true

    const turnId = makeId('asst')
    appendItem({
      kind: 'assistant',
      id: turnId,
      textBlocks: [],
      toolCalls: [],
      maxStepIndex: 0,
      streaming: true,
      startedAt: Date.now(),
      modelId,
    })
    // Record the in-flight turn so a tab switch / refresh can re-attach
    // to the detached server-side run instead of losing it.
    persistActive({ turnId, savedAt: Date.now() })

    textoDelTurnoRef.current = ''
    parteDelTurnoRef.current = null
    enCursoRef.current.clear()
    const handleEvent = makeHandleEvent(turnId, userMsg, false)
    const updateTurn = (updater: (a: AssistantItem) => AssistantItem) => {
      updateItem<AssistantItem>(turnId, updater)
    }

    try {
      const controller = new AbortController()
      abortRef.current = controller
      // The sign of life starts HERE, not with the first event: otherwise, sending a
      // message and switching apps in the same second would cut a stream that had just
      // been born, because the mark would still be the previous turn's.
      ultimoDatoRef.current = Date.now()
      const reset = pendingResetRef.current
      pendingResetRef.current = false
      const resp = await apiFetch('/api/agent/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: userMsg, session_id: sessionId, reset,
          // What THIS interface can paint. The server prunes by this list, so not declaring
          // it amounts to asking for the answer without cards — and the employee, which gets
          // the list in the turn, would know it makes no sense to emit them here.
          tarjetas: [...TIPOS_QUE_PINTO],
          ...(soportaHilos ? { thread_id: hiloId } : {}),
          // What this client can DO in this turn. Without this the employee does not even
          // consider asking for the location, which is right when it cannot be given.
          capacidades: compartirUbicacion ? ['ubicacion'] : [],
          // How much THIS message is asked to think. It is only sent when there is a choice:
          // without the field, the employee applies the person's level or its own.
          ...(nivelRazonamiento ? { razonamiento: nivelRazonamiento } : {}),
          ...(metas.length ? { attachments: metas } : {}),
          // When answering a clarifying question, route this turn as a reply
          // so the paused run resumes instead of starting a new prompt.
          ...(opts.requestId ? { request_id: opts.requestId } : {}),
          // Where the conversation happens. Without this the employee cannot know that by
          // voice it must answer briefly, nor that there the conversationalist drafts, not it.
          canal: opts.canal || (esMovil ? 'movil' : 'web'),
          ...(opts.reconocido?.capacidad ? { capacidad: opts.reconocido.capacidad } : {}),
          ...(opts.reconocido?.argumentos ? { argumentos: opts.reconocido.argumentos } : {}),
          ...(opts.reconocido?.razonamiento
            ? { razonamiento_sugerido: opts.reconocido.razonamiento } : {}),
          // WHAT WAS SAID EARLIER by voice. The reception resolves on its own the turns it can
          // —asking for the missing datum— and those do not go through the employee, so they
          // are not in its memory: without this only the last sentence reached it and it
          // answered with whatever it remembered from another conversation (22/09/2026). It
          // was already being collected in the client and thrown away here.
          ...(opts.hablado?.length ? { hablado: opts.hablado } : {}),
        }),
        signal: controller.signal,
      })
      if (!resp.ok || !resp.body) {
        // The reason, not the number. A bare «HTTP 503» is what someone opening the chat of
        // a freshly deployed employee saw —the agent takes a few seconds longer than its
        // application— and it says neither what is happening nor what to do. The backend
        // sends a `detail` written to be read; if there were none, it falls back to the code.
        const motivo = await resp.json().then((d) => d?.detail).catch(() => null)
        if (resp.status === 409 && motivo?.code === 'TURN_IN_PROGRESS') {
          // 15.11.0 — the previous turn of this conversation is still working on the server
          // (this tab had lost it). Nothing is replaced: the message goes back to the box, the
          // bubble just opened re-attaches to the LIVE turn, and the person sees the work again.
          setItems((prev) => prev.filter((it) => it.id !== userId))
          setInput(userMsg)
          persistDraft(userMsg)
          appendItem({
            kind: 'error', id: makeId('err'),
            message: t('agent.chat.turnInProgress',
              'Sigo con tu mensaje anterior. Te he dejado el nuevo en la caja: envíalo cuando termine o pulsa Detener.') as string,
          })
          reengancharRef.current?.(turnId)
          return false
        }
        appendItem({
          kind: 'error', id: makeId('err'),
          // A `{code, message}` detail is said in the person's language (`errors.api.<code>`).
          message: (motivo && formatApiError(motivo, t)) || `HTTP ${resp.status}`,
          retryPrompt: userMsg, retryRequestId: opts.requestId })
        updateTurn((m) => ({ ...m, streaming: false, finishedAt: Date.now() }))
        persistActive(null)
        setLoading(false)
        return false
      }

      await pumpReader(resp.body.getReader(), handleEvent)

      // Stream ended cleanly: belt-and-braces finalise (the ``complete``
      // event already cleared the active pointer).
      updateTurn((m) => ({ ...m, streaming: false, finishedAt: m.finishedAt ?? Date.now() }))
      persistActive(null)
      return true
    } catch (err: any) {
      if (err?.name === 'AbortError') {
        // A USER "Stop" (v7.86.0) FINALISES the turn — stop the spinner but keep
        // the partial answer already streamed. An unmount / tab-switch abort
        // instead LEAVES the turn ``streaming`` + the active pointer set, so the
        // mount reconnect effect re-attaches to the still-alive server run.
        if (stoppedByUserRef.current) {
          stoppedByUserRef.current = false
          updateTurn((m) => ({ ...m, streaming: false, finishedAt: m.finishedAt ?? Date.now() }))
          persistActive(null)
        }
        return true  // the send was accepted (stream started); not a failure
      } else if (readActive()?.turnId === turnId) {
        // NETWORK CUT with the turn still alive ON THE SERVER.
        //
        // Before, this painted an error, finalised the bubble and —worst of all— deleted
        // the pointer to the run (`persistActive(null)`). The server kept working and
        // storing the answer in its buffer, but the client had just thrown away the only
        // way of finding it: the answer existed and nobody was ever going to pick it up.
        //
        // On a phone that happens constantly —switching from wifi to data, a tunnel, a
        // lift, or the 60 s of silence of `pumpReader`—, and that is why the chat seemed
        // «very sensitive to the network»: it was not that it went down, it gave up.
        //
        // Now the turn is LEFT in `streaming` with the pointer set: the reattach (when
        // coming back to the foreground or recovering the network) reattaches to the live
        // run and continues where it was.
        reengancharRef.current?.(turnId)
        return true
      } else {
        appendItem({
          kind: 'error', id: makeId('err'),
          message: err?.message ? String(err.message) : 'network error',
          retryPrompt: userMsg, retryRequestId: opts.requestId,
        })
        updateTurn((m) => ({ ...m, streaming: false, finishedAt: m.finishedAt ?? Date.now() }))
        persistActive(null)
        return false
      }
    } finally {
      abortRef.current = null
      setLoading(false)
    }
  }

  // ── Reconnect to a detached run on mount (R-CHAT-4) ───────────────
  // Re-attach to the operator's in-flight turn so refreshing the page or
  // switching tabs resumes the live answer in place rather than dropping
  // it. ``from_index=0`` replays the full server buffer; the handler
  // clears the stale optimistic partial on its first real event.
  /** Reattaches to the live turn on the server. `true` = resolved (nothing pending any
   *  more), `false` = recoverable failure, worth a retry.
   *
   *  The distinction is the heart of the fix: before, ANY failure finalised the bubble and
   *  deleted the pointer, so a network hiccup was treated the same as a finished turn —
   *  and the answer, still alive on the server, was lost forever. */
  const reconnectToActiveTurn = useCallback(async (turnId: string): Promise<boolean> => {
    setLoading(true)
    stickToBottomRef.current = true
    const handleEvent = makeHandleEvent(turnId, '', true)
    const finalise = () => {
      updateItem<AssistantItem>(turnId, (m) => ({
        ...m, streaming: false, finishedAt: m.finishedAt ?? Date.now(),
      }))
      persistActive(null)
    }
    try {
      const controller = new AbortController()
      abortRef.current = controller
      // The sign of life starts HERE, not with the first event: otherwise, sending a
      // message and switching apps in the same second would cut a stream that had just
      // been born, because the mark would still be the previous turn's.
      ultimoDatoRef.current = Date.now()
      // v12.0.0 — WITH the thread: the reattach without `thread_id` went to the default
      // scope; in another thread it returned `no_active_turn`, the bubble closed and the real
      // answer kept arriving at the server without anyone painting it.
      const resp = await apiFetch(
        `/api/agent/chat/stream?from_index=0${soportaHilos ? `&thread_id=${encodeURIComponent(hiloId)}` : ''}`, {
        signal: controller.signal,
      })
      // 5xx or no body: the server is unwell NOW, it is not that the turn does not exist.
      // It is retried instead of giving the answer up for lost.
      if (resp.status >= 500 || !resp.body) return false
      if (!resp.ok) { finalise(); return true }
      await pumpReader(resp.body.getReader(), handleEvent)
      // It finished cleanly: either `complete` arrived (the handler already cleared the
      // pointer) or the server said `no_active_turn`. In both cases nothing is left.
      finalise()
      return true
    } catch (err: any) {
      // Abort = someone took over (unmount, «Parar», another reattach):
      // neither finalise nor retry.
      if (err?.name === 'AbortError') return true
      // Everything else is network: the turn is LEFT alive for the next attempt.
      return false
    } finally {
      abortRef.current = null
      setLoading(false)
    }
  }, [makeHandleEvent, updateItem])

  // ── Reattach with retries ────────────────────────────────────────
  //
  // It retries because the most common reason for losing the thread on a phone is
  // temporary: switching from wifi to data, a tunnel, the browser suspending the page.
  // Giving up on the first attempt is giving up just when the network is about to come
  // back.
  //
  // The delays grow (1 s, 2 s, 4 s, 8 s, 15 s) so as not to hammer the server during a
  // long outage, and it stops at five: if after ~30 s there is no way, the problem is not
  // a hiccup.
  const reenganchandoRef = useRef(false)
  const reengancharConReintentos = useCallback(async (turnId: string) => {
    // One at a time: without this, coming back to the foreground with the network
    // recovering fires two reattaches at once and the second steps on the first.
    if (reenganchandoRef.current) return
    reenganchandoRef.current = true
    const esperas = [1000, 2000, 4000, 8000, 15000]
    try {
      for (let i = 0; i < esperas.length; i += 1) {
        // If while we waited the turn stopped being alive (`complete` arrived, or the
        // user stopped it), there is nothing to reattach.
        // It stops if the turn stopped being alive (`complete` arrived, or the user
        // stopped it). With an expired pointer it carries on anyway: the bubble is
        // spinning and has to be closed one way or another.
        const puntero = readActive()
        if (puntero && puntero.turnId !== turnId) return
        const ok = await reconnectToActiveTurn(turnId)
        if (ok) return
        await new Promise((r) => setTimeout(r, esperas[i]))
      }
      // Attempts exhausted: the bubble is finalised so it does not spin forever, but it
      // SAYS it was left half-done instead of pretending it finished — the answer may still
      // be alive on the server.
      updateItem<AssistantItem>(turnId, (m) => ({
        ...m, streaming: false, finishedAt: m.finishedAt ?? Date.now(),
      }))
      persistActive(null)
      appendItem({
        kind: 'error', id: makeId('err'),
        message: t('agent.chat.reconnect.failed',
          'Se perdió la conexión mientras respondía. Vuelve a preguntar o recarga la página para recuperar la respuesta.') as string,
      })
    } finally {
      reenganchandoRef.current = false
    }
  }, [reconnectToActiveTurn, updateItem, appendItem, t])

  useEffect(() => { reengancharRef.current = (id) => { void reengancharConReintentos(id) } },
    [reengancharConReintentos])

  // ── When to reattach ─────────────────────────────────────────────
  //
  // Before, ONLY on mount, and that was the serious mobile bug: when coming back from
  // another app the page is RESTORED, not remounted, so the effect never ran and the turn
  // stayed hung asking to reconnect — with the answer already finished and waiting on the
  // server.
  //
  // Now it is attempted on mount AND every time there is reason to believe what was
  // missing has come back: the tab returns to the foreground, or the browser recovers the
  // network.
  const didResumeRef = useRef(false)
  useEffect(() => {
    const intentar = () => {
      if (reenganchandoRef.current) return
      // `abortRef` being set does NOT prove the stream is alive, and believing it did was
      // the bug: when the page is suspended, the phone leaves the request up in the air
      // —it neither answers nor fails, and its timers freeze too—, so on coming back it
      // was still «in progress» forever and the reattach refused to act. It kept asking to
      // reconnect until the page was reloaded.
      //
      // Now the sign of life rules: if no data has arrived for more than 45 s —with the
      // server's heartbeat every 20 s, that is already a dead channel—, it is cut and
      // reattached. Cutting a stream that was healthy breaks nothing: the reattach asks for
      // the buffer from the beginning and the server replays it whole.
      const mudo = Date.now() - ultimoDatoRef.current > 45_000
      if (abortRef.current && !mudo) return
      if (abortRef.current && mudo) {
        try { abortRef.current.abort() } catch { /* da igual: ya estaba muerto */ }
        abortRef.current = null
      }
      // The pointer rules, but it EXPIRES (`readActive` discards old ones). If it has
      // expired and the conversation still has a spinning turn, it must reattach anyway:
      // the server will answer `no_active_turn` and the bubble will close. Without this
      // second path, that turn kept spinning forever and there was no way to stop it
      // except reloading.
      const colgado = [...itemsRef.current].reverse().find(
        (it): it is AssistantItem => it.kind === 'assistant' && (it as AssistantItem).streaming,
      )
      const turnId = readActive()?.turnId ?? colgado?.id
      if (!turnId) return
      void reengancharConReintentos(turnId)
    }

    if (!didResumeRef.current) {
      didResumeRef.current = true
      intentar()
    }

    const alVolver = () => { if (document.visibilityState === 'visible') intentar() }
    document.addEventListener('visibilitychange', alVolver)
    window.addEventListener('online', intentar)
    // `pageshow` covers iOS bfcache: when going «back» or from the app switcher, the page
    // is restored whole and fires nothing else.
    window.addEventListener('pageshow', intentar)
    return () => {
      document.removeEventListener('visibilitychange', alVolver)
      window.removeEventListener('online', intentar)
      window.removeEventListener('pageshow', intentar)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reengancharConReintentos])

  // ── Retry helper ─────────────────────────────────────────────────
  const retryFromError = useCallback((errorId: string, prompt: string, requestId?: string) => {
    setItems((prev) => prev.filter((it) => it.id !== errorId))
    // v7.88.0 — carry the question's request_id through Retry so a failed
    // answer RESUMES the paused run instead of starting a fresh prompt.
    void sendMessage(prompt, requestId ? { requestId } : {})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // ── Regenerate ───────────────────────────────────────────────────
  // Re-run the user message that produced an assistant turn: drop that
  // turn (and anything after it) and resend the preceding user prompt.
  const regenerateTurn = useCallback((assistantId: string) => {
    if (loading) return
    setItems((prev) => {
      const idx = prev.findIndex((it) => it.id === assistantId)
      if (idx < 0) return prev
      let userText = ''
      let userIdx = -1
      for (let i = idx - 1; i >= 0; i -= 1) {
        const it = prev[i]
        if (it.kind === 'user') { userText = it.text; userIdx = i; break }
      }
      if (userIdx < 0 || !userText.trim()) return prev
      // Defer the resend until after this state commit so sendMessage sees
      // the trimmed timeline (it re-appends its own fresh user + assistant
      // items — so we cut from the user message, not the assistant turn).
      window.setTimeout(() => { void sendMessage(userText) }, 0)
      return prev.slice(0, userIdx)
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loading])

  // ── Export transcript ────────────────────────────────────────────
  // Download the visible conversation as a Markdown file. Only the
  // business-facing turns (user prompt + final answer) — the reasoning
  // steps stay out so the export reads like a clean transcript.
  const exportTranscript = useCallback(() => {
    const lines: string[] = [`# ${agentName || 'Conversación'}`, '']
    for (const it of items) {
      if (it.kind === 'user') {
        lines.push(`**${t('agent.chat.you', 'Tú')}:** ${it.text}`, '')
      } else if (it.kind === 'assistant') {
        const view = buildTurnView(it)
        const ans = (view.finalAnswer || '').trim()
        if (ans) lines.push(`**${agentName || 'Empleado'}:** ${ans}`, '')
      }
    }
    const blob = new Blob([lines.join('\n')], { type: 'text/markdown;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `conversacion-${new Date().toISOString().slice(0, 10)}.md`
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items, agentName, t])

  // ── Restart the session in progress ──────────────────────────────
  // Deletes the local timeline AND removes the session from the registry/sidecar RIGHT
  // AWAY (POST /sessions/delete). Before, only pendingResetRef was set and the rotation
  // happened on the next message — barely visible and easy to believe it «does nothing».
  // pendingResetRef is kept in case the delete fails (network) and the next send still
  // carries reset:true.
  const [resetSnack, setResetSnack] = useState(false)
  const handleClearContext = useCallback(async () => {
    setClearing(true)
    try {
      // v12.0.0 — WITH the thread: «Limpiar contexto» deleted the default scope's session even
      // if you were in thread X — it emptied another conversation and left this one as it was.
      const curResp = await apiFetch(
        `/api/agent/session/current${soportaHilos ? `?thread_id=${encodeURIComponent(hiloId)}` : ''}`,
      )
      if (curResp.ok) {
        const cur = await curResp.json().catch(() => null)
        const scope = (cur?.scope_key || '').trim()
        if (scope) {
          await apiFetch('/api/agent/sessions/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ scope_key: scope }),
          })
        }
      }
    } catch { /* ignore — still wipe local UI */ }

    // And THE CONVERSATION IS DELETED, not just the screen.
    //
    // Until now this closed the agent's session and emptied `items`. The history stayed
    // stored, so on reload —which since 12.0.0 asks the server for it— it came back whole.
    // The button is called «Reiniciar sesión», its help says «deletes the conversation» and
    // the dialog says «the conversation on this screen is deleted»: three promises the next
    // refresh contradicted.
    //
    // Deleting the default conversation ROTATES it and returns another empty one (12.3.0),
    // so this leaves nobody without a place to talk. The turns stay in the audit.
    if (soportaHilos) {
      try {
        const r = await apiFetch(`/api/agent/threads/${encodeURIComponent(hiloId)}`,
          { method: 'DELETE' })
        const d = await r.json().catch(() => ({}))
        const destino = (d as { id_por_defecto?: unknown })?.id_por_defecto
        if (typeof destino === 'string' && destino) {
          setHiloId(destino)
          persistHilo(destino)
        }
      } catch { /* if it could not be deleted, at least the session was restarted */ }
    }
    setItems([])
    setSessionId(null)
    persistSession(null)
    setCtxTokens(0)
    setCtxMessages(0)
    pendingResetRef.current = true
    abortRef.current?.abort()
    persistActive(null)
    try { window.localStorage.removeItem(nsKey(STORAGE_TIMELINE_KEY)) } catch { /* ignore */ }
    setClearOpen(false)
    setResetSnack(true)
    window.setTimeout(() => setClearing(false), 180)
    // WITH DEPENDENCIES. It was `[]` and the body reads `hiloId`, so it captured the FIRST
    // render's thread: if you switched conversation and restarted, it restarted another
    // one's session. It is the same bug the comment above says was fixed in 12.0.0 — the
    // call's parameter was corrected and the old capture was left, which brings it back.
    // Now it also DELETES the conversation, so with `[]` it would delete the wrong one.
  }, [hiloId, soportaHilos])

  // Id of the last assistant turn — only it gets a "Regenerar" action.
  const lastAssistantId = (() => {
    for (let i = items.length - 1; i >= 0; i -= 1) {
      if (items[i].kind === 'assistant') return items[i].id
    }
    return null
  })()

  // ── Render helpers ───────────────────────────────────────────────
  const renderItem = (it: TurnItem) => {
    switch (it.kind) {
      case 'user':
        return <UserBubble key={it.id} text={it.text} attachments={it.attachments} hablado={it.hablado} />
      case 'assistant': {
        const dur = it.finishedAt && it.startedAt ? it.finishedAt - it.startedAt : null
        const view = buildTurnView(it)
        const canRegenerate = it.id === lastAssistantId && !it.streaming && !loading
        return (
          <AssistantTurn
            key={it.id}
            steps={view.steps}
            progressTexts={view.progressTexts}
            finalAnswer={view.finalAnswer}
            streaming={it.streaming}
            modelId={it.modelId}
            durationMs={dur}
            credits={it.totalCredits}
            resuelto={it.resuelto}
            latido={it.streaming && it.latido ? textoDelLatido(it.latido, t) : null}
            persona={persona}
            agentName={agentName}
            onRegenerate={canRegenerate ? () => regenerateTurn(it.id) : undefined}
            // v7.140.0 — only the LAST settled turn may offer an inline rule
            // approval: what you agreed just now belongs in the thread, older
            // pending proposals belong in Operaciones → Aprendizaje.
            allowRuleProposals={it.id === lastAssistantId && !it.streaming && !loading}
          />
        )
      }
      case 'error':
        return (
          <ErrorBubble
            key={it.id}
            message={it.message}
            onRetry={it.retryPrompt && !loading ? () => retryFromError(it.id, it.retryPrompt!, it.retryRequestId) : undefined}
            retrying={loading}
          />
        )
      case 'question':
        return (
          <QuestionBubble
            key={it.id}
            item={it}
            disabled={loading}
            onAnswer={(requestId, answer) => {
              // Optimistic lock for instant feedback, but UNLOCK if the send
              // fails (v7.88.0) so a network/HTTP error doesn't leave the widget
              // permanently "Respondido" with the paused run never resumed.
              updateItem<QuestionItem>(it.id, (q) => ({ ...q, answered: answer }))
              void sendMessage(answer, { requestId }).then((ok) => {
                if (ok === false) {
                  updateItem<QuestionItem>(it.id, (q) => ({ ...q, answered: undefined }))
                }
              })
            }}
          />
        )
    }
  }

  const isEmpty = items.length === 0

  return (
    <Box
      sx={{
        display: 'flex',
        flexDirection: 'column',
        height: '100%',
        minHeight: 0,
      }}
    >
      {/* Header */}
      <Stack
        direction="row"
        alignItems="center"
        spacing={esMovil ? 0.25 : 1.25}
        sx={{ mb: esMovil ? 1 : 1.5, flexShrink: 0, minWidth: 0 }}
      >
        <PersonaAvatar persona={persona} size={esMovil ? 28 : 32} fallbackName={functionalName} />
        <Box sx={{ minWidth: 0, flex: 1 }}>
          {/* `noWrap` + `minWidth: 0` on the parent: without both, a long name pushes the
              actions off screen instead of being truncated — which is exactly what was seen
              on a phone. */}
          <Typography
            variant={esMovil ? 'subtitle1' : 'h6'}
            noWrap
            sx={{ fontWeight: 700, lineHeight: 1.15 }}
          >
            {agentName}
          </Typography>
          {agentTagline && !esMovil && (
            <Typography variant="caption" color="text.secondary" sx={{
              display: 'block', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
            }}>
              {agentTagline}
            </Typography>
          )}
        </Box>
        {modelId && !esMovil && (
          <Chip
            label={modelId}
            size="small"
            variant="outlined"
            sx={{ fontFamily: 'monospace', fontSize: '0.7rem', height: 22 }}
          />
        )}
        {/* The context counter is OPERATOR information: on a phone it competes for space
            with what does get touched (workspace, restart) and it was what overlapped the
            employee's name. The orange high-cost warning DOES stay: that one does not
            inform, it warns. */}
        {(ctxMessages > 0 || ctxTokens > 0) && !esMovil && (
          <Tooltip
            arrow
            title={t('agent.chat.context.tooltip', {
              messages: ctxMessages,
              max: ctxRotateMax,
            }) as string}
          >
            <Chip
              icon={<DataUsageIcon sx={{ fontSize: 14 }} />}
              label={t('agent.chat.context.label', {
                tokens: fmtTokens(ctxTokens),
              }) as string}
              size="small"
              variant="outlined"
              color={ctxMessages >= ctxRotateMax * 0.85 ? 'warning' : 'default'}
              sx={{ fontSize: '0.7rem', height: 22 }}
            />
          </Tooltip>
        )}
        {ctxTokens >= CTX_EXPENSIVE_TOKENS && (
          <Tooltip
            arrow
            title={t('agent.chat.context.expensiveTooltip',
              'Cada mensaje reenvía todo el contexto al modelo: a más contexto, más coste por pregunta (aunque sea sencilla). Para reducirlo tienes dos opciones: «Compactar» resume la conversación y mantiene el hilo (para seguir con el mismo tema), y «Limpiar contexto» borra todo y empieza de cero (mejor al cambiar de tema).') as string}
          >
            <Chip
              size="small"
              color="warning"
              variant="outlined"
              label={t('agent.chat.context.expensiveChip', 'Contexto grande · coste alto por mensaje') as string}
              sx={{ fontSize: '0.7rem', height: 22, color: 'warning.dark' }}
            />
          </Tooltip>
        )}
        <Tooltip
          title={t('agent.chat.panel.toggle') as string}
          arrow
        >
          <span>
            <IconButton
              size="small"
              color={panelOpen ? 'primary' : 'default'}
              onClick={() => setPanelOpen((v) => !v)}
              aria-label={t('agent.chat.panel.toggle') as string}
              sx={{
                border: '1px solid',
                borderColor: 'divider',
                borderRadius: 1.5,
                bgcolor: 'background.paper',
                '&:hover': { bgcolor: 'action.hover' },
              }}
            >
              <ViewSidebarIcon sx={{ fontSize: 16 }} />
            </IconButton>
          </span>
        </Tooltip>
        {items.length > 0 && (
          <Tooltip title={t('agent.chat.exportTranscript', 'Exportar conversación') as string} arrow>
            <span>
              <IconButton
                size="small"
                onClick={exportTranscript}
                aria-label={t('agent.chat.exportTranscript', 'Exportar conversación') as string}
                sx={{
                  border: '1px solid',
                  borderColor: 'divider',
                  borderRadius: 1.5,
                  bgcolor: 'background.paper',
                  '&:hover': { bgcolor: 'action.hover' },
                }}
              >
                <DownloadIcon sx={{ fontSize: 16 }} />
              </IconButton>
            </span>
          </Tooltip>
        )}
        {/* Switching conversation goes FIRST: it is what is touched before writing, and it
            only appears if the employee publishes the threads flag. */}
        {soportaHilos && (
          <SelectorDeHilos
            hiloActual={hiloId}
            onCambiar={(id: string) => { void cambiarDeHilo(id) }}
            refrescarEn={loading}
            deshabilitado={loading || clearing || compacting}
          />
        )}
        {(items.length > 0 || sessionId || ctxMessages > 0) && (
          <Tooltip
            title={t('agent.chat.compact.tooltip', 'Resume la conversación para reducir el contexto (y el coste) sin perder el hilo') as string}
            arrow
          >
            <span>
              <IconButton
                size="small"
                color="primary"
                onClick={() => void compactContext()}
                disabled={loading || clearing || compacting}
                aria-label={t('agent.chat.compact.button', 'Compactar') as string}
                sx={{
                  border: '1px solid',
                  borderColor: 'divider',
                  borderRadius: 1.5,
                  bgcolor: 'background.paper',
                  '&:hover': { bgcolor: 'action.hover' },
                }}
              >
                {compacting
                  ? <CircularProgress size={16} thickness={5} />
                  : <CompressIcon sx={{ fontSize: 16 }} />}
              </IconButton>
            </span>
          </Tooltip>
        )}
        <Tooltip
          title={t('agent.chat.clearContext.tooltip') as string}
          arrow
        >
          <span>
            <Button
              size="small"
              color="warning"
              variant="outlined"
              onClick={() => setClearOpen(true)}
              disabled={loading || clearing}
              startIcon={clearing
                ? <CircularProgress size={14} thickness={5} color="inherit" />
                : <RestartAltIcon sx={{ fontSize: 16 }} />}
              aria-label={t('agent.chat.clearContext.button') as string}
              sx={{
                // The theme's warning (orange) as TEXT on white is 2.9:1, under the 3:1 a
                // small label needs (the evolve's captures flagged it): the darker shade reads.
                color: 'warning.dark',
                textTransform: 'none',
                fontSize: '0.75rem',
                height: 28,
                borderRadius: 1.5,
                px: esMovil ? 0.75 : 1,
                // On mobile the text broke into two lines and misaligned the whole row. The
                // icon stays, which is unmistakable, and the label remains in `aria-label`
                // for screen-reader users.
                minWidth: esMovil ? 0 : undefined,
                '& .MuiButton-startIcon': esMovil ? { mr: 0, ml: 0 } : undefined,
              }}
            >
              {!esMovil && t('agent.chat.clearContext.button')}
            </Button>
          </span>
        </Tooltip>
      </Stack>

      {/* Conversation + per-user side panel */}
      <Box sx={{ flex: 1, minHeight: 0, display: 'flex', gap: 1.5 }}>
      <Box sx={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', minHeight: 0, position: 'relative' }}>
      {/* BLOCKING veil while compacting. Compacting summarises the thread in the pod and the
          call BLOCKS until the summary turn finishes (several seconds). Until now that only
          changed a button's icon: the chat stayed «usable», you could type and send in the
          middle of the summary and, since nothing was visible, it looked hung.
          This veil covers the conversation AND the composer, with a spinner and a clear sentence. */}
      {compacting && (
        <Box sx={{
          position: 'absolute', inset: 0, zIndex: (th) => th.zIndex.modal,
          display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
          gap: 1.5, px: 3, textAlign: 'center',
          bgcolor: 'rgba(0, 0, 0, 0.45)', backdropFilter: 'blur(2px)', borderRadius: 2,
        }}>
          <CircularProgress sx={{ color: 'common.white' }} />
          <Typography variant="subtitle1" sx={{ color: 'common.white', fontWeight: 600 }}>
            {t('agent.chat.compact.overlayTitle', 'Compactando la conversación…')}
          </Typography>
          <Typography variant="body2" sx={{ color: 'rgba(255, 255, 255, 0.85)', maxWidth: 380 }}>
            {t('agent.chat.compact.overlayHint',
              'Estoy resumiendo el hilo para reducir el contexto. Tarda unos segundos; no cierres ni escribas hasta que termine.')}
          </Typography>
        </Box>
      )}
      {/* Conversation */}
      <Card
        variant="outlined"
        sx={{
          flex: 1, minHeight: 0, position: 'relative',
          display: 'flex', flexDirection: 'column',
          mb: 1.5, borderRadius: 2,
        }}
      >
        <Box
          ref={scrollRef}
          onScroll={onScroll}
          sx={{
            flex: 1,
            overflow: 'auto',
            px: { xs: 1.5, md: 3 },
            pt: 2,
            pb: 3,
          }}
        >
          {/* VOICE mode goes FIRST: with an empty conversation the welcome screen won and
              the orb never got painted —which is exactly when the microphone is pressed the
              most, at the start—. */}
          {/* Voice mode is ALWAYS mounted and painted only when open: that way closing and
              reopening it continues the spoken conversation instead of starting from
              scratch. Unmounting it closed the session and with it the context. */}
          <VozEnVivo
            abierta={modoVoz && tieneVoz}
            orbitas={enMarcha}
            trabajando={loading}
            avance={avanceVoz}
            // THE WRITTEN THREAD, so the conversation is not born blank. It is a FUNCTION and
            // not a data prop on purpose: it is read when OPENING the microphone —what counts
            // is what is written at that moment— and that way nothing is remounted on each
            // chat repaint. From the assistant the final text travels, not its steps or its
            // tools: the speaker cares about what was said, not how it got there.
            pideAlAparato={pideAlAparato}
            // READING and ANSWERING are two steps, and both belong here. The conversation only
            // asks, because between one and the other there may be a «no» said out loud.
            leerDelAparato={async (que) => {
              if (que !== 'ubicacion') return 'Eso no se puede pedir aquí. Sigue sin ello.'
              const datos = await leerUbicacion()
              datoDelAparato.current = { id: peticionDelAparato.current, datos }
              // What comes back is for TELLING it out loud; the real data travels separately.
              // Sending this sentence to the employee was the bug: its gate expects numbers.
              return typeof datos.lat === 'number'
                ? `Está en latitud ${(datos.lat as number).toFixed(5)}, longitud `
                  + `${(datos.lon as number).toFixed(5)}.`
                : 'No se ha podido saber dónde está. Sigue sin ello y no insistas.'
            }}
            onAparato={() => {
              // THE ONLY GATE. It waits inside its call with a short cap, so an answer must
              // always be given —with the data or with the reason—; staying silent only makes
              // it run out of time. The TEXT the machine brings is for the conversation, not
              // for it: the data goes here, and if there is no data, the reason.
              const id = peticionDelAparato.current
              const leido = datoDelAparato.current
              peticionDelAparato.current = ''
              datoDelAparato.current = null
              if (!id) return
              contestarUbicacion(id, leido?.id === id ? leido.datos : { no_disponible: 'rechazado' })
            }}
            hiloEscrito={() => items.flatMap((it) => (
              it.kind === 'user'
                ? [{ de: 'persona', texto: it.text }]
                : it.kind === 'assistant'
                  ? [{ de: 'empleado', texto: it.textBlocks.map((b) => b.text).join('\n').trim() }]
                  : []
            )).filter((x) => x.texto)}
            onCerrar={() => { setModoVoz(false); setTimeout(jumpToBottom, 0) }}
            onPeticion={(texto, hablado, reconocido) =>
              new Promise<{ texto: string; requestId?: string }>((resolver) => {
                esperaVoz.current = resolver
                // `requestId` RESUMES the paused turn instead of opening a new one.
                void sendMessage(texto, { hablado, canal: 'voz', reconocido,
                                          requestId: reconocido?.requestId })
              })}
          />
          {(modoVoz && tieneVoz) ? null : isEmpty ? (
            <EmptyChat
              persona={persona}
              greeting={agentGreeting}
              agentName={agentName}
              agentTagline={agentTagline}
              cards={capabilityCards}
              onPick={(p) => setInput(p)}
            />
          ) : (
            <>
              {plegados > 0 && (
                <Box
                  onClick={() => setVerAnteriores((v) => !v)}
                  sx={{
                    display: 'flex', alignItems: 'center', gap: 1, alignSelf: 'center',
                    py: 0.75, px: 1.5, mb: 1, borderRadius: 1, cursor: 'pointer',
                    bgcolor: 'action.hover', color: 'text.secondary',
                    '&:hover': { bgcolor: 'action.selected' },
                  }}
                >
                  <UnfoldMoreIcon sx={{ fontSize: 16 }} />
                  <Typography variant="caption">
                    {verAnteriores
                      ? t('agent.chat.compact.hideEarlier', 'Ocultar los mensajes anteriores')
                      : plegados === 1
                        ? t('agent.chat.compact.showCompactedOne',
                          'Un mensaje anterior, resumido al compactar')
                        : t('agent.chat.compact.showCompacted',
                          '{{n}} mensajes anteriores, resumidos al compactar',
                          { n: plegados })}
                  </Typography>
                </Box>
              )}
              {items.map((it, i) => (i < plegados && !verAnteriores ? null : renderItem(it)))}
            </>
          )}
        </Box>

        {showJump && !isEmpty && (
          <IconButton
            size="small"
            onClick={jumpToBottom}
            sx={{
              position: 'absolute',
              bottom: 12, right: 12,
              bgcolor: 'background.paper',
              border: '1px solid',
              borderColor: 'divider',
              boxShadow: 2,
              '&:hover': { bgcolor: 'action.hover' },
            }}
            aria-label={t('agent.chat.jumpToBottom')}
          >
            <KeyboardArrowDownIcon fontSize="small" />
          </IconButton>
        )}
      </Card>

      {/* Composer. In voice mode it is NOT painted: there you only talk, and having the
          writing field, the paperclip and another microphone in front is offering three
          paths for the same thing and leaving people unsure which one rules. */}
      {!(modoVoz && tieneVoz) && (
      <Box
        sx={{
          display: 'flex',
          flexDirection: 'column',
          gap: 0.75,
          p: 1,
          bgcolor: 'background.paper',
          borderRadius: 2.5,
          border: '1px solid',
          borderColor: 'divider',
          boxShadow: '0 -4px 16px rgba(0,0,0,0.06)',
          flexShrink: 0,
        }}
      >
        {attachments.length > 0 && (
          <Stack direction="row" spacing={0.75} flexWrap="wrap" useFlexGap sx={{ px: 0.5 }}>
            {attachments.map((a) => (
              <Tooltip key={a.id} title={a.error || a.filename}>
                <Chip
                  size="small"
                  variant="outlined"
                  color={a.status === 'error' ? 'error' : 'default'}
                  icon={a.status === 'uploading'
                    ? <CircularProgress size={12} thickness={6} sx={{ ml: 0.75 }} />
                    : <InsertDriveFileIcon sx={{ fontSize: 16 }} />}
                  label={a.status === 'error'
                    ? a.filename
                    : `${a.filename}${a.size ? ` · ${fmtBytes(a.size)}` : ''}`}
                  onDelete={() => removeAttachment(a.id)}
                  deleteIcon={<CloseIcon sx={{ fontSize: 14 }} />}
                  sx={{ maxWidth: 260, '& .MuiChip-label': { overflow: 'hidden', textOverflow: 'ellipsis' } }}
                />
              </Tooltip>
            ))}
          </Stack>
        )}
        <Box
          sx={{ display: 'flex', gap: 1, alignItems: 'flex-start' }}
          onDragOver={(ev) => {
            if (!ev.dataTransfer.types.includes(WORKSPACE_DRAG_TYPE)) return
            ev.preventDefault()
            ev.dataTransfer.dropEffect = 'copy'
            setDropHot(true)
          }}
          onDragLeave={(ev) => {
            if (!ev.currentTarget.contains(ev.relatedTarget as Node)) setDropHot(false)
          }}
          onDrop={(ev) => {
            const p = ev.dataTransfer.getData(WORKSPACE_DRAG_TYPE)
            if (!p) return
            ev.preventDefault()
            setDropHot(false)
            insertWorkspaceRef(p)
          }}
          onPaste={(ev) => {
            // Pasting a screenshot from the clipboard (Win+Shift+S / Cmd+Shift+4)
            // attaches it directly — same pipeline as the paperclip.
            const imgs = Array.from(ev.clipboardData?.items || [])
              .filter((it) => it.kind === 'file' && it.type.startsWith('image/'))
              .map((it) => it.getAsFile())
              .filter((f): f is File => !!f)
            if (!imgs.length) return
            ev.preventDefault()
            void handleFiles(imgs)
          }}
        >
          <input
            ref={fileInputRef}
            type="file"
            multiple
            hidden
            onChange={(e) => { void handleFiles(e.target.files); e.currentTarget.value = '' }}
          />
          {/* On MOBILE the actions go in ONE menu, not in four loose icons. Each icon eats
              ~44 px out of a 390 width: with four, the writing field got so narrow that its
              own placeholder broke into two lines. The space is for writing; the rest fits
              behind a button. */}
          {esMovil ? (
            <>
              <Tooltip title={t('agent.chat.more', 'Adjuntar') as string}>
                <span>
                  <IconButton
                    aria-label={t('agent.chat.more', 'Adjuntar') as string}
                    onClick={(e) => setMenuAdjuntar(e.currentTarget)}
                    disabled={loading}
                    sx={{ mt: 0.25 }}
                  >
                    {ubicando ? <CircularProgress size={18} /> : <AddCircleOutlineIcon fontSize="small" />}
                  </IconButton>
                </span>
              </Tooltip>
              <Menu
                anchorEl={menuAdjuntar}
                open={!!menuAdjuntar}
                onClose={() => setMenuAdjuntar(null)}
                anchorOrigin={{ vertical: 'top', horizontal: 'left' }}
                transformOrigin={{ vertical: 'bottom', horizontal: 'left' }}
              >
                <MenuItem onClick={() => { setMenuAdjuntar(null); fileInputRef.current?.click() }}>
                  <AttachFileIcon fontSize="small" sx={{ mr: 1.5 }} />
                  {t('agent.chat.attach.button')}
                </MenuItem>
                <MenuItem onClick={() => { setMenuAdjuntar(null); camaraRef.current?.click() }}>
                  <PhotoCameraIcon fontSize="small" sx={{ mr: 1.5 }} />
                  {t('agent.chat.camera.button', 'Hacer una foto')}
                </MenuItem>
                <MenuItem onClick={() => { setMenuAdjuntar(null); anadirUbicacion() }}>
                  <MyLocationIcon fontSize="small" sx={{ mr: 1.5 }} />
                  {t('agent.chat.location.button', 'Añadir mi ubicación')}
                </MenuItem>
                {/* v12.1.0 — let the employee ASK for it when the task needs it, instead of
                    attaching it by hand every time. Off by default. On the web the permission
                    is governed by the browser per origin, so the first time its dialog appears. */}
                <MenuItem
                  onClick={() => { setMenuAdjuntar(null); setCompartirUbicacion((v) => !v) }}
                >
                  <MyLocationIcon
                    fontSize="small"
                    color={compartirUbicacion ? 'primary' : 'inherit'}
                    sx={{ mr: 1.5 }}
                  />
                  {compartirUbicacion
                    ? t('agent.chat.location.autoOff', 'Dejar de compartirla cuando la pida')
                    : t('agent.chat.location.autoOn', 'Compartirla cuando la pida')}
                </MenuItem>
              </Menu>
            </>
          ) : (
            <>
          {/* TWO DIFFERENT THINGS, TWO BUTTONS —just as on mobile—. The mic DICTATES: it
              listens and writes here, without sending anything and without spending credits,
              because the browser does it. The wave opens the CONVERSATION, which is really
              talking to the employee. Until 12.50.0 the mic led to the conversation:
              dictating —what is asked for most— did not exist, and whoever only wanted to
              write without typing started a whole call. The mic does NOT depend on the
              employee having voice: dictation belongs to the browser, and where you cannot
              converse is where it is needed most. */}
          {puedeDictar && (
          <Tooltip title={(dictando
            ? t('voz.dictarParar', 'Dejar de dictar')
            : t('voz.dictar', 'Dictar: se escribe aquí, no se envía')) as string}>
            <span>
              <IconButton
                aria-label={(dictando
                  ? t('voz.dictarParar', 'Dejar de dictar')
                  : t('voz.dictar', 'Dictar: se escribe aquí, no se envía')) as string}
                onClick={alternarDictado}
                color={dictando ? 'error' : 'default'}
                sx={{ mt: 0.25 }}
              >
                {dictando ? <StopCircleIcon fontSize="small" /> : <MicIcon fontSize="small" />}
              </IconButton>
            </span>
          </Tooltip>
          )}

          {tieneVoz && (
          <Tooltip title={t('voz.empezar', 'Conversar con el empleado') as string}>
            <span>
              <IconButton
                aria-label={t('voz.empezar', 'Conversar con el empleado') as string}
                onClick={() => setModoVoz(true)}
                sx={{ mt: 0.25 }}
              >
                <GraphicEqIcon fontSize="small" />
              </IconButton>
            </span>
          </Tooltip>
          )}

          <Tooltip title={t('agent.chat.attach.button') as string}>
            <span>
              <IconButton
                aria-label={t('agent.chat.attach.button') as string}
                onClick={() => fileInputRef.current?.click()}
                disabled={loading}
                sx={{ mt: 0.25 }}
              >
                <AttachFileIcon fontSize="small" />
              </IconButton>
            </span>
          </Tooltip>

          <Tooltip title={t('agent.chat.capture.tooltip', 'Captura pantallas de tu aplicación y pregunta sobre ellas') as string}>
            <span>
              <IconButton
                aria-label={t('agent.chat.capture.button', 'Capturar pantallas') as string}
                onClick={() => {
                  // Without support (old Safari, mobile) we degrade to the classic attach:
                  // the user picks an already saved screenshot.
                  if (screenCaptureSupported()) setCaptureOpen(true)
                  else fileInputRef.current?.click()
                }}
                disabled={loading}
                sx={{ mt: 0.25 }}
              >
                <ScreenshotMonitorIcon fontSize="small" />
              </IconButton>
            </span>
          </Tooltip>
            </>
          )}
          <TextField
            fullWidth
            size="small"
            placeholder={dropHot
              ? t('agent.chat.dropToReference', 'Suelta aquí para que trabaje con ese elemento…')
              : t('agent.chat.placeholder')}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                if (!compacting && !attachments.some((a) => a.status === 'uploading')) sendMessage(input)
              }
            }}
            disabled={loading || compacting}
            multiline
            // Enlarged: it grows to fill the screen on a phone and gives plenty of room on a
            // desktop. `minRows` as well as `maxRows` so the space EXISTS when opened, not
            // only once you have written a lot.
            minRows={compositorGrande ? (esMovil ? 12 : 10) : undefined}
            maxRows={compositorGrande ? (esMovil ? 16 : 20) : 6}
            inputRef={chatInputRef}
            // On mobile the help text broke into two lines and stole exactly the space we are
            // trying to gain. The Shift+Enter hint does not apply either: there is no
            // physical keyboard there.
            helperText={esMovil ? undefined : (
              input.length > 0
                ? t('agent.chat.shiftEnter')
                : t('agent.chat.attach.hint', { mb: CHAT_MAX_UPLOAD_MB })
            )}
            FormHelperTextProps={{ sx: { mx: 1, mt: 0.25, fontSize: '0.7rem' } }}
            sx={{
              '& .MuiOutlinedInput-root': {
                borderRadius: 2,
                bgcolor: (t) => (t.palette.mode === 'dark' ? 'background.default' : 'grey.50'),
              },
              ...(dropHot && {
                '& .MuiOutlinedInput-notchedOutline': {
                  borderColor: 'primary.main',
                  borderWidth: 2,
                  borderStyle: 'dashed',
                },
              }),
            }}
          />
          {/* Enlarge the composer. A long task —or anything on a phone— is written terribly
              through a three-line slit, and going to another screen to write is worse: the
              conversation goes out of sight. This only changes the height, so there is
              nothing to save or recover. */}
          <Tooltip title={(compositorGrande
            ? t('agent.chat.shrink', 'Reducir el cuadro de escritura')
            : t('agent.chat.expand', 'Agrandar el cuadro de escritura')) as string}>
            <IconButton
              aria-label={(compositorGrande
                ? t('agent.chat.shrink', 'Reducir el cuadro de escritura')
                : t('agent.chat.expand', 'Agrandar el cuadro de escritura')) as string}
              onClick={() => setCompositorGrande((v) => !v)}
              sx={{ mt: 0.25 }}
            >
              {compositorGrande
                ? <CloseFullscreenIcon fontSize="small" />
                : <OpenInFullIcon fontSize="small" />}
            </IconButton>
          </Tooltip>

          {/* How much it thinks on this message. It sits next to the send button because it
              is a decision OF THE SEND, not of the conversation: it is looked at when sending,
              like someone choosing urgent or normal before handing over a task. */}
          <NivelDeRazonamiento
            valor={nivelRazonamiento}
            onChange={setNivelRazonamiento}
            disabled={loading || compacting}
            compacto={esMovil}
          />

          {loading ? (
            <Button
              variant="contained"
              color="error"
              onClick={stopMessage}
              sx={{
                minWidth: 48, height: 40, alignSelf: 'flex-start',
                mt: 0, borderRadius: 2,
              }}
              aria-label={t('agent.chat.stop', 'Detener')}
              title={t('agent.chat.stop', 'Detener')}
            >
              <StopIcon fontSize="small" />
            </Button>
          ) : (
            <Button
              variant="contained"
              color="primary"
              onClick={() => sendMessage(input)}
              disabled={
                compacting
                || attachments.some((a) => a.status === 'uploading')
                || (!input.trim() && !attachments.some((a) => a.status === 'ready'))
              }
              sx={{
                minWidth: 48, height: 40, alignSelf: 'flex-start',
                mt: 0, borderRadius: 2,
              }}
              aria-label={t('agent.chat.send')}
            >
              <SendIcon fontSize="small" />
            </Button>
          )}
        </Box>
      </Box>
      )}
      </Box>

      {panelOpen && (
        <Card
          variant="outlined"
          sx={{
            width: { xs: '100%', md: 520 },
            flexShrink: 0,
            display: 'flex',
            flexDirection: 'column',
            minHeight: 0,
            mb: 1.5,
            borderRadius: 2,
          }}
        >
          <Stack
            direction="row"
            alignItems="center"
            spacing={1}
            sx={{ px: 1.5, py: 1, borderBottom: '1px solid', borderColor: 'divider', flexShrink: 0 }}
          >
            <Tabs
              value={panelTab}
              onChange={(_e, v) => setPanelTab(v)}
              sx={{ flex: 1, minHeight: 0 }}
              variant="fullWidth"
            >
              <Tab label={t('agent.chat.panel.workspace')} sx={{ minHeight: 40, textTransform: 'none' }} />
              <Tab label={t('agent.chat.panel.memory')} sx={{ minHeight: 40, textTransform: 'none' }} />
            </Tabs>
            <IconButton size="small" onClick={() => setPanelOpen(false)} aria-label={t('agent.chat.panel.close')}>
              <CloseIcon fontSize="small" />
            </IconButton>
          </Stack>
          <Box sx={{ flex: 1, minHeight: 0, overflow: 'auto', p: 1.5 }}>
            {panelTab === 0 && (
              workspaceSubdir
                ? (
                  <>
                    <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 1 }}>
                      {t('agent.chat.panel.dragHint', 'Arrastra un archivo al mensaje para que el empleado trabaje con él.')}
                    </Typography>
                    {/* Browse-only EXCEPT delete (purges the matching
                        user_files memory rows server-side), mkdir (organise
                        your own scope in folders) and upload (self-scoped
                        endpoint that also indexes each file into memory). */}
                    <WorkspaceViewer initialPath={workspaceSubdir} rootPath={workspaceSubdir} readOnly allowDelete allowMkdir allowChatUpload />
                  </>
                )
                : <Typography variant="body2" color="text.secondary">{t('agent.chat.panel.workspaceEmpty')}</Typography>
            )}
            {panelTab === 1 && (
              memoryUser
                ? <MemoryViewer userEmail={memoryUser} hideUserPicker showCompact={false} />
                : <Typography variant="body2" color="text.secondary">{t('agent.chat.panel.memoryEmpty')}</Typography>
            )}
          </Box>
        </Card>
      )}
      </Box>

      <ScreenCaptureHelper
        open={captureOpen}
        onClose={() => setCaptureOpen(false)}
        onDone={onShotsReady}
      />

      <Snackbar
        open={compactResult !== null}
        autoHideDuration={8000}
        onClose={() => setCompactResult(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}
      >
        <Alert
          severity={compactResult === 'success' ? 'success' : compactResult === 'stale' ? 'info' : 'error'}
          variant="filled"
          onClose={() => setCompactResult(null)}
          sx={{ fontSize: 13 }}
        >
          {compactResult === 'success'
            ? t('agent.chat.compact.success', 'Contexto compactado: la conversación sigue donde estaba y los próximos mensajes serán más baratos.')
            : compactResult === 'stale'
              ? t('agent.chat.compact.stale', 'No había nada que compactar: la sesión ya se había reiniciado (el contador estaba desactualizado). Tu próximo mensaje empieza con contexto limpio.')
              : t('agent.chat.compact.failed', 'No se pudo compactar el contexto. Vuelve a intentarlo o usa «Limpiar contexto».')}
        </Alert>
      </Snackbar>

      <Snackbar
        open={!!avisoBreve}
        autoHideDuration={6000}
        onClose={() => setAvisoBreve('')}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}
      >
        <Alert severity="warning" variant="filled"
               onClose={() => setAvisoBreve('')} sx={{ fontSize: 13 }}>
          {avisoBreve}
        </Alert>
      </Snackbar>

      <Snackbar
        open={resetSnack}
        autoHideDuration={5000}
        onClose={() => setResetSnack(false)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}
      >
        <Alert
          severity="success"
          variant="filled"
          onClose={() => setResetSnack(false)}
          sx={{ fontSize: 13 }}
        >
          {t('agent.chat.clearContext.done', 'Sesión reiniciada — el próximo mensaje empezará de cero.')}
        </Alert>
      </Snackbar>

      {/* Reiniciar sesión — confirmation dialog */}
      <Dialog
        open={clearOpen}
        onClose={() => !clearing && setClearOpen(false)}
        maxWidth="xs"
        fullWidth
      >
        <DialogTitle sx={{ display: 'flex', alignItems: 'center', gap: 1, fontWeight: 700 }}>
          <RestartAltIcon color="warning" sx={{ fontSize: 20 }} />
          {t('agent.chat.clearContext.confirmTitle')}
        </DialogTitle>
        <DialogContent>
          <DialogContentText>
            {t('agent.chat.clearContext.confirmBody')}
          </DialogContentText>
        </DialogContent>
        <DialogActions sx={{ px: 3, pb: 2 }}>
          <Button
            onClick={() => setClearOpen(false)}
            disabled={clearing}
          >
            {t('agent.chat.clearContext.cancel')}
          </Button>
          <Button
            onClick={() => { void handleClearContext() }}
            color="warning"
            variant="contained"
            disabled={clearing}
            startIcon={<RestartAltIcon sx={{ fontSize: 16 }} />}
          >
            {t('agent.chat.clearContext.confirm')}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  )
}
