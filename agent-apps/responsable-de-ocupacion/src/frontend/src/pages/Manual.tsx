/**
 * The employee's manual. COPIED VERBATIM by `wai-agent-app-builder` (INVARIANT).
 *
 * Two sections, each one a simple LINK:
 *  1. GLOBAL manual — how ANY WAI employee works. Link to
 *     ``manifest.manual.global_url`` (platform config ``EMPLOYEE_MANUAL_URL``).
 *  2. This employee's SPECIFIC manual — the ZIP the publisher uploads to the marketplace.
 *     Link to ``manifest.manual.specific_url`` (``EMPLOYEE_MANUAL_BASE_URL`` + the employee's
 *     marketplace slug). Each section is hidden if its link is not available.
 */
import {
  Box, Button, Card, CardContent, Stack, Typography,
} from '@mui/material';
import MenuBookIcon from '@mui/icons-material/MenuBook';
import OpenInNewIcon from '@mui/icons-material/OpenInNew';
import { useTranslation } from 'react-i18next';
import { useManifest } from '../hooks/useManifest';

export default function Manual() {
  const { t } = useTranslation();
  const { manifest } = useManifest();
  const manual = (manifest as any)?.manual || {};
  const globalUrl: string = manual.global_url || '';
  const specificUrl: string = manual.specific_url || '';
  const employeeName =
    manifest?.agent?.persona?.name || manifest?.agent?.display_name || '';

  return (
    <Box sx={{ maxWidth: 720, mx: 'auto', p: { xs: 2, md: 3 } }}>
      <Stack direction="row" spacing={1.5} alignItems="center" sx={{ mb: 3 }}>
        <MenuBookIcon color="primary" />
        <Typography variant="h5" fontWeight={700}>{t('manual.title', 'Manual')}</Typography>
      </Stack>

      <Stack spacing={2}>
        {/* 1 · Manual general (global) */}
        <Card variant="outlined">
          <CardContent>
            <Typography variant="subtitle1" fontWeight={600} gutterBottom>
              {t('manual.globalTitle', 'Manual general')}
            </Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
              {t(
                'manual.globalBody',
                'Cómo funciona cualquier empleado WAI: chat, nivel de autonomía, aprobaciones, memoria, conocimiento, conectores y canales.',
              )}
            </Typography>
            {globalUrl ? (
              <Button variant="contained" endIcon={<OpenInNewIcon />} href={globalUrl} target="_blank" rel="noopener noreferrer">
                {t('manual.globalCta', 'Abrir el manual general')}
              </Button>
            ) : (
              <Typography variant="body2" color="text.disabled">
                {t('manual.unavailable', 'No disponible.')}
              </Typography>
            )}
          </CardContent>
        </Card>

        {/* 2 · This employee's manual (specific) */}
        <Card variant="outlined">
          <CardContent>
            <Typography variant="subtitle1" fontWeight={600} gutterBottom>
              {t('manual.specificTitle', 'Manual de {{name}}', {
                name: employeeName || t('manual.thisEmployee', 'este empleado'),
              })}
            </Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
              {t(
                'manual.specificBody',
                'La guía propia de este empleado: cómo configurar sus conectores y sacarle el máximo partido.',
              )}
            </Typography>
            {specificUrl ? (
              <Button variant="outlined" endIcon={<OpenInNewIcon />} href={specificUrl} target="_blank" rel="noopener noreferrer">
                {t('manual.specificCta', 'Abrir el manual de este empleado')}
              </Button>
            ) : (
              <Typography variant="body2" color="text.disabled">
                {t('manual.specificEmpty', 'Este empleado todavía no tiene manual propio.')}
              </Typography>
            )}
          </CardContent>
        </Card>
      </Stack>
    </Box>
  );
}
