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
import { Box, Breadcrumbs as MuiBreadcrumbs, Link as MuiLink, Typography, alpha } from '@mui/material'
import { Link as RouterLink } from 'react-router-dom'

export type BreadcrumbItem = { label: string; to?: string }

export interface PageHeaderProps {
  title: string
  subtitle?: ReactNode
  icon?: ReactNode
  actions?: ReactNode
  breadcrumbs?: BreadcrumbItem[]
}

export default function PageHeader({ title, subtitle, icon, actions, breadcrumbs }: PageHeaderProps) {
  return (
    <Box sx={{ mb: 3.5 }}>
      {breadcrumbs && breadcrumbs.length > 0 && (
        <MuiBreadcrumbs sx={{ mb: 1, fontSize: '0.8rem' }}>
          {breadcrumbs.map((b, i) =>
            b.to ? (
              <MuiLink key={i} component={RouterLink} to={b.to} underline="hover" color="text.secondary">
                {b.label}
              </MuiLink>
            ) : (
              <Typography key={i} color="text.primary" sx={{ fontSize: '0.8rem' }}>
                {b.label}
              </Typography>
            )
          )}
        </MuiBreadcrumbs>
      )}
      <Box sx={{
        display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between',
        gap: 2,
      }}>
        <Box sx={{ display: 'flex', alignItems: 'flex-start', gap: 2 }}>
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
              <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5, maxWidth: 600 }} component="div">
                {subtitle}
              </Typography>
            )}
          </Box>
        </Box>
        {actions && (
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexShrink: 0 }}>
            {actions}
          </Box>
        )}
      </Box>
    </Box>
  )
}

export { PageHeader }
