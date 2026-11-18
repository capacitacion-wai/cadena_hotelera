/**
 * Profiling module — its screens readable and usable with a finger, FROM THE APP'S THEME.
 *
 * INVARIANT FILE — installed by `wai-app-builder/assets/modulos.py`; do not edit it.
 *
 * The platform's screen walk (`PANTALLAS`) warns about a control under 44 px on a touch screen
 * (`CONTROLES_PEQUENOS`) and about text under 4.5:1 (`CONTRASTE_JUSTO`). Quality scenario 03 (26/10/2026):
 * the module's small buttons and fields, and its «Deactivate»/«Delete» in the theme's warning and danger
 * colours (3.2:1 and 4.1:1 on white with WAI's palette), raised both on every app with profiling — and the
 * agent could not fix them without editing these files. So the module meets them itself, with no colour of
 * its own: the theme's darker shade of a status colour on a light background, its lighter one on a dark one.
 */
import type { Theme } from '@mui/material'

export type ColorDeEstado = 'warning' | 'error' | 'success'

/** Goes on the root of each screen of the module and on each of its dialogs (a dialog is a portal: it does
 * not inherit the screen's rule). On a touch screen, buttons and small fields reach 44 px. */
export const TACTIL = {
  '@media (pointer: coarse)': {
    '& .MuiButton-root, & .MuiIconButton-root': { minHeight: 44, minWidth: 44 },
    '& .MuiInputBase-inputSizeSmall, & .MuiInputBase-sizeSmall .MuiInputBase-input': { paddingTop: '10.5px', paddingBottom: '10.5px' },
  },
} as const

const tono = (c: ColorDeEstado) => (t: Theme) => (t.palette.mode === 'dark' ? t.palette[c].light : t.palette[c].dark)

/** A status colour as TEXT — a text or outlined button, an outlined chip. */
export const textoDe = (c: ColorDeEstado) => ({ color: tono(c) })

/** A status colour as a FILLED button: the darker shade under white text. */
export const rellenoDe = (c: ColorDeEstado) => ({
  bgcolor: `${c}.dark`, color: 'common.white', '&:hover': { bgcolor: `${c}.dark` },
})
