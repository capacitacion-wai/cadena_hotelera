// INVARIANT FILE — copied byte for byte by `assets/copiar_base.sh` (wai-app-builder, skill 7.0.0); do not edit
// it: `verify_app.sh` fails if it differs from the skill's. Why it is like this: `patterns/authentication.md`.
import React, { createContext, useContext, useState, useCallback, useEffect } from 'react';

declare global {
  interface Window {
    WAI_NATIVO?: { version?: string; plataforma?: string; pedirTestigo: () => Promise<string>;
                   cerrarSesion?: () => void; verFichero?: (f: { nombre: string; tipo: string; datos: string }) => void };
  }
}

/** Inside the WAI mobile app? Detection is `window.WAI_NATIVO` and only that (dentro-del-movil.md §1). */
export const enMovilDeWai = () => typeof window !== 'undefined' && !!window.WAI_NATIVO;

// The token the HTTP client reads. In native mode it lives ONLY here (memory), never in localStorage.
let testigoNativo: string | null = null;
export const tokenActual = () => (enMovilDeWai() ? testigoNativo : localStorage.getItem('token'));
/** Native mode: a fresh token from the container (on start and on a 401). */
export async function renovarTestigo(): Promise<string | null> {
  if (!enMovilDeWai()) return null;
  testigoNativo = await window.WAI_NATIVO!.pedirTestigo();
  return testigoNativo;
}

interface AuthState {
  token: string | null;
  email: string | null;
}

interface AuthContextValue extends AuthState {
  login: (email: string, password: string) => Promise<void>;
  logout: () => void;
  isAuthenticated: boolean;
  /** Native mode waiting for the container's token: render a loader, NOT the login. */
  esperandoAlMovil: boolean;
  nativo: boolean;
}

const AuthContext = createContext<AuthContextValue>(null!);
export const useAuth = () => useContext(AuthContext);

export const AuthProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const nativo = enMovilDeWai();
  const [state, setState] = useState<AuthState>(() => (nativo
    ? { token: null, email: null }
    : { token: localStorage.getItem('token'), email: localStorage.getItem('email') }));
  const [esperandoAlMovil, setEsperando] = useState(nativo);

  useEffect(() => {
    if (!nativo) return;
    void renovarTestigo()
      .then((t) => setState({ token: t, email: null }))
      .finally(() => setEsperando(false));
  }, [nativo]);

  // Arriving from an employee with `?wai=<pass>` (a sixty-second, single-use pass): exchanged by the SERVER. The
  // address bar is ALWAYS cleaned, whether it goes well or badly: a spent pass keeps showing in history and screenshots.
  useEffect(() => {
    const p = new URLSearchParams(window.location.search);
    const codigo = p.get('wai');
    if (!codigo) return;
    p.delete('wai');
    window.history.replaceState({}, '', window.location.pathname + (p.toString() ? `?${p}` : '') + window.location.hash);
    void fetch('/api/auth/handoff', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ codigo }),
    }).then(async (r) => {
      if (!r.ok) return;              // pass expired or already used: the login is asked for as always
      const d = await r.json();
      localStorage.setItem('token', d.access_token);
      setState({ token: d.access_token, email: null });
    });
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
    });
    if (!res.ok) throw new Error('Invalid credentials');
    const data = await res.json();
    localStorage.setItem('token', data.access_token);
    localStorage.setItem('email', email);
    setState({ token: data.access_token, email });
  }, []);

  const logout = useCallback(() => {
    // A session the app did not open is not its own to close: the container does it.
    if (enMovilDeWai()) { window.WAI_NATIVO!.cerrarSesion?.(); return; }
    localStorage.removeItem('token');
    localStorage.removeItem('email');
    setState({ token: null, email: null });
  }, []);

  return (
    <AuthContext.Provider value={{ ...state, login, logout, isAuthenticated: !!state.token,
                                   esperandoAlMovil, nativo }}>
      {children}
    </AuthContext.Provider>
  );
};
