/**
 * Calibrate the microphone before speaking. Two seconds, and automatic.
 *
 * WHAT IT CALIBRATES, AND WHAT IT DOES NOT. It measures the room noise and with that picks
 * the turn detector's `eagerness` —the OFFICIAL lever: how long the model waits before
 * considering your sentence finished—. What it does NOT do, and must never do, is filter
 * the audio by volume: that was tried and it breaks the detector, which is a model and
 * needs the CONTINUOUS stream to judge. Measured in a real conversation with the gate on:
 * 16 openings and closings for 7 sentences, empty transcriptions and loose fragments
 * («Ah.», «Voilà.») that the model answered.
 *
 * And it serves the other thing that failed silently: checking that the microphone
 * delivers signal. If it does not, that is said HERE and not three layers below disguised
 * as «the employee does not answer».
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Box, Button, Dialog, DialogActions, DialogContent, DialogTitle, LinearProgress,
  MenuItem, Select, Stack, Typography,
} from '@mui/material'
import { useTranslation } from 'react-i18next'

export type Escucha = 'low' | 'medium' | 'high'

export interface Calibrado {
  /** How much noise there is in the room, 0-1 (average effective level). */
  fondo: number
  /** The turn detector's patience that suits this room. */
  escucha: Escucha
  /** The chosen microphone, if a specific one was chosen. */
  dispositivo?: string
}

/** Measures the room noise and decides how much patience the detector needs.
 *
 *  The bands come from a simple idea: the more noise, the more a throat-clearing looks
 *  like a sentence, so we must wait LONGER before considering your turn finished. With a
 *  silent room it can be nimble.  */
export function escuchaSegun(fondo: number): Escucha {
  if (fondo < 0.004) return 'high'      // silence: it can cut earlier
  if (fondo < 0.02) return 'low'        // normal: the value we already used
  return 'low'                          // noisy: the most patience available
}

export async function mideLaSala(dispositivo?: string, ms = 2000): Promise<Calibrado> {
  const flujo = await navigator.mediaDevices.getUserMedia({
    audio: {
      ...(dispositivo ? { deviceId: { exact: dispositivo } } : {}),
      echoCancellation: true, noiseSuppression: true, autoGainControl: true,
    },
  })
  const ctx = new AudioContext()
  try {
    const an = ctx.createAnalyser()
    an.fftSize = 1024
    ctx.createMediaStreamSource(flujo).connect(an)
    const buf = new Float32Array(an.fftSize)
    const muestras: number[] = []
    const hasta = Date.now() + ms
    while (Date.now() < hasta) {
      an.getFloatTimeDomainData(buf)
      let s = 0
      for (let i = 0; i < buf.length; i++) s += buf[i] * buf[i]
      muestras.push(Math.sqrt(s / buf.length))
      await new Promise((r) => window.setTimeout(r, 40))
    }
    muestras.sort((a, b) => a - b)
    // MEDIAN, not mean nor a high percentile: a knock during the measurement dragged the
    // 90th percentile and left an impossible bar (measured: 0.51 with a real background of
    // 0.0005). To move the median there would have to be noise half the time, and then
    // there really is.
    const fondo = muestras[Math.floor(muestras.length / 2)] || 0
    return { fondo, escucha: escuchaSegun(fondo), dispositivo }
  } finally {
    flujo.getTracks().forEach((p) => p.stop())
    void ctx.close()
  }
}

interface Props {
  abierto: boolean
  onCerrar: () => void
  onListo: (c: Calibrado) => void
  actual?: Calibrado | null
}

export default function CalibrarMicro({ abierto, onCerrar, onListo, actual }: Props) {
  const { t } = useTranslation()
  const [micros, setMicros] = useState<MediaDeviceInfo[]>([])
  const [elegido, setElegido] = useState(actual?.dispositivo || '')
  const [nivel, setNivel] = useState(0)
  const [midiendo, setMidiendo] = useState(false)
  const [medido, setMedido] = useState<Calibrado | null>(actual || null)
  const [fallo, setFallo] = useState('')
  const paro = useRef<(() => void) | null>(null)

  // The live meter, so you can see it hears you BEFORE you start talking to anyone.
  const enciende = useCallback(async (dispositivo: string) => {
    paro.current?.()
    setFallo('')
    try {
      const flujo = await navigator.mediaDevices.getUserMedia({
        audio: {
          ...(dispositivo ? { deviceId: { exact: dispositivo } } : {}),
          echoCancellation: true, noiseSuppression: true, autoGainControl: true,
        },
      })
      const ctx = new AudioContext()
      const an = ctx.createAnalyser()
      an.fftSize = 512
      an.smoothingTimeConstant = 0.6
      ctx.createMediaStreamSource(flujo).connect(an)
      const buf = new Float32Array(an.fftSize)
      const reloj = window.setInterval(() => {
        an.getFloatTimeDomainData(buf)
        let s = 0
        for (let i = 0; i < buf.length; i++) s += buf[i] * buf[i]
        setNivel(Math.min(1, Math.sqrt(s / buf.length) * 8))
      }, 60)
      paro.current = () => {
        window.clearInterval(reloj)
        flujo.getTracks().forEach((p) => p.stop())
        void ctx.close()
        paro.current = null
      }
      // Microphone names are only available WITH permission granted:
      // asking for them earlier returns a list of empty labels.
      setMicros((await navigator.mediaDevices.enumerateDevices())
        .filter((d) => d.kind === 'audioinput'))
    } catch (ex) {
      setFallo(t('voz.cal.sinMicro', 'No he podido abrir el micrófono: {{err}}')
        .replace('{{err}}', String((ex as Error).name)))
    }
  }, [t])

  useEffect(() => {
    if (abierto) void enciende(elegido)
    return () => paro.current?.()
  }, [abierto, elegido, enciende])

  const entregaSenal = nivel > 0.001

  return (
    <Dialog open={abierto} onClose={onCerrar} maxWidth="xs" fullWidth
            PaperProps={{ sx: { borderRadius: 3 } }}>
      <DialogTitle sx={{ pb: 0.5 }}>{t('voz.cal.titulo', 'Ajustar el micrófono')}</DialogTitle>
      <DialogContent>
        <Typography variant="body2" color="text.secondary"
                    sx={{ mb: 2, whiteSpace: 'pre-line' }}>
          {t('voz.cal.ayuda',
            'Di algo: si la barra se mueve, te oye.\n\n«Medir la sala» escucha dos segundos el '
            + 'ruido que tienes alrededor. Sirve para que no te corte cuando hagas una pausa, '
            + 'ni te conteste a un ruido.')}
        </Typography>

        {/* `displayEmpty`: without it, MUI leaves the dropdown BLANK when the value is
            the empty string, even though its option exists. It looked broken and was not. */}
        {micros.length > 1 && (
          <Select fullWidth size="small" value={elegido} displayEmpty sx={{ mb: 2 }}
                  onChange={(e) => { setElegido(e.target.value); setMedido(null) }}>
            <MenuItem value="">{t('voz.cal.porDefecto', 'El micrófono por defecto')}</MenuItem>
            {micros.map((m) => (
              <MenuItem key={m.deviceId} value={m.deviceId}>
                {m.label || t('voz.cal.sinNombre', 'Micrófono sin nombre')}
              </MenuItem>
            ))}
          </Select>
        )}

        <Typography variant="caption" color="text.secondary">
          {t('voz.cal.nivel', 'Lo que le llega')}
        </Typography>
        <LinearProgress variant="determinate" value={Math.round(nivel * 100)}
                        color={entregaSenal ? 'success' : 'inherit'}
                        sx={{ height: 10, borderRadius: 5, mb: 1 }} />
        <Typography variant="caption"
                    color={entregaSenal ? 'success.main' : 'text.disabled'}>
          {entregaSenal ? t('voz.cal.teOigo', 'Te oigo.')
                        : t('voz.cal.nada', 'No me llega nada todavía.')}
        </Typography>

        {fallo && <Typography variant="body2" color="error.main" sx={{ mt: 1.5 }}>{fallo}</Typography>}

        {medido && (
          <Box sx={{ mt: 2, p: 1.5, borderRadius: 2, bgcolor: 'action.hover' }}>
            <Typography variant="body2">
              {medido.escucha === 'high'
                ? t('voz.cal.silenciosa', 'Hay poco ruido. Te contestará en cuanto termines de hablar.')
                : t('voz.cal.ruidosa', 'Hay bastante ruido. Esperará un poco más para no cortarte.')}
            </Typography>
            <Typography variant="caption" color="text.disabled" sx={{ display: 'block', mt: 0.5 }}>
              {t('voz.cal.resultado', 'Ruido alrededor: {{fondo}}')
                .replace('{{fondo}}', medido.fondo.toFixed(4))}
            </Typography>
          </Box>
        )}
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 2 }}>
        <Button onClick={onCerrar} color="inherit">{t('voz.cal.cancelar', 'Cancelar')}</Button>
        <Button disabled={midiendo} onClick={async () => {
          setMidiendo(true)
          try {
            paro.current?.()
            const c = await mideLaSala(elegido)
            setMedido(c)
            await enciende(elegido)
          } catch (ex) {
            setFallo(String((ex as Error).message || ex))
          } finally { setMidiendo(false) }
        }}>
          {midiendo ? t('voz.cal.midiendo', 'Midiendo…') : t('voz.cal.medir', 'Medir la sala')}
        </Button>
        <Button variant="contained" onClick={() => {
          paro.current?.()
          onListo(medido || { fondo: 0, escucha: 'low', dispositivo: elegido || undefined })
        }}>
          {t('voz.cal.hablar', 'Hablar')}
        </Button>
      </DialogActions>
    </Dialog>
  )
}
