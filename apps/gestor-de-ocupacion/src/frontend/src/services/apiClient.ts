// INVARIANT FILE — copied byte for byte by `assets/copiar_base.sh` (wai-app-builder, skill 7.0.0); do not edit
// it: `verify_app.sh` fails if it differs from the skill's. Why it is like this: `patterns/authentication.md`.
import i18next from 'i18next';
import { enMovilDeWai, renovarTestigo, tokenActual } from '../auth/AuthContext';

/** ONE client for every call to `/api/*` (downloads too): the token, one retry with a fresh token inside the WAI mobile
 *  app, and a 401 outside it back to the login. */
export async function apiFetch(ruta: string, init: RequestInit = {}): Promise<Response> {
  const conToken = () => ({ ...init, headers: { ...(init.headers || {}), Authorization: `Bearer ${tokenActual()}` } });
  let res = await fetch(ruta, conToken());
  if (res.status === 401 && enMovilDeWai()) {
    await renovarTestigo();              // ONE fresh token from the container, ONE retry
    res = await fetch(ruta, conToken());
  } else if (res.status === 401) {
    localStorage.removeItem('token');
    localStorage.removeItem('email');
    window.location.href = '/login';
  }
  return res;
}

/** An API error with its status and, from an installed module, its stable code (`X-WAI-Codigo`). */
export class ApiError extends Error {
  constructor(message: string, readonly status: number, readonly codigo: string = '') { super(message); }
}

/** The text of a module's error code in the app's language, from that module's i18n namespace (`PERFILADO_X` →
 *  namespace `perfilado`, key `error_PERFILADO_X`), without importing the module: the app may not have it. */
function textoDelModulo(codigo: string): string | null {
  const ns = codigo.split('_')[0].toLowerCase();
  const clave = `error_${codigo}`;
  return ns && i18next.exists(clave, { ns }) ? String(i18next.t(clave, { ns })) : null;
}

async function request<T>(method: string, ruta: string, body?: unknown): Promise<T> {
  const res = await apiFetch(ruta, {
    method,
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    const codigo = res.headers.get('X-WAI-Codigo') || '';
    const texto = (codigo && textoDelModulo(codigo)) || (await res.text().catch(() => '')) || `HTTP ${res.status}`;
    throw new ApiError(texto, res.status, codigo);
  }
  return (res.status === 204 ? undefined : await res.json()) as T;
}

export const apiClient = {
  get: <T>(ruta: string) => request<T>('GET', ruta),
  post: <T>(ruta: string, body?: unknown) => request<T>('POST', ruta, body),
  put: <T>(ruta: string, body?: unknown) => request<T>('PUT', ruta, body),
  patch: <T>(ruta: string, body?: unknown) => request<T>('PATCH', ruta, body),
  delete: <T>(ruta: string) => request<T>('DELETE', ruta),
};
