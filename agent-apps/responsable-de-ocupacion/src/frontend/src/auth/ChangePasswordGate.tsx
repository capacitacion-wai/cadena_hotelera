/**
 * MANDATORY password change (auth F1) — blocking screen.
 *
 * v7.123.0. The admin registers the user in WAI and hands over a one-time password;
 * the platform sets `must_change_password` in the token. While that flag is set,
 * <AuthProvider> renders THIS screen instead of the app: no layout, no routes, no
 * data — exactly the same contract as the WAI panel.
 *
 * It receives everything through props on purpose (it does not import AuthContext) to
 * avoid the import cycle with the provider that mounts it. `needsCurrentPassword` is true
 * when the provider no longer has the temporary one in memory (page reload or Google
 * sign-in): in that case we ask for it explicitly; otherwise the user only types the new one x2.
 */
import { useState, FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { Alert, Box, Button, Card, CardContent, Stack, TextField, Typography } from '@mui/material'

/** Same minimum the backend proxy validates (`MIN_PASSWORD_LENGTH`). */
export const MIN_PASSWORD_LENGTH = 8

interface Props {
  email: string
  needsCurrentPassword: boolean
  onSubmit: (newPassword: string, currentPassword?: string) => Promise<void>
  onCancel: () => void
}

export default function ChangePasswordGate({ email, needsCurrentPassword, onSubmit, onCancel }: Props) {
  const { t } = useTranslation()
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [repeat, setRepeat] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  const tooShort = next.length > 0 && next.length < MIN_PASSWORD_LENGTH
  const mismatch = repeat.length > 0 && next !== repeat

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setError(null)
    if (next.length < MIN_PASSWORD_LENGTH) {
      setError(t('auth.changePassword.tooShort', `La contraseña debe tener al menos ${MIN_PASSWORD_LENGTH} caracteres.`))
      return
    }
    if (next !== repeat) {
      setError(t('auth.changePassword.mismatch', 'Las contraseñas no coinciden.'))
      return
    }
    setLoading(true)
    try {
      await onSubmit(next, needsCurrentPassword ? current : undefined)
    } catch (err: any) {
      const code = err?.code
      if (code === 'CURRENT_REQUIRED') {
        setError(t('auth.changePassword.currentRequired', 'Introduce la contraseña temporal que te han entregado.'))
      } else if (code === 'PASSWORD_TOO_SHORT') {
        setError(t('auth.changePassword.tooShort', `La contraseña debe tener al menos ${MIN_PASSWORD_LENGTH} caracteres.`))
      } else if (code === 'PASSWORD_UNCHANGED') {
        setError(t('auth.changePassword.unchanged', 'La nueva contraseña debe ser distinta de la temporal.'))
      } else if (err?.status === 400) {
        setError(t('auth.changePassword.wrongCurrent', 'La contraseña temporal no es correcta.'))
      } else {
        setError(t('auth.changePassword.error', 'No se ha podido cambiar la contraseña. Inténtalo de nuevo.'))
      }
    } finally {
      setLoading(false)
    }
  }

  return (
    <Box sx={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center', bgcolor: 'background.default', p: 2 }}>
      <Card sx={{ width: '100%', maxWidth: 400 }}>
        <CardContent sx={{ p: 4 }}>
          <Typography variant="h5" sx={{ mb: 1, fontWeight: 800 }}>
            {t('auth.changePassword.title', 'Crea tu contraseña')}
          </Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
            {t(
              'auth.changePassword.subtitle',
              'Has entrado con una contraseña temporal de un solo uso. Crea tu contraseña definitiva para continuar.',
            )}
            {email ? ` (${email})` : ''}
          </Typography>
          <Box component="form" onSubmit={submit}>
            <Stack spacing={2}>
              {error && <Alert severity="error">{error}</Alert>}
              {needsCurrentPassword && (
                <TextField
                  label={t('auth.changePassword.currentPassword', 'Contraseña temporal')}
                  type="password"
                  value={current}
                  onChange={(e) => setCurrent(e.target.value)}
                  required
                  fullWidth
                  autoFocus
                  autoComplete="current-password"
                />
              )}
              <TextField
                label={t('auth.changePassword.newPassword', 'Nueva contraseña')}
                type="password"
                value={next}
                onChange={(e) => setNext(e.target.value)}
                required
                fullWidth
                autoFocus={!needsCurrentPassword}
                autoComplete="new-password"
                error={tooShort}
                helperText={
                  tooShort
                    ? t('auth.changePassword.tooShort', `La contraseña debe tener al menos ${MIN_PASSWORD_LENGTH} caracteres.`)
                    : t('auth.changePassword.hint', `Mínimo ${MIN_PASSWORD_LENGTH} caracteres.`)
                }
              />
              <TextField
                label={t('auth.changePassword.confirmPassword', 'Repite la nueva contraseña')}
                type="password"
                value={repeat}
                onChange={(e) => setRepeat(e.target.value)}
                required
                fullWidth
                autoComplete="new-password"
                error={mismatch}
                helperText={mismatch ? t('auth.changePassword.mismatch', 'Las contraseñas no coinciden.') : ' '}
              />
              <Button type="submit" variant="contained" size="large" disabled={loading}>
                {loading
                  ? t('auth.changePassword.saving', 'Guardando…')
                  : t('auth.changePassword.submit', 'Guardar y entrar')}
              </Button>
              <Button type="button" variant="text" size="small" onClick={onCancel} disabled={loading}>
                {t('auth.signOut', 'Cerrar sesión')}
              </Button>
            </Stack>
          </Box>
        </CardContent>
      </Card>
    </Box>
  )
}
