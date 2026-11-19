/**
 * Translates database codes into human-readable labels based on
 * the current UI language.
 *
 * Mirrors demo_campanas_banco/frontend/src/utils/dataLabels.ts and
 * is extended with the agent-app categories required by
 * Operations / Events / Runs pages: `watcher_type`, `event_type`,
 * `event_status`, `run_mode`, `run_status`.
 *
 * Usage:  const dl = useDataLabels()
 *         dl('row_count_delta', 'watcher_type')  →  "Cambio en el conteo de filas" / "Row count change"
 */
import { useTranslation } from 'react-i18next'

type Category =
  | 'watcher_type'
  | 'event_type'
  | 'event_status'
  | 'run_mode'
  | 'run_status'
  | 'status'

const LABELS: Record<Category, Record<string, { es: string; en: string }>> = {
  watcher_type: {
    row_count_delta: { es: 'Cambio en el conteo de filas', en: 'Row count change' },
    value_change:    { es: 'Cambio de valor',              en: 'Value change' },
    threshold:       { es: 'Umbral',                       en: 'Threshold' },
    cron:            { es: 'Programado (cron)',            en: 'Scheduled (cron)' },
    local_event:     { es: 'Evento local',                 en: 'Local event' },
  },
  event_type: {
    row_count_delta: { es: 'Cambio en el conteo de filas', en: 'Row count change' },
    value_change:    { es: 'Cambio de valor',              en: 'Value change' },
    threshold:       { es: 'Umbral',                       en: 'Threshold' },
    cron:            { es: 'Programado',                   en: 'Scheduled' },
    local_event:     { es: 'Evento local',                 en: 'Local event' },
    manual:          { es: 'Manual',                       en: 'Manual' },
  },
  event_status: {
    pending:         { es: 'Pendiente',         en: 'Pending' },
    claimed:         { es: 'En curso',          en: 'Claimed' },
    completed:       { es: 'Completado',        en: 'Completed' },
    failed:          { es: 'Fallido',           en: 'Failed' },
    retry_scheduled: { es: 'Reintento programado', en: 'Retry scheduled' },
    cancelled:       { es: 'Cancelado',         en: 'Cancelled' },
  },
  run_mode: {
    chat:       { es: 'Chat',       en: 'Chat' },
    autonomous: { es: 'Autónomo',   en: 'Autonomous' },
  },
  run_status: {
    success: { es: 'Éxito',   en: 'Success' },
    error:   { es: 'Error',   en: 'Error' },
    partial: { es: 'Parcial', en: 'Partial' },
  },
  status: {
    pending:         { es: 'Pendiente',            en: 'Pending' },
    claimed:         { es: 'En curso',             en: 'Claimed' },
    completed:       { es: 'Completado',           en: 'Completed' },
    failed:          { es: 'Fallido',              en: 'Failed' },
    retry_scheduled: { es: 'Reintento programado', en: 'Retry scheduled' },
    cancelled:       { es: 'Cancelado',            en: 'Cancelled' },
    success:         { es: 'Éxito',                en: 'Success' },
    error:           { es: 'Error',                en: 'Error' },
    partial:         { es: 'Parcial',              en: 'Partial' },
  },
}

type DataLabelFn = (code: string, category: Category) => string

export function useDataLabels(): DataLabelFn {
  const { i18n } = useTranslation()
  const lang = (i18n.language?.startsWith('en') ? 'en' : 'es') as 'es' | 'en'

  return (code: string, category: Category): string => {
    const key = (code ?? '').toLowerCase()
    const entry = LABELS[category]?.[key] ?? LABELS[category]?.[code]
    if (entry) return entry[lang]
    return code
  }
}

/**
 * Picks the language-appropriate label from a manifest node that may
 * carry localised variants. Manifest entries authored by the skill
 * follow this convention:
 *   { label: 'Pedidos 30 días', label_en: 'Orders 30 days' }
 * or the more explicit:
 *   { labels: { es: '...', en: '...' } }
 *
 * The fallback chain is: labels[lang] → label_<lang> → label → '—'.
 *
 * Use this everywhere the UI consumes manifest-driven copy
 * (KPIs, charts, domain entities, watchers, event-type specs) so the
 * dashboard does not show raw Spanish keys when the user has switched
 * to English (or vice-versa).
 */
export function usePickLabel() {
  const { i18n } = useTranslation()
  const lang = (i18n.language?.startsWith('en') ? 'en' : 'es') as 'es' | 'en'

  return (node: any, fallback = '—'): string => {
    if (!node) return fallback
    const labels = node.labels
    if (labels && typeof labels === 'object') {
      if (labels[lang]) return labels[lang] as string
      if (labels.es) return labels.es as string
      if (labels.en) return labels.en as string
    }
    if (lang === 'en' && node.label_en) return node.label_en as string
    if (lang === 'es' && node.label_es) return node.label_es as string
    if (node.label) return node.label as string
    if (node.title) return node.title as string
    if (node.name) return node.name as string
    return fallback
  }
}

export type { Category }
