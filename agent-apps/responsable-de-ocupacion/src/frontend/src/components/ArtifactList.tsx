/**
 * ArtifactList — downloadable chips for the deliverable files a run
 * produced (PPTX/PDF/CSV/… written to /workspace/exports/).
 *
 * The bytes live on the shared workspace PVC; we fetch them through the
 * authenticated ``/api/agent/workspace/download`` endpoint (apiFetch adds
 * the JWT) and stream the blob to a browser download. Reused by the run
 * detail page, Encargos and the Calendar item dialog.
 */
import { Chip, Stack } from '@mui/material'
import InsertDriveFileOutlinedIcon from '@mui/icons-material/InsertDriveFileOutlined'
import DownloadIcon from '@mui/icons-material/Download'
import { apiFetch } from '../services/apiClient'

export interface Artifact {
  filename: string
  workspace_path: string
  size?: number
  /** Set by backend deliverable filter: deliverable | image | auxiliary | other */
  kind?: string
}

const KIND_ORDER: Record<string, number> = {
  deliverable: 0,
  other: 1,
  image: 2,
  auxiliary: 3,
  qa: 9,
}

function sortArtifacts(list: Artifact[]): Artifact[] {
  return [...list].sort((a, b) => {
    const ka = KIND_ORDER[a.kind || 'other'] ?? 5
    const kb = KIND_ORDER[b.kind || 'other'] ?? 5
    if (ka !== kb) return ka - kb
    return (a.workspace_path || a.filename).localeCompare(
      b.workspace_path || b.filename,
    )
  })
}

function formatSize(n?: number): string {
  if (!n || n <= 0) return ''
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`
  return `${(n / (1024 * 1024)).toFixed(1)} MB`
}

export async function downloadWorkspaceFile(path: string, filename: string): Promise<void> {
  const res = await apiFetch(`/api/agent/workspace/download?path=${encodeURIComponent(path)}`)
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
}

export default function ArtifactList({ artifacts, onError }: {
  artifacts?: Artifact[] | null
  onError?: (msg: string) => void
}) {
  if (!artifacts || artifacts.length === 0) return null
  const sorted = sortArtifacts(artifacts)
  const trigger = (a: Artifact) => {
    const name = a.filename || a.workspace_path.split('/').pop() || 'file'
    downloadWorkspaceFile(a.workspace_path, name).catch((e: any) =>
      onError?.(e?.message || 'download failed'))
  }
  return (
    <Stack direction="row" spacing={0.75} flexWrap="wrap" useFlexGap>
      {sorted.map((a) => {
        const name = a.filename || a.workspace_path.split('/').pop() || 'file'
        const sz = formatSize(a.size)
        return (
          <Chip
            key={a.workspace_path}
            icon={<InsertDriveFileOutlinedIcon />}
            deleteIcon={<DownloadIcon />}
            onClick={() => trigger(a)}
            onDelete={() => trigger(a)}
            label={sz ? `${name} · ${sz}` : name}
            variant="outlined"
            sx={{ maxWidth: 340 }}
          />
        )
      })}
    </Stack>
  )
}
