/**
 * R4: top-level ErrorBoundary — catches render-time exceptions
 * anywhere in the SPA and shows a recovery card instead of a white
 * screen. The previous behaviour was that a single page-level bug
 * (e.g. `valueFormatter` typo, undefined access) blanked the whole
 * UI; the operator had no way to know what happened or to recover.
 */

import { Component, type ReactNode } from 'react'
import { Alert, AlertTitle, Box, Button, Stack, Typography } from '@mui/material'
import RestartAltIcon from '@mui/icons-material/RestartAlt'
import ContentCopyIcon from '@mui/icons-material/ContentCopy'
import i18n from '../i18n/i18n'

interface Props { children: ReactNode }
interface State { error: Error | null; info?: string }

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: { componentStack?: string }) {
    // Surface in the browser console so the operator can grab it
    // even before reading the on-screen card.
    // eslint-disable-next-line no-console
    console.error('ErrorBoundary caught:', error, info)
    this.setState({ info: info.componentStack ?? '' })
  }

  copy = () => {
    const { error, info } = this.state
    const text = `${error?.name}: ${error?.message}\n${error?.stack}\n${info ?? ''}`
    navigator.clipboard?.writeText(text).catch(() => {})
  }

  render() {
    if (!this.state.error) return this.props.children
    const { error, info } = this.state
    return (
      <Box sx={{ p: 4, maxWidth: 720, mx: 'auto' }}>
        <Alert severity="error" variant="outlined" sx={{ borderRadius: 2 }} role="alert">
          <AlertTitle sx={{ fontWeight: 700 }}>
            {i18n.t('errorBoundary.title')}
          </AlertTitle>
          <Typography variant="body2" sx={{ mb: 2 }}>
            {i18n.t('errorBoundary.body')}
          </Typography>
          <Stack direction="row" spacing={1} sx={{ mb: 2 }}>
            <Button
              size="small"
              variant="contained"
              color="error"
              startIcon={<RestartAltIcon />}
              onClick={() => window.location.reload()}
            >
              {i18n.t('errorBoundary.reload')}
            </Button>
            <Button
              size="small"
              variant="outlined"
              color="error"
              startIcon={<ContentCopyIcon />}
              onClick={this.copy}
            >
              {i18n.t('errorBoundary.copy')}
            </Button>
          </Stack>
          <Typography
            variant="caption"
            component="pre"
            sx={{
              p: 1.5,
              bgcolor: 'background.default',
              borderRadius: 1,
              fontFamily: 'monospace',
              fontSize: 11,
              overflow: 'auto',
              maxHeight: 220,
              whiteSpace: 'pre-wrap',
            }}
          >
            {`${error?.name}: ${error?.message}\n${error?.stack ?? ''}\n${info ?? ''}`}
          </Typography>
        </Alert>
      </Box>
    )
  }
}

export default ErrorBoundary
