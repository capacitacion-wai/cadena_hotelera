import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import {
  Alert, alpha, Box, Button, Card, CardContent, Chip, Divider, Grid, LinearProgress,
  Paper, Skeleton, Snackbar, Stack, TablePagination, Typography, useTheme,
} from '@mui/material'
import { CheckCircleOutline, HistoryOutlined, SwapHorizOutlined } from '@mui/icons-material'
import PageHeader from '../components/PageHeader'
import SectionTitle from '../components/SectionTitle'
import PropuestaDialog from '../components/PropuestaDialog'
import { useMiAcceso } from '../wai/perfilado'
import { formatearFechaHora, formatearImporte, formatearNumero } from '../utils/formato'
import { gestorApi, type DatosReubicaciones, type Pagina, type Propuesta } from '../services/gestor'

const PAGE_SIZE = 20
const paginaVacia = <T,>(): Pagina<T> => ({ items: [], total: 0, limit: PAGE_SIZE, offset: 0 })
const datosVacios: DatosReubicaciones = {
  propuestas: paginaVacia(),
  historial: paginaVacia(),
  huespedes_por_reubicar: paginaVacia(),
  destinos: paginaVacia(),
}

function diferenciaTarifa(propuesta: Propuesta) {
  const diferencia = propuesta.diferencia_tarifa
  return `${diferencia > 0 ? '+' : ''}${formatearImporte(diferencia)}`
}

export default function ReubicacionesPage() {
  const { t } = useTranslation()
  const theme = useTheme()
  const { yo } = useMiAcceso()
  const [datos, setDatos] = useState(datosVacios)
  const [cargando, setCargando] = useState(true)
  const [guardando, setGuardando] = useState(false)
  const [dialogo, setDialogo] = useState(false)
  const [mensaje, setMensaje] = useState('')
  const [error, setError] = useState(false)
  const [propuestasOffset, setPropuestasOffset] = useState(0)
  const [historialOffset, setHistorialOffset] = useState(0)
  const puedeAprobar = !!yo?.permisos['CAP-3']?.includes('aprobar')

  const cargar = () => {
    setCargando(true)
    gestorApi.reubicaciones(propuestasOffset, historialOffset, PAGE_SIZE).then((respuesta) => {
      setDatos(respuesta)
      setError(false)
      setCargando(false)
    }).catch(() => {
      setError(true)
      setCargando(false)
    })
  }

  useEffect(() => { cargar() }, [propuestasOffset, historialOffset])

  const guardarPropuesta = (entrada: { reserva_id: number; hotel_destino_id: number; motivo: string }) => {
    setGuardando(true)
    gestorApi.proponer(entrada).then(() => {
      setDialogo(false)
      setMensaje(t('relocations.proposalSaved'))
      setGuardando(false)
      cargar()
    }).catch(() => {
      setError(true)
      setGuardando(false)
    })
  }

  const aprobar = (propuesta: Propuesta) => {
    setGuardando(true)
    gestorApi.aprobar(propuesta.id).then(() => {
      setMensaje(t('relocations.approved'))
      setGuardando(false)
      cargar()
    }).catch(() => {
      setError(true)
      setGuardando(false)
    })
  }

  return (
    <Box>
      <PageHeader
        title={t('relocations.title')}
        subtitle={t('relocations.subtitle')}
        icon={<SwapHorizOutlined />}
        actions={<Button variant="contained" onClick={() => setDialogo(true)}>{t('relocations.new')}</Button>}
      />
      {error && <Alert severity="error" sx={{ mb: 2 }}>{t('relocations.error')}</Alert>}
      {!puedeAprobar && <Alert severity="info" sx={{ mb: 2 }}>{t('relocations.cannotApprove')}</Alert>}
      <Grid container spacing={3}>
        <Grid item xs={12} lg={7}>
          <SectionTitle>{t('relocations.pending')}</SectionTitle>
          {cargando ? <Stack spacing={2}><Skeleton variant="rounded" height={160} /><Skeleton variant="rounded" height={160} /></Stack> : (
            <Stack spacing={2}>
              {datos.propuestas.items.filter((proposal) => proposal.estado === 'pendiente').map((proposal) => (
                <Card key={proposal.id} variant="outlined">
                  <CardContent>
                    <Stack direction="row" justifyContent="space-between" gap={1} alignItems="flex-start">
                      <Box>
                        <Typography variant="h6" fontWeight={750}>{proposal.huesped}</Typography>
                        <Typography variant="body2" color="text.secondary">{proposal.origen} → {proposal.destino}</Typography>
                      </Box>
                      <Chip label={t('relocations.pendingStatus')} size="small" sx={{ bgcolor: alpha(theme.palette.warning.main, 0.16), color: theme.palette.warning.dark, fontWeight: 700 }} />
                    </Stack>
                    <Grid container spacing={1.5} sx={{ mt: 1 }}>
                      <Grid item xs={12} sm={7}>
                        <Typography variant="overline" color="text.secondary">{t('relocations.reason')}</Typography>
                        <Typography variant="body2">{proposal.motivo}</Typography>
                      </Grid>
                      <Grid item xs={6} sm={2.5}>
                        <Typography variant="overline" color="text.secondary">{t('relocations.difference')}</Typography>
                        <Typography variant="body2" fontWeight={700}>{diferenciaTarifa(proposal)}</Typography>
                      </Grid>
                      <Grid item xs={6} sm={2.5}>
                        <Typography variant="overline" color="text.secondary">{t('relocations.distance', { distance: formatearNumero(proposal.distancia_destino_km) })}</Typography>
                        <Typography variant="body2">{formatearImporte(proposal.tarifa_destino)}</Typography>
                      </Grid>
                    </Grid>
                    <Divider sx={{ my: 1.5 }} />
                    <Stack direction={{ xs: 'column', sm: 'row' }} justifyContent="space-between" alignItems={{ sm: 'center' }} gap={1}>
                      <Typography variant="caption" color="text.secondary">
                        {t('relocations.proposedBy', { name: proposal.propuesto_por, date: formatearFechaHora(proposal.propuesto_en) })}
                      </Typography>
                      {puedeAprobar && <Button variant="contained" startIcon={<CheckCircleOutline />} disabled={guardando} onClick={() => aprobar(proposal)}>{t('relocations.approve')}</Button>}
                    </Stack>
                  </CardContent>
                </Card>
              ))}
              {datos.propuestas.total === 0 && <Paper variant="outlined" sx={{ p: 2.5 }}><Typography color="text.secondary">{t('relocations.noPending')}</Typography></Paper>}
              <TablePagination
                component="div"
                count={datos.propuestas.total}
                page={Math.floor(propuestasOffset / PAGE_SIZE)}
                onPageChange={(_event, page) => setPropuestasOffset(page * PAGE_SIZE)}
                rowsPerPage={PAGE_SIZE}
                rowsPerPageOptions={[PAGE_SIZE]}
                labelRowsPerPage={t('pagination.rowsPerPage')}
                labelDisplayedRows={({ from, to, count }) => t('pagination.displayed', { from, to, count })}
              />
            </Stack>
          )}
        </Grid>

        <Grid item xs={12} lg={5}>
          <SectionTitle>{t('relocations.compare')}</SectionTitle>
          <Stack spacing={1.5}>
            {datos.destinos.items.filter((hotel) => hotel.libres > 0).map((hotel) => (
              <Paper key={hotel.id} variant="outlined" sx={{ p: 2 }}>
                <Stack direction="row" justifyContent="space-between" gap={1}>
                  <Typography fontWeight={700}>{hotel.nombre}</Typography>
                  <Chip color="success" size="small" label={t('relocations.destinationAvailable', { count: hotel.libres })} />
                </Stack>
                <Typography variant="body2" color="text.secondary" sx={{ mt: 0.75 }}>
                  {formatearNumero(hotel.distancia_centro_valencia_km)} km · {t('relocations.destinationRate', { rate: formatearImporte(hotel.tarifa_noche_eur) })}
                </Typography>
              </Paper>
            ))}
          </Stack>
          <Box sx={{ mt: 3 }}>
            <SectionTitle>{t('relocations.history')}</SectionTitle>
            <Paper variant="outlined" sx={{ p: 2.5 }}>
              {datos.historial.total === 0 ? <Typography color="text.secondary">{t('relocations.noHistory')}</Typography> : (
                <Stack spacing={2}>
                  {datos.historial.items.map((item) => (
                    <Box key={item.id} sx={{ display: 'flex', gap: 1.5 }}>
                      <HistoryOutlined color="action" />
                      <Box>
                        <Typography variant="body2" fontWeight={700}>{item.huesped}: {item.origen} → {item.destino}</Typography>
                        <Typography variant="caption" color="text.secondary">{t('relocations.proposedBy', { name: item.propuesto_por, date: formatearFechaHora(item.propuesto_en) })}</Typography>
                        <Typography variant="caption" color="text.secondary" display="block">{t('relocations.approvedBy', { name: item.aprobado_por || '', date: formatearFechaHora(item.aprobado_en) })}</Typography>
                      </Box>
                    </Box>
                  ))}
                </Stack>
              )}
              <TablePagination
                component="div"
                count={datos.historial.total}
                page={Math.floor(historialOffset / PAGE_SIZE)}
                onPageChange={(_event, page) => setHistorialOffset(page * PAGE_SIZE)}
                rowsPerPage={PAGE_SIZE}
                rowsPerPageOptions={[PAGE_SIZE]}
                labelRowsPerPage={t('pagination.rowsPerPage')}
                labelDisplayedRows={({ from, to, count }) => t('pagination.displayed', { from, to, count })}
              />
            </Paper>
          </Box>
        </Grid>
      </Grid>
      <PropuestaDialog abierto={dialogo} datos={datos} guardando={guardando} cerrar={() => setDialogo(false)} guardar={guardarPropuesta} />
      <Snackbar open={!!mensaje} autoHideDuration={6000} message={mensaje} onClose={() => setMensaje('')} />
      {cargando && <LinearProgress sx={{ position: 'fixed', top: 0, left: 0, right: 0 }} />}
    </Box>
  )
}
