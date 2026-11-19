/**
 * The welcome of a MEMBER (14.18.0). INVARIANT.
 *
 * A member USES the employee; it does not operate it. The full welcome (business indicators,
 * channels, autonomous routines, learning proposals) is the admin's at-a-glance view and read
 * endpoints that are now admin-only. Here: who the employee is, what it can do for you in plain
 * words, and the way in — talk to it.
 */
import ChatIcon from '@mui/icons-material/Chat'
import { Box, Button, Card, CardContent, Stack, Typography } from '@mui/material'
import { Link as RouterLink } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { useManifest } from '../hooks/useManifest'
import type { AgentPersona } from '../hooks/useManifest'
import { PersonaAvatar } from './PersonaAvatar'

export function BienvenidaDeMiembro() {
  const { t, i18n } = useTranslation()
  const { manifest } = useManifest()
  const lang = i18n.language?.startsWith('en') ? 'en' : 'es'
  const loc = (v: unknown): string => {
    if (!v) return ''
    if (typeof v === 'string') return v
    if (typeof v === 'object') {
      const d = v as Record<string, string>
      return d[lang] || d.es || d.en || ''
    }
    return ''
  }
  const agent = (manifest?.agent ?? {}) as Record<string, unknown>
  const persona = (agent.persona ?? null) as AgentPersona | null
  const nombreFuncional = String(agent.display_name || agent.name || t('app.title'))
  const nombre = (persona?.name || '').trim() || nombreFuncional
  const papel = loc(persona?.role) || loc(agent.tagline) || String(agent.description || '')
  const bio = loc(persona?.personality?.bio)
  const objetivo = String(manifest?.presentation?.objective || '')
  const queHago: string[] = ((manifest?.presentation?.outputs ?? []) as unknown[])
    .map((x) => loc(x)).filter(Boolean).slice(0, 5)

  return (
    <Box sx={{ maxWidth: 720, mx: 'auto', py: { xs: 2, md: 4 } }}>
      <Card variant="outlined">
        <CardContent sx={{ p: { xs: 3, md: 4 } }}>
          <Stack direction={{ xs: 'column', sm: 'row' }} spacing={3} alignItems={{ xs: 'flex-start', sm: 'center' }}>
            <PersonaAvatar persona={persona} size={88} ring fallbackName={nombreFuncional} />
            <Box sx={{ minWidth: 0 }}>
              <Typography variant="h4" sx={{ fontWeight: 800, lineHeight: 1.15 }}>{nombre}</Typography>
              {papel && <Typography variant="h6" color="text.secondary" sx={{ fontWeight: 400 }}>{papel}</Typography>}
            </Box>
          </Stack>

          {(bio || objetivo) && (
            <Typography sx={{ mt: 3 }}>
              {bio || t('welcome.iAmFor', { goal: objetivo.charAt(0).toLowerCase() + objetivo.slice(1) })}
            </Typography>
          )}

          {queHago.length > 0 && (
            <Box sx={{ mt: 3 }}>
              <Typography variant="overline" color="text.secondary">
                {t('welcome.member.whatICanDo', 'Qué puedo hacer por ti')}
              </Typography>
              <Box component="ul" sx={{ mt: 0.5, mb: 0, pl: 2.5 }}>
                {queHago.map((q) => <li key={q}><Typography>{q}</Typography></li>)}
              </Box>
            </Box>
          )}

          <Button component={RouterLink} to="/agent" variant="contained" size="large"
            startIcon={<ChatIcon />} sx={{ mt: 4 }}>
            {t('welcome.member.talk', 'Hablar con {{nombre}}', { nombre })}
          </Button>
        </CardContent>
      </Card>
    </Box>
  )
}
