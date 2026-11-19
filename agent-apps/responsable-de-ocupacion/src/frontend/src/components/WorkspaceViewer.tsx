// Reusable workspace file browser. Extracted from Operations.tsx so it
// can be reused inside the agent chat ("Mi workspace" panel) scoped to a
// single user's workspace folder.
//
// Props:
//   initialPath  starting folder (defaults to rootPath or '')
//   rootPath     navigation floor — Home/Up clamp here and the breadcrumb
//                is shown relative to it (defaults to '' = whole workspace)
//   readOnly     hide every write action (upload/mkdir/move/delete/unzip)
//   allowDelete  re-enable ONLY delete under readOnly (chat panel) — the
//                backend purges the matching user_files memory rows too
//   allowMkdir   re-enable ONLY "Nueva carpeta" under readOnly (chat panel):
//                the operator can organise their own scope's folders without
//                gaining upload/move/unzip on the shared admin surface
//   allowChatUpload  re-enable ONLY upload under readOnly (chat panel) and
//                route it through /chat-upload-to: destination confined
//                server-side to the caller's own scope, and each stored file
//                is indexed into user_files memory with its semantic floor
import { useState, type DragEvent as ReactDragEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert, Box, Button, Chip, CircularProgress, Dialog, DialogActions,
  DialogContent, DialogTitle, IconButton, LinearProgress, Link, List, ListItem,
  ListItemText, Paper, Stack, TextField, Tooltip, Typography,
} from '@mui/material'
import {
  Folder as FolderIcon, InsertDriveFileOutlined as FileIcon,
  UploadFile as UploadIcon, Download as DownloadIcon,
  DeleteOutline as DeleteIcon, CreateNewFolder as NewFolderIcon,
  ArrowUpward as UpIcon, Refresh as RefreshIcon,
  Visibility as PreviewIcon, FolderZip as ZipIcon,
  DriveFileMove as MoveIcon, Home as HomeIcon,
  Storage as StorageIcon,
} from '@mui/icons-material'
import { apiFetch } from '../services/apiClient'
import { PRIMARY } from '../theme/theme'

export type WsEntry = {
  name: string
  path: string
  is_dir: boolean
  size: number | null
  modified: string | null
  kind: string
}
type WsList = { available: boolean; path: string; entries: WsEntry[] }
export type WsUsage = {
  available: boolean
  path?: string
  total_bytes?: number
  file_count?: number
  dir_count?: number
  volume_total_bytes?: number | null
  volume_free_bytes?: number | null
  volume_used_bytes?: number | null
}

export function fmtBytes(n: number | null): string {
  if (n == null) return ''
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`
  return `${(n / 1024 / 1024 / 1024).toFixed(1)} GB`
}

// ── HTML preview: render artifacts like the code-agent file browser ──
//
// A workspace HTML file (a report, a self-contained slide deck, …) is
// rendered as a real web page inside a sandboxed iframe instead of being
// shown as raw source. Relative `src`/`href`/`url(...)` references are
// resolved to blob: URLs of the sibling workspace files so images, linked
// CSS and scripts living next to the .html actually load. This mirrors
// OpenCodeFilePreview in the WAI code agent.

function dirnameOf(p: string): string {
  const ix = p.lastIndexOf('/')
  return ix < 0 ? '' : p.slice(0, ix)
}

// ── Drag a workspace entry into the agent chat ─────────────────────────
//
// Same gesture as the code agent's file explorer: dragging a row carries the
// AGENT-side absolute path (`/workspace/<rel>` — the exact convention the
// chat-attachments block already teaches the agent), so the chat input can
// insert it as a reference and the employee knows which element to work
// with. `text/plain` makes the drop also work on plain text targets; the
// custom type lets the chat input tell this apart from OS file drags.
export const WORKSPACE_DRAG_TYPE = 'application/x-wai-workspace-path'

function dragWorkspaceEntry(ev: ReactDragEvent, entry: WsEntry) {
  const agentPath = `/workspace/${entry.path.replace(/^\/+/, '')}`
  ev.dataTransfer.setData('text/plain', agentPath)
  ev.dataTransfer.setData(WORKSPACE_DRAG_TYPE, agentPath)
  ev.dataTransfer.effectAllowed = 'copy'
  const ghost = document.createElement('div')
  ghost.textContent = `@ ${entry.name}`
  Object.assign(ghost.style, {
    position: 'fixed',
    top: '-200px',
    left: '-200px',
    padding: '3px 10px',
    background: PRIMARY,
    color: '#fff',
    fontSize: '12px',
    fontFamily: 'inherit',
    borderRadius: '12px',
    whiteSpace: 'nowrap',
    pointerEvents: 'none',
    boxShadow: '0 2px 6px rgba(0,0,0,0.25)',
  })
  document.body.appendChild(ghost)
  ev.dataTransfer.setDragImage(ghost, ghost.offsetWidth / 2, ghost.offsetHeight / 2)
  requestAnimationFrame(() => document.body.removeChild(ghost))
}

/** Resolve a relative URL against the directory of an HTML file inside the
 *  workspace. Workspace-absolute (`/foo`) paths drop the leading slash.
 *  External / data / blob / anchor / scheme URLs are left untouched. */
function resolveWorkspacePath(baseDir: string, rel: string): string | null {
  if (!rel) return null
  if (/^(?:[a-z][a-z0-9+.-]*:|\/\/|#|data:|blob:)/i.test(rel)) return null
  const stripped = rel.replace(/^\/+/, '')
  if (rel.startsWith('/')) return stripped
  try {
    const u = new URL(stripped, `https://_local_/${baseDir}/`)
    return decodeURIComponent(u.pathname).replace(/^\//, '')
  } catch {
    return null
  }
}

/** Fetch a workspace file as a blob: URL via the download endpoint. */
async function wsLoadBlobUrl(path: string): Promise<string> {
  const r = await apiFetch(`/api/agent/workspace/download?path=${encodeURIComponent(path)}`)
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return URL.createObjectURL(await r.blob())
}

/** Walk the HTML and replace relative URLs with blob: URLs of the matching
 *  workspace files. Linked CSS is inlined as <style> (its url() refs are
 *  rewritten too, since a blob: URL has no path context). Returns the
 *  rewritten markup plus the list of blob URLs to revoke later. */
async function rewriteHtmlForWorkspace(
  rawHtml: string,
  htmlPath: string,
): Promise<{ html: string; blobs: string[] }> {
  const baseDir = dirnameOf(htmlPath)
  const blobs: string[] = []
  const cache = new Map<string, Promise<string | null>>()

  const fetchBlob = (workspacePath: string): Promise<string | null> => {
    let p = cache.get(workspacePath)
    if (!p) {
      p = wsLoadBlobUrl(workspacePath)
        .then((url) => { blobs.push(url); return url })
        .catch(() => null)
      cache.set(workspacePath, p)
    }
    return p
  }

  const fetchText = async (workspacePath: string): Promise<string | null> => {
    const url = await fetchBlob(workspacePath)
    if (!url) return null
    try {
      const r = await fetch(url)
      if (!r.ok) return null
      return await r.text()
    } catch {
      return null
    }
  }

  const doc = new DOMParser().parseFromString(rawHtml, 'text/html')

  const rewriteAttr = async (el: Element, attr: string) => {
    const raw = el.getAttribute(attr)
    if (!raw) return
    const wsPath = resolveWorkspacePath(baseDir, raw)
    if (!wsPath) return
    const blobUrl = await fetchBlob(wsPath)
    if (blobUrl) el.setAttribute(attr, blobUrl)
  }

  const rewriteCssText = async (css: string, cssDir: string): Promise<string> => {
    const matches = Array.from(css.matchAll(/url\(\s*(['"]?)([^)'"]+)\1\s*\)/g))
    if (matches.length === 0) return css
    const replacements = await Promise.all(matches.map(async (m) => {
      const raw = m[2].trim()
      const wsPath = resolveWorkspacePath(cssDir, raw)
      if (!wsPath) return { match: m[0], replacement: m[0] }
      const blobUrl = await fetchBlob(wsPath)
      return { match: m[0], replacement: blobUrl ? `url("${blobUrl}")` : m[0] }
    }))
    let out = css
    for (const { match, replacement } of replacements) out = out.replace(match, replacement)
    return out
  }

  const linkPromises: Promise<void>[] = []
  doc.querySelectorAll<HTMLLinkElement>('link[rel~="stylesheet"][href]').forEach((link) => {
    const href = link.getAttribute('href') || ''
    const wsPath = resolveWorkspacePath(baseDir, href)
    if (!wsPath) return
    linkPromises.push((async () => {
      const css = await fetchText(wsPath)
      if (css == null) return
      const rewritten = await rewriteCssText(css, dirnameOf(wsPath))
      const style = doc.createElement('style')
      style.textContent = rewritten
      link.replaceWith(style)
    })())
  })

  const targets: Array<[string, string]> = [
    ['img[src]', 'src'], ['source[src]', 'src'], ['video[src]', 'src'],
    ['audio[src]', 'src'], ['iframe[src]', 'src'], ['embed[src]', 'src'],
    ['object[data]', 'data'], ['script[src]', 'src'], ['a[href]', 'href'],
    ['link[rel="icon"]', 'href'], ['link[rel="apple-touch-icon"]', 'href'],
  ]
  const attrPromises: Promise<void>[] = []
  for (const [sel, attr] of targets) {
    doc.querySelectorAll(sel).forEach((el) => { attrPromises.push(rewriteAttr(el, attr)) })
  }

  doc.querySelectorAll<HTMLImageElement>('img[srcset]').forEach((el) => {
    const raw = el.getAttribute('srcset') || ''
    attrPromises.push((async () => {
      const parts = raw.split(',').map((p) => p.trim()).filter(Boolean)
      const newParts = await Promise.all(parts.map(async (part) => {
        const [url, ...rest] = part.split(/\s+/)
        const wsPath = resolveWorkspacePath(baseDir, url)
        if (!wsPath) return part
        const blobUrl = await fetchBlob(wsPath)
        return blobUrl ? [blobUrl, ...rest].join(' ') : part
      }))
      el.setAttribute('srcset', newParts.join(', '))
    })())
  })

  const stylePromises: Promise<void>[] = []
  doc.querySelectorAll('style').forEach((style) => {
    const css = style.textContent || ''
    if (!/url\(/i.test(css)) return
    stylePromises.push((async () => { style.textContent = await rewriteCssText(css, baseDir) })())
  })

  const inlineStylePromises: Promise<void>[] = []
  doc.querySelectorAll<HTMLElement>('[style]').forEach((el) => {
    const css = el.getAttribute('style') || ''
    if (!/url\(/i.test(css)) return
    inlineStylePromises.push((async () => { el.setAttribute('style', await rewriteCssText(css, baseDir)) })())
  })

  await Promise.all([...linkPromises, ...attrPromises, ...stylePromises, ...inlineStylePromises])

  // Fragment-click handler: Chrome blocks navigation to `blob:...#anchor`
  // from a sandboxed iframe without allow-same-origin, so intercept clicks
  // on in-page `#anchor` links and scrollIntoView directly.
  const handler = doc.createElement('script')
  handler.textContent = `
(function(){
  document.addEventListener('click', function(ev) {
    var a = ev.target && ev.target.closest && ev.target.closest('a[href]');
    if (!a) return;
    var href = a.getAttribute('href') || '';
    if (!href || href.charAt(0) !== '#' || href.length < 2) return;
    var id = decodeURIComponent(href.slice(1));
    var target = document.getElementById(id) ||
                 document.querySelector('a[name="' + CSS.escape(id) + '"]');
    if (!target) return;
    ev.preventDefault();
    target.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }, true);
})();
`
  ;(doc.body || doc.documentElement).appendChild(handler)

  return { html: '<!doctype html>\n' + doc.documentElement.outerHTML, blobs }
}

// Reusable storage-footprint card: a headline "occupied" size, optional
// detail chips (file count, breakdown…) and a volume-usage bar when the
// backend reports the backing PVC's free/total. Shared by the Workspace
// tab and the Database monitoring tab so both read identically.
export function StorageUsageCard({
  occupiedLabel,
  occupiedBytes,
  volumeTotalBytes,
  volumeFreeBytes,
  chips,
  sx,
}: {
  occupiedLabel: string
  occupiedBytes: number
  volumeTotalBytes?: number | null
  volumeFreeBytes?: number | null
  chips?: string[]
  sx?: object
}) {
  const { t } = useTranslation()
  const hasVol = typeof volumeTotalBytes === 'number' && volumeTotalBytes > 0
  const used = hasVol && typeof volumeFreeBytes === 'number'
    ? Math.max(0, volumeTotalBytes - volumeFreeBytes)
    : null
  const pct = hasVol && used != null ? Math.min(100, (used / volumeTotalBytes) * 100) : null
  const barColor = pct == null ? 'primary' : pct >= 90 ? 'error' : pct >= 75 ? 'warning' : 'primary'
  return (
    <Paper variant="outlined" sx={{ p: 2, borderRadius: 2, ...sx }}>
      <Stack direction="row" spacing={1.5} alignItems="center" flexWrap="wrap" useFlexGap sx={{ mb: hasVol ? 1.5 : 0 }}>
        <Box
          sx={{
            width: 40, height: 40, borderRadius: 2, flexShrink: 0,
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            bgcolor: 'primary.50', color: 'primary.main',
          }}
        >
          <StorageIcon fontSize="small" />
        </Box>
        <Box sx={{ flex: 1, minWidth: 140 }}>
          <Typography variant="caption" color="text.secondary" sx={{ display: 'block' }}>
            {occupiedLabel}
          </Typography>
          <Typography variant="h6" sx={{ fontWeight: 700, lineHeight: 1.2 }}>
            {fmtBytes(occupiedBytes) || '0 B'}
          </Typography>
        </Box>
        {(chips ?? []).map((c, i) => (
          <Chip key={i} size="small" variant="outlined" label={c} />
        ))}
      </Stack>
      {hasVol && (
        <Box>
          <LinearProgress
            variant="determinate"
            value={pct ?? 0}
            color={barColor}
            sx={{ height: 8, borderRadius: 1 }}
          />
          <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5 }}>
            {t('operations.storage.volumeUsage', {
              used: fmtBytes(used),
              total: fmtBytes(volumeTotalBytes),
              free: fmtBytes(volumeFreeBytes ?? 0),
            })}
          </Typography>
        </Box>
      )}
    </Paper>
  )
}

export default function WorkspaceViewer({
  initialPath,
  rootPath = '',
  readOnly = false,
  allowDelete = false,
  allowMkdir = false,
  allowChatUpload = false,
}: {
  initialPath?: string
  rootPath?: string
  readOnly?: boolean
  /** Show the delete action even in readOnly mode (the chat's "Mi
   *  workspace" panel: browse-only EXCEPT deleting your own files, which
   *  also purges their user_files memory rows server-side). */
  allowDelete?: boolean
  /** Show "Nueva carpeta" even in readOnly mode (chat panel): organising
   *  your own scope without the rest of the write surface. */
  allowMkdir?: boolean
  /** Show upload even in readOnly mode (chat panel) AND route it through
   *  the self-scoped /chat-upload-to endpoint, which confines the target
   *  to the caller's own folder and indexes each file into memory. */
  allowChatUpload?: boolean
}) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const root = rootPath || ''
  const [path, setPath] = useState(initialPath ?? root)
  const [busy, setBusy] = useState(false)
  const [feedback, setFeedback] = useState<{ kind: 'success' | 'error'; msg: string } | null>(null)
  const [mkdirOpen, setMkdirOpen] = useState(false)
  const [mkdirName, setMkdirName] = useState('')
  const [moveItem, setMoveItem] = useState<WsEntry | null>(null)
  const [moveDest, setMoveDest] = useState('')
  const [deleteItem, setDeleteItem] = useState<WsEntry | null>(null)
  const [preview, setPreview] = useState<{
    entry: WsEntry
    content: string | null
    blobUrl: string | null
    revokeUrls: string[]
    tooLarge: boolean
    kind: string
  } | null>(null)

  const query = useQuery({
    queryKey: ['ws-list', path],
    queryFn: () => apiFetch(`/api/agent/workspace/list?path=${encodeURIComponent(path)}`).then(async (r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      return r.json() as Promise<WsList>
    }),
    retry: false,
  })

  // Footprint of the WHOLE workspace (size + free space). Only queried
  // for the full Operations view — the scoped chat panel (rootPath set)
  // would otherwise report the global total under a single user's
  // folder, which is misleading.
  const usageQuery = useQuery({
    queryKey: ['ws-usage'],
    enabled: !root,
    queryFn: () => apiFetch('/api/agent/workspace/usage').then(async (r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      return r.json() as Promise<WsUsage>
    }),
    retry: false,
  })
  const usage = usageQuery.data

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['ws-list'] })
    qc.invalidateQueries({ queryKey: ['ws-usage'] })
  }

  // Navigation is relative to ``root`` so a scoped viewer can never climb
  // above the user's own folder.
  const rel = root && path.startsWith(root) ? path.slice(root.length).replace(/^\//, '') : path
  const segments = rel ? rel.split('/') : []
  const atRoot = segments.length === 0
  const toAbsolute = (relTarget: string) =>
    root ? (relTarget ? `${root}/${relTarget}` : root) : relTarget
  const goUp = () => setPath(toAbsolute(segments.slice(0, -1).join('/')))

  async function saveBlob(url: string, filename: string) {
    setBusy(true)
    try {
      const res = await apiFetch(url)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const blob = await res.blob()
      const obj = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = obj
      a.download = filename
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(obj)
    } catch (e: any) {
      setFeedback({ kind: 'error', msg: e?.message || t('operations.workspace.error') })
    } finally {
      setBusy(false)
    }
  }

  async function onUpload(files: FileList | null) {
    if (!files || files.length === 0) return
    setBusy(true)
    try {
      const fd = new FormData()
      fd.append('path', path)
      Array.from(files).forEach((f) => fd.append('files', f))
      // Chat panel → self-scoped endpoint (server confines the destination to
      // the caller's own folder and indexes each file into memory).
      const endpoint = allowChatUpload
        ? '/api/agent/workspace/chat-upload-to'
        : '/api/agent/workspace/upload'
      const res = await apiFetch(endpoint, { method: 'POST', body: fd })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const out = await res.json()
      const okN = (out.uploaded || []).length
      const koN = (out.rejected || []).length
      setFeedback({
        kind: koN ? 'error' : 'success',
        msg: t('operations.workspace.uploaded', { count: okN })
          + (koN ? ` · ${t('operations.workspace.rejected', { count: koN })}` : ''),
      })
      refresh()
      if (allowChatUpload) qc.invalidateQueries({ queryKey: ['mem-files'] })
    } catch (e: any) {
      setFeedback({ kind: 'error', msg: e?.message || t('operations.workspace.error') })
    } finally {
      setBusy(false)
    }
  }

  async function doMkdir() {
    const name = mkdirName.trim()
    if (!name) return
    setBusy(true)
    try {
      const full = path ? `${path}/${name}` : name
      const res = await apiFetch('/api/agent/workspace/mkdir', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: full }),
      })
      if (!res.ok) throw new Error((await res.text()) || `HTTP ${res.status}`)
      setFeedback({ kind: 'success', msg: t('operations.workspace.created') })
      setMkdirOpen(false)
      setMkdirName('')
      refresh()
    } catch (e: any) {
      setFeedback({ kind: 'error', msg: e?.message || t('operations.workspace.error') })
    } finally {
      setBusy(false)
    }
  }

  async function doMove() {
    if (!moveItem) return
    setBusy(true)
    try {
      const res = await apiFetch('/api/agent/workspace/move', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ src: moveItem.path, dest: moveDest.trim() }),
      })
      if (!res.ok) throw new Error((await res.text()) || `HTTP ${res.status}`)
      setFeedback({ kind: 'success', msg: t('operations.workspace.moved') })
      setMoveItem(null)
      setMoveDest('')
      refresh()
    } catch (e: any) {
      setFeedback({ kind: 'error', msg: e?.message || t('operations.workspace.error') })
    } finally {
      setBusy(false)
    }
  }

  async function doDelete(entry: WsEntry) {
    setDeleteItem(null)
    setBusy(true)
    try {
      const res = await apiFetch(`/api/agent/workspace/file?path=${encodeURIComponent(entry.path)}`, { method: 'DELETE' })
      if (!res.ok) throw new Error((await res.text()) || `HTTP ${res.status}`)
      setFeedback({ kind: 'success', msg: t('operations.workspace.deleted') })
      refresh()
      // The backend also purged the file's user_files MEMORY rows — refresh
      // the memory panel (chat "Mi memoria" tab / MemoryViewer) so the ghost
      // entry disappears immediately.
      qc.invalidateQueries({ queryKey: ['mem-files'] })
    } catch (e: any) {
      setFeedback({ kind: 'error', msg: e?.message || t('operations.workspace.error') })
    } finally {
      setBusy(false)
    }
  }

  async function doUnzip(entry: WsEntry) {
    setBusy(true)
    try {
      const res = await apiFetch('/api/agent/workspace/unzip', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: entry.path, keep: true }),
      })
      if (!res.ok) throw new Error((await res.text()) || `HTTP ${res.status}`)
      setFeedback({ kind: 'success', msg: t('operations.workspace.unzipped') })
      refresh()
    } catch (e: any) {
      setFeedback({ kind: 'error', msg: e?.message || t('operations.workspace.error') })
    } finally {
      setBusy(false)
    }
  }

  async function openPreview(entry: WsEntry) {
    setBusy(true)
    try {
      const res = await apiFetch(`/api/agent/workspace/preview?path=${encodeURIComponent(entry.path)}`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const meta = await res.json()
      let blobUrl: string | null = null
      const revokeUrls: string[] = []
      if (meta.kind === 'image' || meta.kind === 'pdf') {
        const dl = await apiFetch(`/api/agent/workspace/download?path=${encodeURIComponent(entry.path)}`)
        if (dl.ok) {
          blobUrl = URL.createObjectURL(await dl.blob())
          revokeUrls.push(blobUrl)
        }
      } else if (meta.kind === 'html' && meta.content != null && !meta.too_large) {
        // Render the artifact as a web page: rewrite workspace-relative
        // assets to blob URLs, then serve the document itself as a blob so
        // in-page `#anchor` navigation works inside the sandboxed iframe.
        try {
          const { html, blobs } = await rewriteHtmlForWorkspace(meta.content, entry.path)
          revokeUrls.push(...blobs)
          blobUrl = URL.createObjectURL(new Blob([html], { type: 'text/html;charset=utf-8' }))
        } catch {
          blobUrl = URL.createObjectURL(new Blob([meta.content], { type: 'text/html;charset=utf-8' }))
        }
        revokeUrls.push(blobUrl)
      }
      setPreview({
        entry,
        content: meta.content ?? null,
        blobUrl,
        revokeUrls,
        tooLarge: !!meta.too_large,
        kind: meta.kind || 'binary',
      })
    } catch (e: any) {
      setFeedback({ kind: 'error', msg: e?.message || t('operations.workspace.error') })
    } finally {
      setBusy(false)
    }
  }

  function closePreview() {
    preview?.revokeUrls.forEach((u) => URL.revokeObjectURL(u))
    setPreview(null)
  }

  const list = query.data
  const entries = list?.entries ?? []

  if (query.isLoading) {
    return <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}><CircularProgress /></Box>
  }
  if (list && list.available === false) {
    return <Alert severity="info">{t('operations.workspace.unavailable')}</Alert>
  }

  return (
    <Box>
      {!root && usage?.available && (
        <StorageUsageCard
          occupiedLabel={t('operations.workspace.usageLabel')}
          occupiedBytes={usage.total_bytes ?? 0}
          volumeTotalBytes={usage.volume_total_bytes}
          volumeFreeBytes={usage.volume_free_bytes}
          chips={[t('operations.workspace.usageFiles', { count: usage.file_count ?? 0 })]}
          sx={{ mb: 2 }}
        />
      )}
      {!readOnly && (
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          {t('operations.workspace.intro')}
        </Typography>
      )}

      {/* Toolbar: breadcrumb + actions */}
      <Stack
        direction="row"
        spacing={1}
        alignItems="center"
        flexWrap="wrap"
        sx={{ mb: 2, gap: 1 }}
      >
        <Tooltip title={t('operations.workspace.home')}>
          <span>
            <IconButton size="small" onClick={() => setPath(root)} disabled={atRoot}>
              <HomeIcon fontSize="small" />
            </IconButton>
          </span>
        </Tooltip>
        <Tooltip title={t('operations.workspace.up')}>
          <span>
            <IconButton size="small" onClick={goUp} disabled={atRoot}>
              <UpIcon fontSize="small" />
            </IconButton>
          </span>
        </Tooltip>
        <Breadcrumb
          segments={segments}
          onNavigate={(p) => setPath(toAbsolute(p))}
          rootLabel={t('operations.workspace.root')}
        />
        <Box sx={{ flexGrow: 1 }} />
        {busy && <CircularProgress size={18} />}
        <Tooltip title={t('operations.workspace.refresh')}>
          <IconButton size="small" onClick={refresh}><RefreshIcon fontSize="small" /></IconButton>
        </Tooltip>
        {(!readOnly || allowMkdir) && (
          <Button
            size="small"
            startIcon={<NewFolderIcon />}
            onClick={() => { setMkdirName(''); setMkdirOpen(true) }}
          >
            {t('operations.workspace.newFolder')}
          </Button>
        )}
        {(!readOnly || allowChatUpload) && (
          <Button size="small" component="label" startIcon={<UploadIcon />} disabled={busy}>
            {t('operations.workspace.upload')}
            <input
              type="file"
              hidden
              multiple
              onChange={(e) => { onUpload(e.target.files); e.currentTarget.value = '' }}
            />
          </Button>
        )}
        <Button
          size="small"
          startIcon={<ZipIcon />}
          onClick={() => saveBlob(
            `/api/agent/workspace/download-all?path=${encodeURIComponent(path)}`,
            `${segments[segments.length - 1] || 'workspace'}.zip`,
          )}
          disabled={busy || entries.length === 0}
        >
          {t('operations.workspace.downloadZip')}
        </Button>
      </Stack>

      {feedback && (
        <Alert severity={feedback.kind} sx={{ mb: 2 }} onClose={() => setFeedback(null)}>
          {feedback.msg}
        </Alert>
      )}

      {entries.length === 0 ? (
        <Alert severity="info">{t('operations.workspace.emptyFolder')}</Alert>
      ) : (
        <Paper variant="outlined" sx={{ borderRadius: 2 }}>
          <List dense disablePadding>
            {entries.map((e, idx) => (
              <ListItem
                key={e.path}
                divider={idx < entries.length - 1}
                secondaryAction={
                  <Stack direction="row" spacing={0.5}>
                    {!e.is_dir && (
                      <Tooltip title={t('operations.workspace.preview')}>
                        <IconButton size="small" edge={false} onClick={() => openPreview(e)}>
                          <PreviewIcon fontSize="small" />
                        </IconButton>
                      </Tooltip>
                    )}
                    {!e.is_dir && (
                      <Tooltip title={t('operations.workspace.download')}>
                        <IconButton
                          size="small"
                          onClick={() => saveBlob(`/api/agent/workspace/download?path=${encodeURIComponent(e.path)}`, e.name)}
                        >
                          <DownloadIcon fontSize="small" />
                        </IconButton>
                      </Tooltip>
                    )}
                    {e.is_dir && (
                      <Tooltip title={t('operations.workspace.downloadZip')}>
                        <IconButton
                          size="small"
                          onClick={() => saveBlob(`/api/agent/workspace/download-all?path=${encodeURIComponent(e.path)}`, `${e.name}.zip`)}
                        >
                          <ZipIcon fontSize="small" />
                        </IconButton>
                      </Tooltip>
                    )}
                    {!readOnly && !e.is_dir && e.name.toLowerCase().endsWith('.zip') && (
                      <Tooltip title={t('operations.workspace.unzip')}>
                        <IconButton size="small" onClick={() => doUnzip(e)}>
                          <ZipIcon fontSize="small" color="action" />
                        </IconButton>
                      </Tooltip>
                    )}
                    {!readOnly && (
                      <Tooltip title={t('operations.workspace.move')}>
                        <IconButton size="small" onClick={() => { setMoveItem(e); setMoveDest(path) }}>
                          <MoveIcon fontSize="small" />
                        </IconButton>
                      </Tooltip>
                    )}
                    {(!readOnly || allowDelete) && (
                      <Tooltip title={t('operations.workspace.delete')}>
                        <IconButton size="small" color="error" onClick={() => setDeleteItem(e)}>
                          <DeleteIcon fontSize="small" />
                        </IconButton>
                      </Tooltip>
                    )}
                  </Stack>
                }
              >
                <Box
                  draggable
                  onDragStart={(ev) => dragWorkspaceEntry(ev, e)}
                  sx={{ display: 'flex', alignItems: 'center', gap: 1, cursor: e.is_dir ? 'pointer' : 'grab', minWidth: 0, pr: 2, '&:active': { cursor: 'grabbing' } }}
                  onClick={() => { if (e.is_dir) setPath(e.path) }}
                >
                  {e.is_dir ? <FolderIcon fontSize="small" color="primary" /> : <FileIcon fontSize="small" color="action" />}
                  <ListItemText
                    primary={e.name}
                    secondary={[
                      e.is_dir ? t('operations.workspace.folder') : fmtBytes(e.size),
                      e.modified ? new Date(e.modified).toLocaleString() : null,
                    ].filter(Boolean).join(' · ')}
                    primaryTypographyProps={{ noWrap: true, fontWeight: e.is_dir ? 600 : 400 }}
                    secondaryTypographyProps={{ noWrap: true, variant: 'caption' }}
                  />
                </Box>
              </ListItem>
            ))}
          </List>
        </Paper>
      )}

      {/* New folder dialog */}
      <Dialog open={mkdirOpen} onClose={() => setMkdirOpen(false)} maxWidth="xs" fullWidth>
        <DialogTitle>{t('operations.workspace.newFolder')}</DialogTitle>
        <DialogContent>
          <TextField
            autoFocus
            fullWidth
            margin="dense"
            label={t('operations.workspace.folderName')}
            value={mkdirName}
            onChange={(e) => setMkdirName(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') doMkdir() }}
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setMkdirOpen(false)}>{t('operations.workspace.cancel')}</Button>
          <Button variant="contained" onClick={doMkdir} disabled={!mkdirName.trim() || busy}>
            {t('operations.workspace.create')}
          </Button>
        </DialogActions>
      </Dialog>

      {/* Move dialog */}
      <Dialog open={!!moveItem} onClose={() => setMoveItem(null)} maxWidth="xs" fullWidth>
        <DialogTitle>{t('operations.workspace.move')}</DialogTitle>
        <DialogContent>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
            {moveItem?.name}
          </Typography>
          <TextField
            fullWidth
            margin="dense"
            label={t('operations.workspace.destFolder')}
            placeholder={t('operations.workspace.root')}
            value={moveDest}
            onChange={(e) => setMoveDest(e.target.value)}
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setMoveItem(null)}>{t('operations.workspace.cancel')}</Button>
          <Button variant="contained" onClick={doMove} disabled={busy}>
            {t('operations.workspace.move')}
          </Button>
        </DialogActions>
      </Dialog>

      {/* Delete confirmation dialog */}
      <Dialog open={!!deleteItem} onClose={() => setDeleteItem(null)} maxWidth="xs" fullWidth>
        <DialogTitle>{t('operations.workspace.delete')}</DialogTitle>
        <DialogContent>
          <Typography variant="body2">
            {t('operations.workspace.confirmDelete', { name: deleteItem?.name ?? '' })}
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDeleteItem(null)}>{t('operations.workspace.cancel')}</Button>
          <Button
            variant="contained"
            color="error"
            onClick={() => { if (deleteItem) doDelete(deleteItem) }}
            disabled={busy}
          >
            {t('operations.workspace.delete')}
          </Button>
        </DialogActions>
      </Dialog>

      {/* Preview dialog */}
      <Dialog open={!!preview} onClose={closePreview} maxWidth="md" fullWidth>
        <DialogTitle sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
          {preview?.entry.name}
          <Box sx={{ flexGrow: 1 }} />
          {preview && (
            <IconButton
              size="small"
              onClick={() => saveBlob(`/api/agent/workspace/download?path=${encodeURIComponent(preview.entry.path)}`, preview.entry.name)}
            >
              <DownloadIcon fontSize="small" />
            </IconButton>
          )}
        </DialogTitle>
        <DialogContent dividers>
          {preview?.tooLarge && <Alert severity="info">{t('operations.workspace.tooLarge')}</Alert>}
          {!preview?.tooLarge && preview?.kind === 'text' && (
            <Box
              component="pre"
              sx={{
                m: 0, p: 1.5, fontSize: 13, fontFamily: 'monospace',
                whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                maxHeight: '60vh', overflow: 'auto',
                bgcolor: (theme) => theme.palette.grey[50], borderRadius: 1,
              }}
            >
              {preview.content}
            </Box>
          )}
          {preview?.kind === 'image' && preview.blobUrl && (
            <Box sx={{ textAlign: 'center' }}>
              <img src={preview.blobUrl} alt={preview.entry.name} style={{ maxWidth: '100%', maxHeight: '60vh' }} />
            </Box>
          )}
          {preview?.kind === 'pdf' && preview.blobUrl && (
            <Box sx={{ height: '70vh' }}>
              <iframe title={preview.entry.name} src={preview.blobUrl} style={{ width: '100%', height: '100%', border: 'none' }} />
            </Box>
          )}
          {preview?.kind === 'html' && preview.blobUrl && (
            <Box sx={{ height: '70vh', bgcolor: '#fff' }}>
              <iframe
                title={preview.entry.name}
                src={preview.blobUrl}
                sandbox="allow-scripts"
                style={{ width: '100%', height: '100%', border: 'none' }}
              />
            </Box>
          )}
          {preview && !preview.tooLarge && preview.kind === 'binary' && (
            <Alert severity="info">{t('operations.workspace.noPreview')}</Alert>
          )}
        </DialogContent>
        <DialogActions>
          <Button onClick={closePreview}>{t('operations.workspace.close')}</Button>
        </DialogActions>
      </Dialog>
    </Box>
  )
}

function Breadcrumb({ segments, onNavigate, rootLabel }: {
  segments: string[]
  onNavigate: (path: string) => void
  rootLabel: string
}) {
  return (
    <Stack direction="row" spacing={0.5} alignItems="center" flexWrap="wrap" sx={{ minWidth: 0 }}>
      <Link component="button" variant="body2" underline="hover" onClick={() => onNavigate('')}>
        {rootLabel}
      </Link>
      {segments.map((seg, i) => {
        const target = segments.slice(0, i + 1).join('/')
        const isLast = i === segments.length - 1
        return (
          <Stack key={target} direction="row" spacing={0.5} alignItems="center">
            <Typography variant="body2" color="text.disabled">/</Typography>
            {isLast ? (
              <Typography variant="body2" fontWeight={600}>{seg}</Typography>
            ) : (
              <Link component="button" variant="body2" underline="hover" onClick={() => onNavigate(target)}>
                {seg}
              </Link>
            )}
          </Stack>
        )
      })}
    </Stack>
  )
}
