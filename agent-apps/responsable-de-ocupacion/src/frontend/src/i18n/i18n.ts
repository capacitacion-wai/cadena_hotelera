/**
 * react-i18next bootstrap. Imported once from main.tsx via
 * `import './i18n/i18n'`.
 *
 * Extracted verbatim from wai-agent-app-builder SKILL.md §0.j.
 * Uses i18next-browser-languagedetector so the initial language
 * follows the browser; ES is the fallback.
 */
import i18n, { type PostProcessorModule } from 'i18next'
import LanguageDetector from 'i18next-browser-languagedetector'
import { initReactI18next } from 'react-i18next'
import es from './es'
import en from './en'
import skillEs from './skill.es'
import skillEn from './skill.en'
import plataformaEs from './plataforma.es'
import plataformaEn from './plataforma.en'

/**
 * Merges the SKILL's layer (base) with the EMPLOYEE's (on top). The top one wins.
 *
 * This is what makes a new skill string reach every upgraded employee on its own
 * —`skill.*.ts` is INVARIANT and `--fix-invariants` restores it— while still letting
 * the employee override any of them from its own bundle, which is SCAFFOLD and nobody
 * touches. Before, only the employee's layer existed: every new skill key had to be
 * copied by hand in both languages, and if nobody did it English stayed in Spanish
 * without anything failing on screen. See the header of `skill.es.ts`.
 *
 * It only merges OBJECTS: any leaf (text, number) on top replaces the one below
 * entirely, which is what "the employee rules over this string" means.
 */
type Arbol = { [k: string]: unknown }
function fundir(base: Arbol, encima: Arbol): Arbol {
  const fuera: Arbol = { ...base }
  for (const [k, v] of Object.entries(encima)) {
    const abajo = fuera[k]
    fuera[k] = (
      v && typeof v === 'object' && !Array.isArray(v)
      && abajo && typeof abajo === 'object' && !Array.isArray(abajo)
    )
      ? fundir(abajo as Arbol, v as Arbol)
      : v
  }
  return fuera
}

/**
 * Guard against OUTDATED scaffold translations.
 *
 * The pages (`Operations.tsx`…) are INVARIANTS and travel with every upgrade; the
 * language files are the employee's SCAFFOLD and `--fix-invariants` does not touch them.
 * When a key changes its variables, the employee keeps the old translation and i18next
 * paints the placeholder as is: `{{shown}} de 0 comprobaciones`, seen in production on 06/09/2026.
 *
 * If after interpolation unresolved `{{…}}` remain and the call carries `defaultValue` —the
 * text that travels with the invariant, always up to date—, that text is returned interpolated.
 * No need to hunt keys one by one: it covers any drift, in any employee.
 */
const SIN_RESOLVER = /\{\{\s*[\w.-]+\s*\}\}/
const traduccionDesfasada: PostProcessorModule = {
  type: 'postProcessor',
  name: 'traduccionDesfasada',
  process(value, _key, options, translator) {
    if (typeof value !== 'string' || !SIN_RESOLVER.test(value)) return value
    const porDefecto = (options as { defaultValue?: unknown } | undefined)?.defaultValue
    if (typeof porDefecto !== 'string' || !porDefecto) return value
    // The post-processor sees the result ALREADY interpolated; the default text has to
    // be interpolated here with the same variables as the call.
    const interpolador = (translator as { interpolator?: { interpolate: (s: string, d: unknown, l: string, o: unknown) => string } })?.interpolator
    const idioma = (translator as { language?: string })?.language ?? 'es'
    try {
      return interpolador ? interpolador.interpolate(porDefecto, options, idioma, options) : porDefecto
    } catch {
      return porDefecto
    }
  },
}

i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .use(traduccionDesfasada)
  .init({
    resources: {
      // Three layers, bottom to top: the platform screens' strings (`plataforma.*`, every key
      // an invariant uses, so an old employee never lacks one), the skill's, the employee's.
      es: { translation: fundir(fundir(plataformaEs as Arbol, skillEs as Arbol), es as Arbol) },
      en: { translation: fundir(fundir(plataformaEn as Arbol, skillEn as Arbol), en as Arbol) },
    },
    fallbackLng: 'es',
    interpolation: { escapeValue: false },
    postProcess: ['traduccionDesfasada'],
  })

function syncDocumentLang(lng: string) {
  const code = lng.startsWith('en') ? 'en' : 'es'
  document.documentElement.lang = code
}

syncDocumentLang(i18n.language || 'es')
i18n.on('languageChanged', syncDocumentLang)

export default i18n
