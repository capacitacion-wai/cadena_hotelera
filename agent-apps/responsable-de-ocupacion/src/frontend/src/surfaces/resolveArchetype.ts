/**
 * Archetype resolution for "Lo que genero" (the agent's OUTPUT entities).
 *
 * The employee-app is manifest-driven: every `domain_entities[]` entry renders
 * itself from its declared columns/lifecycle. v7.30.0 replaces the single
 * generic CRUD grid with a small library of BUSINESS SURFACES ("archetypes"),
 * and binds each entity to one. The binding is explicit (`entity.ui_archetype`
 * in the manifest, authored by the code-agent) with a SHAPE-INFERENCE fallback,
 * so a NEW entity the agent invents still lands on the right surface — and never
 * worse than a kanban `board` — without a code change.
 *
 * This module is PURE logic (no React) so it can be unit-reasoned and reused by
 * the router (`DomainSurface`) and the nav.
 */

export type Archetype =
  | 'inbox'      // triage queue (alerts): severity lanes + acknowledge/resolve
  | 'outbox'     // review-and-send desk (communications): draft→ready gate
  | 'advisory'   // decision-support cards (recommendations): accept/reject/act
  | 'ledger'     // append-only audit trail (decisions): read + promote
  | 'board'      // generic kanban grouped by a lifecycle status field
  | 'register';  // tabular grid — the current DomainList (last resort)

// v7.38.0 — `scoreboard` was REMOVED: it was too complex and confused operators.
// A metrics/scoring screen is now opt-in only (SKILL.md): model it as a normal
// entity → it renders as the `register` grid. Do not reintroduce a scoreboard
// archetype unless the user explicitly asks for a dedicated metrics surface.
export const ARCHETYPES: readonly Archetype[] = [
  'inbox', 'outbox', 'advisory', 'ledger', 'board', 'register',
] as const;

/** Per-surface tuning the manifest MAY carry under `entity.ui_config`. Every
 * field is optional; a surface infers sensible defaults from the columns when a
 * field is absent. Kept permissive on purpose — surfaces read what they need. */
export interface UiConfig {
  /** The prominent title column (else inferred: title/name/subject/metric). */
  headline?: string;
  /** The categorical column that drives lanes/severity/priority chips. */
  status_field?: string;
  /** Ordered lane values for board/outbox/advisory grouping. */
  lanes?: string[];
  /** A column resolved to a directory USER (avatar+name), never a raw id. */
  person_field?: string;
  /** For scoreboard: the numeric score column and (optional) trend baseline. */
  score_field?: string;
  threshold?: { warn?: number; danger?: number; direction?: 'low_bad' | 'high_bad' };
  /** Long markdown prose column (rationale/reason/body/description). */
  body_field?: string;
  /** For entity-links: {kind_field, id_field} → deep-link into that entity. */
  target?: { kind_field?: string; id_field?: string };
  [k: string]: any;
}

// ---- shape probes ---------------------------------------------------------

/** All column identifiers an entity declares (id AND the physical column). */
function columnIds(entity: any): Set<string> {
  const out = new Set<string>();
  for (const c of (entity?.list_columns ?? [])) {
    if (c?.id) out.add(String(c.id).toLowerCase());
    if (c?.column) out.add(String(c.column).toLowerCase());
  }
  // detail_blocks fields count too — a column may live only in the detail view.
  for (const b of (entity?.detail_blocks ?? [])) {
    for (const f of (b?.fields ?? [])) {
      const key = f?.id ?? f?.column ?? f;
      if (key) out.add(String(key).toLowerCase());
    }
  }
  return out;
}

const lifecycleStates = (entity: any): string[] =>
  (entity?.lifecycle?.states ?? []).map((s: any) => String(s).toLowerCase());

/** The canonical substrate tables map to a fixed surface. Checked before shape
 * inference so agents deployed BEFORE v7.30.0 (whose manifest carries no
 * `ui_archetype`, and whose `list_columns` may be sparse) still land on the
 * right surface with zero manifest change. */
const CANONICAL_TABLE: Record<string, Archetype> = {
  alerts: 'inbox',
  communications: 'outbox',
  recommendations: 'advisory',
  decisions: 'ledger',
  // `scoring` intentionally omitted (v7.38.0): it falls through to `register`.
};

/** True when ANY column id matches the regex. */
const hasCol = (cols: Set<string>, re: RegExp): boolean => {
  for (const c of cols) if (re.test(c)) return true;
  return false;
};

const statesIncludeAny = (states: string[], vals: string[]): boolean =>
  states.some((s) => vals.includes(s));

// ---- inference ------------------------------------------------------------

/**
 * Infer the best surface from an entity's SHAPE. Order matters: the most
 * specific archetypes are tested first; `board`/`register` are the fallbacks.
 * Never throws — an unreadable entity resolves to `register`.
 */
export function inferArchetype(entity: any): Archetype {
  try {
    const cols = columnIds(entity);
    const states = lifecycleStates(entity);
    const table = String(entity?.table ?? entity?.slug ?? '').toLowerCase();
    const slug = String(entity?.slug ?? '').toLowerCase();

    // Canonical substrate → fixed surface (robust for pre-7.30.0 manifests).
    if (CANONICAL_TABLE[table]) return CANONICAL_TABLE[table];
    if (CANONICAL_TABLE[slug]) return CANONICAL_TABLE[slug];

    // outbox — a dispatchable message: a channel, a recipient and a body.
    if (
      hasCol(cols, /(^|_)channel($|_)/) &&
      hasCol(cols, /(^|_)(recipient|to|addressee)($|_)/) &&
      hasCol(cols, /(^|_)(body|message|content)(_md)?($|_)/)
    ) return 'outbox';

    // inbox — an operational alert: a severity, triaged through a lifecycle.
    // v7.40.0 unified states (open/acknowledged/in_progress/done/dismissed); the
    // legacy `resolved` is still accepted for pre-upgrade invented entities.
    if (
      hasCol(cols, /(^|_)severity($|_)/) &&
      (states.length === 0 || statesIncludeAny(states, ['open', 'acknowledged', 'in_progress', 'done', 'resolved', 'dismissed']))
    ) return 'inbox';

    // advisory — a suggestion with a rationale and a priority, on the lifecycle.
    if (
      (hasCol(cols, /(^|_)(rationale|recommendation)(_md)?($|_)/) || hasCol(cols, /(^|_)expected_impact($|_)/)) &&
      (hasCol(cols, /(^|_)priority($|_)/) || statesIncludeAny(states, ['acknowledged', 'in_progress', 'accepted', 'rejected', 'pending', 'done', 'dismissed']))
    ) return 'advisory';

    // ledger — an append-only decision/audit note: no lifecycle, has a reason.
    if (
      states.length === 0 &&
      (hasCol(cols, /(^|_)(reason|decision|rationale)(_md)?($|_)/) || hasCol(cols, /(^|_)source_event_id($|_)/))
    ) return 'ledger';

    // board — anything else that has a real lifecycle → kanban by status.
    if (states.length >= 2) return 'board';
  } catch {
    /* fall through to register */
  }
  // register — tabular reference data with no lifecycle: the grid is best.
  return 'register';
}

/** Normalise an explicit `ui_archetype`, ignoring unknown values. */
function explicitArchetype(entity: any): Archetype | null {
  const a = String(entity?.ui_archetype ?? '').toLowerCase().trim();
  return (ARCHETYPES as readonly string[]).includes(a) ? (a as Archetype) : null;
}

/**
 * The archetype for an entity: the manifest's explicit `ui_archetype` when
 * present and valid, otherwise inferred from the entity's shape. Pure/total.
 */
export function resolveArchetype(entity: any): Archetype {
  return explicitArchetype(entity) ?? inferArchetype(entity);
}

/** The `ui_config` bag (never null). */
export function uiConfig(entity: any): UiConfig {
  const c = entity?.ui_config;
  return c && typeof c === 'object' ? (c as UiConfig) : {};
}
