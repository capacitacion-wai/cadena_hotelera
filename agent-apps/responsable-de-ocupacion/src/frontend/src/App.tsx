/**
 * Canonical App.tsx for every wai-agent-app. COPIED VERBATIM by
 * `wai-agent-app-builder` into `src/frontend/src/App.tsx`.
 *
 * Routing and navigation are 100% manifest-driven:
 *   - `<Layout>` renders the sidebar and reads `domain_entities` from
 *     `useManifest()` to materialise the "Dominio" group.
 *   - The domain pages (`<DomainList>` / `<DomainDetail>`) read the
 *     `:slug` URL param and again look it up in the manifest.
 *
 * The four canonical nav groups are: overview · domain · agent · audit.
 * Their order MUST not change. The verifier (verify_generated_app.sh)
 * blocks modifications to this file.
 */

import { lazy, Suspense } from 'react';
import type { ComponentType } from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
import { Box, CircularProgress } from '@mui/material';
import { AuthProvider, useAuth } from './auth/AuthContext';
import { ProtectedRoute } from './auth/ProtectedRoute';
import LoginPage from './auth/LoginPage';
import Layout from './components/Layout';
import ErrorBoundary from './components/ErrorBoundary';
import LoadingRegion from './components/LoadingRegion';

/**
 * G-5 (v7.2): stale-chunk recovery for lazy-loaded routes.
 *
 * Vite emits hashed bundle filenames (`Operations-abc123.js`). When
 * we deploy a new build, the new ``index.html`` references new
 * hashes and the old chunks vanish from `/assets`. A browser tab
 * that loaded the previous ``index.html`` *before* the deploy
 * still references the old hash; navigating to a not-yet-loaded
 * route then throws ``Failed to fetch dynamically imported module``.
 *
 * We can't *prevent* it (the browser already has a stale module
 * map in memory) but we can recover gracefully: detect the error
 * once, force a hard refresh of ``index.html`` (with a
 * cache-buster) so the SPA boots against the new asset graph, and
 * use ``sessionStorage`` to guarantee at most one reload per tab
 * — if the import *still* fails after a fresh boot the error
 * boundary surfaces a normal error to the operator instead of
 * looping forever.
 */
const RELOAD_FLAG = 'wai-agent.chunkReloadAttempted';

function isChunkLoadError(err: unknown): boolean {
  if (!err) return false;
  const msg = err instanceof Error ? err.message : String(err);
  return (
    /Failed to fetch dynamically imported module/i.test(msg)
    || /Importing a module script failed/i.test(msg)
    || /error loading dynamically imported module/i.test(msg)
    || /ChunkLoadError/i.test(msg)
  );
}

function lazyWithReload<T extends ComponentType<any>>(
  loader: () => Promise<{ default: T }>,
) {
  return lazy(async () => {
    try {
      const mod = await loader();
      // Successful load — clear the reload flag so future failures
      // (e.g. after another deploy) can trigger a fresh recovery.
      try { window.sessionStorage.removeItem(RELOAD_FLAG); } catch { /* ignore */ }
      return mod;
    } catch (err) {
      if (isChunkLoadError(err)) {
        const already = (() => {
          try { return window.sessionStorage.getItem(RELOAD_FLAG); } catch { return null; }
        })();
        if (!already) {
          try { window.sessionStorage.setItem(RELOAD_FLAG, String(Date.now())); } catch { /* ignore */ }
          // Cache-bust ``index.html`` so the browser fetches fresh
          // markup with the new asset hashes.
          const url = new URL(window.location.href);
          url.searchParams.set('_v', Date.now().toString(36));
          window.location.replace(url.toString());
          // Hold the suspense fallback forever — the page is about
          // to reload anyway.
          return new Promise<never>(() => { /* never resolves */ }) as any;
        }
      }
      throw err;
    }
  });
}

// G-4 (P3): lazy-load every page so the login → dashboard journey
// downloads only the chunks it actually uses. Each page becomes its
// own webpack chunk (~10–60 KB) instead of a single 600 KB bundle.
const Welcome        = lazyWithReload(() => import('./pages/Welcome'));
const Dashboard      = lazyWithReload(() => import('./pages/Dashboard'));
const Manual         = lazyWithReload(() => import('./pages/Manual'));
const DomainList     = lazyWithReload(() => import('./pages/DomainList'));
const DomainDetail   = lazyWithReload(() => import('./pages/DomainDetail'));
// v7.30.0 — /domain/:slug resolves a business SURFACE (archetype) per entity;
// DomainSurface falls back to DomainList for the `register` archetype.
const DomainSurface  = lazyWithReload(() => import('./surfaces/DomainSurface'));
const Agent          = lazyWithReload(() => import('./pages/Agent'));
const Operations     = lazyWithReload(() => import('./pages/Operations'));
const Capabilities   = lazyWithReload(() => import('./pages/Capabilities'));
const Colleagues     = lazyWithReload(() => import('./pages/Colleagues'));
const Identity       = lazyWithReload(() => import('./pages/Identity'));
const Users          = lazyWithReload(() => import('./pages/Users'));
const Events         = lazyWithReload(() => import('./pages/Events'));
const Consumo        = lazyWithReload(() => import('./pages/Consumo'));
const EventDetail    = lazyWithReload(() => import('./pages/EventDetail'));
const Runs           = lazyWithReload(() => import('./pages/Runs'));
const RunDetail      = lazyWithReload(() => import('./pages/RunDetail'));
const Channels       = lazyWithReload(() => import('./pages/Channels'));
const ChannelDetail  = lazyWithReload(() => import('./pages/ChannelDetail'));
const Encargos       = lazyWithReload(() => import('./pages/Encargos'));
const EncargoDetail  = lazyWithReload(() => import('./pages/EncargoDetail'));
const CalendarPage   = lazyWithReload(() => import('./pages/Calendar'));
const Approvals      = lazyWithReload(() => import('./pages/Approvals'));
const ApprovalDetail = lazyWithReload(() => import('./pages/ApprovalDetail'));
const AutonomousWork = lazyWithReload(() => import('./pages/AutonomousWork'));
const Activity       = lazyWithReload(() => import('./pages/Activity'));

function PageFallback() {
  return <LoadingRegion />;
}

function AppShell() {
  const { isAuthenticated } = useAuth();
  if (!isAuthenticated) {
    return (
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="*" element={<Navigate to="/login" replace />} />
      </Routes>
    );
  }


  return (
    <Layout>
      <Suspense fallback={<PageFallback />}>
        <Routes>
          <Route path="/login" element={<Navigate to="/" replace />} />

          {/* overview — Welcome ("AI employee" cover letter) is the
              landing page; the Dashboard lives at /dashboard. */}
          <Route path="/"          element={<ProtectedRoute><Welcome /></ProtectedRoute>} />
          <Route path="/dashboard" element={<ProtectedRoute><Dashboard /></ProtectedRoute>} />
          <Route path="/manual"    element={<ProtectedRoute><Manual /></ProtectedRoute>} />

          {/* domain — entities resolved from manifest at runtime */}
          <Route path="/domain/:slug"     element={<ProtectedRoute><DomainSurface /></ProtectedRoute>} />
          <Route path="/domain/:slug/:id" element={<ProtectedRoute><DomainDetail /></ProtectedRoute>} />

          {/* agent */}
          <Route path="/agent"      element={<ProtectedRoute><Agent /></ProtectedRoute>} />
          <Route path="/encargos"   element={<ProtectedRoute><Encargos /></ProtectedRoute>} />
          <Route path="/encargos/:id" element={<ProtectedRoute><EncargoDetail /></ProtectedRoute>} />
          <Route path="/trabajo-autonomo" element={<ProtectedRoute><AutonomousWork /></ProtectedRoute>} />
          <Route path="/calendar"   element={<ProtectedRoute><CalendarPage /></ProtectedRoute>} />
          <Route path="/approvals"  element={<ProtectedRoute><Approvals /></ProtectedRoute>} />
          <Route path="/approvals/:id" element={<ProtectedRoute><ApprovalDetail /></ProtectedRoute>} />
          <Route path="/actividad"  element={<ProtectedRoute><Activity /></ProtectedRoute>} />
          <Route path="/operations" element={<ProtectedRoute><Operations /></ProtectedRoute>} />
          <Route path="/capacidades" element={<ProtectedRoute><Capabilities /></ProtectedRoute>} />
          {/* Colleagues are one more capability: they go alongside the others. */}
          <Route path="/companeros" element={<ProtectedRoute><Colleagues /></ProtectedRoute>} />
          <Route path="/identity"   element={<ProtectedRoute><Identity /></ProtectedRoute>} />
          <Route path="/users"      element={<ProtectedRoute><Users /></ProtectedRoute>} />
          <Route path="/channels"          element={<ProtectedRoute><Channels /></ProtectedRoute>} />
          <Route path="/channels/:id"      element={<ProtectedRoute><ChannelDetail /></ProtectedRoute>} />

          {/* audit */}
          <Route path="/runs"        element={<ProtectedRoute><Runs /></ProtectedRoute>} />
          <Route path="/runs/:id"    element={<ProtectedRoute><RunDetail /></ProtectedRoute>} />
          <Route path="/events"      element={<ProtectedRoute><Events /></ProtectedRoute>} />
          <Route path="/consumo"     element={<ProtectedRoute><Consumo /></ProtectedRoute>} />
          <Route path="/events/:id"  element={<ProtectedRoute><EventDetail /></ProtectedRoute>} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Suspense>
    </Layout>
  );
}

export default function App() {
  return (
    <ErrorBoundary>
      <AuthProvider>
        <AppShell />
      </AuthProvider>
    </ErrorBoundary>
  );
}
