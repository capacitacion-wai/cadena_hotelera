import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import {
  Accordion, AccordionDetails, AccordionSummary,
  Box, IconButton, Stack, Tooltip, Typography,
} from '@mui/material'
import ExpandMoreIcon from '@mui/icons-material/ExpandMore'
import PsychologyIcon from '@mui/icons-material/Psychology'
import ContentCopyIcon from '@mui/icons-material/ContentCopy'
import CheckIcon from '@mui/icons-material/Check'

export interface ReasoningBlockProps {
  text: string
  /** When true, the block stays collapsed by default — useful for
   * historical messages so the chat reads like a conversation, not a
   * stream of thoughts. */
  defaultCollapsed?: boolean
}

/**
 * Collapsible "Razonamiento" block. The agent emits internal-think
 * deltas as a separate SSE event (``reasoning``) that the page
 * accumulates here. Hidden by default once the response settles so
 * the chat stays readable; click to expand.
 */
export function ReasoningBlock({ text, defaultCollapsed = true }: ReasoningBlockProps) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(!defaultCollapsed)
  const [copied, setCopied] = useState(false)
  const onCopy = async (ev: React.MouseEvent) => {
    ev.stopPropagation()
    try {
      await navigator.clipboard.writeText(text || '')
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch { /* ignore */ }
  }
  if (!text) return null
  return (
    <Box sx={{ pl: 5, mb: 1 }}>
      <Accordion
        disableGutters
        elevation={0}
        expanded={open}
        onChange={() => setOpen((v) => !v)}
        sx={{
          bgcolor: 'transparent',
          border: '1px dashed',
          borderColor: 'divider',
          borderRadius: 1.5,
          '&::before': { display: 'none' },
        }}
      >
        <AccordionSummary
          expandIcon={<ExpandMoreIcon sx={{ fontSize: 16 }} />}
          sx={{
            minHeight: '32px',
            px: 1.25,
            '&.Mui-expanded': { minHeight: '32px' },
            '& .MuiAccordionSummary-content': {
              my: 0.5,
              display: 'flex',
              alignItems: 'center',
              gap: 0.75,
            },
          }}
        >
          <PsychologyIcon sx={{ fontSize: 14, color: 'text.secondary' }} />
          <Typography variant="caption" color="text.secondary" sx={{ fontWeight: 600 }}>
            {t('agent.chat.reasoning', 'Razonamiento')}
          </Typography>
          <Box sx={{ flex: 1 }} />
          <Tooltip title={copied ? t('agent.chat.copied', 'copiado') : t('agent.chat.copy', 'copiar')}>
            <IconButton size="small" onClick={onCopy} sx={{ p: 0.25 }}>
              {copied ? <CheckIcon sx={{ fontSize: 14 }} /> : <ContentCopyIcon sx={{ fontSize: 14 }} />}
            </IconButton>
          </Tooltip>
        </AccordionSummary>
        <AccordionDetails sx={{ pt: 0, pb: 1, px: 1.25 }}>
          <Stack spacing={0.5}>
            <Box
              component="pre"
              sx={{
                m: 0,
                p: 1,
                bgcolor: 'action.hover',
                borderRadius: 1,
                fontSize: '0.75rem',
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
                color: 'text.secondary',
                maxHeight: 320,
                overflow: 'auto',
              }}
            >
              {text}
            </Box>
          </Stack>
        </AccordionDetails>
      </Accordion>
    </Box>
  )
}

export default ReasoningBlock
