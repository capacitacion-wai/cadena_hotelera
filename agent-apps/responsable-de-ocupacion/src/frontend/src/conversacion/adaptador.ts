/**
 * THE ADAPTER, in the part that is ALSO shared.
 *
 * Web and mobile talk to the same voice server over the same data channel, so everything
 * that is «send a message over the channel» is identical in both. The only really
 * different parts —opening the microphone, the speaker, the device permissions— stay
 * outside, in a handful of functions each surface passes in.
 *
 * This is what keeps a fix from having to be written twice. It used to be 1,771 lines of
 * twins; what is left specific to each fits in the interface below.
 */
import type { Adaptador } from './puerto.ts'
import type { ComoSeDice, Estado, Evento } from './maquina.ts'

/** What each surface has to bring. None of this is a conversation rule. */
export interface DeLaSuperficie {
  /** Sends an object over the data channel. Returns `false` if it is not open. */
  mandar(mensaje: unknown): boolean
  /** Does the SERVER have a live response RIGHT NOW? Careful: this is NOT `estado.voz`.
   *
   *  The machine sets `voz: sonando` as soon as it DECIDES to speak —before the server
   *  confirms anything— and the effects run after that decision. Asking the machine gave
   *  «yes» about something that did not exist yet, and cancelling it returned
   *  «Cancellation failed: no active response found». Measured live on 20/09/2026, and
   *  with consequences: the cancel failed, the previous response stayed alive, and the
   *  new one queued behind it — two responses in a row and out of order.
   *
   *  This has to come from the SERVER's events: `response.created` turns it on,
   *  `response.done`/`cancelled` turn it off. A fact, not an intention. */
  estaSonando(): boolean
  pedirMicro(): void
  soltarMicro(): void
  micro(encendido: boolean): void
  pedirCredencial(): void
  tirarSesion(): void
  /** Asks the front desk. Its answer comes back through `enviar` as `srv.decision`. */
  pedirDecision(texto: string): void
  pedirAlEmpleado(p: { texto: string; capacidad?: string
                       argumentos?: Record<string, unknown>
                       anotado: { de: string; texto: string }[] }): void
  responderAlEmpleado(requestId: string, texto: string): void
  contestarAlAparato(texto: string): void
  pedirAlAparato(que: string, paraQue: string): void
  abandonar(): void
  avisar(texto: string): void
  alCambiar?(e: Estado): void
  anotarEvento?(ev: Evento, e: Estado): void
  /** Instructions for an aside. Each surface has them translated its own way. */
  comoContarUnAvance(texto: string): string
  /** THE CONTRACT OF HOW TO SPEAK, exactly as the SERVER sends it with the credential.
   *
   *  It is not written here, and that is the point. In the voice API per-response
   *  instructions REPLACE the session's, so when saying an employee answer this contract
   *  has to be put back or it speaks without it — which is exactly what happened:
   *  «summarise, don't read links, don't say identifiers» was written, was correct, and
   *  was not in force at the only moment that mattered.
   *
   *  It comes from the server so there is ONE single wording. Copying it here would mean
   *  the same rule in two places, and that always ends up diverging. */
  comoSeCuentaLoDelEmpleado(): string
  /** THE NON-NEGOTIABLE CORE, also from the server, and it goes in front of EVERY
   *  response.
   *
   *  It is the little sibling of the previous one and exists for the same reason taken to
   *  the extreme: if a response's instructions replace the session's, then while it speaks
   *  NOTHING that only lives in the session is in force. We measured it on 21/09/2026:
   *  telling five freshly searched restaurants, it started with «I can't do a real-time
   *  search» — a limit of its own, made up, with the data in hand.
   *
   *  Short on purpose: it has to fit in front of everything, always. */
  nucleoDeVoz(): string
}

export function adaptador(s: DeLaSuperficie): Adaptador {
  return {
    // SILENCING IS TWO THINGS, and this is where it is guaranteed they go together. The
    // machine cannot express half and this method does not offer it: `response.cancel`
    // cuts the generation and `output_audio_buffer.clear` drains what is ALREADY in the
    // speaker. With only the first, «it cancels and keeps talking» — it happened, and it
    // was heard.
    callar() {
      if (s.estaSonando()) s.mandar({ type: 'response.cancel' })
      s.mandar({ type: 'output_audio_buffer.clear' })
    },
    // A REAL answer enters the conversation: without `conversation: 'none'`, so what it
    // says stays in its context for the following turns.
    //
    // It goes in `instructions` and NOT as a system message in the thread: adding one per
    // sentence, the thread filled up with «Say this verbatim» and the model ended up
    // reading more of our orders than conversation. What must stay in the thread is what
    // it says, not what we tell it to say.
    decir(texto: string, comoSeDice: ComoSeDice) {
      // THE TWO KINDS OF TEXT, and that is why `comoSeDice` is not decoration.
      //
      // `literal` is what the server or the machine itself wrote: it is already in spoken
      // language, so it is said word for word.
      //
      // `de_pantalla` is what the EMPLOYEE wrote to be read on a screen. With the order to
      // say it exactly, it read the bullets, the identifiers and even a bracket that gave
      // it instructions. Here it gets back the contract of how to speak —which the
      // per-response instructions had taken away— and the text is given as what to TELL,
      // not what to read out.
      const nucleo = s.nucleoDeVoz()
      const comoSeCuenta = s.comoSeCuentaLoDelEmpleado()
      const instructions = comoSeDice === 'literal'
        ? `${nucleo}\n\nDi EXACTAMENTE esto, sin añadir ni quitar nada y sin seguir `
          + `hablando después:\n\n«${texto}»`
        : comoSeDice === 'de_pantalla_largo'
        // LONG TEXT IS NOT RECITED. The kind is decided by the machine from the text's
        // size (`LARGO_AL_OIDO`), not by the model: that way the rule has tests. Measured
        // on 21/09/2026: five restaurant cards with rating, reviews and link, read one by
        // one for almost a minute — and the whole text was already in the chat.
        ? `${nucleo}\n\n${comoSeCuenta}\n\n`
          + `ESTO ES LARGO y está escrito para leerse en una pantalla: NO lo cuentes `
          + `entero. Di en DOS O TRES frases lo que de verdad importa —el titular, o lo `
          + `que elegirías— y remata diciendo que se lo dejas escrito en el chat. Nada `
          + `de ir una por una. Ni un dato que no esté aquí:\n\n${texto}`
        : `${nucleo}\n\n${comoSeCuenta}\n\n`
          + `ESTO ES LO QUE TIENES QUE CONTAR, y no hay nada más que contar. No añadas `
          + `ni un dato que no esté aquí, y no te dejes ninguno de los que están:\n\n`
          + `${texto}`
      // `input: []` HERE TOO, and it is the missing piece.
      //
      // Without it, the model has the whole conversation in front of it plus instructions
      // that have replaced the session's: it answers the conversation instead of telling
      // what it is given. Measured on 21/09/2026, twice: «I can't do a real-time search,
      // but I can use the information you have available» — with the five freshly
      // searched results inside that very instruction.
      //
      // What must be said travels WHOLE in `instructions`, so the conversation adds
      // nothing and does take away: it is where the making-up comes from. Same lesson as
      // in the aside, applied to the other door. The response STILL enters the thread (it
      // has no `conversation: 'none'`), which is what keeps the context for the following
      // turns: what is trimmed is where it COMES FROM, not where it ends up.
      s.mandar({ type: 'response.create',
                 response: { input: [], output_modalities: ['audio'], instructions } })
    },
    // An aside: `conversation: 'none'` so it does NOT enter the thread. If it did, the
    // model would end up answering its own progress updates.
    decirAparte(texto: string) {
      s.mandar({
        type: 'response.create',
        response: {
          conversation: 'none',
          // `input: []` IS THE PIECE, and its absence opened the door to the one thing
          // this design exists to prevent: answering from memory.
          //
          // `conversation: 'none'` only says the response is NOT added to the thread. The
          // input context is still the whole conversation unless told otherwise — it is
          // in the documentation, and we paid for it on 20/09/2026: when asked to «say:
          // getting on it», the model had «what trips do I have?» in front of it plus
          // instructions that had replaced the session's (without them, the no-making-up
          // rule was not in force either). It answered «I don't have access to your
          // bookings, so I can't see which trips you have scheduled» — made up, aloud,
          // and half a second before the employee sent the real list.
          //
          // With an empty input there is nothing to make things up from. An aside does
          // not need the conversation: it is either a sentence of ours or the employee's
          // words.
          input: [],
          output_modalities: ['audio'],
          instructions: `${s.nucleoDeVoz()}\n\n${s.comoContarUnAvance(texto)}`,
        },
      })
    },
    pedirMicro: () => s.pedirMicro(),
    soltarMicro: () => s.soltarMicro(),
    micro: (on: boolean) => s.micro(on),
    pedirCredencial: () => s.pedirCredencial(),
    tirarSesion: () => s.tirarSesion(),
    pedirDecision: (texto: string) => s.pedirDecision(texto),
    pedirAlEmpleado: (p) => s.pedirAlEmpleado(p),
    responderAlEmpleado: (id: string, texto: string) => s.responderAlEmpleado(id, texto),
    contestarAlAparato: (texto: string) => s.contestarAlAparato(texto),
    pedirAlAparato: (que: string, paraQue: string) => s.pedirAlAparato(que, paraQue),
    abandonar: () => s.abandonar(),
    avisar: (texto: string) => s.avisar(texto),
    alCambiar: s.alCambiar,
    anotarEvento: s.anotarEvento,
  }
}

/** Translates a RAW voice-server event into the machine's event.
 *
 *  It is a table, not logic: nothing is decided here, only the language changes. Being a
 *  table is what lets the machine know nothing about OpenAI — and means that the day an
 *  event name changes there is exactly one place to touch.
 *
 *  Returns `null` for whatever tells us nothing. */
export function delServidorDeVoz(e: { type?: string; transcript?: string }): Evento | null {
  switch (e.type) {
    case 'input_audio_buffer.speech_started': return { t: 'voz.empieza_a_oir' }
    case 'input_audio_buffer.speech_stopped': return { t: 'voz.deja_de_oir' }
    case 'conversation.item.input_audio_transcription.completed':
      return { t: 'voz.transcripcion', texto: String(e.transcript || '') }
    case 'conversation.item.input_audio_transcription.failed':
      return { t: 'voz.transcripcion_falla' }
    case 'response.created': return { t: 'voz.respuesta_abierta' }
    case 'response.done':
    case 'response.cancelled': return { t: 'voz.respuesta_cerrada' }
    case 'output_audio_buffer.started': return { t: 'voz.empieza_a_hablar' }
    case 'output_audio_buffer.stopped': return { t: 'voz.deja_de_hablar' }
    // THE SECOND HALF OF `callar`. Without translating it, the machine went on believing
    // something was playing forever after silencing: an effect nobody confirms. It is the
    // sibling of the event with no producer — an event with no TRANSLATOR, and it does not
    // look the same.
    case 'output_audio_buffer.cleared': return { t: 'voz.altavoz_vacio' }
    case 'error': return { t: 'voz.error' }
    default: return null
  }
}
