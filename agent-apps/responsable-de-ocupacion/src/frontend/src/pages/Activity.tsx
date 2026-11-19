/**
 * "Actividad" — the user-facing accountability view: what the employee has
 * DONE, in business language (alerts opened, recommendations, communications,
 * tasks, approvals, automations fired…). It is the boss's at-a-glance record of
 * the work, so it lives in the user nav group (last item of "mi trabajo"), NOT
 * under Operaciones/admin.
 *
 * It reuses the `ActivitySection` exported from the Operations page (the same
 * feed + day-range toggle). The technical, cost-oriented twin (per-run tokens,
 * model, duration) stays in the audit area → "Ejecuciones".
 */
import { Box } from '@mui/material'
import { useTranslation } from 'react-i18next'
import PageHeader from '../components/PageHeader'
import { ActivitySection } from './Operations'

export default function Activity() {
  const { t } = useTranslation()
  return (
    <Box>
      <PageHeader
        title={t('activity.title', 'Actividad')}
        subtitle={t('activity.subtitle', 'Todo lo que ha hecho tu empleado, en lenguaje de negocio: avisos, recomendaciones, comunicaciones, tareas, aprobaciones y automatizaciones disparadas. (Para coste y detalle técnico, ve a Ejecuciones.)')}
      />
      <ActivitySection />
    </Box>
  )
}
