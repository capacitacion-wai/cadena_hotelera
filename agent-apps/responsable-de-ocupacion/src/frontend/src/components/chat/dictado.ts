/**
 * DICTATION in the browser: you speak and it is written into the field. It sends nothing,
 * nobody answers.
 *
 * It is the other half of voice, and until now it did not exist on the web: the composer's
 * microphone led to the CONVERSATION —the employee speaking, turns, cost per minute—, which
 * is a lot of machinery for «remind me on Monday to call Marta». Since 12.50.0 they are two
 * different things and they look different, just as in the WAI mobile app: a mic that writes
 * and a button that opens the conversation.
 *
 * THE BROWSER DOES IT (Web Speech API), not a server of ours:
 *
 * · **it costs no credits** — dictating is so frequent that charging per minute would kill it;
 * · **it starts instantly** — the conversation negotiates a WebRTC pipe and takes ~1 s, which
 *   is noticeable when dictating: by the time it connects, you have said half a sentence.
 *
 * In exchange it is less accurate with unusual proper names, and **not every browser has it**
 * (Firefox does not). Hence `soportaDictado()`: where it is missing, the button is not painted
 * — a button that does nothing is worse than not having it.
 *
 * The recogniser can be INJECTED (`crear`). It is not decoration for the tests: it is what
 * makes it possible to verify without a browser the only substantial thing here, which is
 * how the final segments are joined with the interim one.
 */

/** The minimum this piece needs from a recogniser. It is a subset of `SpeechRecognition`,
 *  written by hand because the DOM types do not include it in every TypeScript version and
 *  we are not adding a dependency for this. */
export interface Reconocedor {
  lang: string
  continuous: boolean
  interimResults: boolean
  start(): void
  stop(): void
  onresult: ((e: any) => void) | null
  onerror: ((e: any) => void) | null
  onend: (() => void) | null
}

export interface Dictado {
  /** Stops listening. What has been said so far stays in the field. */
  cancelar: () => void
}

function delNavegador(): (new () => Reconocedor) | null {
  if (typeof window === 'undefined') return null
  const w = window as any
  return w.SpeechRecognition || w.webkitSpeechRecognition || null
}

/** Can this browser dictate? Firefox, today, cannot. */
export function soportaDictado(): boolean {
  return delNavegador() !== null
}

/**
 * THE TEXT OF AN EVENT, which is the only meaty part of all this.
 *
 * The API does not give «the sentence»: it gives a list of segments, and each one can be
 * FINAL or INTERIM. The interim one is rewritten whole on every event —«mar», «Madrid»,
 * «Madrid para tres»—, so accumulating everything that arrives duplicates half a sentence;
 * keeping only the final ones leaves the field frozen while you speak, which makes people
 * think it does not hear them. The final ones are joined and the interim one is appended AT
 * THE END, which is exactly what a well-made dictation shows.
 */
export function textoDe(resultados: any): string {
  const firmes: string[] = []
  let enVuelo = ''
  const n = Number(resultados?.length || 0)
  for (let i = 0; i < n; i++) {
    const tramo = resultados[i]
    const texto = String(tramo?.[0]?.transcript || '').trim()
    if (!texto) continue
    if (tramo?.isFinal) firmes.push(texto)
    else enVuelo = texto
  }
  return [...firmes, enVuelo].filter(Boolean).join(' ')
}

/** Why dictation could not happen, phrased for a person. Raw codes tell nobody anything,
 *  and «no-speech» is not even a failure: it means you did not say anything. */
export function porQue(codigo: string): string {
  switch (codigo) {
    case 'not-allowed':
    case 'service-not-allowed':
      return 'El navegador no deja usar el micrófono. Dale permiso en la barra de direcciones.'
    case 'audio-capture':
      return 'No se encontró ningún micrófono.'
    case 'network':
      return 'El dictado necesitó la red y no la hay.'
    case 'no-speech':
      return ''
    case 'aborted':
      return ''
    default:
      return 'No se pudo dictar.'
  }
}

/**
 * Starts dictation. Returns how to stop it.
 *
 * `onTexto` receives the whole sentence every time it grows; `onFin` is called ONCE when it
 * ends —by hand, by silence or after an error—; `onFallo`, only if there is something to tell.
 */
export function dictar(
  { idioma = 'es-ES', onTexto, onFin, onFallo }: {
    idioma?: string
    onTexto: (texto: string) => void
    onFin: (texto: string) => void
    onFallo: (motivo: string) => void
  },
  crear?: () => Reconocedor,
): Dictado {
  const Clase = delNavegador()
  if (!crear && !Clase) {
    onFallo('Este navegador no sabe dictar.')
    onFin('')
    return { cancelar: () => {} }
  }
  const rec: Reconocedor = crear ? crear() : new (Clase as new () => Reconocedor)()
  rec.lang = idioma
  // It does NOT stop at the first silence: when dictating you think mid-sentence, and cutting
  // off because you hesitated for two seconds is the most infuriating failure.
  rec.continuous = true
  // What is being recognised, not only the final result: without this the field stays empty
  // while you speak and it looks like it does not hear you.
  rec.interimResults = true

  let ultimo = ''
  let cerrado = false
  const fin = () => {
    if (cerrado) return
    cerrado = true
    rec.onresult = null
    rec.onerror = null
    rec.onend = null
    onFin(ultimo)
  }

  rec.onresult = (e: any) => {
    const texto = textoDe(e?.results)
    if (!texto) return
    ultimo = texto
    onTexto(texto)
  }
  rec.onerror = (e: any) => {
    const motivo = porQue(String(e?.error || ''))
    if (motivo) onFallo(motivo)
    // `end` arrives after an error anyway; the closing lives there and only there.
  }
  // `end` ALWAYS arrives —when stopped, when the silence runs out and after an error—, so
  // the closing goes here: spread over the three paths, one would be forgotten.
  rec.onend = fin

  try {
    rec.start()
  } catch {
    // Starting the same recogniser twice throws. It is not a failure to tell anyone about.
    fin()
  }

  return { cancelar: () => { try { rec.stop() } catch { fin() } } }
}
