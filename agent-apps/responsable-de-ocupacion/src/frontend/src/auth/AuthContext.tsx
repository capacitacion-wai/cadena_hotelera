/**
 * Auth context — JWT token + email persisted in localStorage.
 *
 * Extracted verbatim from wai-app-builder SKILL.md (Frontend auth
 * — AuthContext.tsx). Login posts to /api/auth/login (proxied to
 * the WAI backend) and stores the access token. Logout clears
 * everything. Use <AuthProvider> at the root and `useAuth()`
 * everywhere else.
 *
 * v7.123.0 — SAME access contract as WAI: if login returns `must_change_password`
 * (one-time password handed over by the admin), the provider does NOT render the app:
 * it renders <ChangePasswordGate/> until the user creates their definitive password
 * (proxy `/api/auth/change-password` → platform) and we sign in again transparently
 * with the new one.
 *
 * v7.124.0 — this screen is only the UI barrier. The real lock is on the SERVER:
 * `backend/app/auth.py` reads the `must_change_password` claim of the JWT and vetoes
 * the whole API with 403 `PASSWORD_CHANGE_REQUIRED` (except login / change / Google /
 * health). Hence the re-login after the change no longer has a «plan B» with the old
 * token: if we cannot get a clean token, the session is closed.
 */
import React, { createContext, useContext, useState, useCallback, useRef } from 'react';
import ChangePasswordGate from './ChangePasswordGate';

interface AuthState {
  token: string | null;
  email: string | null;
  /** F1: the session is quarantined until the user changes the temporary password. */
  mustChangePassword: boolean;
}

/** Login/change error with the platform's functional contract attached. */
export interface AuthError extends Error {
  status?: number;
  /** `X-Auth-Error-Code`: METHOD_NOT_ALLOWED, ACCOUNT_LOCKED, INACTIVE_USER… */
  code?: string;
  /** `X-Auth-Remaining-Attempts`: attempts left before the lockout. */
  remainingAttempts?: number;
}

interface AuthContextValue extends AuthState {
  user: { email: string } | null;
  login: (email: string, password: string) => Promise<void>;
  loginWithGoogle: () => Promise<void>;
  logout: () => void;
  isAuthenticated: boolean;
  /** Creates the definitive password. `currentPassword` is only needed if we lost
   *  the temporary one from memory (e.g. after reloading the page). */
  changePassword: (newPassword: string, currentPassword?: string) => Promise<void>;
}

const AuthContext = createContext<AuthContextValue>(null!);

const MUST_CHANGE_KEY = 'must_change_password';

// The email is in the `sub` claim of the platform JWT (Google login does not return it
// separately); we extract it to show/persist it just like in password login.
const emailFromToken = (token: string): string => {
  try { return JSON.parse(atob(token.split('.')[1] || '')).sub || ''; } catch { return ''; }
};

// Builds the Error enriched with the functional contract the proxy propagates
// (`X-Auth-Error-Code` / `X-Auth-Remaining-Attempts`), so the login screen can
// say «this account signs in with Google» instead of «invalid credentials».
const authError = (res: Response, message: string): AuthError => {
  const err = new Error(message) as AuthError;
  err.status = res.status;
  err.code = res.headers.get('X-Auth-Error-Code') || undefined;
  const remaining = res.headers.get('X-Auth-Remaining-Attempts');
  if (remaining !== null && remaining !== '') {
    const n = Number(remaining);
    if (!Number.isNaN(n)) err.remainingAttempts = n;
  }
  return err;
};

export const useAuth = () => useContext(AuthContext);

export const AuthProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [state, setState] = useState<AuthState>(() => ({
    token: localStorage.getItem('token'),
    email: localStorage.getItem('email'),
    // Persisted: a reload must NOT skip the mandatory change.
    mustChangePassword: localStorage.getItem(MUST_CHANGE_KEY) === '1',
  }));

  // The temporary password lives ONLY in memory (never in localStorage): it is the
  // `current_password` the platform requires for the change. If the user reloads the
  // page we lose it and the gate asks for it again explicitly.
  const pendingSecret = useRef<string | null>(null);

  const applyToken = useCallback((token: string, email: string, mustChange: boolean) => {
    localStorage.setItem('token', token);
    localStorage.setItem('email', email);
    if (mustChange) localStorage.setItem(MUST_CHANGE_KEY, '1');
    else localStorage.removeItem(MUST_CHANGE_KEY);
    setState({ token, email, mustChangePassword: mustChange });
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
    });
    if (!res.ok) {
      // Attach the HTTP status so LoginPage can distinguish rate-limiting
      // (429 → "demasiados intentos") from bad credentials. Without it the
      // 429 branch in LoginPage was dead code and a locked-out operator was
      // told "credenciales inválidas" and kept retyping, worsening the lockout.
      throw authError(res, res.status === 429 ? 'Too many attempts' : 'Invalid credentials');
    }
    const data = await res.json();
    const mustChange = data.must_change_password === true;
    // We keep the temporary password so we can send it as `current_password` without
    // asking the user again (the gate only asks for the NEW one, twice).
    pendingSecret.current = mustChange ? password : null;
    applyToken(data.access_token, email, mustChange);
  }, [applyToken]);

  // «Sign in with Google»: pass-through to the platform flow (via our proxies
  // /api/auth/google/*). start → consent popup → POLLING complete until the token
  // (the platform answers 425 GOOGLE_LOGIN_PENDING while Composio is not ACTIVE).
  const loginWithGoogle = useCallback(async () => {
    const startRes = await fetch('/api/auth/google/start', { method: 'POST', headers: { 'Content-Type': 'application/json' } });
    if (!startRes.ok) throw new Error('google login start failed');
    const { redirect_url, login_ref } = await startRes.json();
    const popup = window.open(redirect_url, 'wai-google-login', 'width=500,height=680,menubar=no,toolbar=no,location=no,status=no');
    if (!popup) {
      const e = new Error('popup blocked') as AuthError;
      e.code = 'POPUP_BLOCKED';
      throw e;
    }
    const deadline = Date.now() + 180_000; // red de seguridad: 3 min
    let data: { access_token: string; must_change_password?: boolean } | null = null;
    try {
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 2500));
        const res = await fetch('/api/auth/google/complete', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ login_ref }),
        });
        if (res.ok) { data = await res.json(); break; }
        const code = res.headers.get('X-Auth-Error-Code');
        if (res.status === 425 || code === 'GOOGLE_LOGIN_PENDING') continue; // not yet → keep polling
        throw authError(res, 'google login failed');
      }
    } finally {
      try { if (!popup.closed) popup.close(); } catch { /* noop */ }
    }
    if (!data) throw new Error('google login timeout');
    const em = emailFromToken(data.access_token);
    // With Google there is no temporary password in memory: if the platform requires the
    // change, the gate will also ask for the current password.
    pendingSecret.current = null;
    applyToken(data.access_token, em, data.must_change_password === true);
  }, [applyToken]);

  // Defined BEFORE `changePassword` on purpose: the password change uses it as its way
  // out when it cannot get a clean token (see below).
  const logout = useCallback(() => {
    localStorage.removeItem('token');
    localStorage.removeItem('email');
    localStorage.removeItem(MUST_CHANGE_KEY);
    pendingSecret.current = null;
    // v7.88.0 — clear the per-user chat state (history/draft/session/active run)
    // so the NEXT operator on this browser can't see the previous one's
    // conversation. The keys are ``wai-agent.*`` (see pages/Agent.tsx).
    try {
      for (let i = localStorage.length - 1; i >= 0; i--) {
        const k = localStorage.key(i);
        if (k && k.startsWith('wai-agent.')) localStorage.removeItem(k);
      }
    } catch { /* ignore */ }
    setState({ token: null, email: null, mustChangePassword: false });
  }, []);

  const changePassword = useCallback(async (newPassword: string, currentPassword?: string) => {
    const current = currentPassword || pendingSecret.current || '';
    if (!current) {
      const e = new Error('current password required') as AuthError;
      e.code = 'CURRENT_REQUIRED';
      throw e;
    }
    const token = localStorage.getItem('token') || '';
    const res = await fetch('/api/auth/change-password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({ current_password: current, new_password: newPassword }),
    });
    if (!res.ok) throw authError(res, 'change password failed');

    // TRANSPARENT re-login with the new password: the old JWT is still valid
    // but carries `must_change_password`, so we ask for a clean one.
    //
    // v7.124.0 — the veto is on the SERVER (backend/app/auth.py): with that old token the
    // whole API answers 403 PASSWORD_CHANGE_REQUIRED. That is why the 7.123.0 way out
    // («if the re-login fails, I lift the quarantine with the current token») NO longer
    // works: it would now leave the app mounted and broken. We retry the re-login a few
    // times (the correct change clears the pod's per-IP lockout, so the failure can only
    // be transient) and, if there is still no clean token, we LOG OUT: the password is
    // already the new one, so the user signs in with it from the login screen.
    const email = state.email || localStorage.getItem('email') || '';
    pendingSecret.current = null;
    const RELOGIN_DELAYS_MS = [0, 800, 2500];
    for (const delay of RELOGIN_DELAYS_MS) {
      if (delay) await new Promise((r) => setTimeout(r, delay));
      try {
        const relogin = await fetch('/api/auth/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ email, password: newPassword }),
        });
        if (relogin.ok) {
          const data = await relogin.json();
          applyToken(data.access_token, email, data.must_change_password === true);
          return;
        }
      } catch { /* reintentamos */ }
    }
    logout();
  }, [applyToken, logout, state.email]);

  const user = state.email ? { email: state.email } : null;

  // F1 quarantine: with an open session but the temporary password unchanged, the app is
  // NOT mounted (no routes, no layout, no data): only the gate. It goes here — and not in
  // <ProtectedRoute> — so that no generated App.tsx can skip it.
  const gated = !!state.token && state.mustChangePassword;

  return (
    <AuthContext.Provider
      value={{ ...state, user, login, loginWithGoogle, logout, isAuthenticated: !!state.token, changePassword }}
    >
      {gated ? (
        <ChangePasswordGate
          email={state.email || ''}
          needsCurrentPassword={!pendingSecret.current}
          onSubmit={changePassword}
          onCancel={logout}
        />
      ) : (
        children
      )}
    </AuthContext.Provider>
  );
};
