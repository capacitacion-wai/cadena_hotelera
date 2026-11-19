/**
 * Fetch wrapper with automatic JWT injection + 401 handling.
 *
 * The patterns under `patterns/*-page.md` import `apiBase` and
 * `apiFetch` from this module. Every request to `/api/*` MUST go
 * through `apiFetch` so the bearer token is attached and the user
 * is redirected to /login on 401.
 *
 * The exported `apiClient.{get,post}` mirrors wai-app-builder
 * SKILL.md (Frontend — API Client) for callers that prefer the
 * convenience methods.
 */

export const apiBase = '/api'

function authHeaders(extra?: HeadersInit, body?: BodyInit | null): HeadersInit {
  const token = localStorage.getItem('token')
  const headers: Record<string, string> = {}
  if (token) headers['Authorization'] = `Bearer ${token}`
  // Default JSON content-type for string bodies: fetch() would otherwise send
  // text/plain, FastAPI then skips JSON parsing and validates the RAW BYTES
  // against the Body() type → pydantic 422 [{type, loc, msg, input}] before
  // the handler ever runs. Callers can still override via init.headers.
  if (typeof body === 'string') headers['Content-Type'] = 'application/json'
  return { ...headers, ...(extra as Record<string, string> | undefined) }
}

// R7: idempotent verbs are safe to retry on transient ingress / pod
// failures (503 / 504 / network refused). POST / PUT / DELETE are
// generally NOT safe to auto-retry — the server may have already
// committed the side effect. Callers that know their POST is
// idempotent can opt in via ``init.headers['x-retry']='idempotent'``.
const RETRYABLE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS'])
const RETRY_DELAYS_MS = [1000, 3000, 8000]

/**
 * v7.124.0 — temporary-password quarantine (auth F1), client side.
 *
 * The veto lives on the SERVER (`backend/app/auth.py`): with the `must_change_password`
 * claim in the JWT, the WHOLE API answers 403 with
 * `X-Auth-Error-Code: PASSWORD_CHANGE_REQUIRED`. If the SPA gets mounted with a
 * quarantined session (tab opened before the change, `localStorage` cleared by hand,
 * token issued by another surface…) every screen would show an opaque error.
 * Here we turn it into the CORRECT way out: we set the flag and reload ONCE so that
 * `<AuthProvider>` mounts the password-change gate. If the flag was already set we do
 * NOT reload (the gate should already be on screen): that makes a reload loop
 * impossible.
 */
const MUST_CHANGE_KEY = 'must_change_password'
export const PASSWORD_CHANGE_REQUIRED = 'PASSWORD_CHANGE_REQUIRED'

export function isPasswordQuarantine(status: number, code: string | null | undefined): boolean {
  return status === 403 && code === PASSWORD_CHANGE_REQUIRED
}

export function enterPasswordQuarantine(): void {
  let already = false
  try {
    already = localStorage.getItem(MUST_CHANGE_KEY) === '1'
    localStorage.setItem(MUST_CHANGE_KEY, '1')
  } catch { /* ignore */ }
  if (!already && typeof window !== 'undefined') window.location.reload()
}

function _isIdempotentRetry(method: string, init: RequestInit): boolean {
  if (RETRYABLE_METHODS.has(method.toUpperCase())) return true
  const headers = init.headers as Record<string, string> | undefined
  return headers?.['x-retry'] === 'idempotent'
}

export async function apiFetch(input: string, init: RequestInit = {}): Promise<Response> {
  const url = input.startsWith('http') ? input : input
  const method = (init.method || 'GET').toUpperCase()
  const canRetry = _isIdempotentRetry(method, init)

  let lastError: unknown = null
  for (let attempt = 0; attempt <= (canRetry ? RETRY_DELAYS_MS.length : 0); attempt++) {
    try {
      const res = await fetch(url, { ...init, headers: authHeaders(init.headers, init.body) })
      if (res.status === 401) {
        localStorage.removeItem('token')
        localStorage.removeItem('email')
        // v7.88.0 — drop this user's chat state on session expiry so the next
        // operator on the browser can't see it (keys are ``wai-agent.*``).
        try {
          for (let i = localStorage.length - 1; i >= 0; i--) {
            const k = localStorage.key(i)
            if (k && k.startsWith('wai-agent.')) localStorage.removeItem(k)
          }
        } catch { /* ignore */ }
        if (typeof window !== 'undefined' && !window.location.pathname.startsWith('/login')) {
          window.location.href = '/login'
        }
        throw new Error('Unauthorized')
      }
      // F1 quarantine: the server vetoes the session until the temporary password is changed.
      if (isPasswordQuarantine(res.status, res.headers.get('X-Auth-Error-Code'))) {
        enterPasswordQuarantine()
        throw new Error(PASSWORD_CHANGE_REQUIRED)
      }
      // Retry on transient ingress/pod failures only.
      if ((res.status === 503 || res.status === 504) && canRetry && attempt < RETRY_DELAYS_MS.length) {
        await new Promise((r) => setTimeout(r, RETRY_DELAYS_MS[attempt]))
        continue
      }
      return res
    } catch (err) {
      // The quarantine is NOT a transient failure: retrying would only repeat the 403
      // (and delay the reload towards the gate). It is propagated as is.
      if (err instanceof Error && err.message === PASSWORD_CHANGE_REQUIRED) throw err
      lastError = err
      if (!canRetry || attempt >= RETRY_DELAYS_MS.length) break
      await new Promise((r) => setTimeout(r, RETRY_DELAYS_MS[attempt]))
    }
  }
  throw lastError ?? new Error('network error')
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {}
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  const res = await apiFetch(path, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(text || `HTTP ${res.status}`)
  }
  return res.json() as Promise<T>
}

export const apiClient = {
  get: <T>(path: string) => request<T>('GET', path),
  post: <T>(path: string, body: unknown) => request<T>('POST', path, body),
}
