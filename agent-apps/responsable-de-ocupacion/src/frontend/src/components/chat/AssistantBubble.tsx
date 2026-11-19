import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import {
  Box, Chip, IconButton, Paper, Stack, Tooltip, Typography,
} from '@mui/material'
import ContentCopyIcon from '@mui/icons-material/ContentCopy'
import CheckIcon from '@mui/icons-material/Check'
import RefreshIcon from '@mui/icons-material/Refresh'
import BoltIcon from '@mui/icons-material/Bolt'
import ChatBubbleOutlineIcon from '@mui/icons-material/ChatBubbleOutline'
import Button from '@mui/material/Button'
import ReactMarkdown from 'react-markdown'
import EnlaceDelChat from './EnlaceDelChat'
import remarkGfm from 'remark-gfm'
import remarkBreaks from 'remark-breaks'
import rehypeHighlight from 'rehype-highlight'
import '../../styles/markdown.css'

import { normalizeChatMarkdown } from './normalizeChatMarkdown'
import { partirEnTrozos } from './tarjetas'
import TarjetaDeMapa from './TarjetaDeMapa'
import BlinkingCursor from './BlinkingCursor'
import { PersonaAvatar } from '../PersonaAvatar'
import type { AgentPersona } from '../../hooks/useManifest'

export interface AssistantBubbleProps {
  text: string
  streaming?: boolean
  modelId?: string
  durationMs?: number | null
  /** Per-turn cost expressed in agent credits (tenant rate applied
   * server-side). Shown in the footer instead of money. */
  credits?: number | null
  /** How it was resolved, when it was NOT through the normal path. It is shown because
   *  otherwise «this went direct» could only be deduced from the time and the credits —
   *  that is, guessed. And a shortcut that fires where it should not must be visible. */
  resuelto?: { atajo?: string; recepcion?: string } | null
  /** When true, renders an empty bubble with a "thinking" placeholder
   * (used while the agent is busy with a tool call and no text has
   * arrived yet). */
  thinking?: boolean
  /** The "AI employee" persona, so the reply avatar shows the employee's
   * face (assigned emoji/mascot) or first initial — never a generic
   * robot icon. */
  persona?: AgentPersona | null
  /** Fallback name for the avatar monogram when no persona name is set. */
  agentName?: string
  /** When provided (and not streaming), shows a "Regenerar" action in the
   * footer — re-runs the user message that produced this answer. Only the
   * LAST assistant turn gets it. */
  onRegenerate?: () => void
}

function formatDuration(ms?: number | null): string | null {
  if (ms == null || !Number.isFinite(ms) || ms <= 0) return null
  if (ms < 1000) return `${Math.round(ms)} ms`
  const s = ms / 1000
  if (s < 60) return `${s.toFixed(1)} s`
  const m = Math.floor(s / 60)
  const r = Math.round(s % 60)
  return `${m} m ${r} s`
}

function formatCredits(
  credits: number | null | undefined,
  lang: string,
  unit: string,
): string | null {
  if (credits == null || !Number.isFinite(credits) || credits <= 0) return null
  const txt = credits.toLocaleString(lang, {
    maximumFractionDigits: credits >= 1 ? 1 : 2,
  })
  return `${txt} ${unit}`
}

/**
 * Assistant chat bubble — markdown-rendered body, streaming-aware
 * caret, copy-to-clipboard button and a footer with the model id +
 * duration + cost (when available). Mirrors the OpenCode panel's
 * assistant bubble but trimmed: no plan / file-changes / clarifying
 * questions (those concepts don't exist for a custom agent app).
 */
export function AssistantBubble({
  text,
  streaming = false,
  modelId,
  durationMs,
  credits,
  resuelto,
  thinking = false,
  persona,
  agentName,
  onRegenerate,
}: AssistantBubbleProps) {
  const { t, i18n } = useTranslation()
  const [copied, setCopied] = useState(false)
  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(text || '')
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch {
      /* ignore — surface only when there's text to copy */
    }
  }

  const meta: string[] = []
  const dur = formatDuration(durationMs)
  // THE COST DOES NOT GO IN THE CHAT. Someone talking to their employee is not shopping:
  // seeing a price on every answer turns a conversation into a taximeter, and spending
  // already has its place —Operaciones → Consumo— where it is looked at when it is time to.
  if (modelId) meta.push(modelId)
  if (dur) meta.push(dur)

  // CARDS are split out BEFORE normalising the markdown: a fenced block spans several
  // lines and the renderer would show it as a code block with JSON inside. See
  // `components/chat/tarjetas.ts`.
  const trozos = partirEnTrozos(text || '')

  return (
    <Box sx={{ display: 'flex', gap: 1.25, alignItems: 'flex-start', mb: 1.5 }}>
      <Box sx={{ mt: 0.25 }}>
        <PersonaAvatar persona={persona} size={28} fallbackName={agentName} />
      </Box>
      <Box sx={{ flex: 1, minWidth: 0 }}>
        <Paper
          elevation={0}
          sx={{
            px: 2,
            py: 1.5,
            borderRadius: '18px 18px 18px 4px',
            bgcolor: (t) => (t.palette.mode === 'dark' ? 'grey.900' : 'grey.50'),
            border: '1px solid',
            borderColor: 'divider',
            position: 'relative',
            maxWidth: '92%',
            boxShadow: (t) => t.shadows[1],
            '&:hover .copy-btn': { opacity: 1 },
          }}
        >
          {thinking && !text ? (
            <Typography variant="body2" color="text.secondary">
              {t('agent.chat.thinking', 'pensando…')}
              <BlinkingCursor />
            </Typography>
          ) : (
            <Box className="markdown-content" sx={{ '& p:last-child': { mb: 0 } }}>
              {trozos.map((tr, i) => (tr.que === 'tarjeta' ? (
                <TarjetaDeMapa key={`tj-${i}`} tarjeta={tr.tarjeta} />
              ) : (
                <ReactMarkdown
                  key={`md-${i}`}
                  remarkPlugins={[remarkGfm, remarkBreaks]}
                  rehypePlugins={[rehypeHighlight]}
                  components={{
                    img: (props) => (
                      <img {...props} style={{ maxWidth: '100%', height: 'auto', borderRadius: 4 }} />
                    ),
                    // A link in the answer ALWAYS opens in another tab (navigating away
                    // replaces the conversation, and the turn may be in progress), and the one
                    // pointing to ITS application also opens with the session resolved: see
                    // `EnlaceDelChat`.
                    a: ({ node: _node, ...props }) => <EnlaceDelChat {...props} />,
                  }}
                >
                  {normalizeChatMarkdown(tr.texto)}
                </ReactMarkdown>
              )))}
              {streaming && <BlinkingCursor />}
            </Box>
          )}
          {!thinking && (text || '').length > 0 && !streaming && (
            <Tooltip title={copied ? t('agent.chat.copied', 'copiado') : t('agent.chat.copy', 'copiar')}>
              <IconButton
                size="small"
                className="copy-btn"
                onClick={onCopy}
                sx={{
                  position: 'absolute',
                  top: 6,
                  right: 6,
                  opacity: 0,
                  transition: 'opacity 120ms ease',
                  bgcolor: 'background.paper',
                  border: '1px solid',
                  borderColor: 'divider',
                }}
              >
                {copied ? <CheckIcon sx={{ fontSize: 14 }} /> : <ContentCopyIcon sx={{ fontSize: 14 }} />}
              </IconButton>
            </Tooltip>
          )}
        </Paper>
        {(meta.length > 0 || onRegenerate) && !streaming && (
          <Stack direction="row" spacing={0.75} alignItems="center" sx={{ mt: 0.5, pl: 0.5, flexWrap: 'wrap' }}>
            {/* HOW IT WAS RESOLVED, first and in colour: it is what explains why this turn
                took 2 seconds and the next one twenty. It only appears when it was NOT the
                normal path — marking every turn would be noise. */}
            {resuelto?.atajo && (
              <Tooltip title={t('agent.chat.resuelto.atajoAyuda',
                'Su definición declara que esta capacidad se resuelve con una sola llamada, '
                + 'así que se contestó sin arrancar al agente: ni razonó, ni reenvió la '
                + 'conversación, ni miró el catálogo de herramientas.') as string}>
                <Chip
                  icon={<BoltIcon sx={{ fontSize: 13 }} />}
                  label={t('agent.chat.resuelto.atajo', 'Directo · {{c}}')
                    .replace('{{c}}', resuelto.atajo)}
                  size="small" color="success" variant="filled"
                  sx={{ height: 18, fontSize: '0.7rem', '& .MuiChip-label': { px: 0.75 } }}
                />
              </Tooltip>
            )}
            {!resuelto?.atajo && (resuelto?.recepcion === 'contesto' || resuelto?.recepcion === 'pregunto') && (
              <Tooltip title={t('agent.chat.resuelto.recepcionAyuda',
                'Esto no necesitaba trabajo —una cortesía, algo general, o faltaba un dato—, '
                + 'así que se atendió en la conversación sin arrancar al agente.') as string}>
                <Chip
                  icon={<ChatBubbleOutlineIcon sx={{ fontSize: 12 }} />}
                  label={resuelto.recepcion === 'pregunto'
                    ? t('agent.chat.resuelto.pregunto', 'Te ha preguntado antes de trabajar')
                    : t('agent.chat.resuelto.contesto', 'Respondido en la conversación')}
                  size="small" color="info" variant="outlined"
                  sx={{ height: 18, fontSize: '0.7rem', '& .MuiChip-label': { px: 0.75 } }}
                />
              </Tooltip>
            )}
            {meta.map((m, i) => (
              <Chip
                key={`${i}-${m}`}
                label={m}
                size="small"
                variant="outlined"
                sx={{ height: 18, fontSize: '0.7rem', '& .MuiChip-label': { px: 0.75 } }}
              />
            ))}
            {onRegenerate && !thinking && (
              <Button
                size="small"
                variant="text"
                color="inherit"
                startIcon={<RefreshIcon sx={{ fontSize: 14 }} />}
                onClick={onRegenerate}
                sx={{ textTransform: 'none', minWidth: 0, py: 0, px: 0.75, fontSize: '0.7rem', color: 'text.secondary' }}
              >
                {t('agent.chat.regenerate', 'Regenerar')}
              </Button>
            )}
          </Stack>
        )}
      </Box>
    </Box>
  )
}

export default AssistantBubble
