/**
 * Human, internationalised labels for EVENT TYPES (the run's "origen" and the
 * event's "tipo"). Raw values are machine identifiers — SYSTEM types like
 * ``DELEGATED_TASK`` / ``automation_fired`` and per-agent BUSINESS types like
 * ``STOCK_HEALTH_CHECK`` — plus the synthetic run buckets ``(chat)`` / ``(manual)``.
 *
 * Rule: translate the KNOWN system types via i18n (``eventTypes.<TYPE>``); for
 * anything custom, fall back to a de-cased, readable form. The raw value is kept
 * as the option/filter value everywhere — only the DISPLAY is localised — so the
 * selector, chart and table all filter on the same identifier the backend uses.
 *
 * NOT the same as ``dataLabels.ts``'s ``event_type`` category: that one labels
 * WATCHER trigger kinds (row_count_delta, threshold, cron…). This one labels the
 * runtime ``Event.type`` taxonomy (DELEGATED_TASK, automation_fired, business
 * events…) with a humanise fallback for the agent's own business types.
 */
import { useTranslation } from 'react-i18next';

/** De-case a raw identifier: ``STOCK_HEALTH_CHECK`` → "Stock health check".
 *  Synthetic ``(chat)``/``(manual)`` buckets keep their inner word capitalised. */
export function humaniseEventType(value: string): string {
  if (!value) return '';
  const paren = value.trim().match(/^\((.+)\)$/);
  const v = paren ? paren[1] : value;
  const spaced = v.replace(/[_-]+/g, ' ').trim().toLowerCase();
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

/** Truncate a display label with an ellipsis (for chart axis ticks). */
export function truncateLabel(label: string, max = 22): string {
  return label.length > max ? `${label.slice(0, max - 1)}…` : label;
}

/**
 * Hook → ``(type) => localized label``. Looks up ``eventTypes.<key>`` (synthetic
 * ``(chat)`` maps to key ``_chat``); missing keys fall back to
 * ``humaniseEventType`` so a business/custom type is still readable.
 */
export function useEventTypeLabel(): (type: string | null | undefined) => string {
  const { t } = useTranslation();
  return (type) => {
    const v = String(type ?? '').trim();
    if (!v || v === '—') return '—';
    const paren = v.match(/^\((.+)\)$/);
    const key = paren ? `_${paren[1]}` : v;
    return t(`eventTypes.${key}`, humaniseEventType(v));
  };
}

/**
 * Hook → ``(source) => localized label`` for an event's ORIGEN. Raw sources are
 * ``scheduled_item:49`` / ``automation:6`` / ``channel:3`` / ``manual[-…]`` — the
 * bare ``prefix:id`` reads like a bug ("el número está mal"). We render the KIND
 * in business language plus the id: "Encargo #49", "Automatización #6", "Canal #3".
 * Unknown prefixes fall back to a de-cased form. The id is the real row id (the
 * encargo/automation/channel), so it's clickable-meaningful even if we only label.
 */
export function useSourceLabel(): (source: string | null | undefined) => string {
  const { t } = useTranslation();
  return (source) => {
    const v = String(source ?? '').trim();
    if (!v || v === '—') return '—';
    if (v === 'manual' || v.startsWith('manual-') || v.startsWith('manual:')) {
      return t('eventTypes._manual', 'Manual');
    }
    const m = v.match(/^([a-z_]+):(.+)$/i);
    if (m && ['scheduled_item', 'automation', 'channel'].includes(m[1].toLowerCase())) {
      return t(`sources.${m[1].toLowerCase()}`, { id: m[2], defaultValue: `${m[1]} #${m[2]}` });
    }
    return humaniseEventType(v);
  };
}
