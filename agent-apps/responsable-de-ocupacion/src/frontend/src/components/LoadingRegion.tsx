import { Box, CircularProgress, Typography } from '@mui/material'
import { useTranslation } from 'react-i18next'

/** Accessible loading placeholder for route suspense and inline spinners. */
export default function LoadingRegion({ minHeight = '60vh' }: { minHeight?: number | string }) {
  const { t } = useTranslation()
  return (
    <Box
      role="status"
      aria-live="polite"
      aria-label={t('common.loading')}
      sx={{
        display: 'flex',
        flexDirection: 'column',
        justifyContent: 'center',
        alignItems: 'center',
        gap: 1,
        minHeight,
      }}
    >
      <CircularProgress size={32} aria-hidden />
      <Typography variant="body2" color="text.secondary">
        {t('common.loading')}
      </Typography>
    </Box>
  )
}
