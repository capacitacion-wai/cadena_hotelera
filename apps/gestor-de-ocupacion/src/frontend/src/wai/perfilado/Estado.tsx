/**
 * Profiling module — loading and error states of its screens (theme only, no colours of its own).
 *
 * INVARIANT FILE — installed by `wai-app-builder/assets/modulos.py`; do not edit it.
 */
import { Alert, Box, CircularProgress, Typography } from '@mui/material'
import { Boton } from './adaptador'
import { mensajeDeError, useTextos } from './textos'

export function Cargando() {
  const t = useTextos()
  return (
    <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, py: 4 }}>
      <CircularProgress size={20} />
      <Typography variant="body2" color="text.secondary">{t('cargando')}</Typography>
    </Box>
  )
}

export function ErrorDelModulo({ error, reintentar }: { error: unknown; reintentar?: () => void }) {
  const t = useTextos()
  return (
    <Alert severity="warning" action={reintentar ? <Boton color="inherit" size="small" onClick={reintentar}>{t('reintentar')}</Boton> : undefined}>
      {mensajeDeError(t, error)}
    </Alert>
  )
}
