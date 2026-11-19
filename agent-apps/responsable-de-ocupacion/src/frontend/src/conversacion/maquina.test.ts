/**
 * THE MACHINE'S VERIFICATION, in four layers and with no browser.
 *
 *   node --experimental-strip-types src/conversacion/maquina.test.ts
 *
 * It runs with bare node: zero new dependencies in the project, and therefore zero new
 * dependencies in every generated app. The machine is pure, so it needs nothing more than
 * this.
 *
 * The four layers, and the order matters:
 *
 *   1 · TOTALITY — the 9 phases × 27 events. All answered, including the impossible ones.
 *       An event that arrives where it should not —and they do, the network reorders—
 *       cannot fall into a silent `else`.
 *   2 · INVARIANTS — checked after EVERY transition of EVERY test, not as separate cases.
 *       That way a new test about something else also watches this.
 *   3 · THE WALKTHROUGHS — thirteen, and eight of them are bugs that were really measured.
 *   4 · REPLAY — a whole conversation, from loading the chat to coming back and asking
 *       about something from before, run twice: same log, same result.
 */
import { readFileSync } from 'node:fs'
import {
  paso, estadoInicial, EVENTOS, FASES, VOCES, enVuelo,
  esOrdenDeParar, traePalabras, PLAZOS, SILENCIO_MAXIMO, LARGO_AL_OIDO,
  type Estado, type Evento, type Efecto, type Fase,
} from './maquina.ts'
import { esFontaneria, esDeLaMaquina, enOtroIdioma, queHace, contarTarjetas } from './avances.ts'
import { crearMotor, type Adaptador } from './puerto.ts'
import { adaptador, delServidorDeVoz, type DeLaSuperficie } from './adaptador.ts'

// ── Minimal harness ─────────────────────────────────────────────────────────

let pasadas = 0
const fallos: string[] = []

function prueba(nombre: string, fn: () => void) {
  try { fn(); pasadas++ } catch (ex) { fallos.push(`${nombre}\n    ${(ex as Error).message}`) }
}
function afirmar(cond: unknown, msg: string) { if (!cond) throw new Error(msg) }
function igual<T>(a: T, b: T, msg: string) {
  if (JSON.stringify(a) !== JSON.stringify(b)) {
    throw new Error(`${msg}\n    esperaba ${JSON.stringify(b)}\n    y salió   ${JSON.stringify(a)}`)
  }
}
const tipos = (efs: Efecto[]) => efs.map((x) => x.t)

// ── Layer 2 · the invariants, after EVERY transition ─────────────────────────

function invariantes(antes: Estado, ev: Evento, d: { estado: Estado; efectos: Efecto[] }) {
  const { estado: e, efectos } = d
  const donde = `tras ${ev.t} desde ${antes.fase}/${antes.voz}`

  afirmar(FASES.includes(e.fase), `${donde}: fase desconocida «${e.fase}»`)
  afirmar(VOCES.includes(e.voz), `${donde}: voz desconocida «${e.voz}»`)

  // I1 · A response is never opened with something OF OURS IN FLIGHT without silencing
  //      first. This is the mother one. And it says «in flight» and not «playing» because
  //      of what was measured on 20/09/2026: a response being generated does not play yet,
  //      but it will — and opening another on top says both in a row. Asking only about
  //      the speaker left out exactly the window in which they crossed.
  const iDecir = efectos.findIndex((x) => x.t === 'decir' || x.t === 'decirAparte')
  if (iDecir >= 0 && enVuelo(antes.voz)) {
    const iCallar = efectos.findIndex((x) => x.t === 'callar')
    afirmar(iCallar >= 0 && iCallar < iDecir,
      `${donde}: abre respuesta con la voz sonando y sin callar antes`)
  }

  // I2 · In `pidiendo` a real answer is not narrated unless it is THE answer (or its
  //      question, or the permission): a progress update can never sound like an answer.
  if (antes.fase === 'pidiendo' && efectos.some((x) => x.t === 'decir')) {
    afirmar(['emp.responde', 'emp.falla', 'emp.pregunta',
             'emp.pide_al_dispositivo', 'reloj.tic'].includes(ev.t),
      `${donde}: un avance ha sonado como respuesta`)
  }

  // I3 · `cerrado` implies a released microphone and a clean context.
  if (e.fase === 'cerrado' && antes.fase !== 'cerrado') {
    afirmar(efectos.some((x) => x.t === 'soltarMicro'), `${donde}: cierra sin soltar el micro`)
    igual(e.ctx.requestId, null, `${donde}: cierra con una pregunta en pausa`)
    igual(e.ctx.anotado, [], `${donde}: cierra con cosas anotadas`)
  }
  afirmar(!(e.fase === 'cerrado' && enVuelo(e.voz)), `${donde}: cerrado y con algo en vuelo`)

  // I4 · If something is abandoned, it is silenced: what no longer counts is not left playing.
  if (efectos.some((x) => x.t === 'abandonar')) {
    afirmar(efectos.some((x) => x.t === 'callar'), `${donde}: abandona sin callar`)
  }

  // I5 · The device is never asked twice for the same data.
  const nuevos = e.ctx.yaPedido.filter((q) => !antes.ctx.yaPedido.includes(q))
  afirmar(new Set(e.ctx.yaPedido).size === e.ctx.yaPedido.length,
    `${donde}: un dato del aparato apuntado dos veces`)
  if (efectos.some((x) => x.t === 'pedirAlAparato')) {
    const p = efectos.find((x) => x.t === 'pedirAlAparato') as { que: string }
    afirmar(!antes.ctx.yaPedido.includes(p.que), `${donde}: pide otra vez «${p.que}»`)
  }
  void nuevos

  // I7 · A TRANSCRIPTION WITH WORDS IS NEVER LOST. In the LIVE phases, what the person
  //      says has to leave a trace: effects, a phase change, or being noted down to travel
  //      with the answer. Falling into a `quieto` means losing a whole turn without a
  //      single notice.
  //
  //      THE MEASURED BUG (21/09/2026): `escuchando` did not look at the transcription.
  //      The machine went back to rest with the person speaking —the «starts hearing» had
  //      landed in `decidiendo`— and their question evaporated: sixteen seconds of silence
  //      and they had to repeat it entirely. It was the ONLY live phase that did not
  //      handle it, so the guard is not a case: it is this invariant, which watches all of
  //      them at once and also those added tomorrow.
  //      The live phases are DERIVED from the catalog (all but the three with no voice),
  //      not listed: a new phase comes in watched without anyone remembering it.
  const FASES_VIVAS: Fase[] = FASES.filter((f) => !['cerrado', 'arrancando', 'caido'].includes(f))
  if (ev.t === 'voz.transcripcion' && traePalabras(ev.texto) && FASES_VIVAS.includes(antes.fase)) {
    const rastro = efectos.length > 0
      || e.fase !== antes.fase
      || e.ctx.anotado.length > antes.ctx.anotado.length
    afirmar(rastro, `${donde}: lo que dijo la persona se ha perdido sin rastro`)
  }

  // I6 · `pidiendo` with `requestId` is a contradiction: if there is a paused question the
  //      phase is `esperando_respuesta` as soon as it stops speaking.
  afirmar(!(e.fase === 'escuchando' && e.ctx.requestId),
    `${donde}: en reposo con una pregunta en pausa`)
}

/** Takes a step and checks the invariants. Every test uses this.
 *
 *  `ahora` is an input like any other —time is data, not a side effect— and that is why a
 *  test can move the clock forward three minutes without waiting three minutes. */
function dar(e: Estado, ev: Evento, ahora = 0): { estado: Estado; efectos: Efecto[] } {
  const d = paso(e, ev, ahora)
  invariantes(e, ev, d)
  return d
}
const correr = (e: Estado, evs: Evento[]) => evs.reduce((acc, ev) => dar(acc, ev).estado, e)

// ── Sample states, to be able to start in any phase ─────────────────────────

function en(fase: Fase, extra: Partial<Estado> = {}): Estado {
  const base = estadoInicial()
  return { ...base, fase, ...extra, ctx: { ...base.ctx, ...(extra.ctx || {}) } }
}

/** One event of each type, with plausible data. */
function ejemplo(t: Evento['t']): Evento {
  switch (t) {
    case 'voz.transcripcion': return { t, texto: '¿qué viajes tengo?' }
    case 'srv.decision': return { t, accion: 'delego', texto: '¿qué viajes tengo?' }
    case 'emp.escribe': return { t, texto: 'Estoy mirando tus viajes.' }
    case 'emp.responde': return { t, texto: 'Tienes cuatro viajes.' }
    case 'emp.pregunta': return { t, requestId: 'r1', texto: '¿de qué tipo?' }
    case 'emp.pide_al_dispositivo': return { t, que: 'ubicacion', paraQue: 'mirar dónde estás' }
    case 'emp.falla': return { t, texto: 'Ha dado un error.' }
    case 'disp.resultado': return { t, texto: 'Está en Madrid.' }
    default: return { t } as Evento
  }
}

// ═══ LAYER 1 · TOTALITY ═════════════════════════════════════════════════════

prueba('CAPA 1 · las 9 fases × 27 eventos × 3 voces están todas contestadas', () => {
  let casillas = 0
  for (const fase of FASES) {
    for (const voz of VOCES) {
      for (const t of EVENTOS) {
        const antes = en(fase, { voz })
        const ev = ejemplo(t)
        let d
        try { d = paso(antes, ev) } catch (ex) {
          throw new Error(`casilla ${fase}/${voz} × ${t} REVIENTA: ${(ex as Error).message}`)
        }
        afirmar(d && d.estado && Array.isArray(d.efectos),
          `casilla ${fase}/${voz} × ${t} no devuelve un paso`)
        invariantes(antes, ev, d)
        casillas++
      }
    }
  }
  igual(casillas, FASES.length * EVENTOS.length * VOCES.length,
        'no se recorrieron todas las casillas')
})

prueba('CAPA 1 · ningún evento deja la fase fuera del catálogo', () => {
  for (const fase of FASES) for (const t of EVENTOS) {
    const d = paso(en(fase), ejemplo(t))
    afirmar(FASES.includes(d.estado.fase), `${fase} × ${t} → fase inventada «${d.estado.fase}»`)
  }
})

// ═══ LAYER 3 · THE WALKTHROUGHS ═════════════════════════════════════════════

prueba('1 · un ruido sin palabras no abre turno', () => {
  const d = dar(en('oyendo'), { t: 'voz.transcripcion', texto: '...' })
  igual(d.estado.fase, 'escuchando', 'el ruido abrió turno')
  igual(tipos(d.efectos), [], 'el ruido produjo efectos')
})

prueba('2 · una transcripción fallida vuelve a reposo  [hoy falta en el móvil]', () => {
  const d = dar(en('oyendo'), { t: 'voz.transcripcion_falla' })
  igual(d.estado.fase, 'escuchando', 'se queda colgado en oyendo')
  igual(tipos(d.efectos), [], 'no se le puede cortar por no entenderle')
})

prueba('3 · hablar mientras habla: se le calla y se abre turno nuevo', () => {
  const d = dar(en('oyendo', { voz: 'sonando' }), { t: 'voz.transcripcion', texto: 'espera, mejor el 15' })
  igual(d.estado.fase, 'decidiendo', 'no abrió turno')
  igual(d.estado.voz, 'callada', 'se quedó sonando')
  igual(tipos(d.efectos), ['callar', 'pedirDecision'], 'hablar no ganó')
})

prueba('4 · callar es UN efecto, y por eso no se puede hacer a medias', () => {
  const d = dar(en('pidiendo', { voz: 'sonando' }), { t: 'ui.cerrar' })
  afirmar(tipos(d.efectos).includes('callar'), 'sale sin callar')
  // And the guard that it stays indivisible runs on the SOURCE: the day someone adds an
  // effect that cancels without draining the speaker, «it cancels and keeps talking»
  // comes back. The type does not allow expressing half; this watches that it still
  // doesn't.
  const fuente = readFileSync(new URL('./maquina.ts', import.meta.url), 'utf8')
  for (const mitad of ["'cancelar'", "'vaciarAltavoz'", "'response.cancel'"]) {
    afirmar(!fuente.includes(mitad),
      `la máquina conoce «${mitad}»: callar ha dejado de ser indivisible`)
  }
})

prueba('5 · hablar mientras el empleado trabaja se ANOTA, no abre turno', () => {
  const d = dar(en('pidiendo'), { t: 'voz.transcripcion', texto: 'ah, y que lo vea Martín' })
  igual(d.estado.fase, 'pidiendo', 'abrió un turno encima del que trabaja')
  igual(tipos(d.efectos), [], 'soltó otro «déjame que lo mire»')
  igual(d.estado.ctx.anotado.length, 1, 'no se anotó lo dicho')
})

prueba('6 · «para» mientras trabaja SÍ corta', () => {
  const d = dar(en('pidiendo', { voz: 'sonando' }), { t: 'voz.transcripcion', texto: 'para, déjalo' })
  igual(d.estado.fase, 'escuchando', 'no paró')
  igual(tipos(d.efectos), ['callar', 'abandonar'], 'no abandonó la consulta')
})

prueba('6b · «para mí, mexicana» NO es una orden de parar', () => {
  afirmar(!esOrdenDeParar('para mí, mexicana'), 'confunde una frase con una orden')
  afirmar(esOrdenDeParar('para'), 'no reconoce la orden')
  afirmar(esOrdenDeParar('déjalo ya'), 'no reconoce la orden')
})

prueba('7 · un avance NO suena como respuesta  [el fallo del 20/09]', () => {
  const d = dar(en('pidiendo'), { t: 'emp.escribe', texto: 'Voy a mirar las fechas.' })
  igual(tipos(d.efectos), ['decirAparte'], 'un avance se dijo como respuesta')
  igual(d.estado.fase, 'pidiendo', 'un avance cambió de fase')
})

prueba('8 · la respuesta NUNCA se aplaza por un avance: calla y se dice', () => {
  const d = dar(en('pidiendo', { voz: 'sonando' }), { t: 'emp.responde', texto: 'Tienes cuatro viajes.' })
  igual(tipos(d.efectos), ['callar', 'decir'], 'la respuesta se aplazó tras un avance')
  igual(d.estado.fase, 'escuchando', 'no volvió a reposo')
})

prueba('8b · un avance que llega con la voz sonando se TIRA, no se encola', () => {
  const d = dar(en('pidiendo', { voz: 'sonando' }), { t: 'emp.escribe', texto: 'sigo' })
  igual(tipos(d.efectos), [], 'se encoló un avance, que es lo que pisaba la respuesta')
  igual(d.estado.voz, 'sonando', 'tocó la voz al tirar un avance')
})

prueba('9 · salir desde CUALQUIER fase deja calma y sin nada pendiente', () => {
  for (const fase of FASES) {
    if (fase === 'cerrado') continue
    for (const voz of VOCES) {
      const antes = en(fase, { voz, ctx: { requestId: 'r9', anotado: [{ de: 'tu', texto: 'x' }] } as never })
      const d = dar(antes, { t: 'ui.cerrar' })
      igual(d.estado.fase, 'cerrado', `salir desde ${fase} no cierra`)
      igual(d.estado.voz, 'callada', `salir desde ${fase} deja algo en vuelo`)
      afirmar(tipos(d.efectos).includes('callar'), `salir desde ${fase} no calla`)
      afirmar(tipos(d.efectos).includes('soltarMicro'), `salir desde ${fase} no suelta el micro`)
    }
  }
})

prueba('10 · la pregunta del empleado deja su turno EN PAUSA y se reanuda  [hoy se cuelga]', () => {
  let e = en('pidiendo')
  let d = dar(e, { t: 'emp.pregunta', requestId: 'r42', texto: '¿de qué tipo?' })
  igual(tipos(d.efectos), ['decir'], 'no dijo la pregunta')
  igual(d.estado.ctx.requestId, 'r42', 'perdió el turno en pausa')
  e = dar(d.estado, { t: 'voz.respuesta_cerrada' }).estado
  igual(e.fase, 'esperando_respuesta', 'no se queda esperando TU respuesta')
  d = dar(e, { t: 'voz.transcripcion', texto: 'tradicional' })
  igual(tipos(d.efectos), ['responderAlEmpleado'], 'abrió un turno NUEVO en vez de reanudar')
  igual((d.efectos[0] as { requestId: string }).requestId, 'r42', 'reanudó con otro identificador')
  igual(d.estado.ctx.requestId, null, 'dejó la pausa abierta')
})

prueba('11 · dos decisiones solapadas: solo vale la última', () => {
  const d = dar(en('decidiendo'), { t: 'voz.transcripcion', texto: 'no, espera, mejor el 15' })
  igual(tipos(d.efectos), ['pedirDecision'], 'no pidió decisión nueva')
  igual((d.efectos[0] as { texto: string }).texto, 'no, espera, mejor el 15', 'contestó a la anterior')
})

prueba('12 · si la decisión falla se habla igual (se delega)', () => {
  const d = dar(en('decidiendo'), { t: 'srv.decision_falla' })
  igual(d.estado.fase, 'pidiendo', 'la conversación se queda muda')
  igual(tipos(d.efectos), ['pedirAlEmpleado'], 'no se delega')
})

prueba('12b · y si se CUELGA —que no es fallar— también, al vencer el plazo', () => {
  // A hang returns no error: it simply never arrives. Hence the deadline, and hence the
  // deadline lives INSIDE: that way a test can move the clock forward and check it.
  const e = dar(estadoInicial(), { t: 'ui.abrir' }, 0).estado
  const listo = dar(e, { t: 'srv.credencial_ok' }, 0).estado
  const oyendo = dar(listo, { t: 'voz.empieza_a_oir' }, 0).estado
  const decidiendo = dar(oyendo, { t: 'voz.transcripcion', texto: '¿qué viajes tengo?' }, 0).estado
  igual(decidiendo.fase, 'decidiendo', 'no llegó a decidir')
  const antes = dar(decidiendo, { t: 'reloj.tic' }, (PLAZOS.decidiendo || 0) - 1000)
  igual(antes.estado.fase, 'decidiendo', 'se rinde antes de tiempo')
  const d = dar(decidiendo, { t: 'reloj.tic' }, (PLAZOS.decidiendo || 0) + 1000)
  igual(d.estado.fase, 'pidiendo', 'una espera colgada no se cierra nunca')
  igual(tipos(d.efectos), ['pedirAlEmpleado'], 'no delega al vencer')
})

prueba('8c · cerrar la generación NO vacía el altavoz  [el minuto de audio en cola]', () => {
  // THE MEASURED BUG (20/09/2026, skill 12.43.2). In a real conversation: response
  // closed at 139.8 s, speaker stopped at 159.4 s. NINETEEN seconds in which the machine
  // believed it was silent while it talked. At 142.7 s the employee's answer arrived, it
  // looked at `voz`, saw `callada`, and silenced NOTHING before opening the next one:
  // two answers on top of each other and a whole minute of audio queueing.
  let e = en('pidiendo', { voz: 'generando' })
  e = dar(e, { t: 'voz.empieza_a_hablar' }).estado
  igual(e.voz, 'sonando', 'no se entera de que ya suena')

  e = dar(e, { t: 'voz.respuesta_cerrada' }).estado
  igual(e.voz, 'sonando', 'se da por muda en cuanto acaba de generar: el altavoz sigue lleno')

  // And with that, the mother invariant can hold again.
  const d = dar(e, { t: 'emp.responde', texto: 'Tienes cuatro viajes.' })
  igual(tipos(d.efectos), ['callar', 'decir'], 'habla encima de lo que aún suena')

  // The drained speaker is the ONLY thing that means silence.
  igual(dar(e, { t: 'voz.deja_de_hablar' }).estado.voz, 'callada', 'no se apaga ni vaciándose')
  igual(dar(e, { t: 'voz.altavoz_vacio' }).estado.voz, 'callada', 'tras callar se queda sonando para siempre')

  // A response that closes WITHOUT ever playing does switch off: nothing was left.
  igual(dar(en('pidiendo', { voz: 'generando' }), { t: 'voz.respuesta_cerrada' }).estado.voz,
        'callada', 'se queda esperando un altavoz que nunca empezó')
})

/** A REAL time, and not zero.
 *
 *  The clock tests built the state with `ahora = 0`, and that left them blind to exactly
 *  the bug they came to catch: with the clock at zero, an UNINITIALISED memory
 *  (`ultimaSenal: 0`) and a freshly set one give exactly the same subtraction. The notice
 *  fired 300 ms after requesting in the deployed employee, and here everything stayed
 *  green.
 *
 *  With a real time, an uninitialised field gives itself away: the subtraction goes to
 *  fifty-six years. */
const AHORA = 1_758_400_000_000

prueba('8e · al empezar a esperar NO se habla  [el aviso a los 300 ms]', () => {
  // THE MEASURED BUG (20/09/2026, with 12.45.0 already deployed). The clock was moved
  // inside the machine but its memory started at 0 and only the `emp.*` moved it, so on
  // entering `pidiendo` the silence was measured against 1970: the FIRST tick was already
  // past 25 seconds. A phase change is also a signal — something just happened.
  let e = dar(estadoInicial(), { t: 'ui.abrir' }, AHORA).estado
  e = dar(e, { t: 'srv.credencial_ok' }, AHORA).estado
  e = dar(e, { t: 'voz.empieza_a_oir' }, AHORA).estado
  e = dar(e, { t: 'voz.transcripcion', texto: '¿qué viajes tengo?' }, AHORA).estado
  e = dar(e, { t: 'srv.decision', accion: 'atajo', texto: '¿qué viajes tengo?' }, AHORA).estado
  igual(e.fase, 'pidiendo', 'no llegó a pedir')

  // The first ticks, in silence. Exactly what failed live.
  igual(tipos(dar(e, { t: 'reloj.tic' }, AHORA + 300).efectos), [],
        'suelta el aviso a los 300 ms de pedir')
  igual(tipos(dar(e, { t: 'reloj.tic' }, AHORA + SILENCIO_MAXIMO - 1000).efectos), [],
        'se adelanta al silencio')
  igual(tipos(dar(e, { t: 'reloj.tic' }, AHORA + SILENCIO_MAXIMO + 1000).efectos), ['decirAparte'],
        'se queda callado mientras el empleado trabaja')
})

prueba('8d · el PRIMER aviso no es «sigo en ello»', () => {
  // «Still on it» first sounds like you have been at it a while and done nothing. You are
  // still on something once you had started telling it.
  const pidiendo = dar(dar(dar(dar(estadoInicial(), { t: 'ui.abrir' }, 0).estado,
    { t: 'srv.credencial_ok' }, 0).estado, { t: 'voz.empieza_a_oir' }, 0).estado,
    { t: 'voz.transcripcion', texto: '¿qué viajes tengo?' }, 0).estado
  const e = dar(pidiendo, { t: 'srv.decision', accion: 'delego', texto: 'los viajes' }, 0).estado

  const uno = dar(e, { t: 'reloj.tic' }, SILENCIO_MAXIMO + 1000)
  igual((uno.efectos[0] as { texto: string }).texto, 'Me pongo con ello.',
        'dice que SIGUE con algo que acaba de empezar')

  // The second one yes: by then it has been a while.
  const callado = dar(uno.estado, { t: 'voz.altavoz_vacio' }, SILENCIO_MAXIMO + 2000).estado
  const dos = dar(callado, { t: 'reloj.tic' }, 2 * SILENCIO_MAXIMO + 3000)
  igual((dos.efectos[0] as { texto: string }).texto, 'Sigo en ello.',
        'vuelve a anunciarse como si fuera la primera vez')

  // And when the wait ends, the count resets: the next question starts the same way.
  igual(dar(dos.estado, { t: 'emp.responde', texto: 'ya está' }).estado.ctx.avisos, 0,
        'se lleva la cuenta a la pregunta siguiente')
})

prueba('12c · el SILENCIO se rompe solo, y no antes de tiempo', () => {
  // While the employee works it does not stay silent for thirty seconds: every so often
  // it says it is still on it. This lived in the engine, with its own memory of when the
  // last signal was, and that is why the aside fired 0 ms after requesting — without
  // anything screaming, because there was no way to test it. Now the memory is in the
  // state and this test moves the clock forward.
  const decidiendo = dar(dar(dar(dar(estadoInicial(), { t: 'ui.abrir' }, 0).estado,
    { t: 'srv.credencial_ok' }, 0).estado, { t: 'voz.empieza_a_oir' }, 0).estado,
    { t: 'voz.transcripcion', texto: '¿qué viajes tengo?' }, 0).estado
  const pidiendo = dar(decidiendo, { t: 'srv.decision', consultar: true, texto: 'los viajes' }, 0).estado
  igual(pidiendo.fase, 'pidiendo', 'no llegó a pedir')

  // THE MOMENT OF REQUESTING: not a word. It is the bug that was measured live.
  igual(tipos(dar(pidiendo, { t: 'reloj.tic' }, 0).efectos), [], 'suelta el aparte al instante')
  igual(tipos(dar(pidiendo, { t: 'reloj.tic' }, SILENCIO_MAXIMO - 1000).efectos), [],
        'se adelanta al silencio')
  igual(tipos(dar(pidiendo, { t: 'reloj.tic' }, SILENCIO_MAXIMO + 1000).efectos), ['decirAparte'],
        'se queda callado mientras el empleado trabaja')

  // And EVERY SIGNAL REARMS THE CLOCK: if the employee just told something, there is no silence.
  const conSenal = dar(pidiendo, { t: 'emp.avanza', texto: 'Estoy mirando los vuelos de mayo.' },
                       SILENCIO_MAXIMO).estado
  igual(tipos(dar(conSenal, { t: 'reloj.tic' }, SILENCIO_MAXIMO + 1000).efectos), [],
        'cuenta un silencio que no existe: acaba de hablar')
})

prueba('13 · el permiso del aparato: se pregunta, y un «no» no insiste nunca más', () => {
  let d = dar(en('pidiendo'), { t: 'emp.pide_al_dispositivo', que: 'ubicacion', paraQue: 'mirar dónde estás' })
  igual(tipos(d.efectos), ['decir'], 'tocó el aparato sin preguntar')
  afirmar(!tipos(d.efectos).includes('pedirAlAparato'), 'tocó el aparato sin permiso')
  let e = dar(d.estado, { t: 'voz.respuesta_cerrada' }).estado
  igual(e.fase, 'esperando_respuesta', 'no espera la contestación')

  d = dar(e, { t: 'voz.transcripcion', texto: 'no' })
  igual(tipos(d.efectos), ['contestarAlAparato'], 'no contestó al empleado')
  afirmar(d.estado.ctx.yaPedido.includes('ubicacion'), 'no recuerda que ya se intentó')

  // And now the employee insists: it must not ask again.
  const d2 = dar(d.estado, { t: 'emp.pide_al_dispositivo', que: 'ubicacion', paraQue: 'otra vez' })
  igual(tipos(d2.efectos), ['contestarAlAparato'], 'volvió a preguntar por lo mismo')
  afirmar(!tipos(d2.efectos).includes('decir'), 'insistió en voz alta')
  void e
})

prueba('13b · un «sí» sí llega al aparato', () => {
  const conPermiso = en('esperando_respuesta', {
    ctx: { permiso: { que: 'ubicacion', paraQue: 'mirar dónde estás' } } as never })
  const d = dar(conPermiso, { t: 'voz.transcripcion', texto: 'sí, claro' })
  igual(d.estado.fase, 'al_dispositivo', 'no fue al aparato')
  igual(tipos(d.efectos), ['pedirAlAparato'], 'no lo pidió')
})

prueba('14 · LA SALIDA DE EMERGENCIA: tocar la bola vuelve a la calma desde donde sea', () => {
  for (const fase of FASES) {
    for (const voz of VOCES) {
      const antes = en(fase, { voz, ctx: { requestId: 'r', anotado: [{ de: 'tu', texto: 'x' }] } as never })
      const d = dar(antes, { t: 'ui.reposo' })
      if (fase === 'cerrado' || fase === 'arrancando' || fase === 'caido') {
        igual(d.estado.fase, fase, `${fase}: no hay de qué volver y aun así se movió`)
        continue
      }
      igual(d.estado.fase, 'escuchando', `desde ${fase} no vuelve al reposo`)
      igual(d.estado.voz, 'callada', `desde ${fase} deja algo en vuelo`)
      igual(d.estado.ctx.requestId, null, `desde ${fase} deja una pausa abierta`)
      igual(d.estado.ctx.anotado, [], `desde ${fase} deja cosas anotadas`)
      afirmar(tipos(d.efectos).includes('callar'), `desde ${fase} no calla`)
    }
  }
})

prueba('15 · la caída de la conexión se DICE, no se queda mudo', () => {
  const d = dar(en('pidiendo', { voz: 'sonando' }), { t: 'voz.canal_caido' })
  igual(d.estado.fase, 'caido', 'no registra la caída')
  afirmar(tipos(d.efectos).includes('avisar'), 'se queda mudo sin explicación')
  afirmar(tipos(d.efectos).includes('soltarMicro'), 'se queda con el micro cogido')
  const vuelta = dar(d.estado, { t: 'ui.abrir' })
  igual(vuelta.estado.fase, 'arrancando', 'no se puede reintentar')
})

// Test 16 —«the approval is said, not approved by speaking»— has gone with its event:
// the employee's channel emits NO approval, so `emp.aprobacion` was a branch nobody could
// reach. It looked handled and was not. Layer 7's guard uncovered it when removing a
// condition that mentioned it in passing.

prueba('17 · la pestaña oculta apaga el micro sin mover la fase', () => {
  const d = dar(en('escuchando'), { t: 'ui.oculta' })
  igual(d.estado.fase, 'escuchando', 'ocultar la pestaña cambió de fase')
  igual(tipos(d.efectos), ['micro'], 'no apagó el micro')
  igual((d.efectos[0] as { encendido: boolean }).encendido, false, 'dejó el micro abierto')
})

prueba('18 · un avance no se repite a sí mismo', () => {
  let e = en('pidiendo')
  let d = dar(e, { t: 'emp.escribe', texto: 'Estoy mirando tus viajes.' })
  igual(tipos(d.efectos), ['decirAparte'], 'no lo dijo la primera vez')
  e = dar(d.estado, { t: 'voz.respuesta_cerrada' }).estado
  d = dar(e, { t: 'emp.escribe', texto: 'Estoy mirando tus viajes.' })
  igual(tipos(d.efectos), [], 'se repitió en voz alta')
})

// ═══ LAYER 4 · REPLAYING A WHOLE CONVERSATION ═══════════════════════════════

/** The design's walkthrough, end to end: the chat opens, you ask, you interrupt
 *  yourself, it is delegated, the employee asks, you answer, it answers, you leave and
 *  come back. */
const CONVERSACION: Evento[] = [
  { t: 'ui.abrir' },
  { t: 'srv.credencial_ok' },
  { t: 'voz.empieza_a_oir' },
  { t: 'voz.deja_de_oir' },
  { t: 'voz.transcripcion', texto: 'hola' },
  { t: 'srv.decision', accion: 'contesto', texto: '¡Hola! ¿En qué te ayudo?' },
  { t: 'voz.respuesta_abierta' },
  { t: 'voz.empieza_a_oir' },
  { t: 'voz.transcripcion', texto: 'espera — ¿qué viajes tengo?' },   // interrupts it
  { t: 'srv.decision', accion: 'delego', texto: '¿qué viajes tengo?' },
  { t: 'emp.escribe', texto: 'Estoy mirando tus viajes.' },
  { t: 'voz.respuesta_cerrada' },
  { t: 'emp.pregunta', requestId: 'r7', texto: '¿de este año o de todos?' },
  { t: 'voz.respuesta_cerrada' },
  { t: 'voz.empieza_a_oir' },
  { t: 'voz.transcripcion', texto: 'de todos' },
  { t: 'emp.responde', texto: 'Tienes cuatro viajes.' },
  { t: 'voz.respuesta_cerrada' },
  { t: 'ui.cerrar' },
  { t: 'ui.abrir' },
  { t: 'srv.credencial_ok' },
  { t: 'voz.empieza_a_oir' },
  { t: 'voz.transcripcion', texto: '¿y el segundo dónde era?' },
  { t: 'srv.decision', accion: 'delego', texto: '¿y el segundo dónde era?' },
  { t: 'emp.responde', texto: 'El segundo es en Madrid.' },
  { t: 'voz.respuesta_cerrada' },
]

prueba('19 · lo dicho EN REPOSO abre turno  [el turno perdido del 21/09]', () => {
  // Rest is reached with the person speaking: their «starts hearing» landed in
  // `decidiendo`, which ignores it, and the decision returned here. If this phase does not
  // handle the transcription, the question evaporates — and that is exactly what happened.
  const d = dar(en('escuchando'),
                { t: 'voz.transcripcion', texto: '¿Qué tengo que hacer para crear un viaje?' })
  igual(d.estado.fase, 'decidiendo', 'lo dicho en reposo no abrió turno')
  igual(tipos(d.efectos), ['pedirDecision'], 'lo dicho en reposo se perdió')
})

prueba('19b · y si algo nuestro está en vuelo, se calla ANTES de abrir el turno', () => {
  const d = dar(en('escuchando', { voz: 'generando' }),
                { t: 'voz.transcripcion', texto: 'mejor el 15' })
  igual(tipos(d.efectos), ['callar', 'pedirDecision'],
        'se abrió turno sin callar lo que se estaba generando')
  igual(d.estado.voz, 'callada', 'se creyó muda sin haber callado')
})

prueba('20 · lo LARGO del empleado se resume, no se recita  [el minuto de restaurantes]', () => {
  const clase = (d: { efectos: Efecto[] }) =>
    (d.efectos.find((x) => x.t === 'decir') as { comoSeDice: string } | undefined)?.comoSeDice
  const corta = dar(en('pidiendo'), { t: 'emp.responde', texto: 'Tienes cuatro viajes.' })
  igual(clase(corta), 'de_pantalla', 'una respuesta corta dejó de contarse entera')
  const larga = dar(en('pidiendo'),
                    { t: 'emp.responde', texto: 'a'.repeat(LARGO_AL_OIDO + 1) })
  igual(clase(larga), 'de_pantalla_largo', 'una parrafada se recita en vez de resumirse')
})

prueba('CAPA 4 · la conversación entera termina en calma', () => {
  const fin = correr(estadoInicial(), CONVERSACION)
  igual(fin.fase, 'escuchando', 'no termina en reposo')
  igual(fin.voz, 'callada', 'termina con algo en vuelo')
  igual(fin.ctx.requestId, null, 'termina con una pregunta en pausa')
})

prueba('CAPA 4 · el mismo registro da SIEMPRE el mismo resultado', () => {
  const traza = (evs: Evento[]) => {
    let e = estadoInicial()
    return evs.map((ev) => {
      const d = dar(e, ev); e = d.estado
      return `${ev.t} → ${e.fase}/${e.voz} [${tipos(d.efectos).join(',')}]`
    })
  }
  igual(traza(CONVERSACION), traza(CONVERSACION), 'la máquina no es determinista')
})

prueba('CAPA 4 · reanudar desde la mitad da lo mismo que correrlo entero', () => {
  const corte = 13
  const entero = correr(estadoInicial(), CONVERSACION)
  const mitad = correr(estadoInicial(), CONVERSACION.slice(0, corte))
  const resto = correr(mitad, CONVERSACION.slice(corte))
  igual(resto, entero, 'el estado depende de algo que no está en los eventos')
})

prueba('CAPA 4 · en toda la conversación no hay dos respuestas abiertas a la vez', () => {
  let e = estadoInicial()
  for (const ev of CONVERSACION) {
    const d = dar(e, ev)
    if (d.efectos.some((x) => x.t === 'decir' || x.t === 'decirAparte')) {
      afirmar(!enVuelo(e.voz) || d.efectos.some((x) => x.t === 'callar'),
        `abre respuesta sobre otra en ${ev.t}`)
    }
    e = d.estado
  }
})

// ── The small things that hold up the rest ──────────────────────────────────

prueba('traePalabras distingue una frase de un chasquido', () => {
  afirmar(traePalabras('hola'), 'no ve una palabra')
  afirmar(!traePalabras('...'), 've palabras donde no las hay')
  afirmar(!traePalabras(''), 've palabras en el vacío')
  afirmar(traePalabras('el 15'), 'no ve un número como contenido')
  // Fillers: two letters, and they are nothing. One opened a turn on 21/09/2026.
  afirmar(!traePalabras('um'), 'un «um» abre turno')
  afirmar(!traePalabras('Eh... mmm'), 'una duda abre turno')
  afirmar(traePalabras('um, ¿qué viajes tengo?'), 'el relleno se lleva la frase entera')
  afirmar(traePalabras('sí'), 'un «sí» se toma por relleno (es una respuesta)')
  afirmar(traePalabras('Maled'), 'una palabra desconocida se toma por relleno')
})

// ═══ LAYER 5 · THE ENGINE: that the effects REACH the adapter ════════════════
//
// The machine decides well; this checks that what is decided gets run. It is the seam
// where a redesign gets lost: perfect rules nobody calls.

function espia() {
  const hechos: string[] = []
  const a: Adaptador = {
    callar: () => hechos.push('callar'),
    decir: (t) => hechos.push('decir:' + t),
    decirAparte: (t) => hechos.push('aparte:' + t),
    pedirMicro: () => hechos.push('pedirMicro'),
    soltarMicro: () => hechos.push('soltarMicro'),
    micro: (on) => hechos.push('micro:' + on),
    pedirCredencial: () => hechos.push('pedirCredencial'),
    tirarSesion: () => hechos.push('tirarSesion'),
    pedirDecision: (t) => hechos.push('pedirDecision:' + t),
    pedirAlEmpleado: (p) => hechos.push('pedirAlEmpleado:' + p.texto),
    responderAlEmpleado: (id, t) => hechos.push(`responder:${id}:${t}`),
    contestarAlAparato: (t) => hechos.push('alAparato:' + t),
    pedirAlAparato: (q) => hechos.push('pedirAlAparato:' + q),
    abandonar: () => hechos.push('abandonar'),
    avisar: (t) => hechos.push('avisar:' + t),
  }
  return { a, hechos }
}

prueba('MOTOR · cada efecto llega a su método del adaptador', () => {
  const { a, hechos } = espia()
  const m = crearMotor(a)
  for (const ev of CONVERSACION) m.enviar(ev)
  for (const esperado of ['pedirMicro', 'pedirCredencial', 'pedirDecision', 'pedirAlEmpleado',
                          'decir', 'responder', 'callar', 'soltarMicro']) {
    afirmar(hechos.some((h) => h.startsWith(esperado)), `el motor nunca llamó a «${esperado}»`)
  }
  igual(m.estado().fase, 'escuchando', 'el motor no termina en reposo')
})

prueba('MOTOR · no se puede expresar media forma de callar', () => {
  const { a } = espia()
  // The adapter declares `callar` and NOTHING that cancels separately: if someone added
  // `cancelar` or `vaciarAltavoz` to the port, «it cancels and keeps talking» would return.
  const metodos = Object.keys(a)
  for (const prohibido of ['cancelar', 'vaciarAltavoz', 'clear']) {
    afirmar(!metodos.includes(prohibido), `el puerto expone «${prohibido}»`)
  }
  afirmar(metodos.includes('callar'), 'el puerto no sabe callar')
})

prueba('MOTOR · el plazo de una espera la cierra sola', () => {
  const { a, hechos } = espia()
  const m = crearMotor(a)
  m.enviar({ t: 'ui.abrir' }); m.enviar({ t: 'srv.credencial_ok' })
  m.enviar({ t: 'voz.empieza_a_oir' })
  m.enviar({ t: 'voz.transcripcion', texto: '¿qué viajes tengo?' })
  igual(m.estado().fase, 'decidiendo', 'no llegó a decidir')
  // The front desk NEVER answers (a hang is not an error: it does not fail, it does not arrive).
  m.tic(Date.now() + (PLAZOS.decidiendo || 0) + 1000)
  igual(m.estado().fase, 'pidiendo', 'una espera sin respuesta se queda colgada para siempre')
  afirmar(hechos.some((h) => h.startsWith('pedirAlEmpleado')), 'no delegó al vencer el plazo')
})

prueba('MOTOR · a la persona NO se le pone reloj', () => {
  const { a } = espia()
  const m = crearMotor(a)
  m.enviar({ t: 'ui.abrir' }); m.enviar({ t: 'srv.credencial_ok' })
  m.enviar({ t: 'voz.empieza_a_oir' })
  m.enviar({ t: 'voz.transcripcion', texto: 'algo' })
  m.enviar({ t: 'srv.decision', accion: 'delego', texto: 'algo' })
  m.enviar({ t: 'emp.pregunta', requestId: 'r', texto: '¿de qué tipo?' })
  m.enviar({ t: 'voz.respuesta_cerrada' })
  igual(m.estado().fase, 'esperando_respuesta', 'no espera tu contestación')
  m.tic(Date.now() + 10 * 60_000)     // ten minutes thinking it over
  igual(m.estado().fase, 'esperando_respuesta', 'le puso un reloj a una persona')
})

prueba('MOTOR · el registro de eventos permite REPRODUCIR', () => {
  const registro: Evento[] = []
  const { a } = espia()
  const m = crearMotor({ ...a, anotarEvento: (ev) => registro.push(ev) })
  for (const ev of CONVERSACION) m.enviar(ev)
  igual(registro.length, CONVERSACION.length, 'el registro pierde eventos')
  // And replayed in a new machine it gives exactly the same state.
  const { a: a2 } = espia()
  const m2 = crearMotor(a2)
  for (const ev of registro) m2.enviar(ev)
  igual(m2.estado(), m.estado(), 'reproducir el registro no reconstruye el estado')
})

// ═══ LAYER 5 · WHAT IS TOLD WHILE IT WORKS ══════════════════════════════════
//
// Pure functions, and that is why they are tested with the EXACT STRINGS that came out
// wrong through the speaker. While they lived inside the screen there was nowhere to
// write this.

prueba('AVANCES · leer su propio fichero de salida NO es trabajo  [salió en inglés y con la ruta]', () => {
  // What was heard on 21/09/2026: «Estoy con Read the tool output file
  // /var/opencode-data/opencode/tool-output/tool_0c2ea01d6001hbeoBGQ y ha devuelto un
  // texto largo». Two bugs in one sentence; this is the first.
  afirmar(esFontaneria('read'), 'leer un fichero se cuenta como trabajo')
  afirmar(esFontaneria('wai-viajes_read'), 'con el prefijo del servidor deja de reconocerse')
  afirmar(esFontaneria('todowrite'), 'apuntarse una tarea se cuenta como trabajo')
  afirmar(!esFontaneria('google_maps_directions'), 'una herramienta de verdad se toma por fontanería')
  afirmar(!esFontaneria('wai-viajes_crear_viaje'), 'una capacidad del empleado se toma por fontanería')
})

prueba('AVANCES · una ruta, un id o un enlace no se dicen en alto', () => {
  afirmar(esDeLaMaquina('/var/opencode-data/opencode/tool-output/tool_0c2ea01d6001hbeoBGQ'),
          'una ruta de fichero pasa el filtro')
  afirmar(esDeLaMaquina('https://serpapi.com/search.json?engine=google_maps'),
          'un enlace pasa el filtro')
  afirmar(esDeLaMaquina('ChIJ3YOkCgAlQg0RCYhq5JvjFZ8'), 'un identificador pasa el filtro')
  afirmar(!esDeLaMaquina('restaurantes en Moratalaz'), 'una frase de persona se descarta')
})

prueba('AVANCES · de un argumento con ruta dentro no sale una frase', () => {
  // The key is NOT called `path`: it is called `description`. The old filter looked at
  // the field name and that is why it let it through. This one looks at what is inside.
  const dicho = queHace('wai-viajes_leer', {
    description: 'Read the tool output file /var/opencode-data/opencode/tool-output/tool_0c2ea01d6001hbeoBGQ',
  })
  afirmar(!dicho.includes('/var/'), `se dice una ruta en alto: «${dicho}»`)
  afirmar(!dicho.includes('tool_0c2ea01d'), `se dice un identificador en alto: «${dicho}»`)
})

prueba('AVANCES · una palabra larga NO es un identificador  [revisión]', () => {
  afirmar(!esDeLaMaquina('responsabilidades'), 'una palabra de 17 letras se toma por id')
  afirmar(!esDeLaMaquina('electrodomésticos en Moratalaz'), 'una frase normal se descarta')
  afirmar(esDeLaMaquina('0c2ea01d6001hbeoBGQ'), 'un id con cifras pasa')
})

prueba('AVANCES · lo que viene en inglés NO se dice: se descarta antes  [revisión]', () => {
  const q = 'find and compare the best restaurants in Moratalaz, Madrid, with current ratings, cuisine'
  afirmar(enOtroIdioma(q), 'la búsqueda en inglés no se reconoce')
  afirmar(!enOtroIdioma('restaurantes en Moratalaz con buenas reseñas'), 'el español se toma por inglés')
  const dicho = queHace('tavily_search', { query: q })
  afirmar(!/\b(find|compare|best|ratings)\b/i.test(dicho), `sale inglés por el altavoz: «${dicho}»`)
})

prueba('20b · una PREGUNTA larga del empleado se dice entera  [revisión]', () => {
  const d = dar(en('pidiendo'), { t: 'emp.pregunta', requestId: 'r9', texto: 'x'.repeat(LARGO_AL_OIDO + 1) })
  const clase = (d.efectos.find((x) => x.t === 'decir') as { comoSeDice: string } | undefined)?.comoSeDice
  igual(clase, 'de_pantalla', 'una pregunta se resume, y resumir sus opciones es contestar por la persona')
})

prueba('AVANCES · cinco mapas son «5 mapas», no cinco veces la misma frase', () => {
  const frases = { ruta: 'un mapa con la ruta', rutas: '{{n}} mapas con las rutas',
                   lugar: 'un mapa con la dirección', lugares: '{{n}} mapas con las direcciones' }
  igual(contarTarjetas(['lugar', 'lugar', 'lugar', 'lugar', 'lugar'], frases),
        '5 mapas con las direcciones',
        'se repite la misma frase cinco veces, que es lo que se oyó')
  igual(contarTarjetas(['lugar'], frases), 'un mapa con la dirección', 'el singular se pluraliza')
  igual(contarTarjetas(['ruta', 'lugar', 'lugar'], frases),
        'un mapa con la ruta y 2 mapas con las direcciones', 'no se juntan bien los dos tipos')
  igual(contarTarjetas([], frases), '', 'sin tarjetas se dice algo igualmente')
})

// ═══ LAYER 6 · THE ADAPTER: what goes out OVER THE CHANNEL ═══════════════════
//
// The last seam. Here it is checked that «callar» really is TWO messages, that an aside
// carries `conversation: 'none'` and that the event translation makes nothing up.

function canalEspia(sonando = false) {
  const enviados: Record<string, unknown>[] = []
  const de: DeLaSuperficie = {
    mandar: (m) => { enviados.push(m as Record<string, unknown>); return true },
    estaSonando: () => sonando,
    pedirMicro: () => {}, soltarMicro: () => {}, micro: () => {},
    pedirCredencial: () => {}, tirarSesion: () => {},
    pedirDecision: () => {}, pedirAlEmpleado: () => {}, responderAlEmpleado: () => {},
    contestarAlAparato: () => {}, pedirAlAparato: () => {},
    abandonar: () => {}, avisar: () => {},
    comoContarUnAvance: (t) => 'cuéntalo: ' + t,
    comoSeCuentaLoDelEmpleado: () => 'CONTRATO DEL SERVIDOR: resume y no leas enlaces.',
    nucleoDeVoz: () => 'NUCLEO DEL SERVIDOR: no digas lo que no puedes hacer.',
  }
  return { de, enviados, a: adaptador(de) }
}

prueba('ADAPTADOR · NINGUNA respuesta sale sin el núcleo delante  [el «no puedo» inventado]', () => {
  // In the voice API, a response's instructions REPLACE the session's: whatever does not
  // travel in EVERY `response.create` is not in force while it speaks. All four doors are
  // checked, not one.
  const { a, enviados } = canalEspia()
  a.decir('tienes cuatro viajes', 'literal')
  a.decir('- **Osaka Ramen**: 4,9/5', 'de_pantalla')
  a.decir('a'.repeat(900), 'de_pantalla_largo')
  a.decirAparte('me pongo con ello')
  const respuestas = enviados.filter((x) => x.type === 'response.create')
  igual(respuestas.length, 4, 'no salieron las cuatro respuestas')
  for (const r of respuestas) {
    const ins = String((r as { response?: { instructions?: string } }).response?.instructions || '')
    afirmar(ins.includes('NUCLEO DEL SERVIDOR'),
      'una respuesta sale sin el núcleo: mientras habla, esa regla no existe')
  }
})

prueba('ADAPTADOR · una respuesta no tiene de dónde inventar  [el «no puedo buscar»]', () => {
  const { a, enviados } = canalEspia()
  a.decir('- **Osaka Ramen**: 4,9/5', 'de_pantalla')
  const r = enviados.find((x) => x.type === 'response.create') as
    { response?: Record<string, unknown> }
  igual(r?.response?.input, [],
    'la respuesta lleva la conversación de entrada: de ahí salió «no puedo buscar en tiempo real»')
})

prueba('ADAPTADOR · lo LARGO se manda con orden de resumir, no de contar', () => {
  const { a, enviados } = canalEspia()
  a.decir('a'.repeat(900), 'de_pantalla_largo')
  const ins = String((enviados.find((x) => x.type === 'response.create') as
    { response?: { instructions?: string } })?.response?.instructions || '')
  afirmar(ins.includes('NO lo cuentes'), 'una parrafada se manda como si fuera corta')
  afirmar(ins.includes('escrito en el chat'), 'no se le dice que lo deja escrito')
})

prueba('ADAPTADOR · callar son DOS mensajes, y en este orden', () => {
  const { a, enviados } = canalEspia(true)
  a.callar()
  igual(enviados.map((x) => x.type), ['response.cancel', 'output_audio_buffer.clear'],
    'callar no vacía el altavoz: «se cancela y sigue hablando»')
})

prueba('ADAPTADOR · sin nada sonando NO se cancela (ensucia el registro para nada)', () => {
  const { a, enviados } = canalEspia(false)
  a.callar()
  igual(enviados.map((x) => x.type), ['output_audio_buffer.clear'],
    'cancela una respuesta que no existe')
})

prueba('ADAPTADOR · un aparte va FUERA del hilo; una respuesta, dentro', () => {
  const { a, enviados } = canalEspia()
  a.decirAparte('sigo en ello')
  const ap = enviados.find((x) => x.type === 'response.create') as { response?: Record<string, unknown> }
  igual(ap?.response?.conversation, 'none',
    'el aparte entra en el hilo: el modelo acabará contestándose a sus propios avances')

  enviados.length = 0
  a.decir('tienes cuatro viajes', 'literal')
  const resp = enviados.find((x) => x.type === 'response.create') as { response?: Record<string, unknown> }
  afirmar(resp, 'la respuesta no se abre')
  afirmar(resp?.response?.conversation !== 'none',
    'la respuesta de verdad se queda fuera del hilo, y entonces él no recuerda qué dijo')
  afirmar(String(resp?.response?.instructions || '').includes('tienes cuatro viajes'),
    'no se le dice QUÉ tiene que decir')
  // And NO system message is added per sentence: with one per response, the thread filled
  // up with «Say this verbatim» and ended up reading more of our orders than conversation.
  afirmar(!enviados.some((x) => x.type === 'conversation.item.create'),
    'ensucia el hilo con un mensaje de sistema por cada frase')
})

prueba('ADAPTADOR · un aparte no tiene de dónde inventar  [contestó de memoria]', () => {
  // THE WORST BUG OF ALL, measured on 20/09/2026 with 12.45.0 deployed.
  //
  // `conversation: 'none'` says the response is NOT added to the thread. It does NOT say
  // it does not READ the thread: unless `input` is passed, the input context is still the
  // whole conversation. So when asked to «say: getting on it», the model had «what trips
  // do I have?» in front of it plus instructions that had replaced the session's
  // —without them, the no-making-up rule was not in force either—.
  //
  // It answered: «I don't have access to your bookings, so I can't see which trips you
  // have scheduled». Made up, aloud, and half a second before the employee sent the real
  // list. It is exactly the one thing this design exists to prevent, and it came through
  // the channel nobody watched because «it's just a filler sentence».
  //
  // With an empty input there is nothing to make things up from: it stops being forbidden
  // and becomes impossible, which is the only version that holds.
  const { a, enviados } = canalEspia()
  a.decirAparte('Me pongo con ello.')
  const r = (enviados[0] as { response?: Record<string, unknown> })?.response || {}
  igual(r.conversation, 'none', 'el aparte entra en el hilo')
  igual(r.input, [], 'el aparte lee la conversación entera: puede contestar de memoria')
})

prueba('ADAPTADOR · lo del EMPLEADO se cuenta con el contrato del servidor delante', () => {
  // THE MEASURED BUG (20/09/2026): per-response instructions REPLACE the session's in the
  // voice API. So «Say EXACTLY this» switched off —right when speaking— the whole contract
  // of how to speak that the server was already sending. It read the bullets, it read the
  // identifiers, and it even read aloud a bracket that gave it instructions. The contract
  // has to be in front again in EVERY screen response.
  const { a, enviados } = canalEspia()
  a.decir('- **Distancia**: 39,7 km', 'de_pantalla')
  const i = String((enviados[0] as { response?: { instructions?: string } })
    ?.response?.instructions || '')
  afirmar(i.includes('CONTRATO DEL SERVIDOR'),
    'dice lo del empleado sin el contrato: vuelve a leer viñetas y enlaces')
  afirmar(i.includes('39,7 km'), 'no se le dice QUÉ tiene que contar')
  afirmar(!i.includes('EXACTAMENTE'),
    'le manda leer al pie de la letra algo escrito para una pantalla')

  // And the other way round: what the server wrote IS said as is, with no leeway.
  enviados.length = 0
  a.decir('Tienes cuatro viajes.', 'literal')
  const j = String((enviados[0] as { response?: { instructions?: string } })
    ?.response?.instructions || '')
  afirmar(j.includes('EXACTAMENTE'), 'deja resumir un texto que ya venía medido')
  afirmar(!j.includes('CONTRATO DEL SERVIDOR'),
    'le da margen para reescribir lo que redactó la recepción')
})

prueba('ADAPTADOR · la traducción de eventos no se inventa nada', () => {
  const casos: [string, string | null][] = [
    ['input_audio_buffer.speech_started', 'voz.empieza_a_oir'],
    ['conversation.item.input_audio_transcription.completed', 'voz.transcripcion'],
    ['conversation.item.input_audio_transcription.failed', 'voz.transcripcion_falla'],
    ['response.created', 'voz.respuesta_abierta'],
    ['response.done', 'voz.respuesta_cerrada'],
    ['response.cancelled', 'voz.respuesta_cerrada'],
    ['error', 'voz.error'],
    ['rate_limits.updated', null],
    ['session.updated', null],
  ]
  for (const [crudo, esperado] of casos) {
    const ev = delServidorDeVoz({ type: crudo, transcript: 'hola' })
    igual(ev ? ev.t : null, esperado, `«${crudo}» se traduce mal`)
  }
})

prueba('ADAPTADOR · TODOS los eventos que el móvil atendía siguen atendidos', () => {
  // The integrity table said mobile did not handle `transcription.failed`. With a single
  // shared translation, that kind of gap can no longer exist: either it is here for both,
  // or it is there for neither.
  const losQueImportan = [
    'input_audio_buffer.speech_started', 'input_audio_buffer.speech_stopped',
    'conversation.item.input_audio_transcription.completed',
    'conversation.item.input_audio_transcription.failed',
    'response.created', 'response.done', 'response.cancelled',
    'output_audio_buffer.started', 'output_audio_buffer.stopped', 'error',
  ]
  for (const crudo of losQueImportan) {
    afirmar(delServidorDeVoz({ type: crudo }) !== null, `«${crudo}» no se traduce`)
  }
})

// ═══ LAYER 7 · THAT SOMEONE SENDS THEM ══════════════════════════════════════
//
// THE MISSING GUARD, learnt the hard way. The machine can be perfect and useless if nobody
// talks to it: on 20/09/2026 everything was wired, the 38 tests passed, and live the
// employee transcribed «Hola, hola» and DID NOTHING — because nobody sent
// `srv.credencial_ok` and the conversation stayed in `arrancando` forever, which is a
// phase that ignores everything on purpose.
//
// Layers 1-6 test the machine, the engine and the channel. This one tests the invisible
// seam: that every event in the catalog has SOMEONE who produces it. An event nobody sends
// is a part of the machine that does not exist.

prueba('CAPA 7 · todos los eventos tienen quién los mande', () => {
  const aqui = new URL('.', import.meta.url)
  const fuentes = [
    // The two views: the web one and its mobile twin live in different places depending
    // on where you look from, so both are searched and finding one is enough.
    '../components/chat/VozEnVivo.tsx',
    '../componentes/VozEnVivo.tsx',
    './puerto.ts', './adaptador.ts',
  ].map((f) => { try { return readFileSync(new URL(f, aqui), 'utf8') } catch { return '' } })
  const todo = fuentes.join('\n')
  afirmar(todo.length > 5000, 'no se ha encontrado ninguna vista: la guarda no está mirando nada')

  // The SENDING form —`t: 'evento'`— is searched for, not the bare name: searching the
  // bare name accepted an event that only appeared inside an `if`, and that is how two
  // got through with no sender. A guard that settles for a mention is not a guard.
  const sinDueño = EVENTOS.filter((t) => !todo.includes(`t: '${t}'`))
  afirmar(sinDueño.length === 0,
    `estos eventos no los manda NADIE, así que esa parte de la máquina no existe:\n      `
    + sinDueño.join(' · '))
})

prueba('CAPA 7 · arrancar sin avisar deja la conversación muda', () => {
  // The exact bug, pinned: if `srv.credencial_ok` is forgotten, everything you say is
  // ignored. Here it is checked that the start-up phase is indeed a wall —so nobody «fixes»
  // it by letting events through— and that the key exists.
  let e = estadoInicial()
  e = dar(e, { t: 'ui.abrir' }).estado
  igual(e.fase, 'arrancando', 'abrir no arranca')
  const d = dar(e, { t: 'voz.transcripcion', texto: 'hola, hola' })
  igual(d.estado.fase, 'arrancando', 'arrancando ha dejado de ser un muro')
  igual(tipos(d.efectos), [], 'arrancando produce efectos: ya no es un muro')
  igual(dar(e, { t: 'srv.credencial_ok' }).estado.fase, 'escuchando',
    'la llave para salir de arrancando no funciona')
})

// ── Verdict ─────────────────────────────────────────────────────────────────

const total = pasadas + fallos.length
if (fallos.length) {
  console.error(`\n✗ ${fallos.length} de ${total} fallan:\n`)
  for (const f of fallos) console.error('  · ' + f + '\n')
  process.exit(1)
}
console.log(`✓ ${pasadas} pruebas de la máquina de conversación, todas en verde`)
