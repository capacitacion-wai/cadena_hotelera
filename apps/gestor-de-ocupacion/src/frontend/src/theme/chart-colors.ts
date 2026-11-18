/**
 * WAI — chart and status colors, read from the app's THEME (`tema.ts`).
 *
 * CHART_COLORS: the color series for Recharts or another charting library.
 * STATUS_COLORS: the traffic light (ok / warn / error) and the task states.
 * FUNNEL_COLORS: accepted / rejected / no response.
 * SCORE_RANGE_COLORS: the bands of a score, from worst to best.
 *
 * The names are the usual ones (the screens import them); the values are no longer
 * written here. The default theme ships the Tol "muted" series, colorblind-safe.
 */
import { TEMA } from './tema'

const e = TEMA.estados

export const CHART_COLORS: string[] = [...TEMA.graficos]

export const STATUS_COLORS = {
  ok: e.ok,
  warn: e.warn,
  error: e.error,
  neutral: e.neutral,
  info: e.info,
  PENDING: e.warn,
  CLAIMED: e.reclamado,
  RUNNING: e.info,
  COMPLETED: e.ok,
  FAILED: e.error,
  RETRYING: e.warn,
  CANCELLED: e.neutral,
}

export const FUNNEL_COLORS = {
  accepted: e.aceptado,
  rejected: e.rechazado,
  noResponse: e.sin_respuesta,
}

export const SCORE_RANGE_COLORS: string[] = [...TEMA.rangos]
