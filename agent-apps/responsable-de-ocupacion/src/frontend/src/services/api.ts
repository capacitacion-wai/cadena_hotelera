/**
 * Thin axios wrapper for `/api/*` endpoints.
 *
 * Mirrors `demo_campanas_banco/frontend/src/services/api.ts`. Every
 * request gets the JWT bearer token (read from localStorage on each
 * request via the request interceptor) and on 401 the user is
 * redirected to /login. Use this when a page wants axios; otherwise
 * import `apiFetch` from `./apiClient`.
 */
import axios from 'axios'
import { enterPasswordQuarantine, isPasswordQuarantine } from './apiClient'

const api = axios.create({
  baseURL: '/api',
})

api.interceptors.request.use((config) => {
  const token = localStorage.getItem('token')
  if (token) {
    config.headers = config.headers ?? {}
    ;(config.headers as Record<string, string>).Authorization = `Bearer ${token}`
  }
  return config
})

api.interceptors.response.use(
  (res) => res,
  (err) => {
    if (err?.response?.status === 401) {
      localStorage.removeItem('token')
      localStorage.removeItem('email')
      // v7.88.0 — clear this user's chat state (``wai-agent.*``) on expiry.
      try {
        for (let i = localStorage.length - 1; i >= 0; i--) {
          const k = localStorage.key(i)
          if (k && k.startsWith('wai-agent.')) localStorage.removeItem(k)
        }
      } catch { /* ignore */ }
      if (typeof window !== 'undefined' && !window.location.pathname.startsWith('/login')) {
        window.location.href = '/login'
      }
    }
    // v7.124.0 — F1 quarantine: the server vetoes the session until the temporary password
    // is changed (403 PASSWORD_CHANGE_REQUIRED). We take the user to the gate instead of
    // leaving the screen full of opaque errors (see apiClient.ts).
    const status = err?.response?.status
    const code = err?.response?.headers?.['x-auth-error-code']
    if (isPasswordQuarantine(status, code)) enterPasswordQuarantine()
    return Promise.reject(err)
  }
)

export default api
