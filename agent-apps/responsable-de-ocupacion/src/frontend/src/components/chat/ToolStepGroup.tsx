import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import {
  Accordion, AccordionDetails, AccordionSummary,
  Box, Chip, CircularProgress, Stack, Typography,
} from '@mui/material'
import ExpandMoreIcon from '@mui/icons-material/ExpandMore'
import BuildIcon from '@mui/icons-material/Build'
import CheckCircleIcon from '@mui/icons-material/CheckCircle'
import ErrorOutlineIcon from '@mui/icons-material/ErrorOutline'

export type ToolStatus = 'pending' | 'ok' | 'error'

/**
 * One thing a subagent did. The ``task`` tool spawns a subagent that runs
 * its own loop for minutes; without these the row was a frozen "task" line
 * and the operator had no way to tell work from a hang (v7.167.0).
 */
export interface SubStep {
  id: string
  kind: 'tool' | 'text'
  name?: string
  status?: ToolStatus
  args?: unknown
  text?: string
  at: number                   // ms epoch
}

export interface ToolStep {
  id: string                   // unique key (partId or fallback hash)
  name: string                 // raw tool name as emitted by SSE
  status: ToolStatus
  args?: unknown               // any json-serialisable shape
  result?: unknown             // any json-serialisable shape
  startedAt?: number           // ms epoch
  finishedAt?: number          // ms epoch
  sub?: SubStep[]              // subagent activity, when ``name === 'task'``
}

export interface ToolStepGroupProps {
  steps: ToolStep[]
}

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

function StatusChip({ status }: { status: ToolStatus }) {
  const { t } = useTranslation()
  if (status === 'pending') {
    return (
      <CircularProgress
        size={12}
        thickness={6}
        sx={{ mr: 0.25 }}
      />
    )
  }
  if (status === 'error') {
    return (
      <Chip
        size="small"
        icon={<ErrorOutlineIcon sx={{ fontSize: 14 }} />}
        label={t('agent.chat.toolError')}
        color="error"
        variant="outlined"
        sx={{ height: 18, fontSize: '0.65rem', '& .MuiChip-label': { px: 0.5 } }}
      />
    )
  }
  return (
    <Chip
      size="small"
      icon={<CheckCircleIcon sx={{ fontSize: 14 }} />}
      label={t('agent.chat.toolOk')}
      color="success"
      variant="outlined"
      sx={{ height: 18, fontSize: '0.65rem', '& .MuiChip-label': { px: 0.5 } }}
    />
  )
}

/**
 * Vertical group of tool calls executed by the agent during a single
 * turn. Renders a compact line per step (icon + server prefix + tool
 * name + arg summary + status chip + duration), expandable to show
 * the full args / result JSON. Mirrors the OpenCodeChatPanel
 * StepGroup but trimmed: no plan-bucket grouping, no file-change
 * panel — those don't apply to a custom agent.
 */
export function ToolStepGroup({ steps }: ToolStepGroupProps) {
  const items = useMemo(() => steps ?? [], [steps])
  if (items.length === 0) return null
  return (
    <Box sx={{ pl: 5, pr: 1, mb: 1.5 }}>
      <Stack
        spacing={0.5}
        sx={{
          borderLeft: '2px solid',
          borderColor: 'divider',
          pl: 1.25,
          py: 0.5,
        }}
      >
        {items.map((step) => (
          <ToolStepRow key={step.id} step={step} />
        ))}
      </Stack>
    </Box>
  )
}

function ToolStepRow({ step }: { step: ToolStep }) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const { server, short } = splitToolName(step.name)
  const summary = argsSummary(step.args)
  const dur = formatDur(step.startedAt, step.finishedAt)
  return (
    <Accordion
      disableGutters
      elevation={0}
      expanded={open}
      onChange={() => setOpen((v) => !v)}
      sx={{
        bgcolor: 'transparent',
        '&::before': { display: 'none' },
        border: 'none',
      }}
    >
      <AccordionSummary
        expandIcon={<ExpandMoreIcon sx={{ fontSize: 16 }} />}
        sx={{
          minHeight: '28px',
          '&.Mui-expanded': { minHeight: '28px' },
          '& .MuiAccordionSummary-content': {
            my: 0.25,
            display: 'flex',
            alignItems: 'center',
            gap: 0.75,
            flexWrap: 'wrap',
          },
        }}
      >
        <BuildIcon sx={{ fontSize: 14, color: 'text.secondary' }} />
        {server && (
          <Typography variant="caption" color="text.secondary" sx={{ fontFamily: 'monospace' }}>
            {server}
          </Typography>
        )}
        <Typography
          variant="caption"
          sx={{ fontFamily: 'monospace', fontWeight: 600, wordBreak: 'break-all' }}
        >
          {short}
        </Typography>
        {summary && (
          <Typography variant="caption" color="text.secondary" sx={{ ml: 0.5, fontFamily: 'monospace' }}>
            ({summary})
          </Typography>
        )}
        <Box sx={{ flex: 1 }} />
        <StatusChip status={step.status} />
        {dur && (
          <Typography variant="caption" color="text.secondary">
            {dur}
          </Typography>
        )}
      </AccordionSummary>
      <AccordionDetails sx={{ pt: 0, pb: 0.5 }}>
        {step.args !== undefined && (
          <Box sx={{ mb: 0.5 }}>
            <Typography variant="caption" color="text.secondary" sx={{ fontWeight: 600 }}>
              {t('agent.chat.toolArgs')}
            </Typography>
            <Box
              component="pre"
              sx={{
                mt: 0.25,
                p: 1,
                bgcolor: 'action.hover',
                borderRadius: 1,
                fontSize: '0.7rem',
                maxHeight: 220,
                overflow: 'auto',
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
              }}
            >
              {pretty(step.args)}
            </Box>
          </Box>
        )}
        {step.result !== undefined && (
          <Box>
            <Typography variant="caption" color="text.secondary" sx={{ fontWeight: 600 }}>
              {t('agent.chat.toolResult')}
            </Typography>
            <Box
              component="pre"
              sx={{
                mt: 0.25,
                p: 1,
                bgcolor: 'action.hover',
                borderRadius: 1,
                fontSize: '0.7rem',
                maxHeight: 260,
                overflow: 'auto',
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
              }}
            >
              {pretty(step.result)}
            </Box>
          </Box>
        )}
      </AccordionDetails>
    </Accordion>
  )
}

export default ToolStepGroup
