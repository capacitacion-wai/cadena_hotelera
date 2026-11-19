/**
 * WAI — this employee's MUI theme (light + dark).
 *
 * The COLORS and measures come from `tema.ts` (its own, generated from its Definition); the
 * component recipe, from `construirTema.ts` (shared with the platform). This file only
 * puts them together and keeps the names the screens already imported.
 *
 * It is applied with the ColorModeProvider (`theme/ColorMode.tsx`), which builds the theme
 * with `makeTheme(mode)` and lets you toggle light/dark.
 */
import { Theme, createTheme } from '@mui/material/styles'
import { esES as materialEs } from '@mui/material/locale'
import { esES as gridEs } from '@mui/x-data-grid/locales'
import { TEMA } from './tema'
import { aurora as auroraDe, construirTema, ColorModeName } from './construirTema'

export type { ColorModeName }

// The usual names, now read from the theme (the screens import them).
export const SIDEBAR_DARK = TEMA.modos.claro.lateral
export const PRIMARY = TEMA.modos.claro.primario
export const PRIMARY_DARK = TEMA.modos.claro.primario_oscuro
export const PRIMARY_LIGHT = TEMA.modos.claro.primario_claro
export const SECONDARY = TEMA.modos.claro.secundario
export const ACCENT_GOLD = TEMA.modos.claro.acento
export const BG = TEMA.modos.claro.fondo

/** Does this theme have a dark mode? Without it, asking for dark shows the light one. */
export const TIENE_MODO_OSCURO = !!TEMA.modos.oscuro

/** The «presence» gradient (the persona's orb / the cover's aura). */
export function aurora(mode: ColorModeName): string {
  return auroraDe(TEMA, mode)
}

/** The MUI theme for one mode. Both modes are first-class.

    `idioma` = the app's language: the TABLES' own words («No rows», «Rows per page», the column
    menu) come from MUI in English, and a Spanish screen showed them that way (04/10/2026). */
export function makeTheme(mode: ColorModeName, idioma = ''): Theme {
  const base = construirTema(TEMA, mode)
  return idioma.toLowerCase().startsWith('es') ? createTheme(base, materialEs, gridEs) : base
}

// Light by default: for any code that imports the theme directly.
const theme = makeTheme('light')
export default theme
