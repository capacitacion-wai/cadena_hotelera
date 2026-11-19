/**
 * Switching conversation, like in ChatGPT (v9.1.0).
 *
 * Rendered ONLY if the employee publishes `hilos` in its manifest. That check is not a
 * courtesy: there is ONE frontend talking to employees of many versions, and one that has not been
 * rebuilt has neither the table nor the endpoints. Without the flag, this component does not
 * exist and the chat works exactly as before.
 *
 * Notices go through the scaffold's dialogs (`useConfirm`, `usePrompt`) and a Snackbar,
 * never through `window.confirm/prompt/alert`: besides breaking the theme, verifier §8.3y
 * forbids them — and since this file is an INVARIANT, a `window.*` here leaves ALL employees
 * unable to update (`--fix-invariants` restores the asset, and the asset fails the rule again).
 *
 * The thread name is shown IN FULL in the menu and truncated on the button: on the button it competes
 * with the rest of the header, but when choosing it is the only thing that tells one conversation from
 * another, and cutting it there would force opening them to know which is which.
 */
import { useCallback, useEffect, useState } from 'react'
import {
  Alert, Box, CircularProgress, Divider, IconButton, ListItemIcon, ListItemText, Menu,
  ListSubheader, MenuItem, Snackbar, Tooltip, Typography,
} from '@mui/material'
import AddIcon from '@mui/icons-material/Add'
import ArchiveOutlinedIcon from '@mui/icons-material/ArchiveOutlined'
import UnarchiveOutlinedIcon from '@mui/icons-material/UnarchiveOutlined'
import CheckIcon from '@mui/icons-material/Check'
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline'
import DriveFileRenameOutlineIcon from '@mui/icons-material/DriveFileRenameOutline'
import ForumOutlinedIcon from '@mui/icons-material/ForumOutlined'
import { useTranslation } from 'react-i18next'

import { useConfirm, usePrompt } from '../ConfirmDialog'
import { apiFetch } from '../../services/apiClient'


/** The server stores `Conversación` as the title of a thread nobody named (it treats it as
 *  «no title»: `chat_threads.py`). It is a placeholder, not a name: shown as is it put Spanish
 *  inside English screens. Here it reads as empty and the screen says it in its language. */
const SIN_TITULO = 'Conversación'
function tituloDe(titulo?: string | null): string {
  return titulo && titulo !== SIN_TITULO ? titulo : ''
}
export interface Hilo {
  id: string
  titulo: string
  ultimo_uso: string | null
  activo: boolean
  archivado: boolean
  tokens: number
  /** The conversation we go back to when another one is removed from the list. There is always ONE (v12.0.0)
   *    *  and it may change id without notice: deleting or archiving it ROTATES it (v12.3.0). */
  por_defecto?: boolean
}

interface Props {
  hiloActual: string
  onCambiar: (id: string) => void
  /** The list is reloaded when the turn ends: the title improves after the first exchange. */
  refrescarEn?: unknown
  deshabilitado?: boolean
}

/** "Today", "Yesterday", … using the LOCAL date: at 00:30 here, something from an hour ago is still
 *  *  today for whoever lived it. Same criterion as the mobile app, on purpose: the same
 *  *  conversation cannot be under "Today" in one place and under "Yesterday" in the other. */
function grupoDe(iso: string | null): string {
  if (!iso) return 'Antes'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return 'Antes'
  const soloFecha = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime()
  const dias = Math.round((soloFecha(new Date()) - soloFecha(d)) / 86_400_000)
  if (dias <= 0) return 'Hoy'
  if (dias === 1) return 'Ayer'
  if (dias <= 7) return 'Últimos 7 días'
  return 'Antes'
}

const ORDEN_DE_GRUPOS = ['Hoy', 'Ayer', 'Últimos 7 días', 'Antes']

/** When it was used, in the viewer's timezone. Within the week the time is enough; further
 *  *  back, the date — which is the question one asks when looking for an old conversation. */
function cuando(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const g = grupoDe(iso)
  return (g === 'Hoy' || g === 'Ayer')
    ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    : d.toLocaleDateString([], { day: 'numeric', month: 'short' })
}

export default function SelectorDeHilos({
  hiloActual, onCambiar, refrescarEn, deshabilitado,
}: Props) {
  const { t } = useTranslation()
  const [hilos, setHilos] = useState<Hilo[]>([])
  // Archiving cannot be a black hole: if archived ones can be neither seen nor recovered, the
  // conversation is effectively lost even if the server keeps it.
  const [verArchivadas, setVerArchivadas] = useState(false)
  const [ancla, setAncla] = useState<null | HTMLElement>(null)
  const [ocupado, setOcupado] = useState(false)
  // The conversation cap is not a user error but a STATE, so it is told in
  // a notice that goes away on its own, not in a modal that has to be dismissed.
  const [aviso, setAviso] = useState<string | null>(null)
  const { confirm, dialog: dialogoConfirmar } = useConfirm()
  const { prompt, dialog: dialogoNombre } = usePrompt()

  const cargar = useCallback(async () => {
    try {
      const r = await apiFetch('/api/agent/threads'
        + (verArchivadas ? '?incluir_archivados=true' : ''))
      if (!r.ok) return
      const d = await r.json()
      const todas: Hilo[] = Array.isArray(d?.hilos) ? d.hilos : []
      setHilos(verArchivadas ? todas.filter((h) => h.archivado) : todas)
    } catch {
      // Failing to read the list must NOT break the chat: it is navigation, not the
      // conversation. Without it you keep talking in the current thread.
    }
  }, [verArchivadas])

  useEffect(() => { void cargar() }, [cargar, refrescarEn])

  const actual = hilos.find((h) => h.id === hiloActual)
  // v12.0.0 — which conversation to go back to when archiving or deleting the current one: this
  // person's REAL default (the list brings it flagged). 'default' is the alias the server
  // resolves the same way, so it serves as a fallback if the list has not arrived yet.
  const porDefecto: string = hilos.find((h) => h.por_defecto)?.id ?? 'default'
  const nombre = tituloDe(actual?.titulo) || t('agent.threads.default', 'Conversación')

  const crear = async () => {
    setOcupado(true)
    try {
      const r = await apiFetch('/api/agent/threads', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
      })
      if (r.status === 409) {
        // The cap is not a user error: it is a state, and the message says what to do.
        const d = await r.json().catch(() => ({}))
        setAviso(d?.detail || t('agent.threads.full',
          'Tienes demasiadas conversaciones abiertas. Archiva alguna para empezar otra.') as string)
        return
      }
      if (!r.ok) return
      const d = await r.json()
      await cargar()
      if (d?.id) onCambiar(d.id)
    } finally {
      setOcupado(false)
      setAncla(null)
    }
  }

  const renombrar = (h: Hilo) => {
    // Cancelling does not invoke the callback, so there is no longer any need to tell "empty" from
    // "cancelled" as `window.prompt` returning null forced us to.
    prompt({
      title: t('agent.threads.renameTitle', 'Renombrar conversación') as string,
      label: t('agent.threads.renamePrompt', 'Nombre de la conversación') as string,
      defaultValue: tituloDe(h.titulo),
    }, (titulo) => {
      void (async () => {
        await apiFetch(`/api/agent/threads/${h.id}`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ titulo }),
        })
        await cargar()
      })()
    })
    setAncla(null)
  }

  /** Where to go after removing `h` from the list. The SERVER says it in the response, and it must
   *    *  be obeyed: removing the default conversation —deleting or archiving it— ROTATES it
   *    *  (v12.3.0: another empty one is opened, with a new id), so the one this client had flagged in
   *    *  its list no longer exists. Going back to it would be a 404 and a blank screen. */
  const destinoTras = async (r: Response): Promise<string> => {
    const d = await r.json().catch(() => ({}))
    const id = (d as { id_por_defecto?: unknown })?.id_por_defecto
    return typeof id === 'string' && id ? id : porDefecto
  }

  const archivar = async (h: Hilo, archivar_si = true) => {
    const r = await apiFetch(`/api/agent/threads/${h.id}/archive?archivar=${archivar_si}`,
      { method: 'POST' })
    const destino = await destinoTras(r)
    await cargar()
    if (h.id === hiloActual) onCambiar(destino)
  }

  const borrar = (h: Hilo) => {
    confirm({
      title: t('agent.threads.deleteTitle', 'Borrar conversación') as string,
      message: t('agent.threads.deleteConfirm',
        'Se borrará esta conversación. El historial de ejecuciones se conserva para la auditoría.') as string,
      danger: true,
    }, () => {
      void (async () => {
        const r = await apiFetch(`/api/agent/threads/${h.id}`, { method: 'DELETE' })
        const destino = await destinoTras(r)
        await cargar()
        if (h.id === hiloActual) onCambiar(destino)
      })()
    })
    setAncla(null)
  }

  return (
    <>
      <Tooltip arrow title={t('agent.threads.tooltip', 'Cambiar de conversación') as string}>
        <span>
          <IconButton
            size="small"
            onClick={(e) => setAncla(e.currentTarget)}
            disabled={deshabilitado}
            aria-label={t('agent.threads.button', 'Conversaciones') as string}
            sx={{
              border: '1px solid', borderColor: 'divider', borderRadius: 1.5,
              bgcolor: 'background.paper', px: 1, gap: 0.75,
              '&:hover': { bgcolor: 'action.hover' },
            }}
          >
            <ForumOutlinedIcon sx={{ fontSize: 16 }} />
            <Typography variant="caption" sx={{ maxWidth: 130 }} noWrap>{nombre}</Typography>
          </IconButton>
        </span>
      </Tooltip>

      <Menu anchorEl={ancla} open={Boolean(ancla)} onClose={() => setAncla(null)}
        slotProps={{ paper: { sx: { minWidth: 300, maxWidth: 420 } } }}>
        <MenuItem onClick={() => void crear()} disabled={ocupado}>
          <ListItemIcon>
            {ocupado ? <CircularProgress size={16} /> : <AddIcon fontSize="small" />}
          </ListItemIcon>
          <ListItemText primary={t('agent.threads.new', 'Nueva conversación')} />
        </MenuItem>
        <Divider />
        {hilos.length === 0 && (
          <MenuItem disabled>
            <ListItemText secondary={t('agent.threads.empty', 'Aún no hay conversaciones.')} />
          </MenuItem>
        )}
        <MenuItem onClick={() => setVerArchivadas((v) => !v)} sx={{ py: 0.5 }}>
          <ListItemIcon>
            {verArchivadas
              ? <UnarchiveOutlinedIcon fontSize="small" />
              : <ArchiveOutlinedIcon fontSize="small" />}
          </ListItemIcon>
          <ListItemText primaryTypographyProps={{ variant: 'body2' }}
            primary={verArchivadas
              ? t('agent.threads.seeActive', 'Ver las activas')
              : t('agent.threads.seeArchived', 'Ver las archivadas')} />
        </MenuItem>
        <Divider />
        {hilos.map((h, i) => {
          // Header when the group changes. Computed on the fly instead of rebuilding the list:
          // threads already come from the server sorted by recent use, so the order of
          // the groups comes out by itself and there are no two sortings that could disagree.
          const grupo = grupoDe(h.ultimo_uso)
          const nuevoGrupo = i === 0 || grupo !== grupoDe(hilos[i - 1].ultimo_uso)
          return [
            nuevoGrupo ? (
              <ListSubheader key={`g-${grupo}`} sx={{ lineHeight: '28px', fontSize: 11 }}>
                {grupo}
              </ListSubheader>
            ) : null,
          ].concat((
          <MenuItem key={h.id} selected={h.id === hiloActual}
            onClick={() => { onCambiar(h.id); setAncla(null) }}>
            <ListItemIcon>
              {h.id === hiloActual ? <CheckIcon fontSize="small" /> : null}
            </ListItemIcon>
            <ListItemText
              primary={tituloDe(h.titulo) || t('agent.threads.default', 'Conversación')}
              secondary={[
                // WHEN first: it is what tells apart two conversations with similar
                // names, and until now it was shown nowhere in the web app.
                cuando(h.ultimo_uso),
                // `En marcha` (Running) = has live context and will answer instantly. The rest
                // reopen just the same, but the employee will have to re-read what it was about.
                h.activo
                  ? t('agent.threads.live', 'en marcha')
                  : t('agent.threads.idle', 'sin contexto vivo'),
              ].filter(Boolean).join(' · ')}
              primaryTypographyProps={{ noWrap: true }}
            />
            <Box sx={{ display: 'flex', gap: 0.25, ml: 1 }}>
              <IconButton size="small" aria-label="renombrar"
                onClick={(e) => { e.stopPropagation(); renombrar(h) }}>
                <DriveFileRenameOutlineIcon sx={{ fontSize: 15 }} />
              </IconButton>
              <IconButton size="small" aria-label="archivar"
                onClick={(e) => { e.stopPropagation(); void archivar(h, !h.archivado) }}>
                {h.archivado
                  ? <UnarchiveOutlinedIcon sx={{ fontSize: 15 }} />
                  : <ArchiveOutlinedIcon sx={{ fontSize: 15 }} />}
              </IconButton>
              <IconButton size="small" aria-label="borrar"
                onClick={(e) => { e.stopPropagation(); borrar(h) }}>
                <DeleteOutlineIcon sx={{ fontSize: 15 }} />
              </IconButton>
            </Box>
          </MenuItem>
          ) as any)
        })}
      </Menu>
      {/* Outside the <Menu>: choosing an action closes the menu, and a dialog mounted
          inside would unmount with it before ever being seen. */}
      {dialogoConfirmar}
      {dialogoNombre}
      <Snackbar
        open={!!aviso}
        autoHideDuration={6000}
        onClose={() => setAviso(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}
      >
        <Alert severity="info" variant="filled" onClose={() => setAviso(null)}>
          {aviso}
        </Alert>
      </Snackbar>
    </>
  )
}
