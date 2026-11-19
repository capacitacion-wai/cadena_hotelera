/**
 * ColorModeProvider — light/dark theme switching for the generated app.
 *
 * Wraps the app (see `main.tsx`), builds the MUI theme with `makeTheme(mode)`,
 * persists the operator's choice in localStorage and falls back to the OS
 * preference. `useColorMode()` exposes `{ mode, toggle, setMode }`;
 * `<ColorModeToggle />` is the ready-made header button. COPIED VERBATIM by
 * `wai-agent-app-builder` (INVARIANT).
 */
import React, { createContext, useContext, useEffect, useMemo, useState } from 'react'
import { ThemeProvider } from '@mui/material/styles'
import CssBaseline from '@mui/material/CssBaseline'
import IconButton from '@mui/material/IconButton'
import Tooltip from '@mui/material/Tooltip'
import DarkModeOutlinedIcon from '@mui/icons-material/DarkModeOutlined'
import LightModeOutlinedIcon from '@mui/icons-material/LightModeOutlined'
import { useTranslation } from 'react-i18next'
import { makeTheme, ColorModeName } from './theme'
import { TEMA } from './tema'
import { tokensDe } from './construirTema'

const STORAGE_KEY = 'wai-employee-color-mode'

interface ColorModeCtx {
  mode: ColorModeName
  toggle: () => void
  setMode: (m: ColorModeName) => void
  /** Applies a DEFAULT theme (the one from the employee's Identity) — it only takes effect if
   *  the operator has NOT picked a theme by hand (toggle). Not persisted in localStorage. */
  applyDefault: (m: ColorModeName) => void
}

const ColorModeContext = createContext<ColorModeCtx>({
  mode: 'light', toggle: () => {}, setMode: () => {}, applyDefault: () => {},
})

export const useColorMode = () => useContext(ColorModeContext)

function readSaved(): ColorModeName | null {
  try {
    const saved = localStorage.getItem(STORAGE_KEY)
    if (saved === 'light' || saved === 'dark') return saved
  } catch { /* storage unavailable */ }
  return null
}
function systemMode(): ColorModeName {
  try {
    if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) return 'dark'
  } catch { /* matchMedia unavailable */ }
  return 'light'
}

export function ColorModeProvider({ children }: { children: React.ReactNode }) {
  const savedInit = readSaved()
  const [mode, setModeState] = useState<ColorModeName>(savedInit ?? systemMode())
  // Has the operator picked a theme by hand? If so, the Identity default does NOT override it.
  const explicitRef = React.useRef<boolean>(savedInit != null)

  const setMode = React.useCallback((m: ColorModeName) => {
    explicitRef.current = true
    setModeState(m)
    try { localStorage.setItem(STORAGE_KEY, m) } catch { /* ignore */ }
  }, [])
  const toggle = React.useCallback(() => {
    explicitRef.current = true
    setModeState((cur) => {
      const next: ColorModeName = cur === 'dark' ? 'light' : 'dark'
      try { localStorage.setItem(STORAGE_KEY, next) } catch { /* ignore */ }
      return next
    })
  }, [])
  const applyDefault = React.useCallback((m: ColorModeName) => {
    if (!explicitRef.current) setModeState(m)
  }, [])

  useEffect(() => {
    // Expose the mode to any CSS that wants it (e.g. markdown/print styles).
    try { document.documentElement.setAttribute('data-color-mode', mode) } catch { /* ignore */ }
    // And the THEME colors as CSS variables (`--wai-primario`…), for the non-MUI sheets
    // (markdown, printing): without this they carried a hand-written blue from another palette.
    try {
      const raiz = document.documentElement.style
      Object.entries(tokensDe(TEMA, mode)).forEach(([k, v]) => {
        if (v) raiz.setProperty(`--wai-${k.replace(/_/g, '-')}`, v)
      })
    } catch { /* ignore */ }
  }, [mode])

  // The theme follows the app's language too (the tables' own words; see `makeTheme`).
  const { i18n } = useTranslation()
  const idioma = i18n.language || ''
  const theme = useMemo(() => makeTheme(mode, idioma), [mode, idioma])
  const ctx = useMemo<ColorModeCtx>(() => ({ mode, toggle, setMode, applyDefault }), [mode, toggle, setMode, applyDefault])

  return (
    <ColorModeContext.Provider value={ctx}>
      <ThemeProvider theme={theme}>
        <CssBaseline />
        {children}
      </ThemeProvider>
    </ColorModeContext.Provider>
  )
}

/** Header toggle button (sun/moon). Drop it anywhere; it reads the context. */
export function ColorModeToggle(props: { size?: 'small' | 'medium' | 'large' }) {
  const { mode, toggle } = useColorMode()
  const { t } = useTranslation()
  const label = mode === 'dark'
    ? t('common.lightMode', 'Modo claro')
    : t('common.darkMode', 'Modo oscuro')
  return (
    <Tooltip title={label}>
      <IconButton onClick={toggle} size={props.size ?? 'small'} aria-label={label} color="inherit">
        {mode === 'dark' ? <LightModeOutlinedIcon fontSize="small" /> : <DarkModeOutlinedIcon fontSize="small" />}
      </IconButton>
    </Tooltip>
  )
}

export default ColorModeProvider
