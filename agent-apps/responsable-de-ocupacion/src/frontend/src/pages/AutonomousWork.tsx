/**
 * "Trabajo autónomo" (autonomous work) — the user-facing home for the STANDING ORDERS the
 * boss registers for the employee ("when… → then…" rules). This is a boss
 * activity, NOT administration, so it lives in the user nav group (not under Operaciones).
 *
 * It reuses the `AutomationsSection` from the Operations page (the create /
 * list / enable / test / edit surface + the in-screen manual). The design-time
 * routines and the autonomy/triggers dials stay in Operaciones → "Rutina del
 * empleado" (what the employee always does, regardless of these orders).
 */
import { Box } from '@mui/material'
import { useTranslation } from 'react-i18next'
import PageHeader from '../components/PageHeader'
import { AutomationsSection } from './Operations'

export default function AutonomousWork() {
  const { t } = useTranslation()
  return (
    <Box>
      <PageHeader
        title={t('autonomousWork.title', 'Trabajo autónomo')}
        subtitle={t('autonomousWork.subtitle', 'Órdenes permanentes que le das a tu empleado: «Cuando… → entonces…». Tú las defines una vez y él las cumple solo. Nacen desactivadas; también puedes pedírselas por el chat.')}
      />
      <AutomationsSection />
    </Box>
  )
}
