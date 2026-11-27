import { useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import {
  Alert, Button, Dialog, DialogActions, DialogContent, DialogTitle, MenuItem, Stack, TextField,
} from '@mui/material'
import type { Habitacion, Reserva, ReservaCambio, ReservaNueva } from '../services/gestor'

interface Props {
  abierta: boolean
  habitaciones: Habitacion[]
  reserva?: Reserva | null
  onCerrar: () => void
  onGuardar: (datos: ReservaNueva | ReservaCambio) => Promise<void>
}

export default function ReservaDialog({ abierta, habitaciones, reserva, onCerrar, onGuardar }: Props) {
  const { t } = useTranslation()
  const [huesped, setHuesped] = useState(reserva?.huesped ?? '')
  const [habitacionId, setHabitacionId] = useState<number | ''>(reserva?.habitacion_id ?? habitaciones[0]?.id ?? '')
  const [entrada, setEntrada] = useState(reserva?.entrada ?? '')
  const [salida, setSalida] = useState(reserva?.salida ?? '')
  const [guardando, setGuardando] = useState(false)
  const [error, setError] = useState(false)
  const editar = !!reserva
  const habitacionesOperativas = habitaciones.filter((room) => room.estado !== 'fuera_servicio')

  const guardar = async (event: FormEvent) => {
    event.preventDefault()
    if (habitacionId === '') return
    setGuardando(true)
    setError(false)
    try {
      const datos = editar
        ? { habitacion_id: habitacionId, entrada, salida }
        : { huesped, habitacion_id: habitacionId, entrada, salida }
      await onGuardar(datos)
      onCerrar()
    } catch {
      setError(true)
    } finally {
      setGuardando(false)
    }
  }

  return (
    <Dialog open={abierta} onClose={guardando ? undefined : onCerrar} fullWidth maxWidth="sm">
      <form onSubmit={guardar}>
        <DialogTitle>{t(editar ? 'reservation.editTitle' : 'reservation.newTitle')}</DialogTitle>
        <DialogContent>
          <Stack spacing={2} sx={{ pt: 1 }}>
            {error && <Alert severity="error">{t('reservation.saveError')}</Alert>}
            {!habitacionesOperativas.length && <Alert severity="warning">{t('reservation.noUsableRooms')}</Alert>}
            {!editar && <TextField
              label={t('reservation.guest')}
              value={huesped}
              onChange={(event) => setHuesped(event.target.value)}
              required
              inputProps={{ maxLength: 160 }}
            />}
            <TextField
              select
              label={t('reservation.room')}
              value={habitacionId}
              onChange={(event) => setHabitacionId(Number(event.target.value))}
              required
            >
              {habitacionesOperativas.map((room) => (
                <MenuItem key={room.id} value={room.id}>{room.numero}</MenuItem>
              ))}
            </TextField>
            <TextField
              type="date"
              label={t('reservation.checkIn')}
              value={entrada}
              onChange={(event) => setEntrada(event.target.value)}
              InputLabelProps={{ shrink: true }}
              required
            />
            <TextField
              type="date"
              label={t('reservation.checkOut')}
              value={salida}
              onChange={(event) => setSalida(event.target.value)}
              InputLabelProps={{ shrink: true }}
              required
            />
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={onCerrar} disabled={guardando}>{t('relocations.cancel')}</Button>
          <Button type="submit" variant="contained" disabled={guardando || !habitacionesOperativas.length}>
            {guardando ? t('common.loading') : t('reservation.save')}
          </Button>
        </DialogActions>
      </form>
    </Dialog>
  )
}
