// "Aprendido de …" — the badge that keeps the four rule homes from filling up
// with things nobody can explain (v7.140.0).
//
// A silence, a business parameter, an instruction or an automation can now be
// born from something the employee LEARNED. Without this chip, six months later
// the operator finds a rule and has no way to know why it exists — which is
// exactly how people end up deleting rules that were right, or keeping ones that
// were wrong. Clicking it jumps to the learning that caused it (Operaciones →
// Aprendizaje), and from there the effects list jumps back: traceability in both
// directions, one link, no duplicated data.
//
// Renders NOTHING when there is no origin: a rule a human wrote must not be
// dressed up as a learned one.
import { Chip, Tooltip } from '@mui/material'
import { useTranslation } from 'react-i18next'
import { AutoAwesome as LearnedIcon } from '@mui/icons-material'
import { useNavigate } from 'react-router-dom'

export type LearnedFrom = {
  effect_id?: number
  learning_id?: number
  subject?: string | null
  content?: string | null
  approved_by?: string | null
  approved_at?: string | null
  approved_where?: string | null
}

export default function LearnedFromChip({
  origin, size = 'small', onNavigate,
}: {
  origin?: LearnedFrom | null
  size?: 'small' | 'medium'
  onNavigate?: (learningId: number) => void
}) {
  const { t } = useTranslation()
  const navigate = useNavigate()
  if (!origin || !origin.learning_id) return null

  const what = (origin.content || '').trim()
  const subject = (origin.subject || '').trim()
  const who = (origin.approved_by || '').trim()
  const where = origin.approved_where === 'chat'
    ? t('learning.origin.inChat', 'aprobado en una conversación')
    : t('learning.origin.inQueue', 'aprobado en Aprendizaje')

  const tip = [
    t('learning.origin.tooltip', 'Esta regla nació de algo que el empleado aprendió'),
    subject ? `· ${subject}` : '',
    what ? `\n“${what}”` : '',
    who ? `\n${who} — ${where}` : '',
    `\n${t('learning.origin.click', 'Pulsa para ver el aprendizaje')}`,
  ].filter(Boolean).join(' ')

  return (
    <Tooltip title={<span style={{ whiteSpace: 'pre-line' }}>{tip}</span>}>
      <Chip
        size={size}
        variant="outlined"
        color="secondary"
        icon={<LearnedIcon fontSize="small" />}
        label={t('learning.origin.chip', 'Aprendido')}
        onClick={(e) => {
          e.stopPropagation()
          if (onNavigate) { onNavigate(origin.learning_id!); return }
          // Deep-link into the Aprendizaje tab focused on this learning. The tab
          // reads ``?learning=`` on mount, so the jump works from any surface
          // without those surfaces knowing anything about the viewer.
          navigate(`/operations?learning=${origin.learning_id}#aprendizaje`)
        }}
        sx={{ maxWidth: 180 }}
      />
    </Tooltip>
  )
}
