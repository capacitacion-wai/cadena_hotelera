/**
 * THE SPOKEN CONVERSATION, AS A STATE MACHINE.
 *
 * A PURE reducer: an event goes in, the new state and a list of effects come out. It does
 * not touch WebRTC, the DOM, React Native or the clock — and that is why it can be tested
 * end to end in milliseconds, with no browser and no phone. Whoever runs the effects is
 * each surface's adapter.
 *
 * WHY IT EXISTS. Before, this very thing was decided by ten loose flags spread across a
 * 1,134-line component, duplicated on mobile. With that, every fix moved the problem: on
 * 20/09/2026 two answers stepping on each other was fixed, and the same change introduced
 * a notice that could step on the employee's answer. Ten flags cannot be reasoned about at
 * once; a named state can.
 *
 * THE STATE HAS TWO DIMENSIONS, and separating them is what removes the queues:
 *
 *   · `fase` — what we are doing
 *   · `voz`  — how much of ours is IN FLIGHT: nothing, being generated, or already playing
 *
 * The mother invariant comes from there: **a real answer that arrives with something in
 * flight SILENCES whatever there is and is said anyway**; an aside that arrives with
 * something in flight **is dropped**, because said twelve seconds late it is no longer true.
 * No queue to step on, no slot to overwrite, no flag to lie.
 *
 * And `voz` has THREE values because the server gives TWO facts —the response closes, the
 * speaker drains— and packing them into one word was the bug of 20/09/2026: the machine
 * believed it was silent for nineteen seconds of audio and silenced nothing before talking
 * over it.
 */

// ── The state ───────────────────────────────────────────────────────────────

export type Fase =
  | 'cerrado'              // the chat, no voice. Not one byte of audio leaves the device
  | 'arrancando'           // permission, credential, connection
  | 'escuchando'           // at rest. The only stable phase
  | 'oyendo'               // the person is speaking right now
  | 'decidiendo'           // the front desk says what to do with what was said
  | 'pidiendo'             // the request is with the employee
  | 'esperando_respuesta'  // we asked them something and are waiting for THEIR answer
  | 'al_dispositivo'       // the device is asked for what only it has
  | 'caido'                // the connection was lost

/** HOW MUCH OF OURS IS IN FLIGHT. Three values, not two, because the server gives TWO
 *  distinct facts and we were packing them into a single word:
 *
 *   · `generando` — there is an open response (`response.created`)
 *   · `sonando`   — there is audio in the speaker (`output_audio_buffer.started`)
 *
 *  THE MEASURED BUG (20/09/2026, skill 12.43.2): `response.done` set `callada`, but the
 *  speaker was still draining. In a real conversation the response closed at 139.8 s and
 *  the speaker did not stop until 159.4 s: NINETEEN seconds believing it was silent while
 *  it talked. At 142.7 s the employee's answer arrived, the machine looked at `voz`, saw
 *  `callada`, and so silenced NOTHING before opening the next one. Result: two answers
 *  on top of each other, and a whole minute of queued audio.
 *
 *  The mother invariant —«a real answer SILENCES whatever is playing»— did not fail: it
 *  was being lied to about whether something was playing. With three values, `callada`
 *  means exactly one thing: there is NOTHING of ours in flight. */
export type Voz = 'callada' | 'generando' | 'sonando'

/** Is anything of ours in flight? The question the machine really asks before speaking.
 *  Written once so nobody goes back to comparing with plain `'sonando'` and leaves out
 *  the response that is still being generated. */
export const enVuelo = (v: Voz): boolean => v !== 'callada'

export interface Ctx {
  /** An employee turn PAUSED waiting for an answer. While it lives, the next thing the
   *  person says does not go through the front desk: it is an answer, not a request. */
  requestId: string | null
  /** They were asked whether the device may be used, and what for. */
  permiso: { que: string; paraQue: string } | null
  /** Which device data were already attempted in this conversation. Once each: the
   *  catalog said «ONCE» and the model asked three times in a row, reading «don't
   *  insist» each time. A rule that is only written down is not a rule. */
  yaPedido: string[]
  /** The last thing said aloud, so as not to repeat ourselves. */
  ultimoDicho: string
  /** What the person said while the employee was working: it travels with the pending
   *  answer instead of opening a new turn that would drop another «let me look». */
  anotado: { de: 'tu' | 'el'; texto: string }[]
  /** The tab or the app is in the background: the microphone goes off, but the phase
   *  does not change. */
  oculta: boolean
  /** HOW MANY NOTICES HAVE BEEN GIVEN IN THIS WAIT. The first is not «still on it» —
   *  nothing is being continued yet—: it is «getting on it». Saying «still» first sounds
   *  like you have been at it a while and done nothing. It is counted here so the
   *  difference lives in the table and not in anyone's head. */
  avisos: number
  /** THE CLOCK LIVES IN HERE, and not in the engine, because we learnt it the hard way:
   *  the heartbeat fired 0 ms after requesting because its memory («when did the wait
   *  start») was outside the machine, in a second hidden little machine with no tests.
   *
   *  Put in the state, the 486 cells cover it like everything else, and that bug becomes
   *  inexpressible — as already happened with the queues. */
  desdeCuando: number
  /** When something was last heard from the employee. */
  ultimaSenal: number
}

export interface Estado { fase: Fase; voz: Voz; ctx: Ctx }

export function estadoInicial(): Estado {
  return {
    fase: 'cerrado',
    voz: 'callada',
    ctx: { requestId: null, permiso: null, yaPedido: [], ultimoDicho: '', anotado: [],
           oculta: false, avisos: 0, desdeCuando: 0, ultimaSenal: 0 },
  }
}

// ── The events ──────────────────────────────────────────────────────────────

export type Evento =
  // from the person
  | { t: 'ui.abrir' }
  | { t: 'ui.cerrar' }
  | { t: 'ui.oculta' }
  | { t: 'ui.visible' }
  /** THE EMERGENCY EXIT: tapping the orb. From any phase, it goes back to calm without
   *  asking anyone. It does not depend on the model, the front desk or the network —
   *  which is what makes it promisable when everything else fails. */
  | { t: 'ui.reposo' }
  // from the voice server
  | { t: 'voz.empieza_a_oir' }
  | { t: 'voz.deja_de_oir' }
  | { t: 'voz.transcripcion'; texto: string }
  | { t: 'voz.transcripcion_falla' }
  | { t: 'voz.respuesta_abierta' }
  | { t: 'voz.respuesta_cerrada' }
  | { t: 'voz.empieza_a_hablar' }
  | { t: 'voz.deja_de_hablar' }
  /** THE SPEAKER HAS BEEN DRAINED at once, which is what the server answers to the second
   *  half of `callar`. Without this event the machine would stay `sonando` forever after
   *  silencing: an effect nobody confirms. It is the sibling of layer 7's event with no
   *  producer — an event with no TRANSLATOR. */
  | { t: 'voz.altavoz_vacio' }
  | { t: 'voz.error' }
  | { t: 'voz.canal_caido' }
  // from the server and the employee
  | { t: 'srv.credencial_ok' }
  | { t: 'srv.decision'; accion: 'contesto' | 'pregunto' | 'atajo' | 'delego'
      texto?: string; capacidad?: string; argumentos?: Record<string, unknown> }
  | { t: 'srv.decision_falla' }
  | { t: 'emp.escribe'; texto: string }
  | { t: 'emp.responde'; texto: string }
  | { t: 'emp.pregunta'; requestId: string; texto: string }
  | { t: 'emp.pide_al_dispositivo'; que: string; paraQue: string }
  | { t: 'emp.falla'; texto: string }
  // from the device and the clock
  | { t: 'disp.resultado'; texto: string }
  /** ONE single clock event: whoever sends it decides nothing, it only says what time it
   *  is. Whether it means a heartbeat or the end of the wait is decided by the MACHINE,
   *  which is where the tests are. It used to be two events and the engine chose which. */
  | { t: 'reloj.tic' }

/** All 25, in a list so the totality test can walk them all. */
export const EVENTOS: Evento['t'][] = [
  'ui.abrir', 'ui.cerrar', 'ui.oculta', 'ui.visible', 'ui.reposo',
  'voz.empieza_a_oir', 'voz.deja_de_oir', 'voz.transcripcion', 'voz.transcripcion_falla',
  'voz.respuesta_abierta', 'voz.respuesta_cerrada', 'voz.empieza_a_hablar',
  'voz.deja_de_hablar', 'voz.altavoz_vacio', 'voz.error', 'voz.canal_caido',
  'srv.credencial_ok', 'srv.decision', 'srv.decision_falla',
  'emp.escribe', 'emp.responde', 'emp.pregunta', 'emp.pide_al_dispositivo',
  'emp.falla',
  'disp.resultado', 'reloj.tic',
]

/** All three, in a list, so the totality test can walk them all. */
export const VOCES: Voz[] = ['callada', 'generando', 'sonando']

export const FASES: Fase[] = [
  'cerrado', 'arrancando', 'escuchando', 'oyendo', 'decidiendo',
  'pidiendo', 'esperando_respuesta', 'al_dispositivo', 'caido',
]

/** How long each phase waits before giving it up as lost.
 *
 *  `esperando_respuesta` is NOT here, on purpose: there we wait for a PERSON, and a person
 *  may take as long as they like. Putting a clock on someone who is thinking their answer
 *  would be the opposite of natural. */
export const PLAZOS: Partial<Record<Fase, number>> = {
  arrancando: 25_000,
  decidiendo: 8_000,
  pidiendo: 180_000,
  al_dispositivo: 30_000,
}

/** How long the employee may go without saying anything before «still on it» is said. */
export const SILENCIO_MAXIMO = 25_000

// ── The effects ─────────────────────────────────────────────────────────────

export type Efecto =
  | { t: 'pedirMicro' }
  | { t: 'soltarMicro' }
  | { t: 'micro'; encendido: boolean }
  | { t: 'pedirCredencial' }
  | { t: 'tirarSesion' }
  /** ONE only, on purpose: the adapter translates it into cancelling the generation AND
   *  draining the speaker. With only one of the two, «it cancels and keeps talking» — it
   *  happened. Since there is no way to express half of it here, it cannot be half-done
   *  again. */
  | { t: 'callar' }
  /** A REAL answer: it enters the conversation. `comoSeDice` is NOT decoration: it
   *  decides which instructions the response carries, and in the voice API per-response
   *  instructions REPLACE the session's (they do not add up). */
  | { t: 'decir'; texto: string; comoSeDice: ComoSeDice }
  /** An aside while the employee works: `conversation: 'none'`, it does not enter the
   *  thread. If it did, the model would end up answering its own progress updates. */
  | { t: 'decirAparte'; texto: string }
  | { t: 'pedirDecision'; texto: string }
  | { t: 'pedirAlEmpleado'; texto: string; capacidad?: string
      argumentos?: Record<string, unknown>; anotado: { de: string; texto: string }[] }
  | { t: 'responderAlEmpleado'; requestId: string; texto: string }
  | { t: 'contestarAlAparato'; texto: string }
  | { t: 'pedirAlAparato'; que: string; paraQue: string }
  | { t: 'abandonar' }
  | { t: 'avisar'; texto: string }

/** WHAT KIND OF TEXT IS BEING SAID. Two, and they really are different:
 *
 *   · `literal`     — written by the server (the front desk) or by the machine itself,
 *                     already in spoken, measured language. Said as is, word for word.
 *   · `de_pantalla` — written by the EMPLOYEE to be read on a screen: bullets, bold,
 *                     links, identifiers. That is not read out: it is told.
 *
 *  THE MEASURED BUG (20/09/2026): everything went as `literal`, with the order «Say
 *  EXACTLY this, adding or removing nothing». And since per-response instructions REPLACE
 *  the session's, that order switched off —right when speaking— the whole contract of how
 *  to speak that the server was already sending: summarise, don't read links, don't say
 *  identifiers. It was written, it was correct, and it was not in force at the only moment
 *  that mattered. That is why it read «- **Distancia**: 39,7 km» verbatim, and why it read
 *  aloud a bracket that was telling it what to do.
 *
 *  With two kinds, a text cannot be both: the contradiction stops being expressible,
 *  which is the only way it does not come back. */
export type ComoSeDice = 'literal' | 'de_pantalla' | 'de_pantalla_largo'

/** FROM HERE ON, WHAT THE EMPLOYEE WROTE IS NOT TOLD IN FULL: it is summarised and left
 *  in writing.
 *
 *  THE MEASURED BUG (21/09/2026): to a question about restaurants, the employee returned
 *  five cards with ratings, reviews and links —1,400 characters meant to be read on a
 *  screen— and the voice recited them all, one after another, for almost a minute. Nobody
 *  listens to a list of five things with two facts each: that is what the chat is for,
 *  where the text is already complete.
 *
 *  The cap goes HERE and not in the prompt so that the kind of text is decided by the
 *  machine —which has tests— and not by a sentence the model may not apply. 700
 *  characters are about twenty seconds of speech: below it is told, above it is
 *  summarised. */
export const LARGO_AL_OIDO = 700

export interface Paso { estado: Estado; efectos: Efecto[] }

// ── What is known without asking anyone ─────────────────────────────────────

/** Does it carry words? ONE rule, and it is a checked fact, not a threshold. A click
 *  carries none, and noise cannot interrupt them. */
export function traePalabras(texto: string): boolean {
  return /[\p{L}\p{N}]{2,}/u.test(sinRelleno(texto || ''))
}

/** FILLERS ARE NOT WORDS. «um», «eh», «mm» come out of the transcriber when the person
 *  hesitates or clears their throat, and they have two letters: they passed the test
 *  above and OPENED A TURN, silencing whatever was playing and sending «um» to the front
 *  desk. The trace of 21/09/2026 has one at 56.5 s. With the `escuchando` fix the window
 *  in which a filler can interrupt is larger, so this goes in the same change.
 *
 *  It is a SHORT, closed list of sounds, not of words: «sí», «no», «ya» are not in it and
 *  cannot be, because they are answers. */
const RELLENOS = /\b(u+m+|u+h+|e+h+m*|e+m+|m+h*m*|h+m+|a+h+|o+h+|a+j+a+)\b/giu
function sinRelleno(texto: string): string {
  return texto.replace(RELLENOS, ' ')
}

const PARAR = ['para', 'pára', 'déjalo', 'dejalo', 'déjalo estar', 'cancela', 'cancélalo',
               'ya vale', 'vale ya', 'termina', 'olvídalo', 'olvidalo', 'basta', 'stop',
               'para de buscar', 'déjalo ya']

/** An ORDER to stop: the WHOLE sentence is the order, not a word that appears in it.
 *
 *  Its own test caught it: with «starts with», «para mí, mexicana» was enough to abort the
 *  query — exactly the kind of bug that is not forgiven, because it throws away work the
 *  person wanted. An exact match is required, allowing only the tails that do not change
 *  the meaning (trailing "please", "already").
 *
 *  And the bias is deliberate: erring towards NOT stopping costs waiting a few seconds;
 *  erring the other way throws away a two-minute query. */
export function esOrdenDeParar(texto: string): boolean {
  const limpio = (x: string) => x.toLowerCase().replace(/[¡!¿?.;]/g, ' ')
    .replace(/\s+/g, ' ').trim().replace(/\s+(ya|por favor|porfa|gracias)$/g, '').trim()
  const entero = limpio((texto || '').replace(/,/g, ' '))
  if (!entero) return false
  // A length cap first: inside a long sentence, «para» is a preposition.
  // «eso es para ti, gracias» cannot abort a two-minute query.
  if (entero.split(' ').length > 4) return false
  if (PARAR.includes(entero)) return true
  // And orders come in bunches: «para, déjalo» are TWO, and the test caught it. It is
  // enough for one comma-separated piece to be one — «para mí, mexicana» has none.
  return (texto || '').split(',').map(limpio).filter(Boolean).some((x) => PARAR.includes(x))
}

const SI = ['sí', 'si', 'vale', 'claro', 'adelante', 'ok', 'venga', 'de acuerdo', 'dale']
const NO = ['no', 'mejor no', 'prefiero que no', 'ni hablar', 'déjalo', 'dejalo']

export function diceQueSi(texto: string): boolean {
  const t = (texto || '').toLowerCase().replace(/[¡!¿?.,;]/g, ' ').replace(/\s+/g, ' ').trim()
  return SI.some((p) => t === p || t.startsWith(p + ' '))
}
export function diceQueNo(texto: string): boolean {
  const t = (texto || '').toLowerCase().replace(/[¡!¿?.,;]/g, ' ').replace(/\s+/g, ' ').trim()
  return NO.some((p) => t === p || t.startsWith(p + ' '))
}

// ── Internal helpers ────────────────────────────────────────────────────────

/** Goes back to calm without losing what does NOT belong to this turn: what was already
 *  attempted from the device, whether the screen is in the background, and the clock
 *  —which the wrapper keeps. */
const calma = (c: Ctx): Ctx => ({
  requestId: null, permiso: null, yaPedido: c.yaPedido, ultimoDicho: '',
  anotado: [], oculta: c.oculta, avisos: 0,
  desdeCuando: c.desdeCuando, ultimaSenal: c.ultimaSenal,
})

/** SAY something for real. If a voice is playing, it is silenced first: the answer ALWAYS
 *  wins. This is where the queue died — there is nothing to queue and nothing that can
 *  step on it. */
function diciendo(e: Estado, texto: string, faseNueva: Fase,
                  comoSeDice: ComoSeDice, ctx?: Partial<Ctx>): Paso {
  // THE KIND IS DECIDED BY THE MACHINE, not by the caller nor the model: what the
  // employee writes for a screen, if long, is summarised and left in writing. The caller
  // says where the text comes from; how much of it is told is a rule, and rules live
  // here, where the tests are.
  //
  // And an employee QUESTION is never summarised: it expects an answer, and summarising
  // its options would be answering for the person. Here a question is exactly what leaves
  // a `requestId` paused —that is its definition in this machine—, so that is what is
  // checked.
  const clase: ComoSeDice = comoSeDice === 'de_pantalla' && !ctx?.requestId
    && texto.length > LARGO_AL_OIDO ? 'de_pantalla_largo' : comoSeDice
  const efectos: Efecto[] = []
  // IN FLIGHT, not «playing»: a response that is being GENERATED must be cancelled too,
  // or both are said one after the other. Asking only about the speaker left out exactly
  // the window in which they crossed.
  if (enVuelo(e.voz)) efectos.push({ t: 'callar' })
  efectos.push({ t: 'decir', texto, comoSeDice: clase })
  return {
    estado: { fase: faseNueva, voz: 'generando', ctx: { ...e.ctx, ultimoDicho: texto, ...(ctx || {}) } },
    efectos,
  }
}

/** THE PERSON HAS SAID SOMETHING AND THAT OPENS A TURN. In a function because it happens
 *  from TWO phases —`oyendo`, when we saw them start speaking, and `escuchando`, when we
 *  did not— and they must do exactly the same. Written twice, they diverge: the
 *  `oyendo` copy asked about `'sonando'` and left out the response still being
 *  GENERATED, which is the window in which two voices cross (the bug of 20/09/2026,
 *  fixed in `diciendo` and not here).
 *
 *  Speaking ALWAYS wins: whatever is in flight is silenced before anything else. */
function abreTurno(e: Estado, texto: string): Paso {
  const antes: Efecto[] = enVuelo(e.voz) ? [{ t: 'callar' }] : []
  return {
    estado: { fase: 'decidiendo', voz: 'callada', ctx: e.ctx },
    efectos: [...antes, { t: 'pedirDecision', texto }],
  }
}

/** An ASIDE. If something is already in flight, it is dropped: said late it is no longer
 *  true. And it is not repeated. Each one that goes out is counted, because how the next
 *  one sounds depends on the count. */
function aparte(e: Estado, texto: string): Paso {
  if (enVuelo(e.voz) || !texto.trim() || texto.trim() === e.ctx.ultimoDicho) {
    return { estado: e, efectos: [] }
  }
  return {
    estado: { ...e, voz: 'generando',
              ctx: { ...e.ctx, ultimoDicho: texto.trim(), avisos: e.ctx.avisos + 1 } },
    efectos: [{ t: 'decirAparte', texto }],
  }
}

/** THE STILL-ALIVE NOTICE, when the employee has gone a while without saying anything.
 *  The machine writes it because there is nothing to tell, and that is why the FIRST one
 *  is different: «still on it» first sounds like you have been at it a while and done
 *  nothing. You are still on something once you had started telling it. */
const avisoDeVida = (e: Estado): Paso =>
  aparte(e, e.ctx.avisos === 0 ? 'Me pongo con ello.' : 'Sigo en ello.')

const quieto = (e: Estado): Paso => ({ estado: e, efectos: [] })

function cerrar(e: Estado): Paso {
  return {
    estado: { fase: 'cerrado', voz: 'callada', ctx: calma(e.ctx) },
    efectos: [{ t: 'callar' }, { t: 'abandonar' }, { t: 'soltarMicro' }, { t: 'tirarSesion' }],
  }
}

// ── The reducer ─────────────────────────────────────────────────────────────

/** Has this phase's wait run out? The machine decides it with the time it is given: there
 *  is no `Date.now()` anywhere, and that is why all of this can be tested. */
function seAcaboLaEspera(e: Estado, ahora: number): boolean {
  const plazo = PLAZOS[e.fase]
  return !!plazo && ahora - e.ctx.desdeCuando > plazo
}

/** The reducer. `ahora` comes in as one more input — time is data, not a side effect, and
 *  putting it here is what gives the clock tests. */
export function paso(entrada: Estado, ev: Evento, ahora = 0): Paso {
  const d = decidir(entrada, ev, ahora)
  // THE CLOCK KEEPS ITSELF. On a phase change that phase's count starts, and every signal
  // from the employee restarts the silence.
  //
  // A PHASE CHANGE IS ALSO A SIGNAL, and forgetting it kept alive the very bug this came
  // to kill. `ultimaSenal` started at 0 and only the `emp.*` moved it, so on entering
  // `pidiendo` the silence was measured against the year 1970: the FIRST tick was already
  // past 25 s and released the notice 300 ms after requesting. The clock was moved inside
  // the machine and its memory was left uninitialised.
  //
  // And the test did not see it because it built the state with `ahora = 0`: with the
  // clock at zero, an uninitialised clock and a freshly set one are indistinguishable.
  // Since 12.46.0 the clock tests start at a real time.
  const cambioDeFase = d.estado.fase !== entrada.fase
  const algoPasa = cambioDeFase || ev.t.startsWith('emp.')
  if (algoPasa) {
    d.estado = { ...d.estado, ctx: { ...d.estado.ctx,
      desdeCuando: cambioDeFase ? ahora : d.estado.ctx.desdeCuando,
      ultimaSenal: ahora } }
  }
  return d
}

function decidir(entrada: Estado, ev: Evento, ahora: number): Paso {
  let e = entrada
  // ── WHAT HOLDS IN ANY PHASE ───────────────────────────────────────────────
  //
  // The `voz` dimension is orthogonal: these events move it without touching the phase.
  // `error` also switches it off — otherwise any failure would leave the conversation
  // mute forever, waiting for a close that is not going to come.
  // NO SESSION, NO VOICE. `cerrado`, `arrancando` and `caido` have no channel, so they
  // cannot have anything playing — and if for whatever reason they get there in that
  // state, it is healed. The totality test caught it: a `voz.respuesta_abierta` in
  // `cerrado` left «closed and playing at once», a contradiction that then confuses
  // everything.
  const sinSesion = e.fase === 'cerrado' || e.fase === 'arrancando' || e.fase === 'caido'
  if (sinSesion && enVuelo(e.voz)) e = { ...e, voz: 'callada' }

  if (ev.t === 'voz.respuesta_abierta') {
    if (sinSesion) return quieto(e)
    return { estado: { ...e, voz: 'generando' }, efectos: [] }
  }
  if (ev.t === 'voz.respuesta_cerrada') {
    // CLOSING THE GENERATION DOES NOT DRAIN THE SPEAKER, and confusing them cost a minute
    // of queued audio. If it was already playing, it keeps playing until the speaker says
    // so; if it closed without ever playing (a response with no audio, or cancelled before
    // starting), then yes: nothing of ours is left in flight.
    const voz: Voz = e.voz === 'sonando' ? 'sonando' : 'callada'
    // If a question was just said (theirs or a permission one), the next thing you say
    // is an ANSWER: it is awaited, not decided again.
    if (e.fase === 'pidiendo' && (e.ctx.requestId || e.ctx.permiso)) {
      return { estado: { ...e, fase: 'esperando_respuesta', voz }, efectos: [] }
    }
    return { estado: { ...e, voz }, efectos: [] }
  }
  if (ev.t === 'voz.empieza_a_hablar') {
    if (sinSesion) return quieto(e)
    return { estado: { ...e, voz: 'sonando' }, efectos: [] }
  }
  // IT GOES OFF HERE, and only here: the drained speaker is the only fact that means
  // silence.
  if (ev.t === 'voz.deja_de_hablar' || ev.t === 'voz.altavoz_vacio') {
    return { estado: { ...e, voz: 'callada' }, efectos: [] }
  }

  // The emergency exit. From any phase with a session, and without asking anyone.
  if (ev.t === 'ui.reposo') {
    if (e.fase === 'cerrado' || e.fase === 'arrancando' || e.fase === 'caido') return quieto(e)
    return {
      estado: { fase: 'escuchando', voz: 'callada', ctx: calma(e.ctx) },
      efectos: [{ t: 'callar' }, { t: 'abandonar' }],
    }
  }

  if (ev.t === 'ui.oculta' || ev.t === 'ui.visible') {
    const oculta = ev.t === 'ui.oculta'
    if (e.fase === 'cerrado' || e.fase === 'arrancando' || e.fase === 'caido') {
      return { estado: { ...e, ctx: { ...e.ctx, oculta } }, efectos: [] }
    }
    return {
      estado: { ...e, ctx: { ...e.ctx, oculta } },
      efectos: [{ t: 'micro', encendido: !oculta }],
    }
  }

  if (ev.t === 'ui.cerrar') {
    if (e.fase === 'cerrado') return quieto(e)
    return cerrar(e)
  }

  if (ev.t === 'voz.canal_caido') {
    if (e.fase === 'cerrado' || e.fase === 'caido') return quieto(e)
    if (e.fase === 'arrancando') {
      return { estado: { fase: 'cerrado', voz: 'callada', ctx: calma(e.ctx) },
               efectos: [{ t: 'soltarMicro' }, { t: 'tirarSesion' },
                         { t: 'avisar', texto: 'No he podido conectar.' }] }
    }
    return {
      estado: { fase: 'caido', voz: 'callada', ctx: calma(e.ctx) },
      efectos: [{ t: 'soltarMicro' },
                { t: 'avisar', texto: 'Se ha cortado la conexión. Toca para volver a hablar.' }],
    }
  }

  // ── BY PHASE ──────────────────────────────────────────────────────────────
  switch (e.fase) {

    case 'cerrado':
      if (ev.t === 'ui.abrir') {
        return { estado: { ...e, fase: 'arrancando' },
                 efectos: [{ t: 'pedirMicro' }, { t: 'pedirCredencial' }] }
      }
      // The WRITTEN chat is still alive and its events arrive here: they are none of this
      // machine's business, and the thread picks them up anyway.
      return quieto(e)

    case 'arrancando':
      if (ev.t === 'srv.credencial_ok') return { estado: { ...e, fase: 'escuchando' }, efectos: [] }
      if (ev.t === 'voz.error') {
        // The mic IS RELEASED even if we never got to speak: `pedirMicro` was already
        // emitted on entering here, and the browser shows the «using the microphone» dot
        // while the track stays alive. The totality test caught it.
        return { estado: { fase: 'cerrado', voz: 'callada', ctx: calma(e.ctx) },
                 efectos: [{ t: 'soltarMicro' }, { t: 'tirarSesion' },
                           { t: 'avisar', texto: 'No he podido abrir la conversación.' }] }
      }
      return quieto(e)

    case 'escuchando':
      if (ev.t === 'voz.empieza_a_oir') return { estado: { ...e, fase: 'oyendo' }, efectos: [] }
      // A TRANSCRIPTION OPENS A TURN HERE TOO, and this `if` is a measured bug.
      //
      // The machine reached rest with the person SPEAKING: `voz.empieza_a_oir` can land in
      // another phase —`decidiendo`, for instance— that ignores that event, and when the
      // decision returns to `escuchando` nobody is in `oyendo` any more. Whatever that
      // person said was lost without a trace. Trace of 21/09/2026: at 56.1 s it starts
      // hearing during `decidiendo`; at 56.4 s the decision returns to rest; at 59.2 s
      // the question «how do I create a trip?» arrives and NOTHING HAPPENS. Sixteen
      // seconds of silence and the person had to repeat the whole question.
      //
      // The rule that closes it: **starting to hear is an INTENTION, the transcription is
      // the FACT**, and a fact is never ignored for not having seen the intention. Every
      // live phase already handles it —each in its own way—; this was the only one that
      // did not, which is precisely the one that needs it most. The test's invariant I7
      // watches it for all of them at once.
      if (ev.t === 'voz.transcripcion' && traePalabras(ev.texto)) return abreTurno(e, ev.texto)
      // `emp.*` here comes from a WRITTEN turn: it is not narrated, that is the other door.
      return quieto(e)

    case 'oyendo':
      if (ev.t === 'voz.transcripcion') {
        if (!traePalabras(ev.texto)) return { estado: { ...e, fase: 'escuchando' }, efectos: [] }
        return abreTurno(e, ev.texto)
      }
      if (ev.t === 'voz.transcripcion_falla') {
        // As if you had said nothing. They are not interrupted.
        return { estado: { ...e, fase: 'escuchando' }, efectos: [] }
      }
      if (ev.t === 'voz.deja_de_oir') return quieto(e)
      return quieto(e)

    case 'decidiendo': {
      if (ev.t === 'srv.decision') {
        if (ev.accion === 'contesto' || ev.accion === 'pregunto') {
          // Written by the front desk, already in spoken language and with its cap: literal.
          return diciendo(e, ev.texto || '', 'escuchando', 'literal')
        }
        return {
          estado: { fase: 'pidiendo', voz: e.voz, ctx: { ...e.ctx, anotado: [] } },
          efectos: [{ t: 'pedirAlEmpleado', texto: ev.texto || '', capacidad: ev.capacidad,
                      argumentos: ev.argumentos, anotado: e.ctx.anotado }],
        }
      }
      // NO DECISION, SO IT IS DELEGATED. The conversational model no longer has tools, so
      // it cannot decide on its own; the employee resolves it anyway. Accepted cost: a
      // pleasantry ends up at the employee. A convenience cannot prevent speaking.
      if (ev.t === 'srv.decision_falla'
          || (ev.t === 'reloj.tic' && seAcaboLaEspera(e, ahora))) {
        return {
          estado: { fase: 'pidiendo', voz: e.voz, ctx: { ...e.ctx, anotado: [] } },
          efectos: [{ t: 'pedirAlEmpleado', texto: e.ctx.ultimoDicho, anotado: e.ctx.anotado }],
        }
      }
      if (ev.t === 'voz.transcripcion' && traePalabras(ev.texto)) {
        // You spoke again: the decision in flight no longer counts.
        return { estado: e, efectos: [{ t: 'pedirDecision', texto: ev.texto }] }
      }
      return quieto(e)
    }

    case 'pidiendo': {
      if (ev.t === 'emp.escribe') return aparte(e, ev.texto)
      // ONE single clock event, and here it is decided what it means: if the wait ran out
      // it is abandoned; if the employee has been quiet, they are kept company. Whoever
      // sends the tick does not choose — that choice was in the engine and so it had no
      // tests.
      if (ev.t === 'reloj.tic') {
        if (seAcaboLaEspera(e, ahora)) {
          return diciendo(e, 'Esto está tardando más de la cuenta; lo dejo.',
                          'escuchando', 'literal', calma(e.ctx))
        }
        if (ahora - e.ctx.ultimaSenal > SILENCIO_MAXIMO) return avisoDeVida(e)
        return quieto(e)
      }
      // WHAT THE EMPLOYEE SENDS IS WRITTEN FOR A SCREEN. It is told, not read out.
      if (ev.t === 'emp.responde')
        return diciendo(e, ev.texto, 'escuchando', 'de_pantalla', calma(e.ctx))
      if (ev.t === 'emp.falla')
        return diciendo(e, ev.texto, 'escuchando', 'de_pantalla', calma(e.ctx))
      if (ev.t === 'emp.pregunta') {
        // Their turn stays PAUSED. The next thing you say resumes it; it does not open a
        // new one.
        return diciendo(e, ev.texto, 'pidiendo', 'de_pantalla', { requestId: ev.requestId })
      }
      if (ev.t === 'emp.pide_al_dispositivo') {
        if (e.ctx.yaPedido.includes(ev.que)) {
          return { estado: e, efectos: [{ t: 'contestarAlAparato',
            texto: 'Ya se intentó en esta conversación; no se vuelve a pedir.' }] }
        }
        // The device is never touched without saying what for and waiting for an answer.
        return diciendo(e, `¿Te importa si ${ev.paraQue}?`, 'pidiendo', 'literal',
                        { permiso: { que: ev.que, paraQue: ev.paraQue } })
      }
      if (ev.t === 'voz.transcripcion' && traePalabras(ev.texto)) {
        if (esOrdenDeParar(ev.texto)) {
          return {
            estado: { fase: 'escuchando', voz: 'callada', ctx: calma(e.ctx) },
            efectos: [{ t: 'callar' }, { t: 'abandonar' }],
          }
        }
        // It is noted down and travels with the pending answer. No turn is opened:
        // opening one dropped another «let me look» on top of the first.
        return { estado: { ...e, ctx: { ...e.ctx, anotado: [...e.ctx.anotado, { de: 'tu', texto: ev.texto }] } },
                 efectos: [] }
      }
      return quieto(e)
    }

    case 'esperando_respuesta': {
      if (ev.t === 'voz.transcripcion' && traePalabras(ev.texto)) {
        if (esOrdenDeParar(ev.texto)) {
          return { estado: { fase: 'escuchando', voz: 'callada', ctx: calma(e.ctx) },
                   efectos: [{ t: 'callar' }, { t: 'abandonar' }] }
        }
        if (e.ctx.permiso) {
          const p = e.ctx.permiso
          if (diceQueNo(ev.texto)) {
            return { estado: { fase: 'pidiendo', voz: e.voz,
                               ctx: { ...e.ctx, permiso: null, yaPedido: [...e.ctx.yaPedido, p.que] } },
                     efectos: [{ t: 'contestarAlAparato', texto: 'No ha dado permiso. Sigue sin ello y no insistas.' }] }
          }
          if (diceQueSi(ev.texto)) {
            return { estado: { fase: 'al_dispositivo', voz: e.voz, ctx: { ...e.ctx, permiso: null } },
                     efectos: [{ t: 'pedirAlAparato', que: p.que, paraQue: p.paraQue }] }
          }
          // Neither yes nor no: it is something else. It is treated as an answer to the
          // employee and the permission is taken as not granted — insisting is what it
          // used to get wrong.
          return { estado: { fase: 'pidiendo', voz: e.voz,
                             ctx: { ...e.ctx, permiso: null, yaPedido: [...e.ctx.yaPedido, p.que] } },
                   efectos: [{ t: 'contestarAlAparato', texto: `No lo ha concedido. Ha dicho: «${ev.texto}»` }] }
        }
        if (e.ctx.requestId) {
          const id = e.ctx.requestId
          return { estado: { fase: 'pidiendo', voz: e.voz, ctx: { ...e.ctx, requestId: null } },
                   efectos: [{ t: 'responderAlEmpleado', requestId: id, texto: ev.texto }] }
        }
        // Nothing to answer: it is a new request.
        return { estado: { fase: 'decidiendo', voz: e.voz, ctx: e.ctx },
                 efectos: [{ t: 'pedirDecision', texto: ev.texto }] }
      }
      // THERE IS NO DEADLINE HERE, on purpose: we are waiting for a PERSON, and a person
      // may take as long as they like. Putting a clock on someone who is thinking their
      // answer would be the opposite of natural.
      return quieto(e)
    }

    case 'al_dispositivo': {
      if (ev.t === 'disp.resultado') {
        return { estado: { fase: 'pidiendo', voz: e.voz,
                           ctx: { ...e.ctx, yaPedido: [...e.ctx.yaPedido, 'ubicacion'] } },
                 efectos: [{ t: 'contestarAlAparato', texto: ev.texto }] }
      }
      if (ev.t === 'reloj.tic' && seAcaboLaEspera(e, ahora)) {
        return { estado: { fase: 'pidiendo', voz: e.voz, ctx: e.ctx },
                 efectos: [{ t: 'contestarAlAparato', texto: 'El aparato no ha contestado. Sigue sin ello.' }] }
      }
      // The system dialog is modal: it cannot be interrupted. What is said gets noted.
      if (ev.t === 'voz.transcripcion' && traePalabras(ev.texto)) {
        return { estado: { ...e, ctx: { ...e.ctx, anotado: [...e.ctx.anotado, { de: 'tu', texto: ev.texto }] } },
                 efectos: [] }
      }
      return quieto(e)
    }

    case 'caido':
      if (ev.t === 'ui.abrir') {
        // New session. The thread brings it up to date, so the conversation is not lost.
        return { estado: { ...e, fase: 'arrancando' },
                 efectos: [{ t: 'pedirMicro' }, { t: 'pedirCredencial' }] }
      }
      // The request is still alive on the other side: whatever arrives shows in the chat.
      return quieto(e)
  }
}
