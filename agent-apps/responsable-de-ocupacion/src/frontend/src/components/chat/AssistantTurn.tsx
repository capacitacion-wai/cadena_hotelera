/**
 * AssistantTurn — single assistant turn rendered as:
 *   • one "Step" card per chunk of (reasoning + tool calls) the
 *     model produced before each ``step_done`` boundary, and
 *   • a single ``AssistantBubble`` at the bottom with the final
 *     business-facing answer.
 *
 * Visual language is borrowed from the upstream OpenCodeChatPanel
 * (the "code agent") so operators who use both feel at home:
 * a collapsible header with a brain icon, the step number, chips
 * showing how many tools ran in that step, and an expand caret —
 * but stripped of the developer-oriented chrome (no plan buckets,
 * no file-changes panel, no per-command diffs) since this chat is
 * a *business* surface.
 *
 * Why a dedicated component instead of inline-rendering on the
 * page:
 *   • Steps are first-class state. The page used to render text
 *     bubbles + tool groups + reasoning blocks as flat sibling
 *     items, which produced the "duplicated prompt" artifact in
 *     production (a fresh assistant bubble would appear after
 *     each tool group, often parroting the user prompt).
 *   • The header preview line (the first ~80 chars of the
 *     reasoning) is computed inside the component so it stays in
 *     sync with whatever the model emits without leaking the
 *     formatting concern to the page.
 *
 * Streaming contract (matches ``agent_routes.py`` R-CHAT-2):
 *   • ``text_delta`` with ``stepIndex=N`` appends to step N's
 *     reasoning while that step is still open.
 *   • ``tool_call`` / ``tool_result`` with ``stepIndex=N`` appends
 *     to step N's tools.
 *   • ``step_done`` with ``stepIndex=N`` closes the step (the SPA
 *     auto-collapses its body while a later step is active so
 *     the chat stays scannable).
 *   • Once the agent stops emitting tools the final ``text_delta``
 *     run is exposed as ``finalAnswer`` rather than a step.
 */

import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import {
  Box, Chip, CircularProgress, IconButton, Stack, Tooltip, Typography,
} from '@mui/material'
import AccountTreeIcon from '@mui/icons-material/AccountTree'
import ExpandLessIcon from '@mui/icons-material/ExpandLess'
import ExpandMoreIcon from '@mui/icons-material/ExpandMore'
import PsychologyIcon from '@mui/icons-material/Psychology'
import CheckCircleIcon from '@mui/icons-material/CheckCircle'
import BuildIcon from '@mui/icons-material/Build'
import MemoryIcon from '@mui/icons-material/MemoryOutlined'
import MenuBookIcon from '@mui/icons-material/MenuBookOutlined'
import EditNoteIcon from '@mui/icons-material/EditNote'
import AutoAwesomeIcon from '@mui/icons-material/AutoAwesome'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkBreaks from 'remark-breaks'

import { normalizeChatMarkdown } from './normalizeChatMarkdown'
import EnlaceDelChat from './EnlaceDelChat'

import type { SubStep, ToolStatus } from './ToolStepGroup'
import type { AgentPersona } from '../../hooks/useManifest'
import BlinkingCursor from './BlinkingCursor'
import AssistantBubble from './AssistantBubble'
import RuleProposalCard from './RuleProposalCard'

// ─────────────────────────────────────────────────────────────────────
// Types
// ─────────────────────────────────────────────────────────────────────

export interface TurnTool {
  id: string
  name: string
  status: ToolStatus
  args?: unknown
  result?: unknown
  startedAt: number
  finishedAt?: number
  // v7.167.0 — what the subagent did, for ``task`` rows. See SubAgentRow.
  sub?: SubStep[]
}

export interface TurnStep {
  id: string
  index: number          // 0-based step counter (matches backend stepIndex)
  reasoning: string      // accumulated text emitted INSIDE this step
  tools: TurnTool[]
  closed: boolean        // true once ``step_done`` arrived
  startedAt: number
}

export interface AssistantTurnProps {
  steps: TurnStep[]
  /** What the employee SAID to the person between steps («the export finished and
   *  found a problem…»), in order. Shown outside the collapsed steps, before the
   *  answer: inside them it looked like reasoning and nobody read it (30/09/2026). */
  progressTexts?: string[]
  finalAnswer: string
  streaming: boolean
  modelId?: string
  durationMs?: number | null
  credits?: number | null
  /** HOW it was resolved: `atajo` (a declared call, without starting the agent) or
   *  `recepcion` (handled by the written conversation). Empty = normal turn. */
  resuelto?: { atajo?: string; recepcion?: string } | null
  /** 15.12.0 — «Sigo trabajando · consultando el ERP (3 min)» while a step takes long, so a
   *  long wait does not look like a hang. Already translated; null = nothing to say. */
  latido?: string | null
  /** "AI employee" persona so the reply avatar shows the employee's
   * face/initial instead of a generic robot icon. */
  persona?: AgentPersona | null
  agentName?: string
  /** Re-run the user message that produced this turn (last turn only). */
  onRegenerate?: () => void
  /** v7.140.0 — show the inline "asks you for permission" card under this turn. Only
   * the last settled turn passes true: an agreement reached in THIS exchange is
   * approved here, anything older belongs in the Aprendizaje queue. */
  allowRuleProposals?: boolean
}

// ─────────────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────────────

const MCP_PREFIXES = ['wai-data_', 'wai-presentations_', 'wai-documents_', 'wai-objectives_']

function splitToolName(full: string): { server: string | null; short: string } {
  if (!full) return { server: null, short: '' }
  for (const p of MCP_PREFIXES) {
    if (full.startsWith(p)) return { server: p.slice(0, -1), short: full.slice(p.length) }
  }
  if (full.includes('_')) {
    const idx = full.indexOf('_')
    return { server: full.slice(0, idx), short: full.slice(idx + 1) }
  }
  if (full.includes('.')) {
    const idx = full.indexOf('.')
    return { server: full.slice(0, idx), short: full.slice(idx + 1) }
  }
  return { server: null, short: full }
}

function argsSummary(args: unknown): string {
  if (!args || typeof args !== 'object') return ''
  const entries = Object.entries(args as Record<string, unknown>)
  if (entries.length === 0) return ''
  const head = entries.slice(0, 2).map(([k, v]) => {
    let val = ''
    if (typeof v === 'string') val = v.length > 40 ? v.slice(0, 37) + '…' : v
    else if (typeof v === 'number' || typeof v === 'boolean') val = String(v)
    else val = JSON.stringify(v).slice(0, 40)
    return `${k}=${val}`
  })
  const more = entries.length - 2
  return more > 0 ? `${head.join(', ')} +${more}` : head.join(', ')
}

function pretty(value: unknown): string {
  if (value == null) return ''
  if (typeof value === 'string') return value
  try { return JSON.stringify(value, null, 2) } catch { return String(value) }
}

function formatDur(start?: number, end?: number): string | null {
  if (!start || !end || end < start) return null
  const ms = end - start
  if (ms < 1000) return `${ms} ms`
  return `${(ms / 1000).toFixed(1)} s`
}

function previewOf(text: string, max = 90): string {
  const flat = text.replace(/\s+/g, ' ').trim()
  if (flat.length <= max) return flat
  return flat.slice(0, max - 1).trimEnd() + '…'
}

// ─────────────────────────────────────────────────────────────────────
// StepCard — header + collapsible body
// ─────────────────────────────────────────────────────────────────────

function StepCard({ step, isLast, isStreaming }: { step: TurnStep; isLast: boolean; isStreaming: boolean }) {
  const { t } = useTranslation()
  const active = isLast && isStreaming && !step.closed
  // U6: keep the active step expanded while the model is working;
  // auto-collapse completed steps so the chat reads top-down without
  // a wall of intermediate reasoning. The operator can always click
  // to reopen — once they touch the chevron we stop forcing the
  // state so we don't fight them on the next ``active`` flip.
  const userToggled = useRef(false)
  const [open, setOpen] = useState<boolean>(false)
  useEffect(() => {
    if (userToggled.current) return
    setOpen(active)
  }, [active])

  const toolCount = step.tools.length
  const okCount = step.tools.filter((s) => s.status === 'ok').length
  const errCount = step.tools.filter((s) => s.status === 'error').length

  const preview = step.reasoning
    ? previewOf(step.reasoning)
    : toolCount > 0
      ? t('agent.chat.stepPreviewTools', { count: toolCount })
      : t('agent.chat.stepPreviewWorking')

  return (
    <Box
      sx={{
        border: '1px solid',
        borderColor: 'divider',
        borderRadius: 1.5,
        overflow: 'hidden',
        bgcolor: 'background.paper',
        // Soft accent on the left edge that turns into the active
        // primary when the step is currently being worked on.
        borderLeft: '3px solid',
        borderLeftColor: active ? 'primary.main' : step.closed ? 'success.light' : 'divider',
        transition: 'border-color 200ms ease',
      }}
    >
      <Box
        onClick={() => { userToggled.current = true; setOpen((v) => !v) }}
        sx={{
          display: 'flex',
          alignItems: 'center',
          gap: 1,
          px: 1.25,
          py: 0.75,
          cursor: 'pointer',
          userSelect: 'none',
          '&:hover': { bgcolor: 'action.hover' },
        }}
      >
        {active
          ? <CircularProgress size={14} thickness={6} sx={{ color: 'primary.main' }} />
          : <PsychologyIcon sx={{ fontSize: 16, color: 'text.secondary' }} />}
        <Typography variant="caption" sx={{ fontWeight: 700, color: 'text.secondary', flexShrink: 0 }}>
          {t('agent.chat.step', { n: step.index + 1 })}
        </Typography>
        {toolCount > 0 && (
          <Tooltip title={t('agent.chat.toolsRunTooltip', { count: toolCount }) as string}>
            <Chip
              size="small"
              icon={<BuildIcon sx={{ fontSize: 12 }} />}
              label={t('agent.chat.toolsCount', { count: toolCount })}
              sx={{ height: 18, fontSize: '0.7rem', '& .MuiChip-label': { px: 0.5 } }}
            />
          </Tooltip>
        )}
        {errCount > 0 && (
          <Chip size="small" color="error" variant="outlined"
                label={t('agent.chat.toolsErrored', { count: errCount })}
                sx={{ height: 18, fontSize: '0.7rem' }} />
        )}
        {step.closed && okCount === toolCount && toolCount > 0 && (
          <Chip size="small" color="success" variant="outlined"
                icon={<CheckCircleIcon sx={{ fontSize: 12 }} />}
                label={t('agent.chat.stepOk')}
                sx={{ height: 18, fontSize: '0.7rem' }} />
        )}
        <Typography
          variant="caption"
          color="text.secondary"
          sx={{
            flex: 1, minWidth: 0,
            overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
            ml: 0.5,
          }}
        >
          {preview}
        </Typography>
        <IconButton
          size="small"
          sx={{ p: 0.25 }}
          aria-label={t(open ? 'agent.chat.collapseStep' : 'agent.chat.expandStep') as string}
        >
          {open ? <ExpandLessIcon sx={{ fontSize: 18 }} /> : <ExpandMoreIcon sx={{ fontSize: 18 }} />}
        </IconButton>
      </Box>

      {open && (
        <Box sx={{ px: 1.25, pb: 1, pt: 0.25, display: 'flex', flexDirection: 'column', gap: 0.5 }}>
          {step.reasoning.trim() && (
            <Box
              sx={{
                pl: 1,
                py: 0.25,
                borderLeft: '2px solid',
                borderColor: 'divider',
                color: 'text.secondary',
                fontSize: '0.85rem',
                '& p:last-child': { mb: 0 },
                '& p': { mb: 0.5 },
              }}
            >
              <ReactMarkdown
                remarkPlugins={[remarkGfm, remarkBreaks]}
              >
                {normalizeChatMarkdown(step.reasoning)}
              </ReactMarkdown>
              {active && step.tools.length === 0 && <BlinkingCursor />}
            </Box>
          )}
          {step.tools.map((tool) => (
            <ToolRow key={tool.id} tool={tool} live={isStreaming} />
          ))}
        </Box>
      )}
    </Box>
  )
}

/**
 * A ``task`` call — the agent delegating to a subagent (v7.167.0).
 *
 * Not a tool row: a whole nested run that can last minutes. It used to
 * render as a generic row with nothing inside, which read as "frozen"
 * while the subagent was in fact querying Odoo — measured once at seven
 * subagents over ten minutes with a blank screen throughout. Three
 * deliberate choices:
 *
 *  - the title is the subagent's **description**, not the word "task";
 *  - it **auto-expands while running** and folds when done, so the work
 *    is visible without the operator having to know to click;
 *  - the current step is echoed in the header, which is the single most
 *    useful thing at a glance ("… execute_sql").
 */
function SubAgentRow({ tool, live = true }: { tool: TurnTool; live?: boolean }) {
  const { t } = useTranslation()
  // "Running" means pending AND the turn is live. OpenCode does not always re-emit
  // the final state of the `task` before the turn ends, so relying on `status`
  // alone left the spinner turning forever over work already finished —
  // which is worse than not showing it: it says something is ongoing that already ended.
  const running = tool.status === 'pending' && live
  const [manual, setManual] = useState<boolean | null>(null)
  const open = manual ?? running
  const sub = tool.sub ?? []
  const args = (tool.args ?? {}) as Record<string, unknown>
  const title = typeof args.description === 'string' && args.description
    ? args.description
    : t('agent.chat.subagent', 'Subagente')
  const steps = sub.filter((s) => s.kind === 'tool')
  const notes = sub.filter((s) => s.kind === 'text')
  const last = steps[steps.length - 1]
  const dur = formatDur(tool.startedAt, tool.finishedAt)

  return (
    <Box
      sx={{
        border: '1px solid',
        borderColor: running ? 'primary.light' : 'divider',
        borderRadius: 1,
        bgcolor: (th) => (th.palette.mode === 'dark' ? 'background.default' : 'grey.50'),
      }}
    >
      <Box
        onClick={() => setManual(!open)}
        sx={{
          display: 'flex', alignItems: 'center', gap: 0.5, px: 0.75, py: 0.35,
          cursor: 'pointer', flexWrap: 'wrap', '&:hover': { bgcolor: 'action.hover' },
        }}
      >
        <AccountTreeIcon sx={{ fontSize: 13, color: 'primary.main', flexShrink: 0 }} />
        <Typography variant="caption" sx={{ fontWeight: 700 }}>{title}</Typography>
        {steps.length > 0 && (
          <Typography variant="caption" color="text.secondary">
            · {t('agent.chat.subagentSteps', '{{count}} pasos', { count: steps.length })}
          </Typography>
        )}
        {running && last?.name && (
          <Typography
            variant="caption"
            color="text.secondary"
            sx={{
              fontFamily: 'monospace', flex: 1, minWidth: 0,
              overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
            }}
          >
            · {splitToolName(last.name).short}
          </Typography>
        )}
        <Box sx={{ flex: 1 }} />
        <ToolStatusIndicator status={running ? 'pending' : (tool.status === 'error' ? 'error' : 'ok')} />
        {dur && (
          <Typography variant="caption" color="text.secondary" sx={{ flexShrink: 0 }}>{dur}</Typography>
        )}
      </Box>
      {open && (
        <Box sx={{ px: 0.75, pb: 0.5, pt: 0.25 }}>
          {steps.length === 0 && running && (
            <Typography variant="caption" color="text.secondary">
              {t('agent.chat.subagentStarting', 'Arrancando…')}
            </Typography>
          )}
          <Stack spacing={0.25} sx={{ borderLeft: '2px solid', borderColor: 'divider', pl: 1 }}>
            {steps.map((s) => {
              const { server, short } = splitToolName(s.name || '')
              const sum = argsSummary(s.args)
              return (
                <Box key={s.id} sx={{ display: 'flex', alignItems: 'center', gap: 0.5, minWidth: 0 }}>
                  <ToolStatusIndicator status={live ? (s.status ?? 'pending') : (s.status === 'error' ? 'error' : 'ok')} />
                  {server && (
                    <Typography variant="caption" color="text.secondary" sx={{ fontFamily: 'monospace', flexShrink: 0 }}>
                      {server}
                    </Typography>
                  )}
                  <Typography variant="caption" sx={{ fontFamily: 'monospace', flexShrink: 0 }}>{short}</Typography>
                  {sum && (
                    <Typography
                      variant="caption"
                      color="text.secondary"
                      sx={{
                        fontFamily: 'monospace', minWidth: 0,
                        overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                      }}
                    >
                      ({sum})
                    </Typography>
                  )}
                </Box>
              )
            })}
          </Stack>
          {notes.length > 0 && (
            <Box sx={{ mt: 0.75 }}>
              <Typography variant="caption" color="text.secondary" sx={{ fontWeight: 700 }}>
                {t('agent.chat.subagentResult', 'Lo que devolvió')}
              </Typography>
              <Box
                component="pre"
                sx={{
                  mt: 0.25, p: 0.75, bgcolor: 'action.hover', borderRadius: 0.75,
                  fontSize: '0.7rem', maxHeight: 200, overflow: 'auto',
                  whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                }}
              >
                {notes.map((n) => n.text).join('\n\n')}
              </Box>
            </Box>
          )}
        </Box>
      )}
    </Box>
  )
}

function ToolRow({ tool, live = true }: { tool: TurnTool; live?: boolean }) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  if (tool.name === 'task') return <SubAgentRow tool={tool} live={live} />
  const { server, short } = splitToolName(tool.name)
  const summary = argsSummary(tool.args)
  const dur = formatDur(tool.startedAt, tool.finishedAt)
  return (
    <Box
      sx={{
        border: '1px solid',
        borderColor: 'divider',
        borderRadius: 1,
        bgcolor: (th) => (th.palette.mode === 'dark' ? 'background.default' : 'grey.50'),
      }}
    >
      <Box
        onClick={() => setOpen((v) => !v)}
        sx={{
          display: 'flex',
          alignItems: 'center',
          gap: 0.5,
          px: 0.75,
          py: 0.35,
          cursor: 'pointer',
          flexWrap: 'wrap',
          '&:hover': { bgcolor: 'action.hover' },
        }}
      >
        <BuildIcon sx={{ fontSize: 13, color: 'text.secondary', flexShrink: 0 }} />
        {server && (
          <Typography variant="caption" color="text.secondary" sx={{ fontFamily: 'monospace', flexShrink: 0 }}>
            {server}
          </Typography>
        )}
        <Typography variant="caption" sx={{ fontFamily: 'monospace', fontWeight: 600 }}>
          {short}
        </Typography>
        {summary && (
          <Typography variant="caption" color="text.secondary" sx={{ fontFamily: 'monospace', ml: 0.25, flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            ({summary})
          </Typography>
        )}
        <Box sx={{ flex: 1 }} />
        <ToolStatusIndicator status={tool.status} />
        {dur && (
          <Typography variant="caption" color="text.secondary" sx={{ flexShrink: 0 }}>
            {dur}
          </Typography>
        )}
      </Box>
      {open && (
        <Box sx={{ px: 0.75, pb: 0.5, pt: 0.25 }}>
          {tool.args !== undefined && (
            <Box sx={{ mb: 0.5 }}>
              <Typography variant="caption" color="text.secondary" sx={{ fontWeight: 700 }}>{t('agent.chat.toolArgs')}</Typography>
              <Box
                component="pre"
                sx={{
                  mt: 0.25, p: 0.75,
                  bgcolor: 'action.hover',
                  borderRadius: 0.75,
                  fontSize: '0.7rem',
                  maxHeight: 220, overflow: 'auto',
                  whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                }}
              >
                {pretty(tool.args)}
              </Box>
            </Box>
          )}
          {tool.result !== undefined && (
            <Box>
              <Typography variant="caption" color="text.secondary" sx={{ fontWeight: 700 }}>{t('agent.chat.toolResult')}</Typography>
              <Box
                component="pre"
                sx={{
                  mt: 0.25, p: 0.75,
                  bgcolor: 'action.hover',
                  borderRadius: 0.75,
                  fontSize: '0.7rem',
                  maxHeight: 260, overflow: 'auto',
                  whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                }}
              >
                {pretty(tool.result)}
              </Box>
            </Box>
          )}
        </Box>
      )}
    </Box>
  )
}

function ToolStatusIndicator({ status }: { status: ToolStatus }) {
  if (status === 'pending') return <CircularProgress size={11} thickness={6} sx={{ mr: 0.25 }} />
  if (status === 'error') {
    return (
      <Chip size="small" color="error" variant="outlined" label="ERROR"
            sx={{ height: 16, fontSize: '0.6rem', '& .MuiChip-label': { px: 0.4 } }} />
    )
  }
  return (
    <Chip size="small" color="success" variant="outlined" label="OK"
          icon={<CheckCircleIcon sx={{ fontSize: 11 }} />}
          sx={{ height: 16, fontSize: '0.6rem', '& .MuiChip-label': { px: 0.4 } }} />
  )
}

// ─────────────────────────────────────────────────────────────────────
// StepsGroup — one collapsible "Razonamiento" block wrapping every step
// ─────────────────────────────────────────────────────────────────────

function StepsGroup({ steps, streaming }: { steps: TurnStep[]; streaming: boolean }) {
  const { t } = useTranslation()
  // Expanded live so the operator can watch progress; auto-collapsed
  // once the turn finishes so the transcript stays scannable. We stop
  // forcing the state after the first manual toggle.
  const userToggled = useRef(false)
  const [open, setOpen] = useState<boolean>(streaming)
  useEffect(() => {
    if (userToggled.current) return
    setOpen(streaming)
  }, [streaming])

  const totalTools = steps.reduce((n, s) => n + s.tools.length, 0)
  const last = steps[steps.length - 1]
  const preview = streaming && last
    ? (last.reasoning ? previewOf(last.reasoning, 80) : t('agent.chat.stepPreviewWorking'))
    : ''

  return (
    <Box
      sx={{
        border: '1px solid',
        borderColor: 'divider',
        borderRadius: 1.5,
        overflow: 'hidden',
        bgcolor: 'background.default',
        borderLeft: '3px solid',
        borderLeftColor: streaming ? 'primary.main' : 'success.light',
        transition: 'border-color 200ms ease',
      }}
    >
      <Box
        onClick={() => { userToggled.current = true; setOpen((v) => !v) }}
        sx={{
          display: 'flex', alignItems: 'center', gap: 1,
          px: 1.25, py: 0.75, cursor: 'pointer', userSelect: 'none',
          '&:hover': { bgcolor: 'action.hover' },
        }}
      >
        {streaming
          ? <CircularProgress size={14} thickness={6} sx={{ color: 'primary.main' }} />
          : <PsychologyIcon sx={{ fontSize: 16, color: 'text.secondary' }} />}
        <Typography variant="caption" sx={{ fontWeight: 700, color: 'text.secondary', flexShrink: 0 }}>
          {t('agent.chat.reasoningTitle')}
        </Typography>
        <Chip
          size="small"
          label={t('agent.chat.reasoningSteps', { count: steps.length })}
          sx={{ height: 18, fontSize: '0.7rem', '& .MuiChip-label': { px: 0.5 } }}
        />
        {totalTools > 0 && (
          <Chip
            size="small"
            icon={<BuildIcon sx={{ fontSize: 12 }} />}
            label={t('agent.chat.toolsCount', { count: totalTools })}
            sx={{ height: 18, fontSize: '0.7rem', '& .MuiChip-label': { px: 0.5 } }}
          />
        )}
        <Typography
          variant="caption" color="text.secondary"
          sx={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', ml: 0.5 }}
        >
          {preview}
        </Typography>
        <IconButton
          size="small" sx={{ p: 0.25 }}
          aria-label={t(open ? 'agent.chat.collapseReasoning' : 'agent.chat.expandReasoning') as string}
        >
          {open ? <ExpandLessIcon sx={{ fontSize: 18 }} /> : <ExpandMoreIcon sx={{ fontSize: 18 }} />}
        </IconButton>
      </Box>

      {open && (
        <Box sx={{ px: 1, pb: 1, pt: 0.5 }}>
          <Stack spacing={0.75}>
            {steps.map((s, i) => (
              <StepCard
                key={s.id}
                step={s}
                isLast={i === steps.length - 1}
                isStreaming={streaming}
              />
            ))}
          </Stack>
        </Box>
      )}
    </Box>
  )
}

// ─────────────────────────────────────────────────────────────────────
// SourcesBar — "Fuentes consultadas": surfaces, in business language, when
// the employee leaned on the operator's MEMORY or KNOWLEDGE to reach its
// answer. These are buried in the technical tool log otherwise; here they're
// promoted to a friendly, expandable strip right under the reply so the
// operator can see (and trust) what informed the conclusion.
// ─────────────────────────────────────────────────────────────────────

type SourceKind = 'memory' | 'memory_write' | 'knowledge' | 'learning' | 'learning_write'

// Match a tool name to a source kind regardless of the MCP server prefix
// (``search_memory``, ``ventas-app_search_memory`` and ``app.search_memory``
// all count). We test the WHOLE name's tail rather than splitting on ``_``,
// because the tool names themselves contain underscores.
const MEMORY_READ_TOOLS = ['search_memory', 'list_memory']
const MEMORY_WRITE_TOOLS = ['remember_fact', 'forget_fact', 'compact_memory']
// v7.140.0 — the SECOND memory axis: what the employee learned about the
// ENVIRONMENT (not about a person). Kept as its own kind on purpose: "I read
// your profile" and "I used what I learned about your cluster" are different claims,
// and collapsing them would make the strip lie about where the answer came from.
const LEARNING_READ_TOOLS = ['search_learnings']
const LEARNING_WRITE_TOOLS = ['remember_learning', 'forget_learning',
  'compact_learnings', 'propose_silence']

function endsWithTool(name: string, tool: string): boolean {
  return name === tool
    || name.endsWith(`_${tool}`)
    || name.endsWith(`.${tool}`)
    || name.endsWith(`-${tool}`)
}

function sourceKind(rawName: string): SourceKind | null {
  const n = (rawName || '').toLowerCase()
  if (MEMORY_READ_TOOLS.some((t) => endsWithTool(n, t))) return 'memory'
  if (MEMORY_WRITE_TOOLS.some((t) => endsWithTool(n, t))) return 'memory_write'
  if (LEARNING_READ_TOOLS.some((t) => endsWithTool(n, t))) return 'learning'
  if (LEARNING_WRITE_TOOLS.some((t) => endsWithTool(n, t))) return 'learning_write'
  if (n.includes('document') || n.includes('knowledge') || n.includes('conocimiento')) return 'knowledge'
  return null
}

function resultPreview(value: unknown, max = 180): string {
  if (value == null) return ''
  let s = typeof value === 'string' ? value : pretty(value)
  s = s.replace(/\s+/g, ' ').trim()
  return s.length > max ? `${s.slice(0, max - 1).trimEnd()}…` : s
}

function SourceDetail({ tool }: { tool: TurnTool }) {
  const { t } = useTranslation()
  const query = argsSummary(tool.args)
  const preview = resultPreview(tool.result)
  return (
    <Box sx={{ pl: 1, py: 0.25, borderLeft: '2px solid', borderColor: 'divider' }}>
      {query && (
        <Typography variant="caption" sx={{ display: 'block', fontFamily: 'monospace', color: 'text.secondary' }}>
          {t('agent.chat.sources.query')}: {query}
        </Typography>
      )}
      {preview
        ? <Typography variant="caption" sx={{ display: 'block', color: 'text.primary' }}>{preview}</Typography>
        : tool.status === 'pending'
          ? <Typography variant="caption" color="text.secondary">{t('agent.chat.sources.consulting')}</Typography>
          : null}
    </Box>
  )
}

function SourcesBar({ tools }: { tools: TurnTool[] }) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const classified = tools
    .map((tl) => ({ tl, kind: sourceKind(tl.name) }))
    .filter((x): x is { tl: TurnTool; kind: SourceKind } => x.kind != null)
  if (classified.length === 0) return null

  const memory = classified.filter((x) => x.kind === 'memory').map((x) => x.tl)
  const writes = classified.filter((x) => x.kind === 'memory_write').map((x) => x.tl)
  const knowledge = classified.filter((x) => x.kind === 'knowledge').map((x) => x.tl)
  const learning = classified.filter((x) => x.kind === 'learning').map((x) => x.tl)
  const learned = classified.filter((x) => x.kind === 'learning_write').map((x) => x.tl)
  const consulted = [...memory, ...knowledge, ...learning]

  return (
    <Box
      sx={{
        mt: 1,
        border: '1px solid',
        borderColor: 'divider',
        borderLeft: '3px solid',
        borderLeftColor: 'info.main',
        borderRadius: 1.5,
        bgcolor: (th) => (th.palette.mode === 'dark' ? 'background.default' : 'info.50'),
        overflow: 'hidden',
      }}
    >
      <Box
        onClick={() => setOpen((v) => !v)}
        sx={{
          display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap',
          px: 1.25, py: 0.75, cursor: consulted.length ? 'pointer' : 'default',
          userSelect: 'none', '&:hover': consulted.length ? { bgcolor: 'action.hover' } : undefined,
        }}
      >
        <Typography variant="caption" sx={{ fontWeight: 700, color: 'text.secondary' }}>
          {t('agent.chat.sources.title')}
        </Typography>
        {memory.length > 0 && (
          <Chip
            size="small" variant="outlined" color="info"
            icon={<MemoryIcon sx={{ fontSize: 14 }} />}
            label={t('agent.chat.sources.memory', { count: memory.length })}
            sx={{ height: 20, fontSize: '0.7rem', '& .MuiChip-label': { px: 0.6 } }}
          />
        )}
        {knowledge.length > 0 && (
          <Chip
            size="small" variant="outlined" color="info"
            icon={<MenuBookIcon sx={{ fontSize: 14 }} />}
            label={t('agent.chat.sources.knowledge', { count: knowledge.length })}
            sx={{ height: 20, fontSize: '0.7rem', '& .MuiChip-label': { px: 0.6 } }}
          />
        )}
        {learning.length > 0 && (
          <Chip
            size="small" variant="outlined" color="secondary"
            icon={<AutoAwesomeIcon sx={{ fontSize: 14 }} />}
            label={t('agent.chat.sources.learning', 'lo aprendido del entorno ({{count}})', { count: learning.length })}
            sx={{ height: 20, fontSize: '0.7rem', '& .MuiChip-label': { px: 0.6 } }}
          />
        )}
        {learned.length > 0 && (
          <Tooltip title={t('agent.chat.sources.learningWriteTooltip', 'Ha guardado algo que ha aprendido del entorno. Puedes verlo y corregirlo en Operaciones → Aprendizaje.') as string}>
            <Chip
              size="small" variant="outlined" color="secondary"
              icon={<AutoAwesomeIcon sx={{ fontSize: 14 }} />}
              label={t('agent.chat.sources.learningWrite', 'ha aprendido ({{count}})', { count: learned.length })}
              sx={{ height: 20, fontSize: '0.7rem', '& .MuiChip-label': { px: 0.6 } }}
            />
          </Tooltip>
        )}
        {writes.length > 0 && (
          <Tooltip title={t('agent.chat.sources.writeTooltip') as string}>
            <Chip
              size="small" variant="outlined"
              icon={<EditNoteIcon sx={{ fontSize: 14 }} />}
              label={t('agent.chat.sources.memoryWrite', { count: writes.length })}
              sx={{ height: 20, fontSize: '0.7rem', '& .MuiChip-label': { px: 0.6 } }}
            />
          </Tooltip>
        )}
        <Box sx={{ flex: 1 }} />
        {consulted.length > 0 && (
          <IconButton size="small" sx={{ p: 0.25 }} aria-label={t(open ? 'agent.chat.sources.collapse' : 'agent.chat.sources.expand') as string}>
            {open ? <ExpandLessIcon sx={{ fontSize: 18 }} /> : <ExpandMoreIcon sx={{ fontSize: 18 }} />}
          </IconButton>
        )}
      </Box>
      {open && consulted.length > 0 && (
        <Box sx={{ px: 1.25, pb: 1, pt: 0.25, display: 'flex', flexDirection: 'column', gap: 0.75 }}>
          {memory.length > 0 && (
            <Box>
              <Typography variant="caption" sx={{ fontWeight: 700, color: 'text.secondary', display: 'block', mb: 0.25 }}>
                {t('agent.chat.sources.memoryHeading')}
              </Typography>
              <Stack spacing={0.5}>{memory.map((tl) => <SourceDetail key={tl.id} tool={tl} />)}</Stack>
            </Box>
          )}
          {knowledge.length > 0 && (
            <Box>
              <Typography variant="caption" sx={{ fontWeight: 700, color: 'text.secondary', display: 'block', mb: 0.25 }}>
                {t('agent.chat.sources.knowledgeHeading')}
              </Typography>
              <Stack spacing={0.5}>{knowledge.map((tl) => <SourceDetail key={tl.id} tool={tl} />)}</Stack>
            </Box>
          )}
          {learning.length > 0 && (
            <Box>
              <Typography variant="caption" sx={{ fontWeight: 700, color: 'text.secondary', display: 'block', mb: 0.25 }}>
                {t('agent.chat.sources.learningHeading', 'Lo que ha aprendido del entorno')}
              </Typography>
              <Stack spacing={0.5}>{learning.map((tl) => <SourceDetail key={tl.id} tool={tl} />)}</Stack>
            </Box>
          )}
        </Box>
      )}
    </Box>
  )
}

// ─────────────────────────────────────────────────────────────────────
// AssistantTurn — public component
// ─────────────────────────────────────────────────────────────────────

export function AssistantTurn({
  steps,
  progressTexts = [],
  finalAnswer,
  streaming,
  modelId,
  durationMs,
  credits,
  resuelto,
  latido,
  persona,
  agentName,
  onRegenerate,
  allowRuleProposals = false,
}: AssistantTurnProps) {
  // While the agent is working AND no final answer has arrived yet
  // the bubble shows a "Pensando…" hint. Once the answer starts
  // streaming the bubble renders the markdown live.
  const showThinking = streaming && !finalAnswer.trim() && steps.every((s) => s.tools.length === 0 && !s.reasoning.trim())

  const visibleSteps = steps.filter(
    (s) => s.tools.length > 0 || s.reasoning.trim().length > 0,
  )

  // "Fuentes consultadas" — once the answer exists, show whether the employee
  // leaned on the operator's memory / knowledge to reach it.
  const allTools = steps.flatMap((s) => s.tools)
  const showSources = !showThinking && (finalAnswer.trim().length > 0 || !streaming)

  return (
    <Box sx={{ mb: 1.5 }}>
      {visibleSteps.length > 0 && (
        <Box sx={{ pl: 5, pr: 1, mb: finalAnswer.trim() || streaming ? 1 : 0 }}>
          <StepsGroup steps={visibleSteps} streaming={streaming} />
        </Box>
      )}

      {streaming && latido && (
        <Box data-testid="chat-latido" sx={{ pl: 5, pr: 1, mb: 1, color: 'text.secondary', fontSize: 13,
                                             fontStyle: 'italic' }}>
          {latido}
        </Box>
      )}

      {/* What it told the person mid-work, in order, outside the collapsed steps. */}
      {progressTexts.length > 0 && (
        <Box
          data-testid="chat-progress-texts"
          sx={{
            pl: 5, pr: 1, mb: finalAnswer.trim() || streaming ? 1 : 0,
          }}
        >
          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1, borderLeft: '3px solid', borderColor: 'divider', pl: 1.5 }}>
            {progressTexts.map((texto, i) => (
              <Box key={i} className="markdown-content" sx={{ color: 'text.secondary', fontSize: 14, '& p:last-child': { mb: 0 } }}>
                <ReactMarkdown
                  remarkPlugins={[remarkGfm, remarkBreaks]}
                  components={{ a: ({ node: _node, ...props }) => <EnlaceDelChat {...props} /> }}
                >
                  {normalizeChatMarkdown(texto)}
                </ReactMarkdown>
              </Box>
            ))}
          </Box>
        </Box>
      )}

      {(finalAnswer.trim() || (!streaming && visibleSteps.length === 0) || showThinking) && (
        <AssistantBubble
          text={finalAnswer}
          streaming={streaming && !!finalAnswer}
          modelId={modelId}
          durationMs={durationMs ?? null}
          credits={credits ?? null}
          resuelto={resuelto ?? null}
          thinking={showThinking}
          persona={persona}
          agentName={agentName}
          onRegenerate={onRegenerate}
        />
      )}

      {showSources && (
        <Box sx={{ pl: 5, pr: 1 }}>
          <SourcesBar tools={allTools} />
        </Box>
      )}

      {allowRuleProposals && !streaming && (
        <Box sx={{ pl: 5, pr: 1 }}>
          <RuleProposalCard active />
        </Box>
      )}
    </Box>
  )
}

export default AssistantTurn
