import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import {
  Alert, alpha, Box, Button, Chip, Paper, Skeleton, Tab, Table, TableBody, TableCell, TablePagination,
  TableContainer, TableHead, TableRow, Tabs,
  useTheme,
} from '@mui/material'
import { ArrowBack, HotelOutlined } from '@mui/icons-material'
import PageHeader from '../components/PageHeader'
import SectionTitle from '../components/SectionTitle'
import KpiCard from '../components/KpiCard'
import ReservasPanel from '../components/ReservasPanel'
import { formatearImporte, formatearNumero, formatearPorcentaje } from '../utils/formato'
import { gestorApi, type Habitacion, type Hotel } from '../services/gestor'
import { useMiAcceso } from '../wai/perfilado'

const PAGE_SIZE = 20

export default function HotelPage() {
  const { t } = useTranslation()
  const { yo } = useMiAcceso()
  const theme = useTheme()
  const { hotelId = '' } = useParams()
  const [hotel, setHotel] = useState<Hotel | null>(null)
  const [habitaciones, setHabitaciones] = useState<Habitacion[]>([])
  const [totalHabitaciones, setTotalHabitaciones] = useState(0)
  const [habitacionesOffset, setHabitacionesOffset] = useState(0)
  const [pestana, setPestana] = useState(0)
  const [cargando, setCargando] = useState(true)
  const [error, setError] = useState(false)

  const cargar = () => {
    setCargando(true)
    Promise.all([
      gestorApi.hotel(hotelId),
      gestorApi.habitaciones(hotelId, habitacionesOffset, PAGE_SIZE),
    ]).then(([detalle, cuartos]) => {
      setHotel(detalle)
      setHabitaciones(cuartos.items)
      setTotalHabitaciones(cuartos.total)
      setError(false)
      setCargando(false)
    }).catch(() => {
      setError(true)
      setCargando(false)
    })
  }

  useEffect(() => { cargar() }, [hotelId, habitacionesOffset])

  const puedeGestionarReservas = (yo?.roles ?? []).some((role) =>
    role === 'admin' || role === 'direccion' || role === 'recepcion')

  const estadoHabitacion = (room: Habitacion) => {
    if (room.estado === 'fuera_servicio') return <Chip size="small" label={t('hotel.outOfService')} sx={{ bgcolor: alpha(theme.palette.error.main, 0.1), color: theme.palette.error.dark, fontWeight: 700 }} />
    if (room.estado === 'ocupada') return <Chip color="primary" size="small" label={t('hotel.occupied')} />
    return <Chip color="success" size="small" label={t('hotel.free')} />
  }

  return (
    <Box>
      {cargando ? <Skeleton variant="rounded" height={88} sx={{ mb: 3 }} /> : hotel && (
        <PageHeader
          title={hotel.nombre}
          subtitle={`${formatearNumero(hotel.distancia_centro_valencia_km)} km ${t('occupancy.distance')} · ${t('occupancy.ratePerNight', { rate: formatearImporte(hotel.tarifa_noche_eur) })}`}
          icon={<HotelOutlined />}
          actions={<Button component={Link} to="/" startIcon={<ArrowBack />}>{t('hotel.back')}</Button>}
        />
      )}
      {error && <Alert severity="error" action={<Button onClick={cargar}>{t('hotel.retry')}</Button>} sx={{ mb: 2 }}>{t('hotel.error')}</Alert>}
      {hotel?.motivo_fuera_servicio && <Alert severity="warning" sx={{ mb: 2 }}>{t('hotel.outReason', { reason: hotel.motivo_fuera_servicio })}</Alert>}

      {hotel && (
        <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr 1fr', lg: 'repeat(3, 1fr)' }, gap: 2, mb: 3.5 }}>
          <KpiCard icon={<HotelOutlined />} label={t('occupancy.rooms', { count: hotel.habitaciones_totales })} value={formatearPorcentaje(hotel.ocupacion_porcentaje)} color={theme.palette.primary.main} subtitle={t('occupancy.occupancy')} />
          <KpiCard icon={<HotelOutlined />} label={t('hotel.availableRooms')} value={formatearNumero(hotel.disponibles)} color={theme.palette.success.main} />
          <KpiCard icon={<HotelOutlined />} label={t('hotel.soldRooms')} value={formatearNumero(hotel.vendidas)} color={hotel.vendidas > hotel.disponibles ? theme.palette.error.main : theme.palette.secondary.main} />
        </Box>
      )}

      <Paper variant="outlined">
        <Tabs value={pestana} onChange={(_event, value: number) => setPestana(value)} aria-label={t('hotel.status')} variant="fullWidth">
          <Tab label={t('hotel.roomsTab', { count: totalHabitaciones })} />
          <Tab label={t('hotel.reservationsTab')} />
        </Tabs>
        {pestana === 0 ? (
          <Box sx={{ p: { xs: 1, sm: 2 } }}>
            <Box sx={{ px: 1, pt: 1 }}><SectionTitle>{t('hotel.roomTitle')}</SectionTitle></Box>
            <TableContainer>
              <Table size="small" aria-label={t('hotel.roomTitle')}>
                <TableHead><TableRow><TableCell>{t('hotel.roomNumber')}</TableCell><TableCell>{t('hotel.condition')}</TableCell><TableCell>{t('hotel.reason')}</TableCell></TableRow></TableHead>
                <TableBody>
                  {cargando && <TableRow><TableCell colSpan={3}><Skeleton /><Skeleton /><Skeleton /></TableCell></TableRow>}
                  {!cargando && habitaciones.map((room) => <TableRow key={room.id} hover>
                    <TableCell>{room.numero}</TableCell><TableCell>{estadoHabitacion(room)}</TableCell><TableCell>{room.detalle || '—'}</TableCell>
                  </TableRow>)}
                  {!cargando && habitaciones.length === 0 && <TableRow><TableCell colSpan={3}>{t('hotel.noRooms')}</TableCell></TableRow>}
                </TableBody>
              </Table>
            </TableContainer>
            <TablePagination
              component="div"
              count={totalHabitaciones}
              page={Math.floor(habitacionesOffset / PAGE_SIZE)}
              onPageChange={(_event, page) => setHabitacionesOffset(page * PAGE_SIZE)}
              rowsPerPage={PAGE_SIZE}
              rowsPerPageOptions={[PAGE_SIZE]}
              labelRowsPerPage={t('pagination.rowsPerPage')}
              labelDisplayedRows={({ from, to, count }) => t('pagination.displayed', { from, to, count })}
            />
          </Box>
        ) : (
          <Box sx={{ p: { xs: 1, sm: 2 } }}>
            <Box sx={{ px: 1, pt: 1 }}><SectionTitle>{t('hotel.reservationTitle')}</SectionTitle></Box>
            <ReservasPanel hotelId={hotelId} puedeGestionar={puedeGestionarReservas} onCambio={cargar} />
          </Box>
        )}
      </Paper>
    </Box>
  )
}
