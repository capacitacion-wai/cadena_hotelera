import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import {
  Button, Dialog, DialogActions, DialogContent, DialogTitle, FormControl, FormHelperText,
  InputLabel, MenuItem, Select, Stack, TextField, Typography,
} from '@mui/material'
import { formatearImporte, formatearNumero } from '../utils/formato'
import type { DatosReubicaciones } from '../services/gestor'

interface Props {
  abierto: boolean
  datos: DatosReubicaciones
  guardando: boolean
  cerrar: () => void
  guardar: (entrada: { reserva_id: number; hotel_destino_id: number; motivo: string }) => void
}

export default function PropuestaDialog({ abierto, datos, guardando, cerrar, guardar }: Props) {
  const { t } = useTranslation()
  const [reservaId, setReservaId] = useState('')
  const [destinoId, setDestinoId] = useState('')
  const [motivo, setMotivo] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    if (!abierto) return
    setReservaId('')
    setDestinoId('')
    setMotivo('')
    setError('')
  }, [abierto])

  const enviar = () => {
    if (!reservaId || !destinoId) {
      setError(t('relocations.mustChoose'))
      return
    }
    if (!motivo.trim()) {
      setError(t('relocations.mustExplain'))
      return
    }
    guardar({ reserva_id: Number(reservaId), hotel_destino_id: Number(destinoId), motivo: motivo.trim() })
  }

  return (
    <Dialog open={abierto} onClose={cerrar} fullWidth maxWidth="sm">
      <DialogTitle>{t('relocations.dialogTitle')}</DialogTitle>
      <DialogContent>
        <Stack spacing={2.5} sx={{ pt: 1 }}>
          {error && <Typography role="alert" color="error.main">{error}</Typography>}
          <FormControl fullWidth required>
            <InputLabel id="guest-label">{t('relocations.chooseGuest')}</InputLabel>
            <Select labelId="guest-label" label={t('relocations.chooseGuest')} value={reservaId} onChange={(event) => setReservaId(event.target.value)}>
              {datos.huespedes_por_reubicar.items.map((guest) => (
                <MenuItem key={guest.reserva_id} value={String(guest.reserva_id)}>{guest.huesped} · {guest.origen}</MenuItem>
              ))}
            </Select>
            {datos.huespedes_por_reubicar.items.length === 0 && (
              <FormHelperText>{t('relocations.noEligibleGuests')}</FormHelperText>
            )}
          </FormControl>
          <FormControl fullWidth required>
            <InputLabel id="hotel-label">{t('relocations.chooseHotel')}</InputLabel>
            <Select labelId="hotel-label" label={t('relocations.chooseHotel')} value={destinoId} onChange={(event) => setDestinoId(event.target.value)}>
              {datos.destinos.items.filter((hotel) => hotel.libres > 0).map((hotel) => {
                const origen = datos.huespedes_por_reubicar.items.find((guest) => String(guest.reserva_id) === reservaId)?.origen_id
                if (hotel.id === origen) return null
                const origenHotel = datos.destinos.items.find((item) => item.id === origen)
                const diferencia = hotel.tarifa_noche_eur - (origenHotel?.tarifa_noche_eur ?? 0)
                const diferenciaLabel = `${diferencia > 0 ? '+' : ''}${formatearImporte(diferencia)}`
                return <MenuItem key={hotel.id} value={String(hotel.id)}>{hotel.nombre} · {hotel.libres} libres · {formatearNumero(hotel.distancia_centro_valencia_km)} km · {diferenciaLabel}</MenuItem>
              })}
            </Select>
            <FormHelperText>{t('relocations.chooseHotelHint')}</FormHelperText>
          </FormControl>
          <TextField
            label={t('relocations.reason')}
            placeholder={t('relocations.reasonHint')}
            value={motivo}
            onChange={(event) => setMotivo(event.target.value)}
            multiline
            minRows={3}
            required
            fullWidth
          />
          <Typography variant="body2" color="text.secondary">{t('relocations.approvalNotice')}</Typography>
        </Stack>
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 2.5 }}>
        <Button onClick={cerrar} disabled={guardando}>{t('relocations.cancel')}</Button>
        <Button variant="contained" onClick={enviar} disabled={guardando || datos.huespedes_por_reubicar.items.length === 0}>
          {t('relocations.submit')}
        </Button>
      </DialogActions>
    </Dialog>
  )
}
