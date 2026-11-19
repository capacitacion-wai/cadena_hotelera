import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import {
  Alert, alpha, Box, Button, Card, CardActionArea, CardContent, Chip, Grid, LinearProgress,
  Skeleton, Stack, Typography, useMediaQuery, useTheme,
} from '@mui/material'
import { ArrowForward, BedOutlined, ErrorOutline, HotelOutlined, WarningAmberOutlined } from '@mui/icons-material'
import PageHeader from '../components/PageHeader'
import SectionTitle from '../components/SectionTitle'
import KpiCard from '../components/KpiCard'
import { formatearImporte, formatearNumero, formatearPorcentaje } from '../utils/formato'
import { gestorApi, type Hotel, type Ocupacion } from '../services/gestor'
import { useMiAcceso } from '../wai/perfilado'

export default function OcupacionPage() {
  const { t } = useTranslation()
  const { yo } = useMiAcceso()
  const theme = useTheme()
  const esMovil = useMediaQuery(theme.breakpoints.down('sm'))
  const [ocupacion, setOcupacion] = useState<Ocupacion | null>(null)
  const [hoteles, setHoteles] = useState<Hotel[]>([])
  const [cargando, setCargando] = useState(true)
  const [cargandoEjemplo, setCargandoEjemplo] = useState(false)
  const [resultadoCarga, setResultadoCarga] = useState<'success' | 'error' | null>(null)
  const [error, setError] = useState(false)

  const cargar = () => {
    setCargando(true)
    setError(false)
    Promise.all([gestorApi.ocupacion(), gestorApi.hoteles()]).then(([resumen, lista]) => {
      setOcupacion(resumen)
      setHoteles(lista.items)
      setCargando(false)
    }).catch(() => {
      setError(true)
      setCargando(false)
    })
  }

  useEffect(() => { cargar() }, [])
  const puedeCargarEjemplo = !!yo?.permisos['CAP-4']?.includes('crear') || !!yo?.permisos['*']?.includes('crear')
  const sinDatos = !cargando && ocupacion?.hoteles === 0 && hoteles.length === 0
  const sobrevendidos = hoteles.filter((hotel) => hotel.vendidas > hotel.disponibles)

  const cargarEjemplo = async () => {
    setCargandoEjemplo(true)
    setResultadoCarga(null)
    try {
      await gestorApi.cargarDatosEjemplo()
      setResultadoCarga('success')
    } catch {
      setResultadoCarga('error')
    } finally {
      setCargandoEjemplo(false)
      cargar()
    }
  }

  return (
    <Box>
      <PageHeader title={t('occupancy.title')} subtitle={t('occupancy.subtitle')} icon={<HotelOutlined />} />
      {error && <Alert severity="error" action={<Button onClick={cargar}>{t('occupancy.retry')}</Button>}>{t('occupancy.error')}</Alert>}
      {sinDatos && (
        <Alert
          severity="info"
          sx={{ mb: 3.5, alignItems: 'center' }}
          action={puedeCargarEjemplo ? (
            <Button color="inherit" onClick={cargarEjemplo} disabled={cargandoEjemplo}>
              {cargandoEjemplo ? t('occupancy.loadingSample') : t('occupancy.loadSample')}
            </Button>
          ) : undefined}
        >
          {t('occupancy.empty')}
        </Alert>
      )}
      {resultadoCarga && (
        <Alert severity={resultadoCarga} sx={{ mb: 3.5 }}>
          {t(resultadoCarga === 'success' ? 'occupancy.sampleLoaded' : 'occupancy.sampleLoadError')}
        </Alert>
      )}
      {cargando ? <Skeleton variant="rounded" height={96} sx={{ mb: 3.5 }} /> : sobrevendidos.map((hotel) => (
        <Alert
          key={hotel.id}
          severity="error"
          icon={<WarningAmberOutlined />}
          sx={{ mb: 3.5, alignItems: 'center' }}
          action={<Button component={Link} to="/reubicaciones" color="inherit" endIcon={<ArrowForward />}>{t('occupancy.alertAction')}</Button>}
        >
          <Typography component="span" fontWeight={750}>{t('occupancy.alertTitle')}. </Typography>
          {t('occupancy.alertBody', { hotel: hotel.nombre, count: hotel.vendidas - hotel.disponibles })}
        </Alert>
      ))}
      <Grid container spacing={2} sx={{ mb: 3.5 }}>
        {[
          { label: t('occupancy.hotels'), compactLabel: t('occupancy.hotels'), value: ocupacion?.hoteles, icon: <HotelOutlined />, color: theme.palette.primary.main, to: undefined },
          { label: t('occupancy.sold'), compactLabel: t('occupancy.soldCompact'), value: ocupacion?.vendidas, icon: <BedOutlined />, color: theme.palette.secondary.main, to: undefined },
          { label: t('occupancy.outOfService'), compactLabel: t('occupancy.outOfServiceCompact'), value: ocupacion?.fuera_servicio, icon: <ErrorOutline />, color: theme.palette.warning.dark, to: undefined },
          { label: t('occupancy.overbooked'), compactLabel: t('occupancy.overbooked'), value: ocupacion?.sobreventa, icon: <WarningAmberOutlined />, color: theme.palette.error.dark, to: '/reubicaciones' },
        ].map((item) => (
          <Grid item xs={6} sm={6} lg={3} key={item.label}>
            {cargando ? <Skeleton variant="rounded" height={112} /> : (
              item.to
                ? <CardActionArea component={Link} to={item.to} sx={{ height: '100%', display: 'block', textAlign: 'left', borderRadius: 2 }}>
                    <KpiCard icon={item.icon} label={esMovil ? item.compactLabel : item.label} value={formatearNumero(item.value)} color={item.color} subtitle={t('occupancy.alertAction')} />
                  </CardActionArea>
                : <KpiCard icon={item.icon} label={esMovil ? item.compactLabel : item.label} value={formatearNumero(item.value)} color={item.color} />
            )}
          </Grid>
        ))}
      </Grid>

      <SectionTitle>{t('occupancy.hotelsTitle')}</SectionTitle>
      {cargando ? <Grid container spacing={2}>{[1, 2, 3].map((x) => <Grid item xs={12} md={6} key={x}><Skeleton variant="rounded" height={180} /></Grid>)}</Grid> : (
        <Grid container spacing={2}>
          {[...hoteles].sort((a, b) => Number(b.vendidas > b.disponibles) - Number(a.vendidas > a.disponibles)).map((hotel) => (
            <Grid item xs={12} md={6} key={hotel.id}>
              <Card sx={{
                height: '100%',
                ...(hotel.vendidas > hotel.disponibles && { border: 2, borderColor: 'error.main' }),
              }}>
                <CardActionArea component={Link} to={`/hoteles/${hotel.id}`} sx={{ height: '100%' }}>
                  <CardContent sx={{ p: 2.5 }}>
                    <Stack direction="row" justifyContent="space-between" alignItems="flex-start" gap={1}>
                      <Box>
                        <Typography variant="h6" fontWeight={750}>{hotel.nombre}</Typography>
                        <Typography variant="body2" color="text.secondary">
                          {t('occupancy.rooms', { count: hotel.habitaciones_totales })} · {t('occupancy.ratePerNight', { rate: formatearImporte(hotel.tarifa_noche_eur) })}
                        </Typography>
                      </Box>
                      {hotel.vendidas > hotel.disponibles
                        ? <Chip size="small" label={t('occupancy.over', { count: hotel.vendidas - hotel.disponibles })} sx={{ fontWeight: 700, bgcolor: alpha(theme.palette.error.main, 0.1), color: theme.palette.error.dark }} />
                        : <Chip color="success" size="small" label={t('occupancy.available', { count: hotel.libres })} />}
                    </Stack>
                    <Box sx={{ mt: 2.5 }}>
                      <Stack direction="row" justifyContent="space-between" sx={{ mb: 0.75 }}>
                        <Typography variant="body2" color="text.secondary">{t('occupancy.occupancy')}</Typography>
                        <Typography variant="body2" fontWeight={700}>{formatearPorcentaje(hotel.ocupacion_porcentaje)}</Typography>
                      </Stack>
                      <LinearProgress
                        variant="determinate"
                        value={Math.min(hotel.ocupacion_porcentaje, 100)}
                        color={hotel.vendidas > hotel.disponibles ? 'error' : 'primary'}
                        sx={{ height: 8, borderRadius: 5 }}
                      />
                    </Box>
                    <Stack direction="row" flexWrap="wrap" gap={1} sx={{ mt: 1.5 }}>
                      <Chip variant="outlined" size="small" label={t('occupancy.soldCount', { count: hotel.vendidas })} />
                      <Chip variant="outlined" size="small" label={t('occupancy.outCount', { count: hotel.fuera_servicio })} />
                      <Chip variant="outlined" size="small" label={`${formatearNumero(hotel.distancia_centro_valencia_km)} km ${t('occupancy.distance')}`} />
                    </Stack>
                  </CardContent>
                </CardActionArea>
              </Card>
            </Grid>
          ))}
        </Grid>
      )}
    </Box>
  )
}
