import { Alert, AlertTitle, Box, Button } from '@mui/material'
import ErrorOutlineIcon from '@mui/icons-material/ErrorOutline'
import RestartAltIcon from '@mui/icons-material/RestartAlt'
import { useTranslation } from 'react-i18next'

export interface ErrorBubbleProps {
  message: string
  /** A1: optional retry callback. When provided, render a "Reintentar"
   *  button that re-issues the same prompt without forcing the user
   *  to retype it. */
  onRetry?: () => void
  retrying?: boolean
}

/**
 * Inline error bubble — surfaces SSE / network failures without
 * crashing the chat thread. Replaces the previous "throw and lose
 * the conversation" path.
 */
export function ErrorBubble({ message, onRetry, retrying }: ErrorBubbleProps) {
  const { t } = useTranslation()
  return (
    <Box sx={{ pl: 5, mb: 1.5 }}>
      <Alert
        severity="error"
        variant="outlined"
        icon={<ErrorOutlineIcon fontSize="small" />}
        sx={{ borderRadius: 2 }}
        action={onRetry ? (
          <Button
            size="small"
            color="error"
            startIcon={<RestartAltIcon fontSize="small" />}
            onClick={onRetry}
            disabled={retrying}
            sx={{ textTransform: 'none' }}
          >
            {retrying
              ? t('agent.chat.retrying', 'Reintentando…')
              : t('agent.chat.retry', 'Reintentar')}
          </Button>
        ) : undefined}
      >
        <AlertTitle sx={{ fontSize: '0.85rem', fontWeight: 600 }}>
          {t('agent.chat.error', 'Error de la conversación')}
        </AlertTitle>
        {message}
      </Alert>
    </Box>
  )
}

export default ErrorBubble
