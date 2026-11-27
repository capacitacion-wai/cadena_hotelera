import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import {
  Alert, Box, Button, Chip, Skeleton, Stack, Tab, Table, TableBody, TableCell, TablePagination,
  TableContainer, TableHead, TableRow, Tabs,
} from '@mui/material'
import { AddOutlined, EditOutlined } from '@mui/icons-material'
import { formatearFecha, formatearFechaHora } from '../utils/formato'
import {
  gestorApi,
  type Habitacion,
  type PeriodoReservas,
  type Reserva,
  type ReservaCambio,
  type ReservaNueva,
} from '../services/gestor'
import ReservaDialog from './ReservaDialog'
import CancelacionDialog from './CancelacionDialog'

const PAGE_SIZE = 20
const PERIODOS: PeriodoReservas[] = ['actuales', 'futuras', 'pasadas', 'canceladas']

interface Props {
  hotelId: string
  puedeGestionar: boolean
  onCambio: () => void
}

export default function ReservasPanel({ hotelId, puedeGestionar, onCambio }: Props) {
  const { t } = useTranslation()
  const [periodo, setPeriodo] = useState<PeriodoReservas>('actuales')
  const [reservas, setReservas] = useState<Reserva[]>([])
  const [habitaciones, setHabitaciones] = useState<Habitacion[]>([])
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)
  const [cargando, setCargando] = useState(true)
  const [error, setError] = useState(false)
  const [dialogo, setDialogo] = useState<'crear' | 'editar' | 'cancelar' | null>(null)
  const [seleccionada, setSeleccionada] = useState<Reserva | null>(null)

  const recargar = async () => {
    setCargando(true)
    setError(false)
    try {
      const pagina = await gestorApi.reservas(hotelId, periodo, 0, PAGE_SIZE)
      setReservas(pagina.items)
      setTotal(pagina.total)
      setOffset(0)
    } catch {
      setError(true)
    } finally {
      setCargando(false)
    }
  }

  useEffect(() => {
    let vigente = true
    setCargando(true)
    setError(false)
    const cuartosPeticion = puedeGestionar
      ? gestorApi.habitaciones(hotelId, 0, 100)
      : Promise.resolve(null)
    Promise.all([gestorApi.reservas(hotelId, periodo, offset, PAGE_SIZE), cuartosPeticion]).then(([pagina, cuartos]) => {
      if (!vigente) return
      setReservas(pagina.items)
      setTotal(pagina.total)
      if (cuartos) setHabitaciones(cuartos.items)
      setCargando(false)
    }).catch(() => {
      if (!vigente) return
      setError(true)
      setCargando(false)
    })
    return () => { vigente = false }
  }, [hotelId, periodo, offset, puedeGestionar])

  const guardar = async (datos: ReservaNueva | ReservaCambio) => {
    if (seleccionada) await gestorApi.editarReserva(seleccionada.id, datos as ReservaCambio)
    else await gestorApi.crearReserva(hotelId, datos as ReservaNueva)
    setSeleccionada(null)
    setDialogo(null)
    setOffset(0)
    if (periodo !== 'actuales') setPeriodo('actuales')
    else await recargar()
    onCambio()
  }

  const cancelar = async (motivo: string) => {
    if (!seleccionada) return
    await gestorApi.cancelarReserva(seleccionada.id, motivo)
    setSeleccionada(null)
    setDialogo(null)
    setOffset(0)
    if (periodo !== 'canceladas') setPeriodo('canceladas')
    else await recargar()
    onCambio()
  }

  const abrirEditar = (reserva: Reserva) => {
    setSeleccionada(reserva)
    setDialogo('editar')
  }

  const abrirCancelar = (reserva: Reserva) => {
    setSeleccionada(reserva)
    setDialogo('cancelar')
  }

  return (
    <Box sx={{ p: { xs: 1, sm: 2 } }}>
      <Stack direction={{ xs: 'column', sm: 'row' }} alignItems={{ sm: 'center' }} justifyContent="space-between" gap={1} sx={{ mb: 1 }}>
        <Tabs
          value={periodo}
          onChange={(_event, value: PeriodoReservas) => { setOffset(0); setPeriodo(value) }}
          variant="scrollable"
          allowScrollButtonsMobile
          aria-label={t('reservation.periods')}
        >
          {PERIODOS.map((value) => <Tab key={value} value={value} label={t(`reservation.period.${value}`)} />)}
        </Tabs>
        {puedeGestionar && <Button startIcon={<AddOutlined />} variant="contained" onClick={() => { setSeleccionada(null); setDialogo('crear') }}>
          {t('reservation.new')}
        </Button>}
      </Stack>
      {error && <Alert severity="error" sx={{ mb: 1 }}>{t('reservation.listError')}</Alert>}
      <TableContainer>
        <Table size="small" aria-label={t('hotel.reservationTitle')}>
          <TableHead><TableRow>
            <TableCell>{t('reservation.guest')}</TableCell>
            <TableCell>{t('reservation.room')}</TableCell>
            <TableCell>{t('reservation.checkIn')}</TableCell>
            <TableCell>{t('reservation.checkOut')}</TableCell>
            <TableCell>{t('hotel.status')}</TableCell>
            {periodo === 'canceladas' && <TableCell>{t('reservation.cancellationAudit')}</TableCell>}
            {puedeGestionar && periodo !== 'canceladas' && <TableCell>{t('reservation.actions')}</TableCell>}
          </TableRow></TableHead>
          <TableBody>
            {cargando && <TableRow><TableCell colSpan={7}><Skeleton /><Skeleton /><Skeleton /></TableCell></TableRow>}
            {!cargando && reservas.map((reserva) => <TableRow key={reserva.id} hover>
              <TableCell>{reserva.huesped}</TableCell>
              <TableCell>{reserva.habitacion}</TableCell>
              <TableCell>{formatearFecha(reserva.entrada)}</TableCell>
              <TableCell>{formatearFecha(reserva.salida)}</TableCell>
              <TableCell><Chip size="small" label={t(`reservation.status.${reserva.estado}`)} /></TableCell>
              {periodo === 'canceladas' && <TableCell>
                <Stack spacing={0.25}>
                  <span>{reserva.motivo_cancelacion}</span>
                  <span>{t('reservation.cancelledBy', { name: reserva.cancelada_por || '' })}</span>
                  <span>{reserva.cancelada_en ? t('reservation.cancelledAt', { date: formatearFechaHora(reserva.cancelada_en) }) : ''}</span>
                </Stack>
              </TableCell>}
              {puedeGestionar && periodo !== 'canceladas' && <TableCell>
                <Stack direction="row" gap={0.5}>
                  <Button size="small" startIcon={<EditOutlined />} onClick={() => abrirEditar(reserva)}>{t('reservation.edit')}</Button>
                  <Button size="small" color="error" onClick={() => abrirCancelar(reserva)}>{t('reservation.cancelAction')}</Button>
                </Stack>
              </TableCell>}
            </TableRow>)}
            {!cargando && !reservas.length && <TableRow><TableCell colSpan={7}>{t('reservation.empty')}</TableCell></TableRow>}
          </TableBody>
        </Table>
      </TableContainer>
      <TablePagination
        component="div"
        count={total}
        page={Math.floor(offset / PAGE_SIZE)}
        onPageChange={(_event, page) => setOffset(page * PAGE_SIZE)}
        rowsPerPage={PAGE_SIZE}
        rowsPerPageOptions={[PAGE_SIZE]}
        labelRowsPerPage={t('pagination.rowsPerPage')}
        labelDisplayedRows={({ from, to, count }) => t('pagination.displayed', { from, to, count })}
      />
      <ReservaDialog
        key={`${dialogo}-${seleccionada?.id ?? 'nueva'}`}
        abierta={dialogo === 'crear' || dialogo === 'editar'}
        habitaciones={habitaciones}
        reserva={dialogo === 'editar' ? seleccionada : null}
        onCerrar={() => { setDialogo(null); setSeleccionada(null) }}
        onGuardar={guardar}
      />
      <CancelacionDialog
        abierta={dialogo === 'cancelar'}
        huesped={seleccionada?.huesped ?? ''}
        onCerrar={() => { setDialogo(null); setSeleccionada(null) }}
        onConfirmar={cancelar}
      />
    </Box>
  )
}
