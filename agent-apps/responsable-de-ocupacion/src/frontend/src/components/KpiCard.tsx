/**
 * WAI Design System — KpiCard.
 *
 * Copy this file verbatim into every new app at:
 *   src/frontend/src/components/KpiCard.tsx
 *
 * Use in a 6-column grid row at the top of dashboards. Do NOT tweak
 * the left gradient bar, the icon tile, or the font sizes unless the
 * user explicitly requests a change.
 */
import { ReactElement } from 'react'
import { Box, Card, CardContent, Typography, alpha, useTheme } from '@mui/material'
import { ArrowUpward, ArrowDownward, InsightsRounded } from '@mui/icons-material'

export interface KpiCardProps {
  label: string
  value: string | number
  icon?: ReactElement
  color?: string
  subtitle?: string
  trend?: number
}

export default function KpiCard({
  icon = <InsightsRounded />,
  label,
  value,
  color: colorPedido,
  subtitle,
  trend,
}: KpiCardProps) {
  // Without an explicit color, the app THEME's primary (before, a fixed blue from another palette).
  const theme = useTheme()
  const color = colorPedido ?? theme.palette.primary.main
  return (
    <Card sx={{ height: '100%', position: 'relative', overflow: 'hidden' }}>
      <Box sx={{
        position: 'absolute', top: 0, left: 0, width: 4, height: '100%',
        background: `linear-gradient(180deg, ${color} 0%, ${alpha(color, 0.3)} 100%)`,
        borderRadius: '4px 0 0 4px',
      }} />
      <CardContent sx={{ pl: 2.5, pr: 2, py: 2, '&:last-child': { pb: 2 } }}>
        <Box sx={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 0.5 }}>
          <Box sx={{ minWidth: 0, flex: 1 }}>
            <Typography noWrap sx={{
              color: 'text.secondary', fontWeight: 600,
              textTransform: 'uppercase', letterSpacing: '0.04em', fontSize: '0.62rem',
              lineHeight: 1.3,
            }}>
              {label}
            </Typography>
            <Typography sx={{
              fontWeight: 800, mt: 0.5, lineHeight: 1.1, color: 'text.primary',
              fontSize: 'clamp(1.25rem, 2vw, 1.75rem)',
            }}>
              {value}
            </Typography>
            {subtitle && (
              <Typography variant="caption" sx={{
                color: 'text.secondary', mt: 0.25, display: 'block', fontSize: '0.65rem',
              }}>
                {subtitle}
              </Typography>
            )}
          </Box>
          <Box sx={{
            width: 38, height: 38, minWidth: 38, borderRadius: 2, display: 'flex',
            alignItems: 'center', justifyContent: 'center',
            bgcolor: alpha(color, 0.08),
            color: color,
            '& .MuiSvgIcon-root': { fontSize: 20 },
          }}>
            {icon}
          </Box>
        </Box>
        {trend !== undefined && (
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5, mt: 0.75 }}>
            {trend >= 0
              ? <ArrowUpward sx={{ fontSize: 14, color: 'success.main' }} />
              : <ArrowDownward sx={{ fontSize: 14, color: 'error.main' }} />
            }
            <Typography variant="caption" sx={{
              fontWeight: 700, color: trend >= 0 ? 'success.main' : 'error.main',
            }}>
              {Math.abs(trend)}%
            </Typography>
          </Box>
        )}
      </CardContent>
    </Card>
  )
}
