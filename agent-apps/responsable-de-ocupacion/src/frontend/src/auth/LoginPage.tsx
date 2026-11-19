/**
 * Login page — minimal MUI form that calls useAuth().login().
 *
 * Authored from the wai-app-builder SKILL.md description (no
 * canonical fenced block exists). Keep it minimal: e-mail +
 * password + submit, with an Alert for invalid credentials. On
 * success react-router redirects via <ProtectedRoute>.
 */
import { useState, FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { Alert, Box, Button, Card, CardContent, Divider, Stack, TextField, Typography } from '@mui/material'
import GoogleIcon from '@mui/icons-material/Google'
import { useAuth } from './AuthContext'

export default function LoginPage() {
  const { t } = useTranslation()
  const { login, loginWithGoogle } = useAuth()
  const navigate = useNavigate()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [googleLoading, setGoogleLoading] = useState(false)
  // v7.123.0 — Google-only account detected (403 METHOD_NOT_ALLOWED): we highlight the
  // Google button so the way out is obvious, not just an error message.
  const [googleOnly, setGoogleOnly] = useState(false)

  const onGoogle = async () => {
    setError(null)
    setGoogleLoading(true)
    try {
      await loginWithGoogle()
      navigate('/', { replace: true })
    } catch (err: any) {
      if (err?.code === 'POPUP_BLOCKED') setError(t('auth.popupBlocked', 'El navegador bloqueó la ventana emergente. Permítela e inténtalo de nuevo.'))
      else if (err?.code === 'GOOGLE_LOGIN_FAILED') setError(t('auth.googleNoAccount', 'Esta cuenta de Google no tiene acceso. Pide al administrador que te dé de alta.'))
      else if (err?.code === 'INACTIVE_USER') setError(t('auth.inactiveUser', 'Esta cuenta está desactivada. Contacta con el administrador.'))
      else if (err?.code === 'ACCOUNT_LOCKED' || err?.status === 423) setError(t('auth.accountLocked', 'Cuenta bloqueada por demasiados intentos fallidos. Inténtalo más tarde.'))
      else setError(t('auth.googleError', 'No se pudo iniciar sesión con Google. Inténtalo de nuevo.'))
    } finally {
      setGoogleLoading(false)
    }
  }

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault()
    setError(null)
    setGoogleOnly(false)
    setLoading(true)
    try {
      await login(email, password)
      // If the platform requires changing the temporary password, <AuthProvider> mounts the
      // blocking gate instead of the app: navigating here is harmless.
      navigate('/', { replace: true })
    } catch (err: any) {
      // U3: distinguish rate-limit (HTTP 429) from credential
      // failure so the operator knows whether to wait or retype.
      // The login() helper rethrows the underlying Error with a
      // ``status`` field when the backend returns a non-200.
      // v7.123.0 — and with the platform's functional CODE (`X-Auth-Error-Code`,
      // propagated by the proxy) so as not to lie to someone who signs in with Google.
      const status = err?.status ?? err?.response?.status
      const code = err?.code
      if (code === 'METHOD_NOT_ALLOWED') {
        setGoogleOnly(true)
        setError(t('auth.googleOnly', 'Esta cuenta entra con Google. Usa el botón «Entrar con Google».'))
      } else if (code === 'INACTIVE_USER') {
        setError(t('auth.inactiveUser', 'Esta cuenta está desactivada. Contacta con el administrador.'))
      } else if (code === 'ACCOUNT_LOCKED' || status === 423) {
        setError(t('auth.accountLocked', 'Cuenta bloqueada por demasiados intentos fallidos. Inténtalo más tarde.'))
      } else if (status === 429) {
        setError(t('auth.tooManyAttempts', 'Demasiados intentos. Espera un minuto antes de volver a probar.'))
      } else if (typeof err?.remainingAttempts === 'number' && err.remainingAttempts > 0) {
        setError(
          `${t('auth.invalidCredentials', 'Credenciales incorrectas')}. ` +
          t('auth.attemptsLeft', {
            count: err.remainingAttempts,
            defaultValue: `Te quedan ${err.remainingAttempts} intentos antes de bloquear la cuenta.`,
          }),
        )
      } else {
        setError(t('auth.invalidCredentials', 'Credenciales incorrectas'))
      }
    } finally {
      setLoading(false)
    }
  }

  return (
    <Box sx={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center', bgcolor: 'background.default', p: 2 }}>
      <Card sx={{ width: '100%', maxWidth: 400 }}>
        <CardContent sx={{ p: 4 }}>
          <Typography variant="h5" sx={{ mb: 3, fontWeight: 800 }}>
            {t('auth.login')}
          </Typography>
          <Box component="form" onSubmit={onSubmit}>
            <Stack spacing={2}>
              {error && <Alert severity="error">{error}</Alert>}
              <TextField
                label={t('auth.email')}
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
                fullWidth
                autoFocus
              />
              <TextField
                label={t('auth.password')}
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                fullWidth
              />
              <Button type="submit" variant="contained" size="large" disabled={loading || googleLoading}>
                {t('auth.signIn')}
              </Button>
              <Divider sx={{ my: 0.5 }}>{t('auth.or', 'o')}</Divider>
              <Button
                type="button"
                variant={googleOnly ? 'contained' : 'outlined'}
                color={googleOnly ? 'primary' : undefined}
                size="large"
                startIcon={<GoogleIcon />}
                onClick={onGoogle}
                disabled={loading || googleLoading}
              >
                {googleLoading
                  ? t('auth.googleConnecting', 'Conectando con Google…')
                  : t('auth.signInWithGoogle', 'Entrar con Google')}
              </Button>
            </Stack>
          </Box>
        </CardContent>
      </Card>
    </Box>
  )
}
