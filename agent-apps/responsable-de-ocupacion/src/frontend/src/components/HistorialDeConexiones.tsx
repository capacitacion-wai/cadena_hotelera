/**
 * 14.27.0 — THE CONNECTION HISTORY (Operations → Conectores): who connected, changed the account, activated or
 * disconnected each connector, and when. Kept by the platform, so it survives restarts and log rotation (a customer
 * asked who disconnected their Odoo and nobody could say). Only non-secret account data (address, database, user).
 */
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery } from '@tanstack/react-query'
import {
  Alert, Box, Collapse, Table, TableBody, TableCell, TableHead, TableRow, Typography,
} from '@mui/material'
import ExpandMoreIcon from '@mui/icons-material/ExpandMore'
import ChevronRightIcon from '@mui/icons-material/ChevronRight'
import { mcpConnectorsApi, type ConnectionEvent } from '../services/mcpConnectorsApi'

function cuentaLegible(a: ConnectionEvent['account']): string {
  if (!a) return ''
  const usuario = a.login || a.username || a.user || a.email || ''
  const donde = a.url || a.base_url || a.instance || a.host || ''
  const base = a.db || a.database || ''
  return [usuario, donde, base].filter(Boolean).join(' · ')
}

export default function HistorialDeConexiones({ nombres }: { nombres: Record<string, string> }) {
  const { t, i18n } = useTranslation()
  const [abierto, setAbierto] = useState(false)
  const { data, isLoading } = useQuery({
    queryKey: ['mcp-connectors-history'],
    queryFn: () => mcpConnectorsApi.history(),
    enabled: abierto,
  })
  const eventos = data?.events || []
  return (
    <Box sx={{ my: 1.5 }}>
      <Box role="button" tabIndex={0} onClick={() => setAbierto((v) => !v)}
        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setAbierto((v) => !v) } }}
        sx={{ display: 'inline-flex', alignItems: 'center', gap: 0.5, cursor: 'pointer', userSelect: 'none' }}>
        {abierto ? <ExpandMoreIcon fontSize="small" /> : <ChevronRightIcon fontSize="small" />}
        <Typography variant="body2" sx={{ fontWeight: 700 }}>{t('historialDeConexiones.titulo')}</Typography>
      </Box>
      <Collapse in={abierto} unmountOnExit>
        {!isLoading && data && !data.loaded && (
          <Alert severity="warning" sx={{ mt: 1 }}>{t('historialDeConexiones.sinPlataforma')}</Alert>
        )}
        {!isLoading && data?.loaded && eventos.length === 0 && (
          <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>{t('historialDeConexiones.vacio')}</Typography>
        )}
        {eventos.length > 0 && (
          <Box sx={{ overflowX: 'auto', mt: 1 }}>
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell>{t('historialDeConexiones.cuando')}</TableCell>
                  <TableCell>{t('historialDeConexiones.conector')}</TableCell>
                  <TableCell>{t('historialDeConexiones.accion')}</TableCell>
                  <TableCell>{t('historialDeConexiones.quien')}</TableCell>
                  <TableCell>{t('historialDeConexiones.cuenta')}</TableCell>
                  <TableCell>{t('historialDeConexiones.resultado')}</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {eventos.map((e) => (
                  <TableRow key={e.id}>
                    <TableCell sx={{ whiteSpace: 'nowrap' }}>
                      {new Date(e.created_at).toLocaleString(i18n.language, { dateStyle: 'short', timeStyle: 'medium' })}
                    </TableCell>
                    <TableCell>{nombres[e.connector] || e.connector}</TableCell>
                    <TableCell>{t(`historialDeConexiones.acciones.${e.action}`, e.action)}</TableCell>
                    <TableCell>{e.actor || '—'}</TableCell>
                    <TableCell>{cuentaLegible(e.account) || '—'}</TableCell>
                    <TableCell title={e.detail || undefined} sx={{ color: e.result === 'ok' ? 'success.main' : e.result === 'error' ? 'error.main' : 'text.secondary' }}>
                      {e.result === 'ok' ? t('historialDeConexiones.ok') : e.result === 'error' ? t('historialDeConexiones.error') : e.result}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Box>
        )}
      </Collapse>
    </Box>
  )
}
