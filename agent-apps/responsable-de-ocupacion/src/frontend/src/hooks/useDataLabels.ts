/**
 * Re-export of `useDataLabels` so callers can import it from the
 * `hooks/` namespace if they prefer. The implementation lives in
 * `utils/dataLabels.ts` (the patterns import it from there).
 */
export { useDataLabels } from '../utils/dataLabels'
export type { Category } from '../utils/dataLabels'
