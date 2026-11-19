/**
 * A place or a route, painted as a card instead of as a three-hundred-character URL.
 *
 * The web has NO native map —unlike mobile, which draws the embedded map from the
 * coordinates— so here the card is the LIST, well presented, plus links to open it
 * wherever the user wants. That asymmetry is exactly what the contract allows: the
 * employee delivers the same data and each interface decides how to present it.
 *
 * Each stop opens separately on purpose: you are at the third point and want only that one.
 */
import { Box, Button, Chip, Link, Stack, Typography } from '@mui/material'
import PlaceOutlinedIcon from '@mui/icons-material/PlaceOutlined'
import RouteOutlinedIcon from '@mui/icons-material/RouteOutlined'

import type { Tarjeta, PuntoDeTarjeta } from './tarjetas'

const NOMBRE_DEL_MODO: Record<string, string> = {
  andando: 'andando', coche: 'en coche', transporte: 'en transporte', bici: 'en bici',
}

/** Without a link from the employee, a search one is composed: the name always works. */
function enlaceDe(p: PuntoDeTarjeta): string {
  if (typeof p.lat === 'number' && typeof p.lon === 'number') {
    return `https://www.google.com/maps/search/?api=1&query=${p.lat},${p.lon}`
  }
  return `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(p.nombre)}`
}

export default function TarjetaDeMapa({ tarjeta }: { tarjeta: Tarjeta }) {
  const esRuta = tarjeta.tipo === 'ruta'
  const paradas: PuntoDeTarjeta[] = esRuta ? (tarjeta as any).paradas : [tarjeta as any]
  const principal = (tarjeta.enlaces?.google || tarjeta.enlaces?.apple || '')

  return (
    <Box sx={{
      border: '1px solid', borderColor: 'divider', borderRadius: 2,
      my: 1, overflow: 'hidden',
    }}>
      <Box sx={{ px: 1.5, pt: 1.25, pb: 1, display: 'flex', alignItems: 'center', gap: 1 }}>
        {esRuta ? <RouteOutlinedIcon fontSize="small" /> : <PlaceOutlinedIcon fontSize="small" />}
        <Typography variant="subtitle2" sx={{ fontWeight: 700, flex: 1 }}>
          {paradas[0]?.nombre || 'Ubicación'}
          {esRuta && paradas.length > 1
            && paradas[paradas.length - 1].nombre !== paradas[0].nombre
            ? ` → ${paradas[paradas.length - 1].nombre}` : ''}
        </Typography>
        {esRuta && (
          <Chip size="small" variant="outlined" sx={{ height: 20, fontSize: 11 }}
            label={`${paradas.length} puntos`
              + ((tarjeta as any).modo ? ` · ${NOMBRE_DEL_MODO[(tarjeta as any).modo]}` : '')} />
        )}
      </Box>

      <Stack sx={{ px: 1.5, pb: 1 }} spacing={0.25}>
        {paradas.map((p, i) => (
          <Box key={`${p.nombre}-${i}`} sx={{ display: 'flex', alignItems: 'baseline', gap: 1 }}>
            {esRuta && (
              <Typography variant="caption" color="text.disabled" sx={{ minWidth: 16 }}>
                {i + 1}
              </Typography>
            )}
            <Link href={enlaceDe(p)} target="_blank" rel="noopener" variant="body2"
              underline="hover">
              {p.nombre}
            </Link>
            {!!p.direccion && (
              <Typography variant="caption" color="text.disabled">{p.direccion}</Typography>
            )}
          </Box>
        ))}
      </Stack>

      {!!principal && (
        <Box sx={{ px: 1.5, pb: 1.25 }}>
          <Button size="small" variant="contained" href={principal} target="_blank"
            rel="noopener" sx={{ textTransform: 'none' }}>
            {esRuta ? 'Abrir la ruta' : 'Abrir en el mapa'}
          </Button>
        </Box>
      )}
    </Box>
  )
}
