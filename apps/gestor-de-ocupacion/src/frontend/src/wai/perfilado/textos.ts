/**
 * Profiling module — its texts, through the APP's i18n instance (react-i18next), in the namespace
 * `perfilado`: the module's defaults (es, en) overwritten by the app's vocabulary (the adapter's
 * TEXTOS_PROPIOS). Registered on first use, when the app's i18n is already initialised, and ONLY in
 * the languages the app declares (its i18n `resources`, else `supportedLngs`): an English-only app
 * gets an English bundle and nothing else, and it follows the app's current language and selector.
 * A language the module does not ship (fr, pt…) starts from the English texts: its words go in
 * TEXTOS_PROPIOS[<language>] (quality scenario 03, 26/10/2026).
 *
 * INVARIANT FILE — installed by `wai-app-builder/assets/modulos.py`; do not edit it.
 */
import i18next from 'i18next'
import type { i18n as I18n } from 'i18next'
import { useTranslation } from 'react-i18next'
import { TEXTOS_PROPIOS } from './adaptador'
import { TEXTOS_DE_LA_DEFINICION } from './textos_de_la_definicion'
import en from './i18n/en'
import es from './i18n/es'
import { ErrorDeApi } from './tipos'
import { nombreCorto } from './resumen'
import type { Capacidad, Rol } from './tipos'

export const NS = 'perfilado'
const BASE: Record<string, Record<string, string>> = { es, en }

export type Traducir = (clave: string, opciones?: Record<string, unknown>) => string

/** The languages of the APP's screens, as its i18n declares them — never a fixed list of the module's. */
function idiomasDeLaApp(i18n: I18n): string[] {
  const recursos = Object.keys((i18n.options?.resources as Record<string, unknown> | undefined) || {})
  const soportados = Array.isArray(i18n.options?.supportedLngs) ? i18n.options.supportedLngs : []
  const lista = recursos.length ? recursos : soportados.filter((l) => l !== 'cimode')
  return lista.length ? lista : [i18n.resolvedLanguage || i18n.language || 'en']
}

function registrar(i18n: I18n): void {
  idiomasDeLaApp(i18n).forEach((lng) => {
    if (!i18n.hasResourceBundle(lng, NS)) {
      const base = BASE[lng] || BASE[lng.split('-')[0]] || BASE.en
      const corto = lng.split('-')[0]
      i18n.addResourceBundle(lng, NS, {
        ...base, ...(TEXTOS_DE_LA_DEFINICION[corto] || {}), ...(TEXTOS_DE_LA_DEFINICION[lng] || {}),
        ...(TEXTOS_PROPIOS[lng] || {}),
      }, true, true)
    }
  })
}

export function useTextos(): Traducir {
  const { i18n } = useTranslation()
  registrar(i18n)
  const t = i18n.getFixedT(null, NS) as unknown as Traducir
  return (clave, opciones) => String(t(clave, opciones))
}

/** The text of one of the module's error codes (`X-WAI-Codigo`, e.g. `PERFILADO_SIN_PERMISO`) in the app's
 *  language, or null if it is not one of the module's. For the app's OWN `apiClient`: the module's 403 carries its
 *  `detail` in English (for the logs) and the app showed it as is to the person (quality pilot, 23/10/2026). */
export function textoDeUnCodigo(codigo: string): string | null {
  if (!codigo.startsWith('PERFILADO_')) return null
  registrar(i18next)
  const clave = `error_${codigo}`
  return i18next.exists(clave, { ns: NS }) ? String(i18next.getFixedT(null, NS)(clave)) : null
}

/** The message for an error of the module's API, in the app's language (by its stable code). */
export function mensajeDeError(t: Traducir, e: unknown): string {
  const codigo = e instanceof ErrorDeApi ? e.codigo : ''
  return codigo ? t(`error_${codigo}`, { defaultValue: t('error_generico') }) : t('error_generico')
}

/** The name of a role: the fixed ones in the app's language; the app's own as the Definition names them, in the
 *  current language when `textos_de_la_definicion.ts` has it (`def_rol_<id>`). */
export function nombreDeRol(t: Traducir, id: string, nombre?: string, fijo?: boolean): string {
  return fijo || !nombre ? t(`rol_${id}`, { defaultValue: nombre || id }) : t(`def_rol_${id}`, { defaultValue: nombre })
}

/** A task's whole sentence (`que` of the Definition) in the current language (`def_tarea_<id>`). */
export function queDeTarea(t: Traducir, c: Capacidad): string {
  return c.que ? t(`def_tarea_${c.id}`, { defaultValue: c.que }) : ''
}

/** A task's SHORT name in the current language (`def_tarea_<id>_corto`); without it, the start of its sentence. */
export function cortoDeTarea(t: Traducir, c: Capacidad): string {
  return t(`def_tarea_${c.id}_corto`, { defaultValue: '' }) || nombreCorto({ ...c, que: queDeTarea(t, c) }, t('sin_nombre'))
}

/** What the app calls its outside organisations: the setting `nombre_organizacion` («Proveedores»),
 *  else the module's word in the app's language. */
export function nombreDeOrganizaciones(t: Traducir, propio?: string | null): string {
  return propio ? t('def_organizaciones', { defaultValue: propio }) : t('menu_organizaciones')
}

/** A role's one line: «who it is for» from the Definition; the fixed roles' from the module's texts. */
export function descripcionDeRol(t: Traducir, r?: Pick<Rol, 'id' | 'quien' | 'fijo' | 'administra'> | null): string {
  if (!r) return ''
  const quien = (r.quien || '').trim()
  return (quien && t(`def_rol_${r.id}_quien`, { defaultValue: quien })) || (r.fijo ? t(`rol_${r.id}_quien`, { defaultValue: '' }) : '') || (r.administra ? t('rol_admin_quien') : '')
}
