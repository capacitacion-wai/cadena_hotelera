// INVARIANT FILE — copied byte for byte by `assets/copiar_base.sh` (wai-app-builder, skill 7.0.0); do not edit
// it: `verify_app.sh` fails if it differs from the skill's. Why it is like this: `patterns/authentication.md`.
import React from 'react';
import { Navigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Box, CircularProgress, Typography } from '@mui/material';
import { useAuth } from './AuthContext';

export const ProtectedRoute: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { t } = useTranslation();
  const { isAuthenticated, esperandoAlMovil, nativo } = useAuth();
  if (esperandoAlMovil) return <Box sx={{ p: 4, textAlign: 'center' }}><CircularProgress /></Box>;
  if (!isAuthenticated) {
    // Native mode without a token: the mobile session dropped. Say it; never send to /login.
    if (nativo) return <Box sx={{ p: 4 }}><Typography>{t('auth.sessionLost', 'Tu sesión se ha cerrado. Vuelve a entrar desde la aplicación de WAI.')}</Typography></Box>;
    return <Navigate to="/login" replace />;
  }
  return <>{children}</>;
};
