/**
 * U6: shared empty-state card.
 *
 * Use across Dashboard / Runs / Events / DomainList / RunDetail
 * sections so an operator never sees a bland "Sin resultados" or
 * a silent zero-row table. Three variants:
 *
 *   - ``info`` (default): "nothing here yet, this is normal" — the
 *     standby-style copy with `severity="info"`.
 *   - ``warning``: "nothing here AND that's worth checking" — used
 *     when zero counts are expected to be rare.
 *   - ``error``: "we couldn't load anything" — paired with a retry.
 */

import type { ReactNode } from 'react'
import { Alert, AlertTitle, Box, Button, Stack, Typography } from '@mui/material'
import RestartAltIcon from '@mui/icons-material/RestartAlt'

export interface EmptyStateProps {
  title: string
  body?: string | ReactNode
  variant?: 'info' | 'warning' | 'error'
  actionLabel?: string
  onAction?: () => void
  icon?: ReactNode
}

export function EmptyState({
  title,
  body,
  variant = 'info',
  actionLabel,
  onAction,
  icon,
}: EmptyStateProps) {
  return (
    <Alert
      severity={variant}
      variant="outlined"
      icon={icon ?? undefined}
      sx={{ borderRadius: 2 }}
    >
      <AlertTitle sx={{ fontWeight: 600 }}>{title}</AlertTitle>
      {body && (
        <Stack spacing={1.5}>
          <Typography variant="body2">{body}</Typography>
          {actionLabel && onAction && (
            <Box>
              <Button
                size="small"
                color={variant === 'error' ? 'error' : 'primary'}
                variant="contained"
                startIcon={<RestartAltIcon />}
                onClick={onAction}
                sx={{ textTransform: 'none' }}
              >
                {actionLabel}
              </Button>
            </Box>
          )}
        </Stack>
      )}
    </Alert>
  )
}

export default EmptyState
