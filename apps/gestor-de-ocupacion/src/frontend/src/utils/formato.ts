/**
 * WAI base — numbers, money, percentages and dates in the screen's language (patterns/product-baseline.md §3).
 *
 * Copied by `copiar_base.sh` into every app at src/frontend/src/utils/formato.ts — never edit it: if it does not
 * fit, it is the skill's to fix. The locale is the one i18n has active (`i18n.resolvedLanguage`), so a change of
 * language in the base Layout re-formats everything. The API returns NUMBERS and ISO dates; only the screen formats.
 *
 *   <KpiCard value={formatearImporte(kpis.total)} … />      → «302,50 €»
 *   {formatearFecha(fila.fecha)}                             → «05/08/2026»
 *
 * An empty value (null, undefined, '' or not a number) is shown as «—», never as «NaN» or «Invalid Date».
 */
import i18n from 'i18next'

const VACIO = '—'

function idioma(): string | undefined {
  return i18n.resolvedLanguage || i18n.language || undefined
}

function numero(v: number | string | null | undefined): number | null {
  if (v === null || v === undefined || v === '') return null
  const n = typeof v === 'number' ? v : Number(v)
  return Number.isFinite(n) ? n : null
}

/** 1234567.891 → «1.234.567,89» (es) / «1,234,567.89» (en). `decimales` fixes them; by default up to 2. */
export function formatearNumero(v: number | string | null | undefined, decimales?: number): string {
  const n = numero(v)
  if (n === null) return VACIO
  const opciones: Intl.NumberFormatOptions = decimales === undefined
    ? { maximumFractionDigits: 2 }
    : { minimumFractionDigits: decimales, maximumFractionDigits: decimales }
  return new Intl.NumberFormat(idioma(), opciones).format(n)
}

/** An amount with its currency (ISO 4217, `EUR` by default), two decimals, the symbol where the language puts it. */
export function formatearImporte(v: number | string | null | undefined, moneda = 'EUR'): string {
  const n = numero(v)
  if (n === null) return VACIO
  return new Intl.NumberFormat(idioma(), { style: 'currency', currency: moneda }).format(n)
}

/** A percentage given as a PERCENT (12.34 → «12,3 %»), one decimal at most. */
export function formatearPorcentaje(v: number | string | null | undefined, decimales = 1): string {
  const n = numero(v)
  if (n === null) return VACIO
  return new Intl.NumberFormat(idioma(), { style: 'percent', maximumFractionDigits: decimales }).format(n / 100)
}

function fecha(v: string | number | Date | null | undefined): Date | null {
  if (v === null || v === undefined || v === '') return null
  // A date-only ISO string («2026-08-05») is a calendar day, not UTC midnight: read it as local so it never
  // shows as the day before in a timezone west of UTC.
  const soloDia = typeof v === 'string' ? /^(\d{4})-(\d{2})-(\d{2})$/.exec(v) : null
  const d = soloDia ? new Date(Number(soloDia[1]), Number(soloDia[2]) - 1, Number(soloDia[3]))
    : v instanceof Date ? v : new Date(v)
  return Number.isNaN(d.getTime()) ? null : d
}

/** A date: `corta` → «05/08/2026», `larga` → the long localized form (month spelled out). */
export function formatearFecha(v: string | number | Date | null | undefined, estilo: 'corta' | 'larga' = 'corta'): string {
  const d = fecha(v)
  if (d === null) return VACIO
  return new Intl.DateTimeFormat(idioma(), estilo === 'larga'
    ? { day: 'numeric', month: 'long', year: 'numeric' }
    : { day: '2-digit', month: '2-digit', year: 'numeric' }).format(d)
}

/** A date with its time: «05/08/2026, 14:30». */
export function formatearFechaHora(v: string | number | Date | null | undefined): string {
  const d = fecha(v)
  if (d === null) return VACIO
  return new Intl.DateTimeFormat(idioma(), {
    day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit',
  }).format(d)
}
