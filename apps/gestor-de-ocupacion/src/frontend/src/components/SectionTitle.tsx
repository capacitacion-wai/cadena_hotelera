/**
 * WAI Design System — SectionTitle.
 *
 * Copy this file verbatim into every new app at:
 *   src/frontend/src/components/SectionTitle.tsx
 *
 * Use at the top of every card / section inside a page. Shows a small
 * blue-to-teal gradient bar on the left of the title.
 */
import { ReactNode } from 'react'
import { Typography } from '@mui/material'

export default function SectionTitle({ children }: { children: ReactNode }) {
  return (
    <Typography variant="subtitle1" sx={{
      fontWeight: 700, mb: 2, display: 'flex', alignItems: 'center',
      '&::before': {
        content: '""', width: 3, height: 18, borderRadius: 2, mr: 1.5,
        background: (theme) =>
          `linear-gradient(180deg, ${theme.palette.primary.main} 0%, ${theme.palette.secondary.main} 100%)`,
      },
    }}>
      {children}
    </Typography>
  )
}
