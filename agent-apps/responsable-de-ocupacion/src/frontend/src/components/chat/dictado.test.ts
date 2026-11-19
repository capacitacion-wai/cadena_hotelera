/**
 * WEB DICTATION, verified without a browser (12.50.0).
 *
 *   node --experimental-strip-types src/components/chat/dictado.test.ts
 *
 * It runs with bare node, like `conversacion/maquina.test.ts`: zero new dependencies in
 * the project and, therefore, zero in every generated app.
 *
 * The only substantial thing here is how the segments given by the Web Speech API are
 * joined, and it is exactly what does not show when testing by hand: the API does not
 * deliver «the sentence», it delivers a list of segments where the last one is REWRITTEN
 * whole on every event. Concatenating everything that arrives duplicates half a sentence
 * —«Find a hotel Find a hotel in Madrid»— and keeping only the final ones leaves the
 * field frozen while you speak, which makes people think the microphone is not working.
 * Both approaches «look» correct reading the code.
 */
import { textoDe, porQue, dictar, type Reconocedor } from './dictado.ts'

let pasadas = 0
const fallos: string[] = []

function prueba(nombre: string, fn: () => void) {
  try { fn(); pasadas++ } catch (ex) { fallos.push(`${nombre}\n    ${(ex as Error).message}`) }
}
function igual<T>(a: T, b: T, msg: string) {
  if (JSON.stringify(a) !== JSON.stringify(b)) {
    throw new Error(`${msg}\n    esperaba ${JSON.stringify(b)}\n    y salió   ${JSON.stringify(a)}`)
  }
}

/** An event as the browser delivers it: a list of segments, each one with `isFinal`. */
const evento = (tramos: Array<[string, boolean]>) => ({
  results: tramos.map(([transcript, isFinal]) => ({ 0: { transcript }, isFinal, length: 1 })),
})

/** A fake recogniser, with the three hooks the piece uses. */
function falso() {
  const r: Reconocedor & { arrancado: number; parado: number } = {
    lang: '', continuous: false, interimResults: false,
    arrancado: 0, parado: 0,
    start() { this.arrancado++ },
    stop() { this.parado++; this.onend?.() },
    onresult: null, onerror: null, onend: null,
  }
  return r
}

// ── What gets joined ────────────────────────────────────────────────────────

prueba('el tramo provisional se REEMPLAZA, no se acumula', () => {
  igual(textoDe(evento([['Busca un hotel', false]]).results), 'Busca un hotel', 'el primero')
  igual(textoDe(evento([['Busca un hotel en Madrid', false]]).results),
    'Busca un hotel en Madrid',
    'el provisional se reescribe entero: acumularlo duplicaría la frase')
})

prueba('lo definitivo se conserva y lo provisional va detrás', () => {
  igual(
    textoDe(evento([['Busca un hotel en Madrid', true], ['para tres adultos', false]]).results),
    'Busca un hotel en Madrid para tres adultos',
    'quedarse solo con lo definitivo congela el campo mientras hablas',
  )
})

prueba('varios tramos definitivos se juntan en orden', () => {
  igual(
    textoDe(evento([['Hola', true], ['busca un hotel', true], ['en Madrid', false]]).results),
    'Hola busca un hotel en Madrid', 'el orden de la frase es el orden de los tramos',
  )
})

prueba('un evento vacío no borra lo que había', () => {
  igual(textoDe(evento([]).results), '', 'sin tramos, cadena vacía')
  igual(textoDe(undefined), '', 'sin evento tampoco revienta')
})

// ── What the person is told ───────────────────────────────────────────────────

prueba('«no dijiste nada» no es un fallo que contar', () => {
  igual(porQue('no-speech'), '', 'no-speech no se enseña')
  igual(porQue('aborted'), '', 'parar a mano tampoco')
  igual(porQue('not-allowed') !== '', true, 'la falta de permiso SÍ se explica')
  igual(porQue('vaya-usted-a-saber') !== '', true, 'lo desconocido se dice en corto')
})

// ── El ciclo entero ─────────────────────────────────────────────────────────

prueba('dictar: arranca, va escribiendo y al parar entrega lo dicho UNA vez', () => {
  const rec = falso()
  const visto: string[] = []
  let final: string | null = null
  const d = dictar({ onTexto: (t) => visto.push(t), onFin: (t) => { final = t }, onFallo: () => {} },
    () => rec)

  igual(rec.arrancado, 1, 'no arrancó')
  igual([rec.continuous, rec.interimResults], [true, true],
    'sin continuous se corta al primer silencio; sin interim el campo se queda vacío')

  rec.onresult?.(evento([['Busca un hotel', false]]))
  rec.onresult?.(evento([['Busca un hotel en Madrid', true]]))
  d.cancelar()

  igual(visto, ['Busca un hotel', 'Busca un hotel en Madrid'], 'lo que fue cayendo en el campo')
  igual(final, 'Busca un hotel en Madrid', 'lo que queda al parar')
  igual(rec.parado, 1, 'no se paró el reconocedor')
})

prueba('tras un error el cierre ocurre UNA sola vez', () => {
  const rec = falso()
  let veces = 0
  let motivo = ''
  dictar({ onTexto: () => {}, onFin: () => { veces++ }, onFallo: (m) => { motivo = m } }, () => rec)

  rec.onresult?.(evento([['algo', true]]))
  rec.onerror?.({ error: 'network' })
  rec.onend?.()
  rec.onend?.()          // the browser may emit it again; it must not count twice

  igual(veces, 1, 'el fin se entregó más de una vez: el campo se pisaría a sí mismo')
  igual(motivo !== '', true, 'un fallo de red sí se cuenta')
})

// ── Veredicto ───────────────────────────────────────────────────────────────

const total = pasadas + fallos.length
if (fallos.length) {
  console.error(`\n✗ ${fallos.length} de ${total} fallan:\n`)
  for (const f of fallos) console.error('  · ' + f + '\n')
  process.exit(1)
}
console.log(`✓ ${pasadas} pruebas del dictado de la web, todas en verde`)
