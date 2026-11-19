/**
 * The chat's VOICE mode. It is entered from the chat and takes over the screen while it lasts.
 *
 * THE SPLIT, which is the design decision:
 *
 *   · you talk to the VOICE, which converses with you and decides when a lookup is needed;
 *   · when it looks something up, that request enters the CHAT as a real turn, with the
 *     spoken conversation that led to it hanging from the request;
 *   · the employee's answer is painted in the chat AND said out loud.
 *
 * That way the chat stays the complete record —what was asked, what it answered and how
 * that request came about— without turning into an unreadable transcript of everything
 * said out loud.
 *
 * WHAT THE VOICE DOES NOT DO. It looks nothing up on its own: it has ONE tool and it is
 * asking the employee, who is the one who knows and holds the permissions. It asserts
 * nothing about the real world without having asked —not a timetable, not a distance, not
 * a price—, even if it knows it. When it was allowed to decide more, it made up answers
 * nobody had looked up: measured, 5 out of every 24 sentences.
 *
 * A SINGLE PIPE. Your voice goes in and its voice comes out through the SAME WebRTC
 * connection. That is what makes the browser's echo cancellation work —it subtracts from
 * the mic what it itself plays through the speaker— and what leaves interruption in the
 * server's hands. Taking the audio out of there to save money cost 335 lines of
 * compensation and a bug with no possible fix: it heard itself and cut off mid-sentence.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { Alert, Box, Button, Chip, IconButton, Paper, Stack, Tooltip, Typography } from '@mui/material'
import ContentCopyIcon from '@mui/icons-material/ContentCopy'
import CloseIcon from '@mui/icons-material/Close'
import KeyboardIcon from '@mui/icons-material/KeyboardVoice'
import { useTranslation } from 'react-i18next'
import { apiFetch } from '../../services/apiClient'
import BolaDeVoz from './BolaDeVoz'
import { capacidadesDelNavegador } from './superficieDeVoz'
import { crearMotor, type Motor } from '../../conversacion/puerto.ts'
import { adaptador, delServidorDeVoz, type DeLaSuperficie } from '../../conversacion/adaptador.ts'
import { enVuelo } from '../../conversacion/maquina.ts'
import type { EstadoBola } from './BolaDeVoz'
import type { TurnoHablado } from './UserBubble'
import CalibrarMicro, { mideLaSala } from './CalibrarMicro'
import type { Calibrado } from './CalibrarMicro'

// The calibration is remembered: measuring the room every time you open voice is two
// seconds of waiting for a figure that almost never changes. Re-measure by hand whenever you want.
const GUARDADO = 'wai.voz.calibrado'

/** Mic always open, or only while I hold? It is stored in the BROWSER, like the calibration,
 *  and for the same reason: it depends on where you are —an empty office or a room full of
 *  people—, not on who the employee is. */
const COMO_SE_HABLA = 'wai.voz.pulsar'
function leeCalibrado(): Calibrado | null {
  try { const x = localStorage.getItem(GUARDADO); return x ? JSON.parse(x) : null } catch { return null }
}

interface Props {
  /** Is voice mode open? When it is not, NOTHING is painted but the component stays
   *  mounted and the session stays open: on closing and reopening, the model remembers
   *  what was said. Destroying it on close was a mistake —the whole context was lost— and
   *  keeping it costs nothing: a silent session bills 0 tokens (measured). */
  abierta: boolean
  /** The voice has decided to look something up. The chat sends it as a turn and returns
   *  the employee's answer so it is said out loud. */
  /** `reconocido` is what the conversationalist got from reading: the capability marked
   *  [DIRECTO] it recognised (if any), its data, and how much thought it believes it needs.
   *  The server is the one who executes; this only NAMES it. */
  onPeticion: (texto: string, hablado: TurnoHablado[],
               reconocido?: { capacidad?: string; argumentos?: Record<string, unknown>
                              razonamiento?: string
                              /** Resumes the employee's turn that was left paused. */
                              requestId?: string }) =>
    /** `requestId` when what comes back is NOT the answer but a QUESTION from it, with its
     *  turn waiting: answering it as a new turn left it hanging forever. */
    Promise<{ texto: string; requestId?: string }>
  /** Close voice mode and go back to the chat. */
  onCerrar: () => void
  /** What the employee is using NOW, in the person's language. The chat knows it, since
   *  it receives its events; here it is only painted in the orbit. */
  orbitas?: string[]
  /** Is a lookup in progress? */
  trabajando?: boolean
  /** A REAL progress update, written by the employee while it works. `n` goes up with each
   *  one so it is said once and not on every repaint. */
  avance?: { n: number; texto: string; fuente: 'dice' | 'herramienta' | 'piensa' } | null
  /** WHAT WAS ALREADY SAID IN WRITING in this thread. The chat provides it, since it holds
   *  it —the employee does not expose history—, and it travels with the credential so the
   *  server puts it into the instructions. Without this the conversation was born blank and
   *  continued nothing that was written: from outside it looks like «it lost the session». */
  hiloEscrito?: () => { de: string; texto: string }[]
  /** THE DEVICE, in one piece and in the PARENT. They are the three sides of the same gate
   *  and that is why they go together: the chat is the one that receives the employee's
   *  request, the one that has the device, and —this is what was missing— the only one
   *  that can answer it, because the employee waits inside its call with an id only the
   *  chat knows.
   *
   *  It used to be decided here, and decided badly: the conversation read the device on
   *  its own and returned a SENTENCE («It is at latitude 40.4…»), and that sentence was
   *  sent to the employee through a gate that expects NUMBERS. The server sees no latitude
   *  in a text, so it stored it as «not available» and the employee still did not know
   *  where you were after you had said yes. Nothing failed: it simply never arrived. */
  /** Read from the device what the employee asks for. Returns the sentence to be told;
   *  the real data is kept by the parent to answer the employee. */
  leerDelAparato?: (que: string, paraQue: string) => Promise<string>
  /** Closes the employee's request: with what was read, or with the reason why not. */
  onAparato?: (texto: string) => void
  /** THE EMPLOYEE ASKS FOR SOMETHING FROM THE DEVICE —where you are— mid-work. Through the
   *  written gate it resolves by itself; when talking, you have to be told first and your
   *  answer awaited, which is what the machine does. `n` goes up with each request. */
  pideAlAparato?: { n: number; que: string; paraQue: string } | null
}

export default function VozEnVivo({ abierta, onPeticion, onCerrar, orbitas = [], trabajando,
                                    avance, hiloEscrito, leerDelAparato, onAparato,
                                    pideAlAparato }: Props) {
  const { t } = useTranslation()
  const [estado, setEstado] = useState<EstadoBola>('reposo')
  const [rotulo, setRotulo] = useState('')
  // CAN IT SPEAK YET? From opening until the session is up a few seconds pass
  // —microphone, credential, WebRTC: measured, five— and the label said «Listening · speak
  // whenever you want» from the very first instant. So people spoke and nobody heard
  // them. What is not ready is not announced as ready.
  const [listo, setListo] = useState(false)
  const [fallo, setFallo] = useState('')
  const [oculta, setOculta] = useState(false)
  const [calibrado, setCalibrado] = useState<Calibrado | null>(leeCalibrado)
  const [ajustando, setAjustando] = useState(false)
  // PUSH TO TALK, BY DEFAULT (12.50.0), just as on mobile. An open mic is comfortable in
  // a silent room and a trap anywhere else: a nearby conversation opens a turn, the
  // employee answers nobody and it gets paid for. Whoever wants hands-free says so once
  // and this browser remembers it.
  const [pulsar, setPulsar] = useState<boolean>(() => {
    try {
      const v = localStorage.getItem(COMO_SE_HABLA)
      return v === null ? true : v === '1'
    } catch { return true }
  })
  useEffect(() => {
    try { localStorage.setItem(COMO_SE_HABLA, pulsar ? '1' : '0') } catch { /* sin guardar */ }
  }, [pulsar])
  const [pulsado, setPulsado] = useState(false)
  const [entrada, setEntrada] = useState<MediaStream | null>(null)
  const [salida, setSalida] = useState<MediaStream | null>(null)
  const [ultimo, setUltimo] = useState<TurnoHablado | null>(null)
  const [gastado, setGastado] = useState(0)
  // The log of what happens inside, so it can be copied and pasted as is.
  // It records what IS HEARD, not what it was told to say: when it stored the order
  // instead of the result, two keys had to be cross-checked by hand to discover that it
  // sometimes said something else —once, 444 characters of advice nobody had looked up—.
  const [registro, setRegistro] = useState<{ seg: string; k: string; v: string }[]>([])
  const arranque = useRef(Date.now())
  const apunta = useCallback((k: string, v: unknown = '') => {
    const seg = ((Date.now() - arranque.current) / 1000).toFixed(1) + 's'
    setRegistro((x) => [...x, { seg, k, v: String(v ?? '').slice(0, 4000) }].slice(-400))
  }, [])
  const [copiado, setCopiado] = useState(false)
  const [verRegistro, setVerRegistro] = useState(false)

  /** The color of each line according to what it tells. The ones that matter —what was
   *  said, what the employee was asked, the errors— are read without searching; the rest
   *  steps aside without disappearing. */
  function pinta(clave: string) {
    if (clave.startsWith('TÚ DICES')) return { raya: 'primary.main', color: 'primary.main', fuerte: true }
    if (clave.startsWith('ÉL DICE')) return { raya: 'info.main', color: 'info.main', fuerte: true }
    if (clave.startsWith('→') || clave.startsWith('←')) return { raya: 'success.main', color: 'success.main', fuerte: true }
    if (clave.includes('ERROR') || clave.includes('NO HE PODIDO')) return { raya: 'error.main', color: 'error.main', fuerte: true }
    return { raya: 'divider', color: 'text.disabled', fuerte: false }
  }

  // THE POCKET, now without rules. TEN flags used to live here —`enVuelo`, `respondiendo`,
  // `pendiente`, `aparcado`, `decision`, `hablado_en`, `ultimoContado`, `avisado`,
  // `escritoContado`, `abierta`— that together decided who spoke and when. Ten things to
  // reason about at once, written from seven different places: that is why every fix
  // moved the problem instead of closing it.
  //
  // What is left is what really belongs to this screen: the connection, the microphone,
  // what was spoken so it travels with the request, and what is needed to paint and bill.
  const r = useRef<{
    pc: RTCPeerConnection | null; canal: RTCDataChannel | null
    micro: MediaStream | null; pista: MediaStreamTrack | null
    pulsado: boolean; hablado: TurnoHablado[]; desde: number; duro: number
    dispositivo?: string
    /** How many written-chat turns it has already been told, so as not to repeat them. */
    escritoContado: number
    /** The turn in progress, for the cost ledger: it is what turns «answered from
     *  memory» into something that can be COUNTED. */
    turno: { dicho: string; conHerramienta: boolean; exigida: boolean }
    /** The last progress update told, so it is not said twice on repaint. */
    ultimoAvance: number
    /** The last request from the employee to the device already handled. */
    ultimoAparato: number
    /** Does the SERVER have a live response? It is a FACT —its events say so— and not the
     *  machine's intention, which marks it when deciding to speak. Confusing them gave
     *  «Cancellation failed: no active response found». */
    servidorHablando: boolean
    /** THE CONTRACT FOR HOW THE EMPLOYEE'S THINGS ARE TOLD, as the server sends it.
     *  It is kept because it is needed in EVERY on-screen response: per-response
     *  instructions replace the session's, so without putting it in front again it speaks
     *  without a contract — and that means reading bullets, links and ids out loud. */
    comoSeCuenta: string
    /** THE NON-NEGOTIABLE CORE, from the server, and it goes in front of ALL responses —the
     *  literal ones and the asides too—. Without it, while speaking not even «do not say
     *  what you cannot do» is in force: it twice made up a limit of its own with the freshly
     *  fetched data right in front of it (21/09/2026). */
    nucleo: string
    modelo?: string; gastado: number; acompana: string
  }>({ pc: null, canal: null, micro: null, pista: null, pulsado: false, hablado: [],
       turno: { dicho: '', conHerramienta: false, exigida: false },
       desde: 0, duro: 0, ultimoAvance: 0,
       servidorHablando: false, ultimoAparato: 0, gastado: 0, acompana: 'hechos',
       comoSeCuenta: '', nucleo: '', escritoContado: 0 })

  // OPENING A RESPONSE IS DONE THROUGH HERE, AND ONLY THROUGH HERE.
  //
  // THE MEASURED FAILURE (20/09/2026, seen on mobile and with the same defect here):
  // «Conversation already has an active response in progress: resp_EQD… Wait until the
  // response is finished». `response.create` was sent from SEVEN places —the
  // transcription, the device function, the second rejected lookup, the employee's
  // answer and the three that keep company while it works— and none checked whether one
  // was already live. The company-keeping ones are the worst: they fire on a timer, right
  // while the employee is working.
  //
  // And `respondiendo` lied: it switched on when `response.created` arrived —AFTER our
  // send—, so two sends at the same instant both saw it off.
  // Now it is marked ON SENDING, which is when there really starts to be one.
  //
  // If one is already live, the new one is not thrown away: it is kept and opened as soon
  // as that one closes. Losing it would be worse than the error — the person spoke and
  // nobody answers. `abrirRespuesta`, `callarle` and `exigeHerramienta` lived here and no
  // longer exist: opening a response, silencing and deciding are now effects of the
  // machine, and the adapter executes them. Three fewer helpers that could be called from
  // seven different places.

  const anota = useCallback((de: 'tu' | 'el', texto: string) => {
    r.current.hablado.push({ de, texto })
    setUltimo({ de, texto })
  }, [])

  const aplicaMicro = useCallback(() => {
    const p = r.current.pista
    if (!p) return
    // It listens if the mode is open, the tab is visible, you are NOT adjusting the
    // microphone and —in push mode— with the key held down. There is NO pause button: if
    // the mode is open it listens to you, and the X closes it. Two controls for the same
    // thing only force you to remember which of the two states you left it in.
    //
    // The adjusting part matters: while testing the mic you say «testing, testing», and
    // without this the agent hears it, transcribes it and answers you. The panel opens its
    // OWN stream for the meter, so the bar keeps moving even though the agent is muted.
    p.enabled = abierta && !oculta && !ajustando && (!pulsar || r.current.pulsado)
  }, [abierta, oculta, ajustando, pulsar])
  useEffect(() => { aplicaMicro() }, [aplicaMicro])
  // I AM HOLDING. The mic reads it from the pocket (`r.current`, without repainting) and
  // the screen from the state: they are the same truth told to two places, so it is
  // changed in ONE place. Until 12.50.0 only the pocket existed, and that is why the button
  // could not show whether it was hearing you — with the keyboard you could tell from the
  // orb, but a button that does not respond to the finger looks broken.
  const apretar = useCallback((v: boolean) => {
    if (r.current.pulsado === v) return
    r.current.pulsado = v
    setPulsado(v)
    aplicaMicro()
  }, [aplicaMicro])
  // `r.current.abierta` no longer exists: whether you are in or out is known by the
  // machine, and it is a named phase (`cerrado`) instead of a flag you had to remember to
  // copy when rebuilding the pocket.

  // ON REOPENING, WHAT WAS WRITTEN IN THE MEANTIME.
  //
  // The thread travels with the credential… and the credential is requested ONCE:
  // `arrancar` stops when `r.current.pc` is non-null, and the session is kept alive on
  // purpose —it is what makes it remember when you come back—. So the written thread only
  // arrived the very first time the microphone was pressed, and everything written later
  // never got in. Measured on 20/09/2026: you give it recommendations in writing, open
  // voice and it does not know what you are talking about.
  //
  // The session is not rebuilt to fix it —that would throw away the whole context—: it is
  // TOLD what is new, which is what anyone would do when coming back to a conversation.
  useEffect(() => {
    if (!abierta) return
    const todo = hiloEscrito?.() || []
    const nuevos = todo.slice(r.current.escritoContado)
    r.current.escritoContado = todo.length
    const canal = r.current.canal
    // On the FIRST opening there is no channel yet —the session is created afterwards—, so
    // here only the mark is noted and the credential carries the thread. From the second
    // one onwards, the channel exists and it goes in through here. A single path, no duplication.
    if (!nuevos.length || canal?.readyState !== 'open') return
    const dicho = nuevos
      .map((x) => `${x.de === 'persona' ? 'Ella escribió' : 'Tú contestaste'}: «${x.texto.slice(0, 320)}»`)
      .join('\n')
    apunta('· le pongo al día de lo escrito', `${nuevos.length} turnos`)
    canal.send(JSON.stringify({
      type: 'conversation.item.create',
      item: {
        type: 'message', role: 'system',
        content: [{ type: 'input_text', text:
          'Mientras no hablabais, en el chat escrito de ESTA misma conversación se dijo '
          + `esto:\n${dicho}\n\nSigue desde ahí: es la misma conversación por otra `
          + 'puerta. No lo resumas de vuelta ni saludes como si empezarais ahora.' }],
      },
    }))
    // And NO response is opened: it is context, not a turn. Opening one would make it
    // start talking by itself as soon as you come back, without you saying anything.
    // WITHOUT `hiloEscrito` IN THE DEPENDENCIES, and on purpose: it is a new function on
    // every chat repaint, so this effect fired again and again and put the same context
    // block into the session nonstop —measured: five times in one turn—.
    // The trigger has to be OPENING, which is what bringing it up to date means.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [abierta])

  // ON LEAVING, THE MICROPHONE IS RELEASED, muting it is not enough.
  //
  // The session stays open on purpose —so that it remembers what was said when you come
  // back— and until now the track was only set to `enabled = false`. The browser shows the
  // «this tab is using the microphone» dot while the track is ALIVE, whether or not it is
  // muted: asked live, «when I leave it stays like that». And the person is right to
  // distrust it —what the browser says is true: the mic was still captured with nobody in front—.
  //
  // So on closing the track is stopped, and on coming back another one is requested and
  // swapped into the same connection (`replaceTrack`, the same path as switching
  // microphones). The permission is already granted, so it is not asked again; what is
  // recovered is the session, which is what mattered.
  // LEAVING MEANS SILENCING IT, and releasing the mic does not silence it. Both things are
  // now emitted by the machine as effects of `ui.cerrar` —`callar` first, `soltarMicro`
  // after— and the adapter executes them. Nothing is left to do here: what was here was
  // this same order written by hand, and written by hand it was forgotten once.

  useEffect(() => {
    if (!abierta || !r.current.pc || r.current.micro) return
    let vivo = true
    void (async () => {
      try {
        const nuevo = await navigator.mediaDevices.getUserMedia({
          audio: {
            ...(r.current.dispositivo ? { deviceId: { exact: r.current.dispositivo } } : {}),
            echoCancellation: true, noiseSuppression: true, autoGainControl: true,
          },
        })
        if (!vivo) { nuevo.getTracks().forEach((p) => p.stop()); return }
        const pista = nuevo.getAudioTracks()[0]
        const emisor = r.current.pc?.getSenders().find((x) => x.track?.kind === 'audio')
        if (!emisor) { nuevo.getTracks().forEach((p) => p.stop()); return }
        await emisor.replaceTrack(pista)
        r.current.micro = nuevo
        r.current.pista = pista
        setEntrada(nuevo)
        aplicaMicro()
        apunta('· vuelvo a coger el micrófono', 'volviste a la conversación')
      } catch (ex) {
        setFallo(t('voz.cal.sinMicro', 'No he podido abrir el micrófono: {{err}}')
          .replace('{{err}}', String((ex as Error).name)))
      }
    })()
    return () => { vivo = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [abierta])

  // Space = talk, only in «while I hold». It is the only mode that really guarantees that
  // only you are heard: filtering by volume sounds reasonable and breaks the turn
  // detector, which needs the continuous audio to judge whether you have finished.
  useEffect(() => {
    const tecla = (v: boolean) => (ev: KeyboardEvent) => {
      if (ev.code !== 'Space' || !pulsar) return
      const d = ev.target as HTMLElement
      if (d && ['INPUT', 'TEXTAREA', 'SELECT'].includes(d.tagName)) return
      ev.preventDefault()
      apretar(v)
    }
    const a = tecla(true); const b = tecla(false)
    document.addEventListener('keydown', a); document.addEventListener('keyup', b)
    return () => { document.removeEventListener('keydown', a); document.removeEventListener('keyup', b) }
  }, [pulsar, aplicaMicro])

  // Refreshing or closing without stopping left the session ALIVE on OpenAI's side and the
  // mic taken: on coming back there were two listening at once and everything behaved oddly.
  useEffect(() => {
    // `vivo` is not excessive caution: in development React mounts the effect, cleans it
    // up and mounts it again (StrictMode). Without this TWO voice sessions were opened and
    // the employee spoke to you twice, once per connection. And since starting takes time
    // —mic permission, credential, negotiation—, it has to be checked AFTER each wait: if
    // you left in the meantime, what was just created is closed instead of being left
    // orphaned and playing.
    // THIS effect does not depend on `abierta` on purpose, and it matters: when it did,
    // closing voice mode ran its cleanup —closing the session and releasing the mic— and
    // on reopening the start condition no longer held, because `pc` existed even though it
    // was closed. Result: the orb appeared and heard nothing. The session is opened and
    // closed with the SCREEN, not with the button.
    const salir = () => { r.current.pc?.close(); r.current.micro?.getTracks().forEach((p) => p.stop()) }
    window.addEventListener('beforeunload', salir)
    window.addEventListener('pagehide', salir)
    return () => {
      window.removeEventListener('beforeunload', salir)
      window.removeEventListener('pagehide', salir)
      salir()
    }
  }, [])

  // The start, the first time it is opened. No cleanup: closing voice mode does not close
  // the session, it only stops painting it and mutes the mic, and so on coming back the
  // model remembers what was said. `vivo` survives StrictMode's double mount.
  useEffect(() => {
    // No «I am already starting» lock, and that is deliberate: with it, in StrictMode's
    // double mount the second attempt was blocked by the first —which also gets aborted—
    // and NEITHER started. What makes it safe for both to run is that `arrancar` builds
    // everything in LOCAL variables and only hands over the session at the end: the aborted
    // one throws away its own and the one that arrives stays. A non-null `pc` does stop it,
    // and that is what prevents opening a second session when voice mode is reopened.
    if (!abierta || r.current.pc) return
    let vivo = true
    void arrancar(() => vivo)
    return () => { vivo = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [abierta])

  // If you switch to another tab, it stops listening: hidden, it kept hearing the room and
  // answering whatever it caught, with nobody in front. On coming back it does not resume by itself.
  useEffect(() => {
    // With the tab hidden it kept hearing the room and answering whatever it caught,
    // with nobody in front. It mutes by itself and comes back when you come back: it is not
    // your decision, so it has no button.
    const cambio = () => {
      const fuera = document.visibilityState === 'hidden'
      setOculta(fuera)
      // Literals and not a ternary: the layer-7 guard looks for the SEND form so as not to
      // accept a name that only appears inside an `if`, and a ternary hides it just the
      // same. Writing it like this costs one line and can be seen from outside.
      if (fuera) motor.current?.enviar({ t: 'ui.oculta' })
      else motor.current?.enviar({ t: 'ui.visible' })
    }
    document.addEventListener('visibilitychange', cambio)
    return () => document.removeEventListener('visibilitychange', cambio)
  }, [])

  // WHAT THE EMPLOYEE KEEPS TELLING is now an EVENT, not three scattered effects. The
  // machine decides whether it sounds —it may already be speaking, or it may repeat
  // itself— and the engine's clock releases the heartbeat if it has been silent. Here it is only translated.
  useEffect(() => {
    if (!abierta || !avance || avance.n === r.current.ultimoAvance) return
    r.current.ultimoAvance = avance.n
    if (r.current.acompana === 'nada') { apunta('· callo (así lo pide su perfil)', ''); return }
    motor.current?.enviar({ t: 'emp.escribe', texto: avance.texto })
  }, [abierta, avance, apunta])

  useEffect(() => {
    if (!abierta || !pideAlAparato || pideAlAparato.n === r.current.ultimoAparato) return
    r.current.ultimoAparato = pideAlAparato.n
    motor.current?.enviar({ t: 'emp.pide_al_dispositivo',
                            que: pideAlAparato.que, paraQue: pideAlAparato.paraQue })
  }, [abierta, pideAlAparato])

  // OPENING AND CLOSING are machine events, not loose effects. Before, these were four
  // `useEffect`s that stepped on each other.
  useEffect(() => {
    motor.current?.enviar(abierta ? { t: 'ui.abrir' } : { t: 'ui.cerrar' })
  }, [abierta])

  // ── The engine, which is now the one that decides ────────────────────────────
  //
  // This component no longer has rules. What it does is TRANSLATE —raw events from the
  // voice server into machine events— and EXECUTE the effects the machine emits.
  // The ten flags that lived here (`enVuelo`, `respondiendo`, `pendiente`,
  // `aparcado`, `decision`…) no longer exist: their job is done by a named state that
  // can be tested without a browser.
  const motor = useRef<Motor | null>(null)

  const deLaSuperficie = useCallback((): DeLaSuperficie => ({
    mandar: (m) => {
      const c = r.current.canal
      if (c?.readyState !== 'open') return false
      c.send(JSON.stringify(m)); return true
    },
    // THE FACT, not the intention: the server's `response.created` switches it on and
    // `response.done`/`cancelled` switch it off. Asking the machine gave «yes» about
    // something that did not exist yet —it marks it when it DECIDES to speak— and
    // cancelling it returned «Cancellation failed: no active response found», with the
    // previous one still alive and the new one queuing behind. Two responses in a row, out of order.
    estaSonando: () => r.current.servidorHablando,
    pedirMicro: () => { /* set up by `arrancar`, which already had that job */ },
    soltarMicro: () => {
      r.current.micro?.getTracks().forEach((x) => x.stop())
      r.current.micro = null; r.current.pista = null; setEntrada(null)
      apunta('· suelto el micrófono', '')
    },
    micro: (on) => { if (r.current.pista) r.current.pista.enabled = on },
    pedirCredencial: () => { /* done by `arrancar`; the engine only requests it on opening */ },
    tirarSesion: () => { r.current.pc?.close(); r.current.pc = null; r.current.canal = null },
    // THE GATE. What you just said goes through the SAME reception that governs the written
    // chat, and whatever it answers is executed as is. The conversationalist no longer decides anything.
    pedirDecision: (texto) => {
      void (async () => {
        let d: { accion?: string; texto?: string; capacidad?: string
                 argumentos?: Record<string, unknown> } = {}
        try {
          const resp = await apiFetch('/api/voz/turno', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ texto, charla: r.current.hablado.slice(-7, -1).map((x) => ({
              de: x.de === 'tu' ? 'persona' : 'empleado', texto: x.texto })) }),
          })
          if (resp.ok) d = await resp.json()
        } catch { /* without a decision it delegates: the machine knows */ }
        if (!d.accion) { motor.current?.enviar({ t: 'srv.decision_falla' }); return }
        apunta(`· la recepción dice: ${d.accion}`, d.texto || '')
        motor.current?.enviar({ t: 'srv.decision',
          accion: d.accion as 'contesto' | 'pregunto' | 'atajo' | 'delego',
          texto: d.texto || texto, capacidad: d.capacidad, argumentos: d.argumentos })
      })()
    },
    pedirAlEmpleado: (p) => {
      // The conversation that led to THIS request travels with it and the mailbox is emptied.
      const charla = r.current.hablado
      r.current.hablado = []
      r.current.turno = { dicho: p.texto, conHerramienta: true, exigida: true }
      apunta('→ PIDE AL EMPLEADO', p.texto)
      void (async () => {
        try {
          const res = await onPeticion(p.texto, charla, {
            capacidad: p.capacidad, argumentos: p.argumentos })
          apunta('← EL EMPLEADO CONTESTA', res.texto)
          // If it carries `requestId`, its turn was left PAUSED: the next thing you say
          // resumes it instead of opening a new one, which is what left it hanging.
          motor.current?.enviar(res.requestId
            ? { t: 'emp.pregunta', requestId: res.requestId, texto: res.texto }
            : { t: 'emp.responde', texto: res.texto })
        } catch (ex) {
          motor.current?.enviar({ t: 'emp.falla',
            texto: t('voz.empleadoFalla', 'Me ha dado un error al mirarlo.') })
          apunta('ERROR del empleado', String((ex as Error)?.message || ex))
        }
      })()
    },
    responderAlEmpleado: (requestId, texto) => {
      apunta('→ REANUDA SU TURNO', texto)
      void (async () => {
        try {
          const res = await onPeticion(texto, [], { requestId })
          motor.current?.enviar(res.requestId
            ? { t: 'emp.pregunta', requestId: res.requestId, texto: res.texto }
            : { t: 'emp.responde', texto: res.texto })
        } catch {
          motor.current?.enviar({ t: 'emp.falla',
            texto: t('voz.empleadoFalla', 'Me ha dado un error al mirarlo.') })
        }
      })()
    },
    contestarAlAparato: (texto) => { apunta('← EL DISPOSITIVO CONTESTA', texto); onAparato?.(texto) },
    pedirAlAparato: (que, paraQue) => {
      apunta(`→ PIDE AL DISPOSITIVO · ${que}`, paraQue)
      void (async () => {
        const salida = await leerDelAparato?.(que, paraQue)
        motor.current?.enviar({ t: 'disp.resultado',
          texto: salida || 'No se ha podido. Sigue sin ello.' })
      })()
    },
    abandonar: () => { r.current.hablado = []; apunta('· abandono lo que hubiera', '') },
    avisar: (texto) => { setFallo(texto); apunta('AVISO', texto) },
    alCambiar: (e) => {
      setEstado(e.fase === 'oyendo' ? 'oyendo'
        : enVuelo(e.voz) ? 'hablando'
        : (e.fase === 'pidiendo' || e.fase === 'decidiendo' || e.fase === 'al_dispositivo')
          ? 'trabajando' : 'reposo')
    },
    // THE TICK IS NOT LOGGED. It beats once per second and the log exists to be read at a
    // glance and pasted whole: with the tick inside, a one-minute conversation is sixty
    // lines that say nothing covering the six that do. What the tick CAUSES (a notice, an
    // expired wait) is seen, because those are effects.
    anotarEvento: (ev) => { if (ev.t !== 'reloj.tic') apunta(`· ${ev.t}`, '') },
    comoContarUnAvance: (texto) => t('voz.instruccionAvanceReal', '').replace('{{x}}', texto.slice(0, 1200)),
    comoSeCuentaLoDelEmpleado: () => r.current.comoSeCuenta,
    nucleoDeVoz: () => r.current.nucleo,
  }), [apunta, onPeticion, leerDelAparato, onAparato, t])

  // IT IS CREATED ON MOUNT, not during render: reading and writing a ref while painting is
  // what React forbids, and rightly so —in strict mode the render runs twice—. Everything
  // else uses `motor.current?.`, which is safe before it exists: events cannot arrive until
  // `arrancar` opens the channel, and that is also an effect.
  useEffect(() => {
    motor.current = crearMotor(adaptador(deLaSuperficie()))
    // The clock: deadlines and the heartbeat are handled by the engine, here it only gets ticks.
    const reloj = window.setInterval(() => motor.current?.tic(Date.now()), 1000)
    return () => { window.clearInterval(reloj); motor.current?.parar() }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])


  const bola: EstadoBola = trabajando ? 'trabajando' : estado

  // Closed it is not painted, but the component STAYS mounted with its session: it is what
  // makes it remember what was said when you come back.
  if (!abierta) return null

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', alignItems: 'center',
               justifyContent: 'center', flex: 1, minHeight: 420, gap: 2, py: 3 }}>
      {fallo && <Alert severity="warning" onClose={() => setFallo('')}>{fallo}</Alert>}

      {/* Off until it really hears: the label is read, but what is LOOKED AT is the orb,
          and a live orb saying «preparing» reads as «you can go ahead». */}
      {/* TOUCHING THE ORB IS THE EMERGENCY EXIT: it goes back to calm from wherever,
          without asking anyone —not the model, not the reception, not the network—. It is
          the only thing that can be promised when everything else fails, and that is why it
          goes on the biggest thing on the screen, which is what people already touch by instinct. */}
      <Box
        onClick={() => motor.current?.enviar({ t: 'ui.reposo' })}
        role="button" tabIndex={0}
        onKeyDown={(ev) => { if (ev.key === 'Enter' || ev.key === ' ') motor.current?.enviar({ t: 'ui.reposo' }) }}
        aria-label={t('voz.volverALaCalma', 'Callar y volver al reposo')}
        sx={{ opacity: listo ? 1 : 0.35, transition: 'opacity .3s', cursor: 'pointer',
              borderRadius: '50%', '&:focus-visible': { outline: '2px solid', outlineColor: 'primary.main' } }}
      >
        <BolaDeVoz estado={bola} orbitas={trabajando ? orbitas : []}
                   entrada={entrada} salida={salida} tam={340} />
      </Box>

      <Typography variant="body2"
        color={listo ? 'text.secondary' : 'warning.main'}
        sx={{ minHeight: 24, fontWeight: listo ? 400 : 600 }}>
        {!listo ? t('voz.preparando', 'Preparando la conversación · todavía NO te oye')
          : ajustando ? t('voz.ajustandoMicro', 'Ajustando el micrófono · no te escucha')
          : oculta ? t('voz.enOtraPestana', 'Estás en otra pestaña · no te escucha')
          : bola === 'trabajando' ? t('voz.trabajando', 'Está en ello…')
          : bola === 'hablando' ? t('voz.hablando', 'Hablando…')
          : bola === 'oyendo' ? t('voz.teOye', 'Te está oyendo…')
          : rotulo || t('voz.escuchando', 'Escuchando · habla cuando quieras')}
      </Typography>

      {/* The last thing said, so you know it understood you. The whole conversation is not
          painted here: its place is the chat, hanging from the request. */}
      {ultimo && (
        <Typography variant="caption" color="text.disabled"
                    sx={{ maxWidth: 520, textAlign: 'center' }}>
          {(ultimo.de === 'tu' ? '“' : '') + ultimo.texto + (ultimo.de === 'tu' ? '”' : '')}
        </Typography>
      )}

      <Stack direction="row" spacing={1} alignItems="center">
        {/* PRESS AND HOLD WITH THE FINGER TOO. Space still works, but since «push» is the
            default (12.50.0) a touch laptop or a tablet would have been left WITHOUT a way
            to talk: there is no space bar there. `pointer` is used, which covers mouse and
            finger with the same code, and `onPointerLeave` because sliding off the button
            without releasing leaves the mic open forever. */}
        {pulsar ? (
          <Button
            variant="contained"
            color={pulsado ? 'success' : 'primary'}
            onPointerDown={() => apretar(true)}
            onPointerUp={() => apretar(false)}
            onPointerLeave={() => apretar(false)}
            onPointerCancel={() => apretar(false)}
            sx={{ borderRadius: 8, px: 3, textTransform: 'none', userSelect: 'none' }}
          >
            {pulsado
              ? t('voz.teEscucha', 'Te escucha')
              : t('voz.mantenPulsado', 'Mantén pulsado para hablar')}
          </Button>
        ) : (
          <Chip color="success" variant="outlined"
                label={t('voz.manosLibres', 'Manos libres · habla cuando quieras')} />
        )}
        {/* And THE WAY BACK, which was the only thing missing to be able to start in «push»:
            a mode you cannot leave is not a mode, it is a trap. */}
        <Tooltip title={(pulsar
          ? t('voz.aManosLibres', 'Pasar a manos libres')
          : t('voz.modoPulsar', 'Hablar solo mientras pulso')) as string}>
          <IconButton color={pulsar ? 'primary' : 'default'}
                      onClick={() => { setPulsar((v) => !v); apretar(false) }}>
            <KeyboardIcon />
          </IconButton>
        </Tooltip>
        <Tooltip title={t('voz.volverAlChat', 'Volver al chat') as string}>
          <IconButton onClick={onCerrar}><CloseIcon /></IconButton>
        </Tooltip>
      </Stack>

      <Typography
        variant="caption" component="button"
        onClick={() => { setAjustando(true); apunta('· dejo de escuchar', 'estás ajustando el micrófono') }}
        sx={{ border: 0, bgcolor: 'transparent', cursor: 'pointer', color: 'text.disabled',
              opacity: 0.6, fontSize: 11, '&:hover': { opacity: 1, color: 'text.secondary' } }}
      >
        {/* A raw number («room measured at 0.0031») tells nobody anything: what matters is
            whether there is noise or not. */}
        {calibrado
          ? t('voz.ajustarMedido', 'Ajustar el micrófono · {{f}}').replace('{{f}}',
              calibrado.escucha === 'high'
                ? t('voz.salaSilenciosa', 'poco ruido alrededor')
                : t('voz.salaRuidosa', 'bastante ruido alrededor'))
          : t('voz.ajustar', 'Ajustar el micrófono')}
      </Typography>

      <CalibrarMicro
        abierto={ajustando}
        actual={calibrado}
        onCerrar={() => { setAjustando(false); apunta('· vuelvo a escuchar', 'cerraste el ajuste') }}
        onListo={(c) => {
          setAjustando(false)
          apunta('· vuelvo a escuchar', 'ajuste terminado')
          setCalibrado(c)
          try { localStorage.setItem(GUARDADO, JSON.stringify(c)) } catch { /* sin guardar */ }
          apunta('sala medida a mano', `ruido ${c.fondo.toFixed(4)} · paciencia ${c.escucha}`)
          void aplicaCalibrado(c)
        }}
      />
      <Stack direction="row" spacing={1} alignItems="center">
        {pulsar && <Chip size="small" variant="outlined"
                         label={t('voz.oEspacio', 'o mantén pulsado el espacio')} />}
        {/* THE SPENDING IS NOT PAINTED. It is still recorded in the cost ledger —that does
            not change— but watching it run while you talk turns a conversation into a
            taximeter. Its place is Operaciones → Consumo, which is where it is looked at
            on purpose. */}
      </Stack>

      {/* The inner workings, very discreet: while you talk they are not the protagonist, and
          taking the centre broke the orb's calm. It opens only if you look for it. */}
      <Box sx={{ mt: 'auto', pt: 3, width: '100%', maxWidth: 760 }}>
        {!verRegistro ? (
          <Typography
            variant="caption" component="button" onClick={() => setVerRegistro(true)}
            sx={{ display: 'block', mx: 'auto', border: 0, bgcolor: 'transparent',
                  cursor: 'pointer', color: 'text.disabled', opacity: 0.55, fontSize: 11,
                  '&:hover': { opacity: 1, color: 'text.secondary' } }}
          >
            {`${t('voz.registro', 'Qué está pasando por dentro')} · ${registro.length}`}
          </Typography>
        ) : (
          <Paper variant="outlined" sx={{ borderRadius: 2, overflow: 'hidden' }}>
            <Stack direction="row" alignItems="center" spacing={1}
                   sx={{ px: 1.5, py: 0.75, borderBottom: 1, borderColor: 'divider' }}>
              <Typography variant="caption" sx={{ flex: 1, color: 'text.secondary',
                          textTransform: 'uppercase', letterSpacing: '.06em', fontSize: 10 }}>
                {t('voz.registro', 'Qué está pasando por dentro')}
              </Typography>
              <Button size="small" startIcon={<ContentCopyIcon sx={{ fontSize: 14 }} />}
                      sx={{ textTransform: 'none', fontSize: 12 }}
                      onClick={() => {
                        void navigator.clipboard.writeText(
                          registro.map((x) => `${x.seg}\t${x.k}\t${x.v}`).join('\n'))
                        setCopiado(true); window.setTimeout(() => setCopiado(false), 1500)
                      }}>
                {copiado ? t('voz.copiado', 'Copiado') : t('voz.copiar', 'Copiar todo')}
              </Button>
              <IconButton size="small" onClick={() => setVerRegistro(false)}>
                <CloseIcon sx={{ fontSize: 16 }} />
              </IconButton>
            </Stack>
            <Box sx={{ maxHeight: 300, overflow: 'auto', px: 1.5, py: 1 }}>
              {registro.length === 0 && (
                <Typography variant="caption" color="text.disabled">
                  {t('voz.registroVacio', 'Todavía no ha pasado nada.')}
                </Typography>
              )}
              {registro.map((x, i) => {
                const e = pinta(x.k)
                return (
                  <Box key={i} sx={{ display: 'flex', gap: 1.25, py: 0.4,
                                     borderTop: i ? 1 : 0, borderColor: 'divider' }}>
                    <Box sx={{ minWidth: 42, textAlign: 'right', fontSize: 11,
                               color: 'text.disabled', fontVariantNumeric: 'tabular-nums',
                               pt: '2px' }}>{x.seg}</Box>
                    <Box sx={{ width: 3, borderRadius: 2, bgcolor: e.raya, flexShrink: 0 }} />
                    <Box sx={{ minWidth: 0, flex: 1 }}>
                      <Typography sx={{ fontSize: 11, fontWeight: e.fuerte ? 700 : 500,
                                        color: e.color, letterSpacing: '.02em' }}>
                        {x.k}
                      </Typography>
                      {x.v && (
                        <Typography sx={{ fontSize: 12.5, color: 'text.secondary',
                                          wordBreak: 'break-word', whiteSpace: 'pre-wrap' }}>
                          {x.v}
                        </Typography>
                      )}
                    </Box>
                  </Box>
                )
              })}
            </Box>
          </Paper>
        )}
      </Box>
    </Box>
  )

  /** An event of the voice session. */
  /** Applies a new calibration WITHOUT closing the session.
   *
   *  Before, this only warned that it would be applied «next time», and it was a notice
   *  that got stuck and also half-lied: since the session survives closing and reopening
   *  voice mode, that next time never came. The session accepts having its configuration
   *  changed on the fly, and the microphone is switched by replacing the track in the
   *  connection, without renegotiating anything. */
  async function aplicaCalibrado(c: Calibrado) {
    const canal = r.current.canal
    if (canal?.readyState === 'open') {
      canal.send(JSON.stringify({
        type: 'session.update',
        session: {
          type: 'realtime',
          audio: { input: { turn_detection: {
            type: 'semantic_vad', eagerness: c.escucha,
            create_response: false, interrupt_response: false,
          } } },
        },
      }))
      apunta('· paciencia al vuelo', c.escucha)
    }
    const pc = r.current.pc
    if (!pc || c.dispositivo === r.current.dispositivo) return
    try {
      const nuevo = await navigator.mediaDevices.getUserMedia({
        audio: {
          ...(c.dispositivo ? { deviceId: { exact: c.dispositivo } } : {}),
          echoCancellation: true, noiseSuppression: true, autoGainControl: true,
        },
      })
      const pista = nuevo.getAudioTracks()[0]
      const emisor = pc.getSenders().find((x) => x.track?.kind === 'audio')
      if (!emisor) { nuevo.getTracks().forEach((p) => p.stop()); return }
      await emisor.replaceTrack(pista)
      r.current.micro?.getTracks().forEach((p) => p.stop())
      r.current.micro = nuevo
      r.current.pista = pista
      r.current.dispositivo = c.dispositivo
      setEntrada(nuevo)
      aplicaMicro()
      apunta('· micrófono cambiado al vuelo', c.dispositivo || 'el de por defecto')
    } catch (ex) {
      setFallo(t('voz.cal.sinMicro', 'No he podido abrir el micrófono: {{err}}')
        .replace('{{err}}', String((ex as Error).name)))
    }
  }

  /** The translator. It does NOT decide: it changes language and steps aside. */
  function alLlegar(ev: MessageEvent, _canal: RTCDataChannel) {
    let e: { type?: string; transcript?: string; response?: { usage?: unknown; id?: string }
             error?: { message?: string } }
    try { e = JSON.parse(ev.data) } catch { return }

    // What was consumed, to the cost ledger. It is sent on EACH response and not accumulated:
    // an accumulation window is lost entirely if you close the tab abruptly.
    if (e.type === 'response.done' && e.response?.usage) {
      void apiFetch('/api/voz/uso', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ usage: e.response.usage, model: r.current.modelo,
                               session_id: String(e.response?.id || ''),
                               dicho: r.current.turno.dicho,
                               con_herramienta: r.current.turno.conHerramienta,
                               exigida: r.current.turno.exigida }),
      }).then(async (resp) => {
        if (!resp.ok) return
        const d = await resp.json()
        r.current.gastado += Number(d?.coste_usd || 0)
        setGastado(r.current.gastado)
      }).catch(() => { /* billing can never break a conversation */ })
    }

    // What is seen on screen —what you say and what it says— is presentation, not state.
    if (e.type === 'conversation.item.input_audio_transcription.completed') {
      const dicho = String(e.transcript || '').trim()
      if (dicho) { anota('tu', dicho); apunta('TÚ DICES', dicho) }
    }
    if (e.type === 'response.output_audio_transcript.done') {
      const dicho = String(e.transcript || '').trim()
      if (dicho) { anota('el', dicho); apunta('ÉL DICE', dicho) }
    }
    if (e.type === 'error') setFallo(String(e?.error?.message || 'error en la sesión de voz'))

    // THE FACT IS RECORDED HERE, before the machine has its say: it is its events that
    // tell whether the server has something live.
    if (e.type === 'response.created') r.current.servidorHablando = true
    if (e.type === 'response.done' || e.type === 'response.cancelled'
        || e.type === 'error') r.current.servidorHablando = false

    const traducido = delServidorDeVoz(e)
    if (traducido) motor.current?.enviar(traducido)
  }

  async function arrancar(sigue: () => boolean) {
    // Everything is built in LOCAL variables and only handed to `r.current` at the end.
    //
    // Before, it was written there directly, and with StrictMode's two starts the two
    // stepped on each other: the first one's `abortar()` left `r.current` with nulls while
    // the second was already using it, and on reaching `getAudioTracks()` it blew up. The
    // failure was swallowed whole —the exception died in the `void`— so it stayed
    // «Listening» with no session and not one line in the log.
    let micro: MediaStream | null = null
    let pc: RTCPeerConnection | null = null
    const tirar = () => {
      pc?.close()
      micro?.getTracks().forEach((p) => p.stop())
      micro = null; pc = null
      // If it goes down before being up, nobody must believe they are being heard.
      setListo(false)
    }
    try {
      // If this room has never been measured, it is measured before connecting: two seconds
      // that avoid a whole conversation fighting the turn detector. If it fails, the usual
      // values are used — calibrating must never prevent talking.
      let cal = calibrado
      if (!cal) {
        setRotulo(t('voz.calibrando', 'Escuchando la sala un momento…'))
        try {
          cal = await mideLaSala()
          if (!sigue()) return tirar()
          setCalibrado(cal)
          try { localStorage.setItem(GUARDADO, JSON.stringify(cal)) } catch { /* sin guardar */ }
          apunta('sala medida', `ruido ${cal.fondo.toFixed(4)} · paciencia ${cal.escucha}`)
        } catch (ex) {
          apunta('sala sin medir, sigo igual', String((ex as Error).name))
        }
      }
      micro = await navigator.mediaDevices.getUserMedia({
        audio: {
          ...(cal?.dispositivo ? { deviceId: { exact: cal.dispositivo } } : {}),
          echoCancellation: true, noiseSuppression: true, autoGainControl: true,
        },
      })
      if (!sigue()) return tirar()
      apunta('micrófono', 'concedido')

      // THE SURFACE IS DECLARED. The server cannot know what is here —a mobile browser is
      // not the app, and a denied permission cannot be seen from outside—, so we tell it and
      // it composes the instructions and returns the functions to resolve.
      const resp = await apiFetch('/api/voz/credencial', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        // It is read ON OPENING and not on mount: what counts is what is written at the
        // moment the microphone is pressed, not what there was when the screen was painted.
        body: JSON.stringify({ canal: 'web', capacidades: await capacidadesDelNavegador(),
                               hablado: hiloEscrito?.() || [] }),
      })
      if (!resp.ok) throw new Error(`la credencial de voz devolvió ${resp.status}`)
      const cred = await resp.json()
      if (!sigue()) return tirar()
      const clave = cred?.value || cred?.client_secret?.value
      if (!clave) throw new Error('la credencial de voz vino vacía')
      apunta('credencial de voz', `ok · modelo ${cred?.modelo || '?'}`)

      pc = new RTCPeerConnection()
      const altavoz = document.createElement('audio')
      altavoz.autoplay = true
      pc.ontrack = (ev) => { altavoz.srcObject = ev.streams[0]; setSalida(ev.streams[0]) }
      // IT DROPS AND IT SAYS SO. Before, there was NOTHING that detected it: WebRTC got cut
      // —network change, suspension, tunnel— and the conversation went silent without a
      // word. The layer-7 guard found it: an event nobody sends is a part of the machine
      // that does not exist.
      pc.onconnectionstatechange = () => {
        if (pc && (pc.connectionState === 'failed' || pc.connectionState === 'disconnected')) {
          motor.current?.enviar({ t: 'voz.canal_caido' })
        }
      }
      const pista = micro.getAudioTracks()[0]
      pc.addTrack(pista)

      const canal = pc.createDataChannel('oai-events')
      canal.onopen = () => {
        canal.send(JSON.stringify({
          type: 'session.update',
          session: {
            type: 'realtime',
            instructions: cred?.ajustes?.instrucciones || '',
            // NO TOOLS, and this is what makes it true. It had two —asking the employee for
            // something and asking the device for something— and both are gone:
            //
            // · the employee is no longer called by it: the reception decides as soon as you
            //   finish speaking and the client executes. Answering from memory stops being
            //   forbidden and becomes IMPOSSIBLE, which is something else;
            // · the device is asked by the EMPLOYEE, who knows whether it needs it and what for.
            //
            // And with the list empty, `tool_choice` ceases to exist: that is the end of the
            // «required only on the first response of the turn» regime, which was a patch on
            // a model that did not need to be asked for permission, but to have the option
            // taken away.
            tools: [],
            output_modalities: ['audio'],
            audio: {
              input: {
                transcription: { model: 'gpt-4o-mini-transcribe', language: 'es' },
                // THE API DETECTS THE TURNS; ACTING, NO.
                //
                // `semantic_vad` decides by the AUDIO, and with it a throat-clearing, a chair
                // or the end of your own sentence bouncing back count as a turn. If it is also
                // allowed to act (`create_response`/`interrupt_response`), each of those noises
                // does two bad things: it CUTS it off mid-sentence and starts an answer to
                // something nobody said. Measured: three times in a row «nothing was understood
                // (silence)» and, 300 ms later, starting to improvise.
                //
                // So the turn is opened HERE, and with a fact and not a hunch: the
                // transcription of what you said brings words, or it does not. If it does, it
                // is silenced and answered; if not, NOTHING happens — it keeps talking as if
                // nothing had happened, which is exactly what we want.
                //
                // It costs what the transcription takes: 300 ms measured in the trace. That is
                // the price of not cutting it off with a noise, and it is cheap.
                turn_detection: { type: 'semantic_vad', eagerness: cal?.escucha || 'low',
                                  create_response: false, interrupt_response: false },
              },
              output: { voice: cred?.ajustes?.voz || 'coral' },
            },
          },
        }))
        setRotulo(t('voz.escuchando', 'Escuchando · habla cuando quieras'))
        setListo(true)
        // The employee decides how it is spoken to: if its profile says «only while
        // holding», it opens that way and nobody has to remember to set it every time.
        if (cred?.ajustes?.arranque === 'pulsar') setPulsar(true)
        apunta('sesión de voz',
               `abierta · voz ${cred?.ajustes?.voz || '?'} · acompaña ${cred?.ajustes?.acompana || '?'}`)
        aplicaMicro()
      }
      canal.onmessage = (ev) => { void alLlegar(ev, canal) }

      const oferta = await pc.createOffer()
      await pc.setLocalDescription(oferta)
      const sdp = await fetch(
        `https://api.openai.com/v1/realtime/calls?model=${encodeURIComponent(cred?.modelo || '')}`,
        { method: 'POST', body: oferta.sdp,
          headers: { Authorization: `Bearer ${clave}`, 'Content-Type': 'application/sdp' } })
      if (!sdp.ok) throw new Error(`OpenAI rechazó la conexión (${sdp.status})`)
      if (!sigue()) return tirar()
      await pc.setRemoteDescription({ type: 'answer', sdp: await sdp.text() })
      if (!sigue()) return tirar()

      // Only NOW is it handed over: from here on this session is the good one.
      r.current = { pc, canal, micro, pista, pulsado: false, hablado: [],
                    dispositivo: cal?.dispositivo,
                    turno: { dicho: '', conHerramienta: false, exigida: false },
                    desde: 0, duro: 0, ultimoAvance: 0, servidorHablando: false,
                    ultimoAparato: r.current.ultimoAparato,
                    escritoContado: r.current.escritoContado,
                    modelo: cred?.modelo, gastado: r.current.gastado,
                    acompana: cred?.ajustes?.acompana || 'hechos',
                    comoSeCuenta: String(cred?.ajustes?.como_se_cuenta || ''),
                    nucleo: String(cred?.ajustes?.nucleo || '') }
      // And NOW THE MACHINE IS TOLD THAT IT IS UP. Without this it stays in `arrancando`
      // forever, and that phase ignores EVERYTHING on purpose —«no conversation event is
      // accepted until finished»—: it transcribes what you say and nothing happens.
      // Measured live on 20/09/2026, and it is the failure the 38 tests could not see: they
      // test the machine, not that someone talks to it.
      motor.current?.enviar({ t: 'srv.credencial_ok' })
      setEntrada(micro)
      aplicaMicro()
    } catch (ex) {
      // A start that fails silently is the worst thing that can happen here: it stays
      // «Listening» with no session and there is nothing to look at. It is said, and logged.
      tirar()
      const motivo = (ex as Error)?.message || String(ex)
      setFallo(motivo)
      apunta('NO HE PODIDO ARRANCAR', motivo)
    }
  }
}
