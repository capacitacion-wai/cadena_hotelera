/**
 * Identity — admin page to edit the "AI employee" identity (name, role,
 * bio, personality traits, greeting and the assignable icon/avatar) AND the
 * business timezone (v7.39.0).
 *
 * This is the canonical place to edit the employee's identity. It lives
 * in the Administración area (its own "Identidad" menu entry) rather than
 * on the public Welcome page, so a casual viewer can't rewrite the
 * persona but an operator can tune it without a redeploy. The persona form
 * is the reusable ``PersonaEditorCard``; the timezone is part of the
 * employee's business identity and drives EVERY date/clock/cron in the app.
 */
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import {
  Alert, Box, Button, Card, CardContent, MenuItem, Snackbar, Stack, TextField, Typography,
} from '@mui/material'
import AssignmentIndIcon from '@mui/icons-material/AssignmentInd'
import ScheduleIcon from '@mui/icons-material/Schedule'
import DarkModeOutlinedIcon from '@mui/icons-material/DarkModeOutlined'
import RecordVoiceOverIcon from '@mui/icons-material/RecordVoiceOver'
import VolumeUpIcon from '@mui/icons-material/VolumeUp'

import PageHeader from '../components/PageHeader'
import PersonaEditorCard from '../components/PersonaEditorCard'
import { apiFetch } from '../services/apiClient'
import { useColorMode } from '../theme/ColorMode'

/** Business timezone — an IANA name that fixes the company's wall clock for
 * every date, schedule and cron the employee handles. Stored as an app setting
 * (operator override → deploy default), edited here as part of the identity. */
function BusinessTimezoneCard() {
  const { t } = useTranslation()
  const [tz, setTz] = useState('')
  const [zones, setZones] = useState<string[]>([])
  const [deflt, setDeflt] = useState('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [snack, setSnack] = useState<{ msg: string; sev: 'success' | 'error' } | null>(null)

  useEffect(() => {
    let alive = true
    apiFetch('/api/agent/settings')
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (!alive || !d) return
        setTz(d.business_timezone || '')
        setDeflt(d.business_timezone_default || '')
        // Keep the current value in the list even if it's not in the shortlist.
        const list: string[] = Array.isArray(d.timezones) ? d.timezones : []
        setZones(d.business_timezone && !list.includes(d.business_timezone) ? [d.business_timezone, ...list] : list)
      })
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [])

  const save = async (next: string) => {
    setSaving(true)
    try {
      const r = await apiFetch('/api/agent/settings', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ business_timezone: next }),
      })
      if (!r.ok) throw new Error(await r.text())
      setTz(next)
      setSnack({ msg: t('identity.timezone.saved', 'Zona horaria guardada — recarga para actualizar las fechas mostradas.'), sev: 'success' })
    } catch (e: any) {
      setSnack({ msg: e?.message || 'error', sev: 'error' })
    } finally { setSaving(false) }
  }

  return (
    <Card variant="outlined" sx={{ mt: 2 }}>
      <CardContent>
        <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.5 }}>
          <ScheduleIcon fontSize="small" color="action" />
          <Typography variant="subtitle1" sx={{ fontWeight: 700 }}>
            {t('identity.timezone.title', 'Zona horaria del negocio')}
          </Typography>
        </Stack>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
          {t('identity.timezone.hint', 'Todas las fechas, horarios, programaciones (cron) y vencimientos de encargos se interpretan y se muestran en esta zona. Cámbiala aquí sin necesidad de redeploy.')}
        </Typography>
        <TextField
          select fullWidth size="small" disabled={loading || saving}
          label={t('identity.timezone.label', 'Zona horaria (IANA)')}
          value={zones.length ? tz : ''}
          onChange={(e) => save(e.target.value)}
          helperText={deflt ? t('identity.timezone.default', 'Por defecto del despliegue: {{tz}}', { tz: deflt }) : undefined}
        >
          {zones.map((z) => <MenuItem key={z} value={z}>{z}</MenuItem>)}
        </TextField>
      </CardContent>
      <Snackbar open={!!snack} autoHideDuration={4000} onClose={() => setSnack(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}>
        {snack ? <Alert severity={snack.sev} onClose={() => setSnack(null)}>{snack.msg}</Alert> : undefined}
      </Snackbar>
    </Card>
  )
}

/** How the employee SOUNDS when you talk to it (v12.12.0).
 *
 *  It lives here, in its identity, and not in the listener's preferences: the voice is
 *  its own, like the name and the greeting. Four settings and not one more — the turn
 *  detector timings, the gaps between notices and the lists of fields read out
 *  loud are NOT exposed: they are twelve numbers that already took an afternoon to bring down to two, and a
 *  setting that can only be set wrong is not a setting.
 */
function VozCard() {
  const { t } = useTranslation()
  const [d, setD] = useState<{
    ajustes: { voz: string; estilo: string; acompana: string; arranque: string }
    voces: { id: string; como_es: string }[]
    acompanamientos: string[]; arranques: string[]
  } | null>(null)
  const [sonando, setSonando] = useState('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [snack, setSnack] = useState<{ msg: string; sev: 'success' | 'error' } | null>(null)

  useEffect(() => {
    let alive = true
    apiFetch('/api/voz/ajustes')
      .then((r) => (r.ok ? r.json() : null))
      .then((x) => { if (alive && x) setD(x) })
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [])

  const guarda = async (cambio: Record<string, string>) => {
    setSaving(true)
    try {
      const r = await apiFetch('/api/voz/ajustes', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(cambio),
      })
      if (!r.ok) throw new Error(String(r.status))
      const x = await r.json()
      setD((v) => (v ? { ...v, ajustes: x.ajustes } : v))
      setSnack({ msg: t('voz.perfil.guardado', 'Guardado.'), sev: 'success' })
    } catch {
      setSnack({ msg: t('voz.perfil.error', 'No se pudo guardar.'), sev: 'error' })
    } finally { setSaving(false) }
  }

  // A deployment older than 12.12.0 does not publish these routes: instead of showing a
  // broken form, the card does not appear.
  if (loading || !d) return null
  const a = d.ajustes

  const COMO_ACOMPANA: Record<string, string> = {
    nada: t('voz.perfil.acompanaNada', 'Calla mientras trabaja'),
    hechos: t('voz.perfil.acompanaHechos', 'Cuenta lo que va haciendo'),
    todo: t('voz.perfil.acompanaTodo', 'Cuenta todo, incluso lo que va pensando'),
  }
  const COMO_ARRANCA: Record<string, string> = {
    abierto: t('voz.perfil.arranqueAbierto', 'Escucha en cuanto se abre'),
    pulsar: t('voz.perfil.arranquePulsar', 'Solo mientras se pulsa el espacio'),
  }

  return (
    <Card variant="outlined" sx={{ mb: 2 }}>
      <CardContent>
        <Stack direction="row" spacing={1.25} alignItems="center" sx={{ mb: 0.5 }}>
          <RecordVoiceOverIcon color="primary" />
          <Typography variant="h6">{t('voz.perfil.titulo', 'Su voz')}</Typography>
        </Stack>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2.5 }}>
          {t('voz.perfil.ayuda', 'Cómo suena cuando alguien habla con él desde el chat. '
            + 'Es parte de su identidad, así que lo mismo para todo el mundo.')}
        </Typography>

        <Stack spacing={2.5}>
          {/* The hint ("clear and friendly") narrows the search; the point is to HEAR it. A
              name like "coral" means nothing to anyone, and an adjective chosen by me
              neither: what decides is the ear, so each one can be listened to. */}
          <TextField select fullWidth size="small" label={t('voz.perfil.voz', 'Voz')}
                     value={a.voz} disabled={saving}
                     onChange={(e) => void guarda({ voz: e.target.value })}>
            {d.voces.map((v) => (
              <MenuItem key={v.id} value={v.id}>
                <Stack direction="row" spacing={1} alignItems="baseline" sx={{ width: '100%' }}>
                  <Box sx={{ minWidth: 72 }}>{v.id}</Box>
                  <Typography variant="caption" color="text.secondary">{v.como_es}</Typography>
                </Stack>
              </MenuItem>
            ))}
          </TextField>

          <Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap', gap: 1, mt: -1.5 }}>
            {d.voces.map((v) => (
              <Button key={v.id} size="small" variant={v.id === a.voz ? 'contained' : 'outlined'}
                      startIcon={<VolumeUpIcon fontSize="small" />}
                      disabled={sonando === v.id}
                      sx={{ textTransform: 'none', borderRadius: 5 }}
                      onClick={async () => {
                        setSonando(v.id)
                        try {
                          // It is downloaded and played: handing the URL to an <audio> would not
                          // carry the credential, and the endpoint requires it.
                          const r = await apiFetch(`/api/voz/muestra?voz=${encodeURIComponent(v.id)}`)
                          if (r.ok) {
                            const au = new Audio(URL.createObjectURL(await r.blob()))
                            au.onended = () => setSonando('')
                            au.onerror = () => setSonando('')
                            await au.play()
                          } else setSonando('')
                        } catch { setSonando('') }
                      }}>
                {sonando === v.id ? t('voz.perfil.sonando', 'sonando…') : v.id}
              </Button>
            ))}
          </Stack>

          <TextField fullWidth size="small" multiline minRows={2}
                     label={t('voz.perfil.estilo', 'Cómo habla')}
                     placeholder={t('voz.perfil.estiloEj',
                       'Por ejemplo: cercano y tranquilo. O: seco y ejecutivo, al grano.')}
                     defaultValue={a.estilo} disabled={saving}
                     helperText={t('voz.perfil.estiloAyuda',
                       'Una línea. Cambia el tono, no lo que dice: las cifras, los nombres y '
                       + 'las fechas nunca se tocan.')}
                     onBlur={(e) => {
                       if (e.target.value !== a.estilo) void guarda({ estilo: e.target.value })
                     }} />

          <TextField select fullWidth size="small" disabled={saving}
                     label={t('voz.perfil.acompana', 'Mientras consulta algo')}
                     value={a.acompana}
                     helperText={t('voz.perfil.acompanaAyuda',
                       'Puede tardar dos minutos. Cada frase que dice mientras espera es un '
                       + 'turno que se paga, así que hay empleados a los que les conviene callar.')}
                     onChange={(e) => void guarda({ acompana: e.target.value })}>
            {d.acompanamientos.map((v) => (
              <MenuItem key={v} value={v}>{COMO_ACOMPANA[v] || v}</MenuItem>
            ))}
          </TextField>

          <TextField select fullWidth size="small" disabled={saving}
                     label={t('voz.perfil.arranque', 'Al abrir la voz')}
                     value={a.arranque}
                     helperText={t('voz.perfil.arranqueAyuda',
                       '«Solo mientras se pulsa» es lo único que garantiza que no se oye a '
                       + 'nadie más. En una oficina abierta no es opcional.')}
                     onChange={(e) => void guarda({ arranque: e.target.value })}>
            {d.arranques.map((v) => (
              <MenuItem key={v} value={v}>{COMO_ARRANCA[v] || v}</MenuItem>
            ))}
          </TextField>
        </Stack>
      </CardContent>
      <Snackbar open={!!snack} autoHideDuration={2500} onClose={() => setSnack(null)}
                anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}>
        <Alert severity={snack?.sev || 'success'} onClose={() => setSnack(null)}>
          {snack?.msg}
        </Alert>
      </Snackbar>
    </Card>
  )
}

/** Default color theme (light/dark/system) the app starts with for anyone who hasn't
 *  picked a theme via the header toggle. Stored as the ``default_color_mode`` app setting;
 *  the SPA applies it on load (Layout) unless the operator has an explicit choice. */
function DefaultThemeCard() {
  const { t } = useTranslation()
  const { applyDefault } = useColorMode()
  const [mode, setMode] = useState<'system' | 'light' | 'dark'>('system')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [snack, setSnack] = useState<{ msg: string; sev: 'success' | 'error' } | null>(null)

  useEffect(() => {
    let alive = true
    apiFetch('/api/agent/settings')
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (!alive || !d) return
        const m = d.default_color_mode
        if (m === 'light' || m === 'dark' || m === 'system') setMode(m)
      })
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [])

  const save = async (next: string) => {
    setSaving(true)
    try {
      const r = await apiFetch('/api/agent/settings', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ default_color_mode: next }),
      })
      if (!r.ok) throw new Error(await r.text())
      setMode(next as 'system' | 'light' | 'dark')
      // Immediate preview for whoever has not picked a theme by hand.
      if (next === 'light' || next === 'dark') applyDefault(next)
      setSnack({ msg: t('identity.theme.saved', 'Tema por defecto guardado.'), sev: 'success' })
    } catch (e: any) {
      setSnack({ msg: e?.message || 'error', sev: 'error' })
    } finally { setSaving(false) }
  }

  return (
    <Card variant="outlined" sx={{ mt: 2 }}>
      <CardContent>
        <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.5 }}>
          <DarkModeOutlinedIcon fontSize="small" color="action" />
          <Typography variant="subtitle1" sx={{ fontWeight: 700 }}>
            {t('identity.theme.title', 'Tema por defecto')}
          </Typography>
        </Stack>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
          {t('identity.theme.hint', 'Apariencia con la que arranca la app para quien aún no haya elegido tema. Cada usuario puede cambiarlo con el botón sol/luna de la cabecera; su elección manda sobre este defecto.')}
        </Typography>
        <TextField
          select fullWidth size="small" disabled={loading || saving}
          label={t('identity.theme.label', 'Tema por defecto')}
          value={mode}
          onChange={(e) => save(e.target.value)}
        >
          <MenuItem value="system">{t('identity.theme.system', 'Según el sistema')}</MenuItem>
          <MenuItem value="light">{t('identity.theme.light', 'Claro')}</MenuItem>
          <MenuItem value="dark">{t('identity.theme.dark', 'Oscuro')}</MenuItem>
        </TextField>
      </CardContent>
      <Snackbar open={!!snack} autoHideDuration={4000} onClose={() => setSnack(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}>
        {snack ? <Alert severity={snack.sev} onClose={() => setSnack(null)}>{snack.msg}</Alert> : undefined}
      </Snackbar>
    </Card>
  )
}

export default function Identity() {
  const { t } = useTranslation()
  return (
    <Box>
      <PageHeader
        title={t('identity.title')}
        subtitle={t('identity.subtitle')}
        icon={<AssignmentIndIcon />}
      />
      <PersonaEditorCard />
      <VozCard />
      <BusinessTimezoneCard />
      <DefaultThemeCard />
    </Box>
  )
}
