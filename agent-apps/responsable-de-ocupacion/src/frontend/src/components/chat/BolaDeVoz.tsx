/**
 * The orb: the employee, while you talk to it. It is the BRAND, not an audio widget.
 *
 * A night-blue sphere with its rim and the W over AI, like the logo; turquoise is what
 * is alive. It stays dark in both themes on purpose: it is a seal, and a seal that
 * changes color with the theme stops being recognisable.
 *
 * Everything it does means something that is really happening, and that is why it can
 * be watched instead of read:
 *
 *   · at rest   — it breathes, very slowly;
 *   · hears you — the ring deforms with YOUR voice and the inside lights up in turquoise;
 *   · working   — the ring calms down and marks appear IN ORBIT, one for each tool
 *                 the employee is really using, with its name;
 *   · speaking  — it pulses with ITS voice, in warm light.
 *
 * The orbit is what makes the wait SEEN instead of told, and that is what takes the
 * pressure off the voice: the employee takes a median of 21 seconds and 150 in the worst
 * case, and filling that gap by talking was expensive —each sentence is a turn— and risky
 * —sometimes it improvised instead of repeating—.
 *
 * The mic level is used ONLY for drawing. Filtering audio by volume breaks the turn
 * detector, which judges the continuous stream; here it is measured to paint, never
 * to decide who is speaking.
 */
import { useEffect, useRef } from 'react'
import { Box } from '@mui/material'

export type EstadoBola = 'reposo' | 'oyendo' | 'trabajando' | 'hablando'

interface Props {
  estado: EstadoBola
  /** Tools running right now, in the person's language («el mapa», the map). */
  orbitas?: string[]
  /** Streams to take the level from, only for drawing. */
  entrada?: MediaStream | null
  salida?: MediaStream | null
  tam?: number
}

// The brand colors, raw and not from the theme: this is a seal.
// color-fijo:inicio — the voice orb is a drawing with its own light (night, turquoise,
// warm): it does not follow the app theme, just as an icon does not.
const NOCHE_CLARO = '#12304d'
const NOCHE_OSCURO = '#07121f'
const REBORDE = '#2a5578'
const TURQUESA = '#17a39c'
const CALIDO = '#e8d9b0'
// color-fijo:fin

function medidor(ctx: AudioContext, stream: MediaStream) {
  const an = ctx.createAnalyser()
  an.fftSize = 512
  an.smoothingTimeConstant = 0.75
  ctx.createMediaStreamSource(stream).connect(an)
  const buf = new Float32Array(an.fftSize)
  return () => {
    an.getFloatTimeDomainData(buf)
    let s = 0
    for (let i = 0; i < buf.length; i++) s += buf[i] * buf[i]
    return Math.min(1, Math.sqrt(s / buf.length) * 6)
  }
}

export default function BolaDeVoz({ estado, orbitas = [], entrada, salida, tam = 340 }: Props) {
  const lienzo = useRef<HTMLCanvasElement>(null)
  const datos = useRef({ estado, orbitas, suave: 0, giro: 0, t: 0 })
  datos.current.estado = estado
  datos.current.orbitas = orbitas

  useEffect(() => {
    const cv = lienzo.current
    if (!cv) return
    const g = cv.getContext('2d')
    if (!g) return
    const dpr = window.devicePixelRatio || 1
    cv.width = tam * dpr; cv.height = tam * dpr
    g.scale(dpr, dpr)

    const quieto = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    let audio: AudioContext | null = null
    let leeEntrada: (() => number) | null = null
    let leeSalida: (() => number) | null = null
    try {
      audio = new AudioContext()
      if (entrada) leeEntrada = medidor(audio, entrada)
      if (salida) leeSalida = medidor(audio, salida)
    } catch { /* no meter: the sphere breathes all the same */ }

    let vivo = true
    const c = tam / 2
    const R = tam * 0.30              // sphere radius
    const RO = tam * 0.40             // orbit radius

    const dibuja = () => {
      const d = datos.current
      d.t += quieto ? 0 : 0.016
      const crudo = d.estado === 'oyendo' ? (leeEntrada?.() ?? 0)
                  : d.estado === 'hablando' ? (leeSalida?.() ?? 0.4)
                  : 0
      d.suave += (crudo - d.suave) * 0.18
      d.giro += d.estado === 'trabajando' ? 0.011 : 0.0016
      const luz = d.estado === 'hablando' ? CALIDO : TURQUESA
      const respira = Math.sin(d.t * 0.9) * R * 0.012

      g.clearRect(0, 0, tam, tam)

      // 1 · the outer halo, which is what gives the logo its air
      const halo = g.createRadialGradient(c, c, R * 0.9, c, c, R * 1.75)
      halo.addColorStop(0, hex(luz, d.estado === 'reposo' ? 0.10 : 0.16 + d.suave * 0.22))
      halo.addColorStop(1, hex(luz, 0))
      g.fillStyle = halo
      g.beginPath(); g.arc(c, c, R * 1.75, 0, Math.PI * 2); g.fill()

      // 2 · the sphere: gradient with the light at the top left, so it has volume
      const cuerpo = g.createRadialGradient(c - R * 0.35, c - R * 0.4, R * 0.1, c, c, R * 1.1)
      cuerpo.addColorStop(0, NOCHE_CLARO)
      cuerpo.addColorStop(1, NOCHE_OSCURO)
      g.save()
      g.beginPath(); g.arc(c, c, R + respira, 0, Math.PI * 2); g.clip()
      g.fillStyle = cuerpo
      g.fillRect(0, 0, tam, tam)

      // 3 · the inner light, which moves. Two slow, out-of-phase blobs: with a single
      //     one the trick shows, with two it looks like something is breathing in there.
      const fuerza = d.estado === 'reposo' ? 0.16 : 0.3 + d.suave * 0.7
      for (let i = 0; i < 2; i++) {
        const a = d.t * (0.35 + i * 0.22) + i * 2.3
        const x = c + Math.cos(a) * R * 0.38
        const y = c + Math.sin(a * 1.3) * R * 0.34
        const m = g.createRadialGradient(x, y, 0, x, y, R * 0.85)
        m.addColorStop(0, hex(luz, 0.30 * fuerza))
        m.addColorStop(1, hex(luz, 0))
        g.fillStyle = m
        g.fillRect(0, 0, tam, tam)
      }
      g.restore()

      // 4 · the ring. Round at rest; it deforms with the voice of whoever is speaking.
      const amp = R * 0.055 * d.suave
      g.beginPath()
      const N = 160
      for (let i = 0; i <= N; i++) {
        const a = (i / N) * Math.PI * 2
        const onda = Math.sin(a * 3 + d.t * 2.4) * 0.6 + Math.sin(a * 5 - d.t * 1.8) * 0.4
        const rr = R + respira + amp * onda
        const x = c + Math.cos(a) * rr
        const y = c + Math.sin(a) * rr
        if (i === 0) g.moveTo(x, y); else g.lineTo(x, y)
      }
      g.closePath()
      g.strokeStyle = d.estado === 'reposo' ? REBORDE : hex(luz, 0.85)
      g.lineWidth = d.estado === 'reposo' ? 1.25 : 2
      g.stroke()

      // 5 · the top-left highlight: without it the sphere looks like a disc
      g.beginPath()
      g.arc(c, c, R - 1, Math.PI * 1.08, Math.PI * 1.62)
      g.strokeStyle = hex('#ffffff', 0.22)
      g.lineWidth = 1.5
      g.stroke()

      // 6 · the brand: W over AI with its fillet
      const cuerpoTipo = tam * 0.085
      g.textAlign = 'center'
      g.textBaseline = 'middle'
      g.fillStyle = hex('#ffffff', 0.94)
      g.font = `${cuerpoTipo}px Georgia, 'Times New Roman', serif`
      g.fillText('W', c, c - cuerpoTipo * 0.52)
      g.font = `${cuerpoTipo * 0.86}px Georgia, 'Times New Roman', serif`
      g.fillText('AI', c, c + cuerpoTipo * 0.62)
      g.beginPath()
      g.moveTo(c - cuerpoTipo * 0.62, c)
      g.lineTo(c + cuerpoTipo * 0.62, c)
      g.strokeStyle = hex('#ffffff', 0.7)
      g.lineWidth = 1
      g.stroke()

      // 7 · the orbit: one mark per REAL tool running, with its name underneath.
      //     It is the data, not decoration: no tools, no marks.
      const n = d.orbitas.length
      if (n) {
        g.font = `600 11px system-ui, -apple-system, sans-serif`
        for (let i = 0; i < n; i++) {
          const a = d.giro + (i / n) * Math.PI * 2
          const x = c + Math.cos(a) * RO
          const y = c + Math.sin(a) * RO
          const brillo = g.createRadialGradient(x, y, 0, x, y, 9)
          brillo.addColorStop(0, hex(TURQUESA, 0.9))
          brillo.addColorStop(1, hex(TURQUESA, 0))
          g.fillStyle = brillo
          g.beginPath(); g.arc(x, y, 9, 0, Math.PI * 2); g.fill()
          g.fillStyle = TURQUESA
          g.beginPath(); g.arc(x, y, 3, 0, Math.PI * 2); g.fill()
        }
      }
    }

    const pinta = () => {
      if (!vivo) return
      // A bad frame cannot kill the animation: it happened once —an invalid color in a
      // gradient— and the orb stopped painting ENTIRELY, silently.
      try { dibuja() } catch { /* al siguiente */ }
      requestAnimationFrame(pinta)
    }
    requestAnimationFrame(pinta)
    return () => { vivo = false; void audio?.close() }
  }, [tam, entrada, salida])

  return (
    <Box sx={{ display: 'flex', justifyContent: 'center' }}>
      <canvas ref={lienzo} style={{ width: tam, height: tam }} aria-hidden />
    </Box>
  )
}

/** A color with transparency, without concatenating strings: gluing `'33'` to an `rgba()`
 *  gave an invalid color and `addColorStop` threw, killing the whole drawing. */
function hex(color: string, alfa: number): string {
  const n = color.replace('#', '')
  const r = parseInt(n.slice(0, 2), 16)
  const g = parseInt(n.slice(2, 4), 16)
  const b = parseInt(n.slice(4, 6), 16)
  return `rgba(${r}, ${g}, ${b}, ${Math.max(0, Math.min(1, alfa)).toFixed(3)})`
}
