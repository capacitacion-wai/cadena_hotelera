/**
 * Shared error extraction for axios/fetch failures.
 *
 * FastAPI signals operational errors as `{detail: "..."}` (string),
 * `{detail: {code, message}}` (stable codes), or `{detail: [...]}`
 * (pydantic 422). Surfacing that detail instead of the generic axios
 * "Request failed with status code 400" is the difference between an
 * actionable banner and a support ticket.
 */
import type { TFunction } from 'i18next'

type ApiDetail = { code?: string; message?: string }

function detailFromData(data: unknown): unknown {
  if (typeof data === 'string') return data
  if (data && typeof data === 'object' && 'detail' in data) {
    return (data as { detail: unknown }).detail
  }
  return data
}

function codeToI18nKey(code: string): string {
  return `errors.api.${code.replace(/\./g, '_')}`
}

/** Map a FastAPI ``detail`` payload to a localized operator message. */
export function formatApiError(detail: unknown, t: TFunction): string {
  if (typeof detail === 'string') return detail
  // Pydantic 422: [{type, loc, msg, input}, ...]. NEVER hand this to JSX —
  // rendering a plain object as a React child crashes the page (error #31).
  if (Array.isArray(detail)) {
    const msgs = detail
      .map((e) => {
        const item = e as { loc?: unknown[]; msg?: string }
        const field = Array.isArray(item.loc) ? item.loc.filter((p) => p !== 'body').join('.') : ''
        return item.msg ? (field ? `${field}: ${item.msg}` : item.msg) : ''
      })
      .filter(Boolean)
    if (msgs.length) return msgs.join(' · ')
  }
  if (detail && typeof detail === 'object') {
    const d = detail as ApiDetail
    if (d.code) {
      const key = codeToI18nKey(d.code)
      const translated = t(key, { defaultValue: '' })
      if (translated) return translated
      if (d.message) return d.message
      return d.code
    }
    if ('message' in d && typeof (d as { message?: string }).message === 'string') {
      return (d as { message: string }).message
    }
    try { return JSON.stringify(detail) } catch { /* fall through */ }
  }
  return t('common.error')
}

export function extractApiError(exc: unknown): string {
  if (typeof exc === 'object' && exc && 'response' in exc) {
    const r = (exc as { response?: { data?: unknown; statusText?: string } }).response
    const detail = detailFromData(r?.data)
    if (typeof detail === 'string') return detail
    if (detail && typeof detail === 'object') {
      const d = detail as ApiDetail
      if (d.message) return d.message
      if (d.code) return d.code
      try { return JSON.stringify(detail) } catch { /* fall through */ }
    }
    return r?.statusText || 'request failed'
  }
  return exc instanceof Error ? exc.message : 'unknown error'
}

/** Parse JSON error body from a fetch Response and localize when possible. */
export async function formatFetchError(res: Response, t: TFunction): Promise<string> {
  try {
    const data = await res.json()
    const detail = detailFromData(data)
    if (detail !== undefined) return formatApiError(detail, t)
  } catch {
    /* ignore */
  }
  return `HTTP ${res.status}`
}
