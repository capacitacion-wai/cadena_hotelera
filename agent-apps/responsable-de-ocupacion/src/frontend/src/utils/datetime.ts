/**
 * Centralised date/time formatting for the SPA.
 *
 * Policy: the backend ships every timestamp as ISO-8601 with an
 * explicit offset (``…Z`` / ``+00:00``). We render it in a single
 * EFFECTIVE timezone chosen by the operator (a global toggle, see
 * below), and we always label which one it is (the formatters append a
 * short tz abbreviation like "CEST" and ``Layout`` shows a header
 * indicator).
 *
 * Two modes, persisted per browser (``localStorage['wai.tzMode']``):
 *   * ``business`` (default) — the company's timezone declared in the
 *     manifest (``timezone`` / ``locale.timezone``, mirrored on
 *     ``window.__waiTimezone`` by ``Layout``). Every operator sees the
 *     same wall clock the agent's crons and calendar use, regardless of
 *     where they sit.
 *   * ``local`` — the operator's OWN browser timezone, for when they
 *     prefer to read everything in their local wall clock.
 *
 * The choice is global (one switch in the top bar applies to every
 * screen) and reactive: components re-render through ``Layout`` when it
 * changes. Every component MUST format dates through these helpers
 * instead of calling ``toLocaleString`` directly, otherwise the toggle
 * and the tz label can't stay consistent.
 */

import { useSyncExternalStore } from 'react'

export function businessTimeZone(): string | undefined {
  if (typeof window !== 'undefined') {
    const tz = (window as unknown as { __waiTimezone?: string }).__waiTimezone
    if (tz) return tz
  }
  return undefined
}

/** The operator's own browser timezone (IANA), or undefined if the
 * runtime can't resolve it. */
export function browserTimeZone(): string | undefined {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || undefined
  } catch {
    return undefined
  }
}

// ── Display-timezone preference (business vs the operator's own) ──────
// A tiny external store so the pure formatter functions can read the
// current choice, and React can subscribe to changes via
// ``useSyncExternalStore`` (see ``useTzMode`` below) without threading a
// context through every component that prints a date.
export type TzMode = 'business' | 'local'

const TZ_STORAGE_KEY = 'wai.tzMode'
const tzListeners = new Set<() => void>()

function readInitialTzMode(): TzMode {
  try {
    const v = localStorage.getItem(TZ_STORAGE_KEY)
    if (v === 'business' || v === 'local') return v
  } catch {
    /* ignore */
  }
  return 'business'
}

let tzMode: TzMode = readInitialTzMode()

export function getTzMode(): TzMode {
  return tzMode
}

export function setTzMode(mode: TzMode): void {
  if (mode !== 'business' && mode !== 'local') return
  if (mode === tzMode) return
  tzMode = mode
  try {
    localStorage.setItem(TZ_STORAGE_KEY, mode)
  } catch {
    /* ignore */
  }
  tzListeners.forEach((cb) => cb())
}

export function subscribeTzMode(cb: () => void): () => void {
  tzListeners.add(cb)
  return () => {
    tzListeners.delete(cb)
  }
}

/** React hook: the current display-timezone mode, re-rendering the
 * caller whenever it changes (used by ``Layout`` for the switch and to
 * remount the routed screen so every date re-formats). */
export function useTzMode(): TzMode {
  return useSyncExternalStore(subscribeTzMode, getTzMode, getTzMode)
}

/** The IANA timezone every formatter renders in, honouring the toggle.
 * Returns undefined (→ the runtime default) only when the chosen tz
 * can't be resolved. */
export function effectiveTimeZone(): string | undefined {
  if (tzMode === 'local') return browserTimeZone()
  return businessTimeZone() ?? browserTimeZone()
}

/** Active timezone IANA name, for the header indicator / tooltips. */
export function activeTimeZoneName(): string | undefined {
  return tzMode === 'local' ? browserTimeZone() : businessTimeZone() ?? browserTimeZone()
}

/** Short tz abbreviation (e.g. "CEST", "EDT") of the ACTIVE timezone at
 * the given instant (DST-correct). Used by the header indicator. */
export function timeZoneAbbrev(
  value?: string | number | Date | null,
): string {
  const d = toDate(value ?? Date.now()) ?? new Date()
  try {
    const parts = new Intl.DateTimeFormat(browserLocale(), {
      timeZone: effectiveTimeZone(),
      timeZoneName: 'short',
    }).formatToParts(d)
    return parts.find((p) => p.type === 'timeZoneName')?.value ?? ''
  } catch {
    return ''
  }
}

/** True when the company timezone and the operator's browser timezone
 * actually differ — i.e. the toggle changes what they see. Lets the UI
 * hide the switch when it would be a no-op. */
export function businessDiffersFromLocal(): boolean {
  const b = businessTimeZone()
  const l = browserTimeZone()
  if (!b || !l) return false
  return b !== l
}

function browserLocale(): string {
  if (typeof navigator !== 'undefined' && navigator.language) return navigator.language
  return 'es-ES'
}

function toDate(value: string | number | Date | null | undefined): Date | null {
  if (value === null || value === undefined || value === '') return null
  const d = value instanceof Date ? value : new Date(value)
  return Number.isNaN(d.getTime()) ? null : d
}

/** Full date + time in the effective timezone, labelled with its short
 * abbreviation (e.g. "16 jun 2026, 14:41 CEST"). */
export function formatDateTime(
  value: string | number | Date | null | undefined,
  opts?: Intl.DateTimeFormatOptions,
): string {
  const d = toDate(value)
  if (!d) return '—'
  try {
    return new Intl.DateTimeFormat(browserLocale(), {
      timeZone: effectiveTimeZone(),
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      // Always say which timezone the time is in, so an operator never
      // has to guess whether "14:41" is business or their own.
      timeZoneName: 'short',
      ...opts,
    }).format(d)
  } catch {
    return d.toLocaleString()
  }
}

/** Date only, in the effective timezone (no time → no tz label). */
export function formatDate(
  value: string | number | Date | null | undefined,
  opts?: Intl.DateTimeFormatOptions,
): string {
  return formatDateTime(value, {
    hour: undefined, minute: undefined, timeZoneName: undefined, ...opts,
  })
}

/** Time only (HH:mm) in the effective timezone, with the tz label. */
export function formatTime(
  value: string | number | Date | null | undefined,
): string {
  return formatDateTime(value, {
    year: undefined, month: undefined, day: undefined,
    hour: '2-digit', minute: '2-digit', timeZoneName: 'short',
  })
}

/** Compact relative phrasing ("hace 5 min", "en 2 h"); falls back to an
 * absolute business-tz datetime past a day. Locale-aware via Intl. */
export function formatRelative(
  value: string | number | Date | null | undefined,
  lang: string = 'es',
): string {
  const d = toDate(value)
  if (!d) return '—'
  const diffMs = d.getTime() - Date.now()
  const absMin = Math.abs(diffMs) / 60000
  try {
    const rtf = new Intl.RelativeTimeFormat(lang, { numeric: 'auto' })
    if (absMin < 1) return lang.startsWith('en') ? 'just now' : 'ahora'
    if (absMin < 60) return rtf.format(Math.round(diffMs / 60000), 'minute')
    if (absMin < 60 * 24) return rtf.format(Math.round(diffMs / 3600000), 'hour')
    if (absMin < 60 * 24 * 7) return rtf.format(Math.round(diffMs / 86400000), 'day')
  } catch {
    /* fall through to absolute */
  }
  return formatDateTime(value)
}

/** True when the instant is strictly in the past. */
export function isPast(value: string | number | Date | null | undefined): boolean {
  const d = toDate(value)
  return d ? d.getTime() < Date.now() : false
}

/**
 * Convert an ISO/Date into the ``YYYY-MM-DDTHH:mm`` string a native
 * ``<input type="datetime-local">`` expects, expressed in the BUSINESS
 * timezone so editing keeps the operator's wall clock.
 */
export function toLocalInputValue(
  value: string | number | Date | null | undefined,
): string {
  const d = toDate(value)
  if (!d) return ''
  const tz = effectiveTimeZone()
  try {
    const parts = new Intl.DateTimeFormat('en-CA', {
      timeZone: tz,
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', hour12: false,
    }).formatToParts(d)
    const get = (t: string) => parts.find((p) => p.type === t)?.value ?? '00'
    return `${get('year')}-${get('month')}-${get('day')}T${get('hour')}:${get('minute')}`
  } catch {
    return ''
  }
}

/**
 * Turn a ``<input type="datetime-local">`` wall-clock string (no offset)
 * into an ISO-8601 string WITH the business-tz offset, so the backend
 * stores the correct instant. We compute the tz offset for that wall
 * time and append it.
 */
export function fromLocalInputValue(local: string): string | null {
  if (!local) return null
  const tz = effectiveTimeZone()
  if (!tz) {
    // No business tz configured → treat as UTC wall clock.
    return new Date(local + ':00Z').toISOString()
  }
  // Find the offset (minutes) the business tz has at this wall time.
  const naive = new Date(local + ':00Z') // interpret the digits as UTC first
  try {
    const dtf = new Intl.DateTimeFormat('en-US', {
      timeZone: tz, hour12: false,
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    })
    const parts = dtf.formatToParts(naive)
    const get = (t: string) => Number(parts.find((p) => p.type === t)?.value ?? '0')
    const asUTC = Date.UTC(
      get('year'), get('month') - 1, get('day'),
      get('hour'), get('minute'), get('second'),
    )
    const offsetMs = asUTC - naive.getTime()
    // The real instant = the wall-clock digits minus the tz offset.
    return new Date(naive.getTime() - offsetMs).toISOString()
  } catch {
    return new Date(local + ':00Z').toISOString()
  }
}


/** The locale for `Intl` / `toLocale*`: one of the app's two languages (14.17.4). The raw
 *  `i18n.language` is whatever the browser reported, and an odd tag makes `Intl` throw a
 *  RangeError that blanks the screen (seen in the headless captures: Calendar). */
export function localeSeguro(lang?: string | null): 'es' | 'en' {
  return String(lang || '').toLowerCase().startsWith('en') ? 'en' : 'es'
}
