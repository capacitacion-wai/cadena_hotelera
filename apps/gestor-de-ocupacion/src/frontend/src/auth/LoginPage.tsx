// INVARIANT FILE — copied byte for byte by `assets/copiar_base.sh` (wai-app-builder, skill 7.0.0); do not edit
// it: `verify_app.sh` fails if it differs from the skill's. Why it is like this: `patterns/authentication.md`.
import React, { useState } from 'react';
import { Link as RouterLink, Navigate, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Alert, Box, Button, Card, CardContent, CircularProgress, Link, TextField, Typography } from '@mui/material';
import { useAuth } from './AuthContext';

/** The sign-in with the person's WAI account. In the WAI mobile app it never renders: the session is the app's.
 *  `enlaceExterno`: with external accounts (profiling level 4), the module's public sign-in for people of an outside
 *  organisation — `<LoginPage enlaceExterno="/perfilado/entrar" />` in App.tsx. */
export default function LoginPage({ enlaceExterno }: { enlaceExterno?: string } = {}) {
  const { t } = useTranslation();
  const { login, nativo, isAuthenticated } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [entrando, setEntrando] = useState(false);
  if (nativo || isAuthenticated) return <Navigate to="/" replace />;
  const entrar = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setEntrando(true);
    try {
      await login(email.trim(), password);
      navigate('/', { replace: true });
    } catch {
      setError(t('auth.invalid', 'El correo o la contraseña no son correctos.'));
    } finally {
      setEntrando(false);
    }
  };
  return (
    <Box sx={{ minHeight: '100dvh', display: 'flex', alignItems: 'center', justifyContent: 'center', p: 2, bgcolor: 'background.default' }}>
      <Card sx={{ width: '100%', maxWidth: 400 }}>
        <CardContent component="form" onSubmit={entrar} sx={{ display: 'flex', flexDirection: 'column', gap: 2, p: 3 }}>
          <Typography variant="h5" component="h1">{t('auth.title', 'Inicia sesión')}</Typography>
          <Typography variant="body2" color="text.secondary">{t('auth.subtitle', 'Entra con tu cuenta de WAI.')}</Typography>
          {error && <Alert severity="error">{error}</Alert>}
          <TextField label={t('auth.email', 'Correo electrónico')} type="email" autoComplete="email" required fullWidth
            value={email} onChange={(e) => setEmail(e.target.value)} />
          <TextField label={t('auth.password', 'Contraseña')} type="password" autoComplete="current-password" required fullWidth
            value={password} onChange={(e) => setPassword(e.target.value)} />
          <Button type="submit" variant="contained" size="large" disabled={entrando}
            startIcon={entrando ? <CircularProgress size={18} color="inherit" /> : undefined}>
            {t('auth.submit', 'Entrar')}
          </Button>
          {enlaceExterno && (
            <Link component={RouterLink} to={enlaceExterno} variant="body2" sx={{ textAlign: 'center' }}>
              {t('auth.externalSignIn', '¿Eres de una organización externa? Entra con un código')}
            </Link>
          )}
        </CardContent>
      </Card>
    </Box>
  );
}
