import { useTranslation } from 'react-i18next'
import { Box, Card, CardActionArea, Stack, Typography } from '@mui/material'
import BoltIcon from '@mui/icons-material/Bolt'
import type { AgentPersona } from '../../hooks/useManifest'
import { PersonaAvatar } from '../PersonaAvatar'

export interface EmptyChatCard {
  title: string
  description?: string
  prompt: string
}

export interface EmptyChatProps {
  agentName?: string
  agentTagline?: string
  /** The "AI employee" persona (mascot + greeting). Optional. */
  persona?: AgentPersona | null
  /** First-person greeting (resolved to the active locale by the
   * caller). When set it replaces the generic welcome title. */
  greeting?: string
  /** Optional list of capability cards. Each card describes one
   * thing the agent is good at and carries a `prompt` that, when
   * clicked, is dropped into the composer (lifted up by `onPick`).
   */
  cards?: EmptyChatCard[]
  onPick?: (prompt: string) => void
}

/**
 * "Welcome" view rendered when the chat history is empty. Shows the
 * agent's persona (mascot + greeting) and a 2x2 grid of capability
 * cards driven by the manifest. Clicking a card pre-fills the composer
 * so a non-technical operator can start a productive conversation in
 * one click.
 */
export function EmptyChat({
  agentName,
  agentTagline,
  persona,
  greeting,
  cards = [],
  onPick,
}: EmptyChatProps) {
  const { t } = useTranslation()
  const safe = (cards ?? []).slice(0, 4)
  const title =
    greeting ||
    (agentName
      ? t('agent.chat.welcomeNamed', { name: agentName, defaultValue: `Hola, soy ${agentName}` })
      : t('agent.chat.welcomeTitle', 'Hola, ¿en qué te ayudo?'))
  return (
    <Box
      sx={{
        height: '100%',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        gap: 3,
        px: 2,
        py: 4,
      }}
    >
      <Stack alignItems="center" spacing={1}>
        <PersonaAvatar persona={persona} size={64} status fallbackName={agentName} />
        <Typography variant="h6" fontWeight={600} textAlign="center">
          {title}
        </Typography>
        {agentTagline && (
          <Typography variant="body2" color="text.secondary" textAlign="center" sx={{ maxWidth: 520 }}>
            {agentTagline}
          </Typography>
        )}
        <Typography variant="caption" color="text.secondary" textAlign="center" sx={{ maxWidth: 480, mt: 0.5 }}>
          {t('agent.chat.conversationalHint')}
        </Typography>
        <Typography variant="caption" color="text.secondary" textAlign="center" sx={{ maxWidth: 480 }}>
          {t('agent.chat.memoryHint')}
        </Typography>
      </Stack>

      {safe.length > 0 && (
        <Box
          sx={{
            display: 'grid',
            gridTemplateColumns: {
              xs: '1fr',
              sm: 'repeat(2, minmax(220px, 320px))',
            },
            gap: 1.5,
            width: '100%',
            maxWidth: 720,
          }}
        >
          {safe.map((c, i) => (
            <Card
              key={i}
              variant="outlined"
              sx={{
                borderRadius: 2,
                transition: 'border-color 120ms ease, transform 120ms ease',
                '&:hover': { borderColor: 'primary.main', transform: 'translateY(-1px)' },
              }}
            >
              <CardActionArea
                onClick={() => onPick?.(c.prompt)}
                sx={{ p: 1.5, height: '100%', alignItems: 'flex-start', textAlign: 'left' }}
              >
                <Stack direction="row" spacing={1} alignItems="flex-start">
                  <BoltIcon sx={{ fontSize: 18, color: 'primary.main', mt: 0.25 }} />
                  <Box sx={{ minWidth: 0 }}>
                    <Typography variant="subtitle2" fontWeight={600} sx={{ lineHeight: 1.3 }}>
                      {c.title}
                    </Typography>
                    {c.description && (
                      <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.25 }}>
                        {c.description}
                      </Typography>
                    )}
                  </Box>
                </Stack>
              </CardActionArea>
            </Card>
          ))}
        </Box>
      )}
    </Box>
  )
}

export default EmptyChat
