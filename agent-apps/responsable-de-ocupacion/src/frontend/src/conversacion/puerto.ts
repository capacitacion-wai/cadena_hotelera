/**
 * THE PORT: the only thing each surface has to know how to do.
 *
 * The machine (`maquina.ts`) decides and emits effects; here it is declared who runs them
 * and the ENGINE joining the two is assembled. The engine also carries the clock —
 * deadlines and heartbeat—, because they are rules of the conversation and not of the
 * platform: if they lived in each component, they would diverge again.
 *
 * What is left in each adapter is what really differs between a browser and a phone:
 * opening the microphone, sending over the channel, asking the device for something. Not
 * one conversation rule.
 */
import {
  paso, estadoInicial,
  type ComoSeDice, type Estado, type Evento, type Efecto,
} from './maquina.ts'

/** What each surface implements. One function per effect, and nothing else. */
export interface Adaptador {
  /** SILENCING IS TWO THINGS, and that is why they arrive together: cancel the generation
   *  and drain the speaker. With only one, «it cancels and keeps talking» — measured. The
   *  adapter cannot do half because the machine cannot express it. */
  callar(): void
  /** Opens a REAL answer: it enters the conversation. `comoSeDice` says whether the text
   *  is read as is (the server wrote it) or TOLD (the employee wrote it for a screen).
   *  There is no option to leave it out: the machine always declares it. */
  decir(texto: string, comoSeDice: ComoSeDice): void
  /** An aside while the employee works: `conversation: 'none'`, outside the thread. */
  decirAparte(texto: string): void
  pedirMicro(): void
  soltarMicro(): void
  micro(encendido: boolean): void
  pedirCredencial(): void
  tirarSesion(): void
  /** Asks the front desk what to do with this. The answer comes back as an event. */
  pedirDecision(texto: string): void
  pedirAlEmpleado(p: { texto: string; capacidad?: string
                       argumentos?: Record<string, unknown>
                       anotado: { de: string; texto: string }[] }): void
  /** Resumes the employee's turn that was PAUSED waiting for this answer. */
  responderAlEmpleado(requestId: string, texto: string): void
  /** Answers what the employee asked from the device: the data, or the reason why not. */
  contestarAlAparato(texto: string): void
  pedirAlAparato(que: string, paraQue: string): void
  /** The query in flight no longer matters: whatever arrives will not be narrated. */
  abandonar(): void
  avisar(texto: string): void
  /** For rendering. Called on every state change. */
  alCambiar?(e: Estado): void
  /** Optional: the event log, which is what makes it possible to REPLAY a bug afterwards
   *  instead of asking for a three-hundred-line trace and guessing. */
  anotarEvento?(ev: Evento, e: Estado): void
}

// The DEADLINES and the SILENCE now live in `maquina.ts`, with the other conversation
// rules. They were here, and with them the memory of when each wait started: a second
// hidden little machine, with no tests, that fired the heartbeat 0 ms after requesting.
// What decides gets tested; and to be tested, it has to be inside.

export interface Motor {
  enviar(ev: Evento): void
  estado(): Estado
  /** The clock, called by whoever has one (a `setInterval` of the surface). */
  tic(ahora: number): void
  parar(): void
}

export function crearMotor(a: Adaptador, estadoDePartida?: Estado): Motor {
  let e = estadoDePartida || estadoInicial()
  let vivo = true

  function ejecutar(efs: Efecto[]) {
    for (const f of efs) {
      switch (f.t) {
        case 'callar': a.callar(); break
        case 'decir': a.decir(f.texto, f.comoSeDice); break
        case 'decirAparte': a.decirAparte(f.texto); break
        case 'pedirMicro': a.pedirMicro(); break
        case 'soltarMicro': a.soltarMicro(); break
        case 'micro': a.micro(f.encendido); break
        case 'pedirCredencial': a.pedirCredencial(); break
        case 'tirarSesion': a.tirarSesion(); break
        case 'pedirDecision': a.pedirDecision(f.texto); break
        case 'pedirAlEmpleado':
          a.pedirAlEmpleado({ texto: f.texto, capacidad: f.capacidad,
                              argumentos: f.argumentos, anotado: f.anotado }); break
        case 'responderAlEmpleado': a.responderAlEmpleado(f.requestId, f.texto); break
        case 'contestarAlAparato': a.contestarAlAparato(f.texto); break
        case 'pedirAlAparato': a.pedirAlAparato(f.que, f.paraQue); break
        case 'abandonar': a.abandonar(); break
        case 'avisar': a.avisar(f.texto); break
      }
    }
  }

  function enviar(ev: Evento, ahora = Date.now()) {
    if (!vivo) return
    const d = paso(e, ev, ahora)
    e = d.estado
    a.anotarEvento?.(ev, e)
    ejecutar(d.efectos)
    a.alCambiar?.(e)
  }

  return {
    enviar,
    estado: () => e,
    // THE CLOCK DOES NOT DECIDE: it says what time it is. Whether that means a heartbeat,
    // an expired deadline or nothing is decided by the machine — which is where the tests
    // are. It used to choose here, and that is why the heartbeat could fire 0 ms after
    // requesting without anything screaming.
    tic(ahora: number) {
      if (!vivo) return
      enviar({ t: 'reloj.tic' }, ahora)
    },
    parar() { vivo = false },
  }
}
