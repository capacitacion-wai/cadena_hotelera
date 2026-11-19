/**
 * WAI Design System — SectionTitle.
 *
 * Copy this file verbatim into every new app at:
 *   src/frontend/src/components/SectionTitle.tsx
 *
 * Use at the top of every card / section inside a page. Shows a small
 * gradient bar (primary → secondary of the app's THEME) on the left of the title.
 */
import { ReactNode } from 'react'
import { Box, Typography } from '@mui/material'

export interface SectionTitleProps {
  children?: ReactNode
  title?: ReactNode
  icon?: ReactNode
}

export default function SectionTitle({ children, title, icon }: SectionTitleProps) {
  const content = title ?? children
  return (
    <Typography variant="subtitle1" component="div" sx={{
      fontWeight: 700, mb: 2, display: 'flex', alignItems: 'center',
      '&::before': {
        content: '""', width: 3, height: 18, borderRadius: 2, mr: 1.5,
        background: (theme) =>
          `linear-gradient(180deg, ${theme.palette.primary.main} 0%, ${theme.palette.secondary.main} 100%)`,
      },
    }}>
      {icon && (
        <Box sx={{ display: 'inline-flex', alignItems: 'center', mr: 1, color: 'primary.main', '& .MuiSvgIcon-root': { fontSize: 20 } }}>
          {icon}
        </Box>
      )}
      {content}
    </Typography>
  )
}

export { SectionTitle }
