/**
 * Normalize assistant markdown before ReactMarkdown renders it.
 * Models often stream list items on one line or split an intro clause
 * across reasoning vs answer channels.
 */

/** Join reasoning chunks without repeating overlapping text. */
export function appendReasoningDeduped(existing: string, extra: string): string {
  const base = existing || ''
  const add = extra || ''
  if (!add.trim()) return base
  if (!base.trim()) return add
  if (base.includes(add)) return base
  if (add.includes(base)) return add
  const maxOverlap = Math.min(base.length, add.length)
  for (let i = maxOverlap; i > 0; i -= 1) {
    if (base.endsWith(add.slice(0, i))) {
      return base + add.slice(i)
    }
  }
  return base + add
}

// Tokens we must NEVER rewrite character-by-character: inline code,
// fenced code, URLs, file paths, filenames-with-extension and hash/id
// blobs. Splitting inside these produced the "adminwaicom: 98e4…"
// corruption in the answer bubble.
const PROTECT_RE = new RegExp(
  [
    '```[\\s\\S]*?```',                       // fenced code
    '`[^`]+`',                                // inline code
    'https?://\\S+',                          // urls
    '(?:/[\\w.\\-]+)+/?',                      // absolute paths
    '[\\w\\-]+(?:/[\\w.\\-]+)+',               // relative paths
    // filename.ext — LOWERCASE extension only. A case-insensitive
    // class wrongly matched Spanish sentence boundaries like
    // "severidades.Ahora" / "mínimo.Si" (accents break \w, leaving an
    // ascii tail "dades.Ahora" that looked like name.ext), which then
    // PROTECTED the glued sentence from the missing-space fix. Real
    // file extensions are lowercase, so requiring [a-z0-9] keeps
    // "report.pdf" protected while letting "word.Capital" be fixed.
    '[\\w\\-]+\\.[a-z0-9]{2,5}\\b',           // filename.ext
    '\\b[A-Fa-f0-9]{8,}\\b',                  // hex/hash blobs
  ].join('|'),
  'g',
)

function fixProse(s: string): string {
  let out = s
  // Sentence punctuation glued to the next sentence ("Hecho.Archivo" →
  // "Hecho. Archivo"). Safe here because paths/urls are already masked.
  out = out.replace(/([.!?])([A-ZÁÉÍÓÚ¿¡])/g, '$1 $2')
  // Glued words: lowercase→Uppercase (bajasLas → bajas Las).
  out = out.replace(/([a-záéíóúñ])([A-ZÁÉÍÓÚ][a-záéíóúñ])/g, '$1 $2')
  // Word glued to an acronym ("MédicoS.L." → "Médico S.L.", "ventasS.A." →
  // "ventas S.A."). Matches a lowercase tail immediately followed by an
  // ``Upper.Upper`` initialism. Code/paths/urls are already masked above.
  out = out.replace(/([a-záéíóúñ])([A-ZÁÉÍÓÚ]\.[A-ZÁÉÍÓÚ])/g, '$1 $2')
  // Word glued to a number (Incluye7 → Incluye 7, ventas2026 → ventas 2026).
  out = out.replace(/([A-Za-zÁÉÍÓÚáéíóúñ])(\d)/g, '$1 $2')
  out = out.replace(/(\d)([A-Za-zÁÉÍÓÚ][a-záéíóúñ])/g, '$1 $2')
  // Metric glued to a colon ("stock:-20" → "stock: -20").
  out = out.replace(/:-(\d)/g, ': -$1')
  return out
}

/** Fix inline bullets and glued words WITHOUT corrupting code/paths. */
export function normalizeChatMarkdown(text: string): string {
  if (!text) return text
  let s = text.replace(/\r\n/g, '\n')
  // Block-level fixes are path-safe (operate on bullet/colon structure).
  // Require a NON-space char before the dash so we never strip the
  // indentation of a nested bullet ("  - sub" must stay indented or the
  // nested list collapses into a flat one).
  s = s.replace(/([^\s])(-\s+\S)/g, '$1\n$2')
  s = s.replace(/:\s*\n?-\s+/g, ':\n\n- ')
  // Collapse whitespace-only lines ("\n  \n" hard-break gaps the model
  // emits between list items) into clean blank lines.
  s = s.replace(/\n[ \t]+\n/g, '\n\n')
  // Tighten lists: remove blank lines BETWEEN consecutive bullet items so
  // ReactMarkdown renders one tight list instead of many one-item lists
  // with visible gaps. The colon→first-item break is preserved (its
  // preceding line is not a bullet). Loop because matches overlap.
  let prevS = ''
  while (prevS !== s) {
    prevS = s
    s = s.replace(/(\n[*-] [^\n]+)\n\s*\n(?=[ \t]*[*-] )/g, '$1\n')
  }
  // Separate a trailing paragraph from the list it follows. A list item
  // followed by a single newline + ordinary prose (no bullet/number
  // marker) is parsed as a lazy continuation of the last <li>, so the
  // closing sentence renders GLUED inside the bullet ("…del agente\nNo
  // tengo acceso"). Insert the blank line that closes the list.
  s = s.replace(/(\n(?:[*-]|\d+\.) [^\n]+)\n(?![ \t]*(?:[*-]|\d+\.) )(?=\S)/g, '$1\n\n')

  // Character-level prose fixes only on UNPROTECTED segments.
  let result = ''
  let last = 0
  PROTECT_RE.lastIndex = 0
  let m: RegExpExecArray | null
  // eslint-disable-next-line no-cond-assign
  while ((m = PROTECT_RE.exec(s)) !== null) {
    if (!m[0]) { PROTECT_RE.lastIndex += 1; continue }
    result += fixProse(s.slice(last, m.index))
    result += m[0]
    last = m.index + m[0].length
  }
  result += fixProse(s.slice(last))
  return result
}

/** Rejoin answer text when reasoning ended mid-phrase. */
export function recoverFinalAnswerPrefix(
  steps: { reasoning: string }[],
  answer: string,
): string {
  const trimmed = answer.trimStart()
  if (!trimmed) return answer

  const looksComplete =
    /^[A-ZÁÉÍÓÚ¿¡"']/.test(trimmed)
    && !/^-\s/.test(trimmed)
    && !/^[a-z]{1,5}:\s*-/i.test(trimmed)
  if (looksComplete && trimmed.length > 24) return answer

  const lastReasoning = steps.map((s) => s.reasoning).filter(Boolean).pop()?.trim() ?? ''
  if (!lastReasoning) return answer

  const wordTail = lastReasoning.match(/([A-Za-zÁÉÍÓÚáéíóúÑñ]{2,}\s*)$/)?.[1]
  if (wordTail && /^[a-záéíóú]/i.test(trimmed)) {
    const merged = wordTail + answer
    if (merged.length > answer.length + 2) return merged
  }

  const clause = lastReasoning.match(/(?:^|\n)([^\n.!?]{8,120})\s*$/)?.[1]?.trim()
  if (clause && (/^[a-z]{1,5}:/i.test(trimmed) || /^-\s/.test(trimmed))) {
    return `${clause}\n${answer}`
  }

  return answer
}
