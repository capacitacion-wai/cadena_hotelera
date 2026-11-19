/**
 * WHAT IS TOLD WHILE THE EMPLOYEE WORKS, decided with PURE functions.
 *
 * It lives here, next to the machine, and not inside the screen, for the same reason as
 * the machine: whatever DECIDES something has to be testable. These four functions choose
 * what comes out of the speaker while waiting, and while they were loose inside a
 * 1,700-line component there was no way to write a test for them — so the bugs only
 * showed up while talking, live and with a customer watching.
 *
 * THE BUG THAT BRINGS THEM HERE (21/09/2026). The speaker said, in Spanish and English at
 * once: «Estoy con Read the tool output file
 * /var/opencode-data/opencode/tool-output/tool_0c2ea01d6001hbeoBGQ y ha devuelto un texto
 * largo». Two bugs in one sentence: reading the file where the agent leaves its own output
 * is not work (it is its kitchen), and a path with an identifier inside is never said
 * aloud. Both things are decided here, and now both have a test.
 *
 * The file is BYTE FOR BYTE the same on web and mobile, like `maquina.ts`: a rule written
 * twice diverges by the third change.
 */

/** WHERE it is looking, from the tool's name. It is the only thing said about the
 *  system: «on the map», «in your calendar» — never its technical name. */
export function dondeMira(herramienta: string): string {
  const h = herramienta.toLowerCase()
  const familias: [string[], string][] = [
    [['maps', 'directions', 'geocod'], 'el mapa'],
    [['calendar', 'agenda', 'event'], 'tu agenda'],
    [['search', 'browser', 'tavily', 'exa'], 'internet'],
    [['mail', 'gmail', 'correo'], 'el correo'],
    [['drive', 'sheet', 'docs'], 'documentos'],
    [['odoo', 'erp', 'codigo10', 'multibase', 'sap'], 'el ERP'],
    [['crm', 'kommo'], 'el CRM'],
    [['sql', 'postgres', 'query'], 'los datos'],
    [['contexto_usuario', 'viaje', 'trip'], 'tus viajes'],
  ]
  for (const [claves, donde] of familias) if (claves.some((c) => h.includes(c))) return donde
  return ''
}

/** Is this call WORK, or the agent reading its own scaffolding?
 *
 *  Reading a skill, asking for the tool catalog or its schemas, looking at session
 *  metadata… is not work: it is plumbing. Telling it produced «I'm looking for
 *  recommendations-routes-memories» (its own manual) and «I'm looking on the map» over a
 *  `tool_schemas` listing.
 *
 *  AND NEITHER ARE ITS FILE TOOLS. Reading the file where it left another tool's output,
 *  jotting down a task, listing a directory: its kitchen. The name is compared WITHOUT
 *  the server prefix, which is how they arrive (`wai-viajes_read` and `read` are the same
 *  thing). */
export function esFontaneria(herramienta: string, salida?: unknown): boolean {
  const h = herramienta.toLowerCase()
  if (/skill|tool_schema|list_tools|get_tools|_schema|manage_connections|integrations/.test(h)) return true
  const corto = h.replace(/^[a-z0-9-]+_/, '')
  if (/^(read|write|edit|multiedit|patch|bash|glob|grep|ls|list|todowrite|todoread|task)$/.test(corto)) return true
  const txt = typeof salida === 'string' ? salida : ''
  return txt.startsWith('<skill_content') || txt.includes('"tool_schemas"')
}

/** IS THIS VALUE MACHINE STUFF? Paths, links, identifiers and file names are not said
 *  aloud: they inform nobody and sound like a breakdown.
 *
 *  The CONTENT is checked, not the field name. That is how the path of 21/09/2026 slipped
 *  through: the filter back then discarded keys named `path` or `url`, and that value came
 *  in one named differently. */
export function esDeLaMaquina(v: string): boolean {
  return /\/[\w.-]+\//.test(v)                                  // a path
    || /:\/\//.test(v)                                          // a link
    // An identifier: 16+ characters in a row WITH some digit. The digit is what tells
    // «tool_0c2ea01d6001hbeoBGQ» apart from «responsabilidades» (Spanish for
    // "responsibilities"), which also has sixteen-plus letters and is an ordinary word.
    || /\b(?=[0-9a-z]*\d)[0-9a-z]{16,}\b/i.test(v)
    || /\.(json|txt|log|csv|md|ya?ml|png|jpe?g|pdf)\b/i.test(v) // a file
    || /tool[_ -]?output|opencode/i.test(v)
}

/** IS THIS IN ANOTHER LANGUAGE? The agent phrases its searches in English —«find and
 *  compare the best restaurants in Moratalaz, Madrid, with current ratings, cuisine»— and
 *  that came out of the speaker verbatim, inside a Spanish sentence (21/09/2026). Asking
 *  the voice to translate it leaves the fix in the model's hands; discarding it here is
 *  deterministic. A nuance is lost («searching for restaurants in Moratalaz» becomes
 *  «searching the internet»); what is gained is that it never sounds in English again.
 *
 *  The test is crude on purpose: TWO English function words that do not exist in Spanish.
 *  It does not detect languages; it detects what must not be said. */
export function enOtroIdioma(v: string): boolean {
  const golpes = (v.toLowerCase().match(/\b(the|and|with|for|from|your|this|that|are|you|what|which|how|of|to|is|in|best)\b/g) || [])
  return new Set(golpes).size >= 2
}

/** What came out, told WITHOUT JSON. Before, 700 characters of raw structure travelled
 *  inside the material to narrate; the conversational model cannot interpret it and
 *  filled the gap by making up the state («I don't have an answer yet»). Here the ONLY
 *  thing that needs saying is computed: whether something came out and how much. */
export function queSalio(salida: unknown, huboError: boolean): string {
  if (huboError) return 'y ha dado un error'
  if (salida == null) return 'y no ha devuelto nada'
  if (Array.isArray(salida)) return salida.length ? `y salen ${salida.length}` : 'y no sale ninguno'
  if (typeof salida === 'string') {
    const t = salida.trim()
    if (!t) return 'y no ha devuelto nada'
    // A short text CAN be said as is: it is usually the sentence that matters.
    return t.length <= 160 ? `y dice: ${t.replace(/\s+/g, ' ')}` : 'y ha devuelto un texto largo'
  }
  if (typeof salida === 'object') {
    const filas = Object.values(salida as Record<string, unknown>)
      .find((v) => Array.isArray(v)) as unknown[] | undefined
    if (Array.isArray(filas)) return filas.length ? `y salen ${filas.length}` : 'y no sale ninguno'
    return 'y ha devuelto datos'
  }
  return 'y ha devuelto algo'
}

/** WHAT IT IS DOING, in a person's language, from the REAL arguments.
 *
 *  It comes from the arguments and not from the name: «the route from Atocha to Madrid
 *  Río» says far more than «I'm checking», and it is the difference between keeping
 *  company and filling. It arrives when the call STARTS, so the present tense is true.
 *
 *  The arguments carry `run_token`, which identifies the person, so showing whatever comes
 *  is NOT acceptable here: it is filtered by what it is, not by what the field is called. */
export function queHace(herramienta: string, args: unknown): string {
  const donde = dondeMira(herramienta)
  const plano: Record<string, unknown> = {}
  const aplana = (o: unknown, prof = 0) => {
    if (prof > 3 || !o) return
    if (Array.isArray(o)) { o.slice(0, 4).forEach((x) => aplana(x, prof + 1)); return }
    if (typeof o !== 'object') return
    for (const [k, v] of Object.entries(o as Record<string, unknown>)) {
      const clave = k.toLowerCase()
      if (typeof v === 'string' || typeof v === 'number') {
        if (!(clave in plano)) plano[clave] = v
      } else aplana(v, prof + 1)
    }
  }
  aplana(args)
  const pide = (...cs: string[]) => {
    for (const c of cs) {
      const v = plano[c]
      if (typeof v === 'string' && v.trim() && !esDeLaMaquina(v) && !enOtroIdioma(v)) {
        return v.trim().replace(/\s+/g, ' ').slice(0, 90)
      }
      if (typeof v === 'number') return String(v)
    }
    return ''
  }
  const o = pide('origin', 'origen', 'origin_address')
  const d = pide('destination', 'destino', 'destination_address')
  if (o && d) return `Estoy mirando la ruta de ${o} a ${d}${donde ? ' en ' + donde : ''}.`
  const q = pide('use_case', 'query', 'q', 'search', 'texto', 'text', 'pregunta',
                 'question', 'address', 'direccion', 'place', 'lugar', 'nombre',
                 'name', 'titulo', 'title', 'keyword', 'termino')
  if (q) return `Estoy buscando ${q}${donde ? ' en ' + donde : ''}.`

  // THE CLOSED LIST ENDS HERE, and it was why everything sounded the same: if the tool
  // uses none of those names —and most connectors use their own— it fell back to «I'm
  // looking on the map» over and over. Now, when none matches, the value that says the
  // MOST is taken: the longest text that looks like a person's.
  const suena = Object.entries(plano)
    .filter(([k, v]) => typeof v === 'string'
      && !/(^|_)(id|uuid|key|token|hash|url|path|ref|cursor|page|limit|offset)$/.test(k)
      && !esDeLaMaquina(v as string)
      && !enOtroIdioma(v as string)
      && (v as string).trim().length >= 4)
    .map(([, v]) => (v as string).trim().replace(/\s+/g, ' '))
    .sort((a, b) => b.length - a.length)[0]
  if (suena) return `Estoy con ${suena.slice(0, 90)}${donde ? ' en ' + donde : ''}.`

  // And if there really is NOTHING to say, the action is said by its name instead of the
  // place: «I'm checking availability» makes sense; «I'm looking in the ERP», said for
  // the sixth time, does not.
  const accion = herramienta.replace(/^[a-z0-9-]+_/i, '').replace(/[_.-]+/g, ' ').trim()
  if (accion) return `Estoy con ${accion.toLowerCase()}${donde ? ' en ' + donde : ''}.`
  return donde ? `Estoy mirando en ${donde}.` : ''
}

/** WHAT WAS LEFT ON THE SCREEN, told ONCE and by its number.
 *
 *  THE MEASURED BUG (21/09/2026): five places in one answer were told one by one and
 *  joined with «and», so the speaker said «a map with the address and a map with the
 *  address and a map with the address and a map with the address and a map with the
 *  address». What must be said is HOW MANY there are.
 *
 *  The phrases come from the caller (already translated): here it only counts and picks
 *  singular or plural, which is the testable part. */
export function contarTarjetas(
  tipos: ('ruta' | 'lugar')[],
  frases: { ruta: string; rutas: string; lugar: string; lugares: string },
): string {
  const trozos = (['ruta', 'lugar'] as const).map((tipo) => {
    const n = tipos.filter((x) => x === tipo).length
    if (!n) return ''
    if (n === 1) return tipo === 'ruta' ? frases.ruta : frases.lugar
    return (tipo === 'ruta' ? frases.rutas : frases.lugares).replace(/\{\{n\}\}/g, String(n))
  }).filter(Boolean)
  return trozos.join(' y ')
}
