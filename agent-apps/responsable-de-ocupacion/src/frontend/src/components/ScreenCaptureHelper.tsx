/**
 * ScreenCaptureHelper — "help with your application" from the chat.
 *
 * Captures screens of ANOTHER window/tab (the tool the
 * user wants help with) without leaving the chat, via the Screen Capture API
 * (`getDisplayMedia`): the user picks the window in the browser's native
 * picker, sees a live preview and freezes as many frames as
 * needed ("capture → navigate in your app → capture again"). On confirm,
 * the captures come back as PNG `File[]` that the chat uploads through its normal
 * attachment pipeline (chat-upload → the agent's ATTACHED FILES block).
 *
 * Privacy: the stream stops on confirm, cancel or close — the
 * sharing lasts only as long as the capture session. If the user
 * cuts it from the browser bar ("Stop sharing"), captures already
 * taken are kept.
 *
 * No expert-system names on purpose: the copy is generic ("your
 * application"), valid for any employee.
 */
import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import {
  Alert, Box, Button, Dialog, DialogActions, DialogContent, DialogTitle,
  IconButton, Stack, Tooltip, Typography,
} from '@mui/material'
import PhotoCameraIcon from '@mui/icons-material/PhotoCamera'
import CloseIcon from '@mui/icons-material/Close'
import ReplayIcon from '@mui/icons-material/Replay'

type Shot = { id: string; blob: Blob; url: string }

export function screenCaptureSupported(): boolean {
  try {
    return typeof navigator !== 'undefined' && !!navigator.mediaDevices?.getDisplayMedia
  } catch { return false }
}

export default function ScreenCaptureHelper({ open, onClose, onDone }: {
  open: boolean
  onClose: () => void
  /** The confirmed captures, as PNGs ready for the attachment pipeline. */
  onDone: (files: File[]) => void
}) {
  const { t } = useTranslation()
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const [shots, setShots] = useState<Shot[]>([])
  const [live, setLive] = useState(false)
  const [denied, setDenied] = useState(false)

  const stopStream = () => {
    streamRef.current?.getTracks().forEach((tk) => tk.stop())
    streamRef.current = null
    setLive(false)
  }

  const startCapture = async () => {
    setDenied(false)
    try {
      // When sharing a TAB, Chrome jumps to the captured tab
      // (Conditional Focus): with this chat in the background the <video> does not
      // start and `Capturar` (Capture) does nothing. CaptureController prevents it; the
      // extra options remove our own tab from the picker and allow
      // switching the shared tab from the browser bar.
      const opts: MediaStreamConstraints & Record<string, unknown> = {
        video: true,
        audio: false,
        selfBrowserSurface: 'exclude',
        surfaceSwitching: 'include',
      }
      const Controller = (window as unknown as { CaptureController?: new () => unknown }).CaptureController
      if (Controller) {
        const controller = new Controller() as { setFocusBehavior?: (b: string) => void }
        try { controller.setFocusBehavior?.('no-focus-change') } catch { /* browser version without support */ }
        opts.controller = controller
      }
      const stream = await navigator.mediaDevices.getDisplayMedia(opts)
      streamRef.current = stream
      const video = videoRef.current
      if (video) {
        video.srcObject = stream
        const tryPlay = () => { void video.play().catch(() => { /* autoplay guard */ }) }
        tryPlay()
        video.addEventListener('loadedmetadata', tryPlay, { once: true })
      }
      setLive(true)
      // The user can cut it from the browser bar: we keep the
      // captures taken and offer to re-share.
      stream.getVideoTracks()[0]?.addEventListener('ended', () => setLive(false))
    } catch {
      // NotAllowedError = cancelled the picker; any other error is handled
      // the same way: soft message + retry button.
      setDenied(true)
    }
  }

  useEffect(() => {
    if (open) void startCapture()
    return () => {
      stopStream()
      setShots((cur) => { cur.forEach((s) => URL.revokeObjectURL(s.url)); return [] })
      setDenied(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  // If the browser jumped to the captured tab (or the user went to navigate
  // their app) and comes back here, retry play(): a video that never got
  // to play in the background stays frozen.
  useEffect(() => {
    const onVisible = () => {
      if (!document.hidden && streamRef.current && videoRef.current) {
        void videoRef.current.play().catch(() => { /* autoplay guard */ })
      }
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => document.removeEventListener('visibilitychange', onVisible)
  }, [])

  const takeShot = async () => {
    const video = videoRef.current
    if (!video) return
    if (!video.videoWidth) {
      // The first frame has not arrived yet (typical right after coming back from the
      // captured tab): force play() and wait briefly instead of
      // failing silently.
      void video.play().catch(() => { /* autoplay guard */ })
      await new Promise<void>((resolve) => {
        const v = video as HTMLVideoElement & { requestVideoFrameCallback?: (cb: () => void) => void }
        if (typeof v.requestVideoFrameCallback === 'function') v.requestVideoFrameCallback(() => resolve())
        window.setTimeout(resolve, typeof v.requestVideoFrameCallback === 'function' ? 1500 : 400)
      })
      if (!video.videoWidth) return
    }
    const canvas = document.createElement('canvas')
    canvas.width = video.videoWidth
    canvas.height = video.videoHeight
    canvas.getContext('2d')?.drawImage(video, 0, 0)
    const blob: Blob | null = await new Promise((res) => canvas.toBlob(res, 'image/png'))
    if (!blob) return
    setShots((cur) => [...cur, {
      id: `shot_${Date.now()}_${cur.length}`, blob, url: URL.createObjectURL(blob),
    }])
  }

  const removeShot = (id: string) => setShots((cur) => {
    const gone = cur.find((s) => s.id === id)
    if (gone) URL.revokeObjectURL(gone.url)
    return cur.filter((s) => s.id !== id)
  })

  const confirm = () => {
    const stamp = new Date()
    const base = `captura-${stamp.getFullYear()}${String(stamp.getMonth() + 1).padStart(2, '0')}${String(stamp.getDate()).padStart(2, '0')}-${String(stamp.getHours()).padStart(2, '0')}${String(stamp.getMinutes()).padStart(2, '0')}`
    const files = shots.map((s, i) => new File([s.blob], `${base}-${i + 1}.png`, { type: 'image/png' }))
    stopStream()
    onDone(files)
    onClose()
  }

  const cancel = () => { stopStream(); onClose() }

  return (
    <Dialog open={open} onClose={cancel} maxWidth="md" fullWidth>
      <DialogTitle>{t('agent.chat.capture.title', 'Captura pantallas de tu aplicación')}</DialogTitle>
      <DialogContent>
        <Stack spacing={1.5}>
          <Typography variant="caption" color="text.secondary">
            {t('agent.chat.capture.privacy', 'Elige la ventana de tu aplicación y captura las pantallas sobre las que quieras preguntar. Revisa que no se vea información que no quieras compartir; la compartición se detiene al terminar.')}
          </Typography>

          {denied && (
            <Alert
              severity="info"
              action={(
                <Button size="small" startIcon={<ReplayIcon />} onClick={() => void startCapture()}>
                  {t('agent.chat.capture.retry', 'Reintentar')}
                </Button>
              )}
            >
              {t('agent.chat.capture.denied', 'No se seleccionó ninguna ventana. Vuelve a intentarlo y elige la ventana de tu aplicación.')}
            </Alert>
          )}

          {!denied && (
            <Box sx={{ position: 'relative', bgcolor: 'common.black', borderRadius: 1, overflow: 'hidden' }}>
              {/* muted: we never ask for audio, but some browsers require
                  muted for the preview autoplay. */}
              <video ref={videoRef} muted playsInline style={{ width: '100%', maxHeight: '46vh', display: 'block' }} />
              {!live && (
                <Box sx={{
                  position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column', gap: 1,
                  alignItems: 'center', justifyContent: 'center', color: 'grey.300', p: 2, textAlign: 'center',
                }}>
                  <Typography variant="body2">
                    {t('agent.chat.capture.ended', 'La compartición se detuvo. Tus capturas se conservan; puedes volver a compartir para hacer más.')}
                  </Typography>
                  <Button size="small" variant="outlined" color="inherit" startIcon={<ReplayIcon />} onClick={() => void startCapture()}>
                    {t('agent.chat.capture.reshare', 'Volver a compartir')}
                  </Button>
                </Box>
              )}
            </Box>
          )}

          <Stack direction="row" spacing={1.5} alignItems="center" justifyContent="center">
            <Button
              variant="contained"
              size="large"
              startIcon={<PhotoCameraIcon />}
              disabled={!live}
              onClick={() => void takeShot()}
            >
              {t('agent.chat.capture.shoot', 'Capturar esta pantalla')}
            </Button>
            <Typography variant="body2" color="text.secondary">
              {t('agent.chat.capture.count', { count: shots.length, defaultValue: '{{count}} captura(s)' })}
            </Typography>
          </Stack>

          {shots.length > 0 && (
            <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
              {shots.map((s, i) => (
                <Box key={s.id} sx={{ position: 'relative', width: 132 }}>
                  <img
                    src={s.url}
                    alt={`captura ${i + 1}`}
                    style={{ width: '100%', borderRadius: 6, border: '1px solid rgba(0,0,0,0.2)', display: 'block' }}
                  />
                  <Tooltip title={t('agent.chat.capture.remove', 'Quitar')}>
                    <IconButton
                      size="small"
                      onClick={() => removeShot(s.id)}
                      sx={{
                        position: 'absolute', top: 2, right: 2, p: 0.25,
                        bgcolor: 'background.paper', boxShadow: 1,
                        '&:hover': { bgcolor: 'background.paper' },
                      }}
                    >
                      <CloseIcon sx={{ fontSize: 14 }} />
                    </IconButton>
                  </Tooltip>
                </Box>
              ))}
            </Stack>
          )}
        </Stack>
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 2 }}>
        <Button onClick={cancel}>{t('common.cancel', 'Cancelar')}</Button>
        <Button variant="contained" disabled={shots.length === 0} onClick={confirm}>
          {t('agent.chat.capture.use', { count: shots.length, defaultValue: 'Preguntar sobre {{count}} captura(s)' })}
        </Button>
      </DialogActions>
    </Dialog>
  )
}
