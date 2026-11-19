import { useState } from 'react'
import {
  Avatar, Box, Button, Chip, Dialog, DialogContent, DialogTitle,
  IconButton, Paper, Stack, Typography, alpha,
} from '@mui/material'
import CloseIcon from '@mui/icons-material/Close'
import RecordVoiceOverIcon from '@mui/icons-material/RecordVoiceOver'
import PersonIcon from '@mui/icons-material/Person'
import InsertDriveFileIcon from '@mui/icons-material/InsertDriveFileOutlined'

export interface UserBubbleAttachment {
  filename: string
  workspace_path: string
  size?: number
  mime?: string
}

/** A spoken exchange: who and what. */
export interface TurnoHablado { de: 'tu' | 'el'; texto: string }

export interface UserBubbleProps {
  text: string
  /** Files the operator attached to this turn (shown as chips under the text). */
  attachments?: UserBubbleAttachment[]
  /** The SPOKEN conversation that led to this request.
   *
   *  When the request was not typed by you but came from talking, the text above is a
   *  summary written by the voice —and keeping only that erases how it got there—. It is
   *  stored hanging from the request, folded, so that the chat stays the complete record
   *  without turning into an unreadable transcript. */
  hablado?: TurnoHablado[]
}

/**
 * User chat bubble — left-aligned avatar + tinted Paper that wraps
 * pre-formatted text. Whitespace is preserved so multi-line prompts
 * read as written (no markdown rendering: the user's input is
 * literal). Mirrors the visual language of OpenCodeChatPanel.UserBubble
 * but kept lean (no clarifying-questions handling). Any attached files
 * are listed as chips beneath the text.
 */
export function UserBubble({ text, attachments, hablado }: UserBubbleProps) {
  const files = attachments ?? []
  const charla = hablado ?? []
  const [abierta, setAbierta] = useState(false)
  return (
    <Box sx={{ display: 'flex', gap: 1.25, alignItems: 'flex-start', mb: 1.5, justifyContent: 'flex-end' }}>
      <Paper
        elevation={0}
        sx={{
          px: 2,
          py: 1.25,
          borderRadius: '18px 18px 4px 18px',
          bgcolor: 'primary.main',
          color: 'primary.contrastText',
          maxWidth: '78%',
          minWidth: 0,
          boxShadow: (t) => t.shadows[1],
        }}
      >
        {text && (
          <Typography
            variant="body2"
            sx={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}
          >
            {text}
          </Typography>
        )}
        {files.length > 0 && (
          <Stack direction="row" spacing={0.75} flexWrap="wrap" useFlexGap sx={{ mt: text ? 1 : 0 }}>
            {files.map((f) => (
              <Chip
                key={f.workspace_path}
                size="small"
                icon={<InsertDriveFileIcon sx={{ fontSize: 15, color: 'inherit !important' }} />}
                label={f.filename}
                sx={{
                  maxWidth: 220,
                  bgcolor: 'rgba(255,255,255,0.18)',
                  color: 'primary.contrastText',
                  '& .MuiChip-label': { overflow: 'hidden', textOverflow: 'ellipsis' },
                }}
              />
            ))}
          </Stack>
        )}
        {/* Only the trigger. The content goes in a MODAL and not in here: inside the
            bubble its lines ended up as dark text on blue —unreadable— and a ten-exchange
            chat swelled the request until it hid it. What is read at a glance is WHAT was
            asked; how it came about, one click away. */}
        {charla.length > 0 && (
          <Button
            size="small"
            startIcon={<RecordVoiceOverIcon fontSize="small" />}
            onClick={() => setAbierta(true)}
            sx={{
              mt: 1, px: 1, textTransform: 'none', color: 'inherit', opacity: 0.85,
              '&:hover': { opacity: 1, bgcolor: 'rgba(255,255,255,0.16)' },
            }}
          >
            <Typography variant="caption">
              {`La conversación que llevó aquí (${charla.length})`}
            </Typography>
          </Button>
        )}
      </Paper>
      <Avatar
        sx={{
          width: 28,
          height: 28,
          bgcolor: 'primary.dark',
          color: 'primary.contrastText',
          mt: 0.25,
          fontSize: 14,
          flexShrink: 0,
        }}
      >
        <PersonIcon sx={{ fontSize: 16 }} />
      </Avatar>
      {/* The spoken conversation, in its place and with real contrast: your sentences on
          the right with the accent tint, its sentences on the left on a neutral surface.
          It is the record of how that request came about, so it reads as a conversation
          and not as a dump. */}
      <Dialog open={abierta} onClose={() => setAbierta(false)} maxWidth="sm" fullWidth
              PaperProps={{ sx: { borderRadius: 3 } }}>
        <DialogTitle sx={{ pb: 1 }}>
          <Stack direction="row" spacing={1.25} alignItems="center">
            <RecordVoiceOverIcon color="primary" />
            <Box sx={{ flex: 1 }}>
              <Typography variant="subtitle1" sx={{ fontWeight: 600, lineHeight: 1.2 }}>
                La conversación que llevó aquí
              </Typography>
              <Typography variant="caption" color="text.secondary">
                {`${charla.length} intercambios hablados antes de esta petición`}
              </Typography>
            </Box>
            <IconButton size="small" onClick={() => setAbierta(false)} aria-label="Cerrar">
              <CloseIcon fontSize="small" />
            </IconButton>
          </Stack>
        </DialogTitle>
        <DialogContent sx={{ pt: 1 }}>
          <Stack spacing={1.25}>
            {charla.map((c, i) => (
              <Box key={i} sx={{ display: 'flex', justifyContent: c.de === 'tu' ? 'flex-end' : 'flex-start' }}>
                <Box sx={{ maxWidth: '82%' }}>
                  <Typography variant="caption"
                              sx={{ display: 'block', mb: 0.25, px: 0.5,
                                    textAlign: c.de === 'tu' ? 'right' : 'left',
                                    color: 'text.disabled', letterSpacing: '.04em',
                                    textTransform: 'uppercase', fontSize: 10 }}>
                    {c.de === 'tu' ? 'Tú' : 'Él'}
                  </Typography>
                  <Paper elevation={0} sx={(th) => ({
                    px: 1.5, py: 1,
                    borderRadius: 2.5,
                    borderTopRightRadius: c.de === 'tu' ? 6 : 20,
                    borderTopLeftRadius: c.de === 'tu' ? 20 : 6,
                    bgcolor: c.de === 'tu' ? alpha(th.palette.primary.main, 0.12) : 'action.hover',
                    color: 'text.primary',
                  })}>
                    <Typography variant="body2" sx={{ whiteSpace: 'pre-wrap' }}>{c.texto}</Typography>
                  </Paper>
                </Box>
              </Box>
            ))}
          </Stack>

          <Typography variant="caption" color="text.secondary"
                      sx={{ display: 'block', mt: 2.5, mb: 0.75, letterSpacing: '.04em',
                            textTransform: 'uppercase', fontSize: 10 }}>
            Y esto es lo que se le pidió
          </Typography>
          <Paper variant="outlined" sx={{ p: 1.5, borderRadius: 2, bgcolor: 'background.default' }}>
            <Typography variant="body2" sx={{ whiteSpace: 'pre-wrap' }}>{text}</Typography>
          </Paper>
        </DialogContent>
      </Dialog>
    </Box>
  )
}

export default UserBubble
