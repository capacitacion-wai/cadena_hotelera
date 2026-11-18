/**
 * WAI Design System — PageHeader.
 *
 * Copy this file verbatim into every new app at:
 *   src/frontend/src/components/PageHeader.tsx
 *
 * Place at the top of every page. Keep the gradient icon, h5 title and
 * small gray subtitle: do not change colors, weights or spacing unless
 * the user explicitly asks for a visual change.
 */
import { ReactNode } from 'react'
import { Box, Typography, alpha } from '@mui/material'

interface PageHeaderProps {
  title: string
  subtitle?: string
  icon?: ReactNode
  actions?: ReactNode
}

export default function PageHeader({ title, subtitle, icon, actions }: PageHeaderProps) {
  return (
    // It WRAPS: on a phone the actions go under the title instead of pushing the page sideways (6.31.7 — three
    // apps in a row failed the mobile screen walk with horizontal overflow from a «New …» button here).
    <Box sx={{
      display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', flexWrap: 'wrap',
      mb: 3.5, gap: 2,
    }}>
      <Box sx={{ display: 'flex', alignItems: 'flex-start', gap: 2, minWidth: 0, flex: '1 1 260px' }}>
        {icon && (
          <Box sx={{
            width: 44, height: 44, borderRadius: 2, display: 'flex',
            alignItems: 'center', justifyContent: 'center', flexShrink: 0, mt: 0.25,
            background: (theme) =>
              `linear-gradient(135deg, ${alpha(theme.palette.primary.main, 0.1)} 0%, ${alpha(theme.palette.secondary.main, 0.08)} 100%)`,
            color: 'primary.main',
            '& .MuiSvgIcon-root': { fontSize: 24 },
          }}>
            {icon}
          </Box>
        )}
        <Box>
          <Typography variant="h5" sx={{ fontWeight: 800, lineHeight: 1.2 }}>
            {title}
          </Typography>
          {subtitle && (
            <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5, maxWidth: 600 }}>
              {subtitle}
            </Typography>
          )}
        </Box>
      </Box>
      {actions && (
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
          {actions}
        </Box>
      )}
    </Box>
  )
}
