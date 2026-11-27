import { useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { Alert, Button, Dialog, DialogActions, DialogContent, DialogTitle, Stack, TextField } from '@mui/material'

interface Props {
  abierta: boolean
  huesped: string
  onCerrar: () => void
  onConfirmar: (motivo: string) => Promise<void>
}

export default function CancelacionDialog({ abierta, huesped, onCerrar, onConfirmar }: Props) {
  const { t } = useTranslation()
  const [motivo, setMotivo] = useState('')
  const [guardando, setGuardando] = useState(false)
  const [error, setError] = useState(false)

  const confirmar = async (event: FormEvent) => {
    event.preventDefault()
    setGuardando(true)
    setError(false)
    try {
      await onConfirmar(motivo)
      setMotivo('')
      onCerrar()
    } catch {
      setError(true)
    } finally {
      setGuardando(false)
    }
  }

  return (
    <Dialog open={abierta} onClose={guardando ? undefined : onCerrar} fullWidth maxWidth="sm">
      <form onSubmit={confirmar}>
        <DialogTitle>{t('reservation.cancelTitle', { guest: huesped })}</DialogTitle>
        <DialogContent>
          <Stack spacing={2} sx={{ pt: 1 }}>
            {error && <Alert severity="error">{t('reservation.cancelError')}</Alert>}
            <TextField
              label={t('reservation.cancelReason')}
              value={motivo}
              onChange={(event) => setMotivo(event.target.value)}
              inputProps={{ maxLength: 500 }}
              multiline
              minRows={2}
              required
            />
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={onCerrar} disabled={guardando}>{t('relocations.cancel')}</Button>
          <Button type="submit" color="error" variant="contained" disabled={guardando || !motivo.trim()}>
            {guardando ? t('common.loading') : t('reservation.cancelAction')}
          </Button>
        </DialogActions>
      </form>
    </Dialog>
  )
}
