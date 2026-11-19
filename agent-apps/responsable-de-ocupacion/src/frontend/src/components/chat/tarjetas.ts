/**
 * Cards: data the employee emits and that this interface paints its own way.
 *
 * Twin of the mobile reader (`wai-mobile/src/lib/tarjetas.ts`) and of the server emitter
 * (`app/tarjetas.py`). They are three places and the schema is ONE: if they diverge, the
 * server rules, since it is the one that validates and prunes before this sees it.
 *
 * It travels inside the message —not as a channel event— so that it is still there when a
 * thread is reopened: the history repaints the text, not the process.
 */
export const TIPOS_QUE_PINTO = ['lugar', 'ruta'] as const

export interface PuntoDeTarjeta { nombre: string; lat?: number; lon?: number; direccion?: string }
export interface TarjetaLugar extends PuntoDeTarjeta {
  tipo: 'lugar'; v: 1; enlaces?: Record<string, string>
}
export interface TarjetaRuta {
  tipo: 'ruta'; v: 1
  modo: '' | 'andando' | 'coche' | 'transporte' | 'bici'
  paradas: PuntoDeTarjeta[]
  enlaces?: Record<string, string>
}
export type Tarjeta = TarjetaLugar | TarjetaRuta
export type Trozo = { que: 'texto'; texto: string } | { que: 'tarjeta'; tarjeta: Tarjeta }

const APERTURA = '```wai-card'

function valida(x: any): Tarjeta | null {
  if (!x || typeof x !== 'object' || x.v !== 1) return null
  if (x.tipo === 'lugar') return typeof x.nombre === 'string' && x.nombre ? x : null
  if (x.tipo === 'ruta') return Array.isArray(x.paradas) && x.paradas.length >= 2 ? x : null
  return null
}

/** An opened and unclosed block is discarded: half a JSON is not shown during streaming. */
export function partirEnTrozos(texto: string): Trozo[] {
  if (!texto || !texto.includes(APERTURA)) return [{ que: 'texto', texto }]
  const fuera: Trozo[] = []
  let resto = texto
  for (;;) {
    const i = resto.indexOf(APERTURA)
    if (i < 0) { if (resto) fuera.push({ que: 'texto', texto: resto }); break }
    if (i > 0) fuera.push({ que: 'texto', texto: resto.slice(0, i) })
    const desde = resto.slice(i + APERTURA.length)
    const j = desde.indexOf('```')
    if (j < 0) break
    try {
      const t = valida(JSON.parse(desde.slice(0, j).trim()))
      if (t) fuera.push({ que: 'tarjeta', tarjeta: t })
    } catch {
      // Unreadable: the prose stays, which stands on its own.
    }
    resto = desde.slice(j + 3)
  }
  return fuera
}
