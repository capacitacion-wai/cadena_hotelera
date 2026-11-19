/**
 * Canonical vite.config.ts for every wai-agent-app frontend.
 * COPIED VERBATIM by `wai-agent-app-builder`. The verifier blocks any
 * byte change.
 *
 * Notes on the configuration:
 * - `allowedHosts: 'all'` lets the deployer expose the dev server
 *   under any FQDN derived from `kubernetes.subdomain` in `wai-app.json`.
 *   At runtime the production bundle is served by FastAPI (StaticFiles)
 *   so this only affects the optional `npm run dev` workflow.
 * - The dev proxy targets the in-cluster sister FastAPI on
 *   `http://localhost:8000`; do not change it — `npm run dev`
 *   relies on it when the operator runs the backend locally.
 */
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    host: true,
    allowedHosts: true,
    proxy: {
      '/api': 'http://localhost:8000',
      '/mcp': 'http://localhost:8000',
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
})
