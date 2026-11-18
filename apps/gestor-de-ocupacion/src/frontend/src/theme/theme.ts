/**
 * WAI — this application's MUI theme.
 *
 * Copy this file as-is to `src/frontend/src/theme/theme.ts`, next to
 * `construirTema.ts` (the component recipe, shared with the platform) and `tema.ts`
 * (THIS application's colors and measures, generated from its Definition).
 *
 * It is applied with <ThemeProvider theme={theme}> in main.tsx. To change the look, do NOT touch
 * this file: change the theme decisions in the Definition and regenerate `tema.ts`.
 */
import { Theme } from '@mui/material/styles'
import { TEMA } from './tema'
import { aurora as auroraDe, construirTema, ColorModeName } from './construirTema'

export type { ColorModeName }

// The usual names, read from the theme (the screens and the Layout import them).
export const SIDEBAR_DARK = TEMA.modos.claro.lateral
export const PRIMARY = TEMA.modos.claro.primario
export const PRIMARY_DARK = TEMA.modos.claro.primario_oscuro
export const PRIMARY_LIGHT = TEMA.modos.claro.primario_claro
export const SECONDARY = TEMA.modos.claro.secundario
export const ACCENT_GOLD = TEMA.modos.claro.acento
export const DANGER = TEMA.modos.claro.peligro
export const BG = TEMA.modos.claro.fondo

/** Does this theme have a dark mode? */
export const TIENE_MODO_OSCURO = !!TEMA.modos.oscuro

export function aurora(mode: ColorModeName = 'light'): string {
  return auroraDe(TEMA, mode)
}

/** The MUI theme for one mode. */
export function makeTheme(mode: ColorModeName): Theme {
  return construirTema(TEMA, mode)
}

/**
 * Publishes the theme colors as CSS variables (`--wai-primario`…) for the non-MUI
 * stylesheets (markdown). Called once on load; without it `markdown.css` would have no colors.
 */
export function publicarVariablesCss(mode: ColorModeName = 'light'): void {
  if (typeof document === 'undefined') return
  const tokens = mode === 'dark' && TEMA.modos.oscuro ? TEMA.modos.oscuro : TEMA.modos.claro
  Object.entries(tokens).forEach(([k, v]) => {
    if (v) document.documentElement.style.setProperty(`--wai-${k.replace(/_/g, '-')}`, v)
  })
}
publicarVariablesCss('light')

const theme = makeTheme('light')
export default theme
