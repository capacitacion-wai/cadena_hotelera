/**
 * Data + action client shared by every business surface. Thin wrappers over the
 * generic domain endpoints (identical contract to `DomainList`/`DomainDetail`)
 * plus the v7.30.0 archetype ACTION endpoint. All requests go through
 * `apiFetch`, so the bearer token is attached and 401 is handled centrally.
 */
import { apiFetch } from '../../services/apiClient';

export interface EntityQuery {
  q?: string;
  sort?: string;
  direction?: 'asc' | 'desc';
  page?: number;      // 1-based
  pageSize?: number;
  filters?: Record<string, string | undefined>;
  /** Filter rows produced by this standing order (automation id). */
  created_by_automation?: string;
  only_open?: boolean;
}

export interface FetchRowsResult {
  rows: any[];
  total: number;
  filtered_by_automation?: { id: number; title: string; only_open?: boolean } | null;
}

async function json(r: Response): Promise<any> {
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

/** GET /api/domain/{slug} → { rows, total }. Same params as the grid. */
export async function fetchRows(slug: string, query: EntityQuery = {}): Promise<FetchRowsResult> {
  const p = new URLSearchParams();
  p.set('page', String(query.page ?? 1));
  // The domain endpoint caps page_size at 200 (Query(le=200)); anything larger
  // is rejected with HTTP 422 BEFORE the handler runs. Clamp here so no surface
  // can trip it regardless of the value it passes.
  p.set('page_size', String(Math.min(Math.max(1, query.pageSize ?? 200), 200)));
  if (query.sort) p.set('sort', query.sort);
  if (query.direction) p.set('direction', query.direction);
  if (query.q) p.set('q', query.q);
  if (query.created_by_automation) p.set('created_by_automation', query.created_by_automation);
  if (query.only_open) p.set('only_open', '1');
  for (const [k, v] of Object.entries(query.filters ?? {})) if (v) p.set(k, v);
  const data = await json(await apiFetch(`/api/domain/${slug}?${p.toString()}`));
  return {
    rows: data.rows ?? [],
    total: data.total ?? 0,
    filtered_by_automation: data.filtered_by_automation ?? null,
  };
}

/** GET one row. */
export async function fetchRow(slug: string, pk: any): Promise<any> {
  return json(await apiFetch(`/api/domain/${slug}/${encodeURIComponent(String(pk))}`));
}

/** PATCH a lifecycle transition (status + optional comment). */
export async function transition(slug: string, pk: any, status: string, comment?: string): Promise<any> {
  return json(await apiFetch(`/api/domain/${slug}/${encodeURIComponent(String(pk))}`, {
    method: 'PATCH',
    body: JSON.stringify({ status, comment }),
  }));
}

/** POST a bulk lifecycle transition over a filter. */
export async function bulkTransition(slug: string, where: Record<string, any>, set: Record<string, any>): Promise<any> {
  return json(await apiFetch(`/api/domain/${slug}/bulk`, {
    method: 'POST',
    body: JSON.stringify({ where, set }),
  }));
}

/** Apply ONE lifecycle transition to MANY rows (bulk status change). Body shape
 * matches the backend `POST /{slug}/bulk` ({ids, status}). Returns
 * {updated, skipped, ...}. */
export async function bulkStatus(slug: string, ids: (string | number)[], status: string): Promise<any> {
  return json(await apiFetch(`/api/domain/${slug}/bulk`, {
    method: 'POST',
    body: JSON.stringify({ ids: ids.map(String), status }),
  }));
}

export async function fetchComments(slug: string, pk: any): Promise<any[]> {
  const d = await json(await apiFetch(`/api/domain/${slug}/${encodeURIComponent(String(pk))}/comments`));
  return d.items ?? d.comments ?? [];
}

export async function addComment(slug: string, pk: any, body: string): Promise<any> {
  return json(await apiFetch(`/api/domain/${slug}/${encodeURIComponent(String(pk))}/comments`, {
    method: 'POST',
    body: JSON.stringify({ body }),
  }));
}

/**
 * v7.30.0 archetype business action — the ONE endpoint behind every surface's
 * primary verbs (outbox `release`, inbox `escalate`, advisory
 * `accept`/`promote`, ledger `promote`). Backed by
 * `POST /api/domain/{slug}/{pk}/action` with `{ action, ...payload }`.
 */
export async function entityAction(slug: string, pk: any, action: string, payload: Record<string, any> = {}): Promise<any> {
  return json(await apiFetch(`/api/domain/${slug}/${encodeURIComponent(String(pk))}/action`, {
    method: 'POST',
    body: JSON.stringify({ action, ...payload }),
  }));
}

/** Delete one or many LOCAL entity rows. `POST /api/domain/{slug}/delete` with
 * `{ids}`. Communications only delete while `draft` (backend enforces). Returns
 * `{deleted, skipped}`. */
export async function deleteRows(slug: string, ids: (string | number)[]): Promise<{ deleted: number; skipped: number }> {
  return json(await apiFetch(`/api/domain/${slug}/delete`, {
    method: 'POST',
    body: JSON.stringify({ ids }),
  }));
}
