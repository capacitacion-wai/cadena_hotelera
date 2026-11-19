/**
 * Fetches the agent-app manifest exposed by the FastAPI backend at
 * `/api/agent/manifest`. Cached in module scope for the lifetime of
 * the SPA. COPIED VERBATIM by `wai-agent-app-builder`.
 */
import { useEffect, useState } from 'react'
import { apiFetch } from '../services/apiClient'

/** A piece of persona copy: either a single string or a per-locale
 * dict (e.g. `{ es: '…', en: '…' }`). Resolved to the active language
 * by the SPA. */
export type LocaleText = string | Record<string, string>

/** The "AI employee" persona surfaced by the backend manifest. Every
 * field is optional; the SPA falls back to the functional
 * `display_name` / `tagline` when a persona isn't defined. */
export interface AgentPersona {
  /** Friendly first name shown as the headline (e.g. "Fina"). */
  name?: string
  /** One-line role ("Your stock-control colleague"). */
  role?: LocaleText
  avatar?: {
    /** Mascot image: a path ("/persona-mascot.png"), URL or data URI. */
    image_url?: string
    /** Emoji fallback when no image is provided. */
    emoji?: string
    /** Optional accent colour seed for the monogram fallback. */
    color?: string
  }
  personality?: {
    tone?: LocaleText
    /** Short first-person bio. */
    bio?: LocaleText
    /** A few adjectives ("Meticulosa", "Directa", …). */
    traits?: LocaleText[]
  }
  /** First-person greeting for the empty chat state. */
  greeting?: LocaleText
}

export interface AgentAppManifest {
  app_name?: string
  /** ITS WAI application, if it has one associated (platform V102). The chat uses it to
   *  recognise a link AS ITS OWN —comparing the host— and open it with the session already
   *  resolved. `null` = it has none, or it has one not yet deployed. */
  aplicacion?: { url: string; nombre?: string } | null
  /** The employee's default theme, configurable in Identity (admin). The SPA applies it
   *  at start-up ONLY if the operator has not picked a theme by hand. */
  default_color_mode?: 'system' | 'light' | 'dark'
  agent?: {
    id?: number
    slug?: string
    name?: string
    display_name?: string
    version?: string | number
    description?: string
    tagline?: string | Record<string, string>
    persona?: AgentPersona
    capabilities?: any[]
    example_prompts?: string[]
    skills?: any[]
    mcps?: { name: string; kind: string }[]
    permissions?: { raw?: Record<string, string>; summary?: string }
  }
  presentation?: {
    objective?: string | null
    outputs?: string[]
    triggers?: string[]
    channels?: string[]
    entities?: string[]
  }
  channels_enabled?: boolean
  business_kpis?: any[]
  trend_charts?: any[]
  domain_entities?: any[]
  watchers?: any[]
  actions?: any[]
  stats?: any
  event_types?: string[]
  permissions?: Record<string, string>
  [k: string]: any
}

let _cached: AgentAppManifest | null = null
let _inflight: Promise<AgentAppManifest> | null = null

// Bumped whenever the persona (or any manifest-derived identity) is
// edited so freshly-mounted pages refetch instead of serving the stale
// module-scope cache. ``useManifest`` keys its effect on this counter.
let _cacheVersion = 0
const _listeners = new Set<() => void>()

/** Drop the cached manifest and notify every mounted ``useManifest``
 * consumer so the new identity (e.g. an edited persona) propagates to
 * the welcome hero, chat header and chat reply avatars without a full
 * page reload. */
export function resetManifestCache() {
  _cached = null
  _inflight = null
  _cacheVersion += 1
  _listeners.forEach((fn) => {
    try { fn() } catch { /* ignore */ }
  })
}

async function fetchManifest(): Promise<AgentAppManifest> {
  if (_cached) return _cached
  if (_inflight) return _inflight
  _inflight = (async () => {
    try {
      const r = await apiFetch('/api/agent/manifest')
      if (!r.ok) throw new Error(`manifest ${r.status}`)
      const data = (await r.json()) as AgentAppManifest
      _cached = data
      return data
    } finally {
      _inflight = null
    }
  })()
  return _inflight
}

export function useManifest() {
  const [manifest, setManifest] = useState<AgentAppManifest | null>(_cached)
  const [loading, setLoading] = useState(!_cached)
  const [error, setError] = useState<Error | null>(null)
  // Re-run the fetch effect when the module-scope cache is reset.
  const [version, setVersion] = useState(_cacheVersion)

  useEffect(() => {
    const onReset = () => setVersion(_cacheVersion)
    _listeners.add(onReset)
    return () => { _listeners.delete(onReset) }
  }, [])

  useEffect(() => {
    let cancelled = false
    if (_cached) {
      setManifest(_cached)
      setLoading(false)
      return
    }
    setLoading(true)
    fetchManifest()
      .then((m) => { if (!cancelled) { setManifest(m); setLoading(false) } })
      .catch((e) => { if (!cancelled) { setError(e); setLoading(false) } })
    return () => { cancelled = true }
  }, [version])

  return { manifest, loading, error }
}
