/**
 * WAI — build the MUI theme from a THEME (tokens), in light or dark.
 *
 * SHARED FILE: it is byte for byte the same in three places, and a guard checks it
 * (scripts/checks/construir_tema_identico.sh):
 *   · wai-agent-app-builder/assets/frontend/src/theme/construirTema.ts   (employees)
 *   · wai-app-builder/assets/construirTema.ts                            (applications)
 *   · frontend-react/src/features/admin/brand-styles/construirTema.ts     (preview)
 * That way the «Brand styles» preview shows exactly what an application will look like.
 *
 * The COLORS and sizes do not live here: they come from the theme (each application's
 * `tema.ts`, which the platform generates from the decisions in its Definition). What lives
 * here is the components' RECIPE: what a card, a table or the sidebar look like with those
 * tokens.
 */
import { createTheme, alpha, Theme } from '@mui/material/styles'

export type ColorModeName = 'light' | 'dark'

/** The semantic tokens of one mode (light or dark). */
export interface TokensDeModo {
  fondo: string; papel: string; papel2: string; texto: string; texto_suave: string
  texto_tenue: string; borde: string; sombra: string; primario: string; primario_oscuro: string
  primario_claro: string; sobre_primario: string; secundario: string; acento: string
  aviso: string; peligro: string; exito: string; info: string; cabecera_tabla: string
  fila_hover: string; lateral: string; lateral_texto: string; gris50: string; gris100: string
}

export interface TemaWai {
  formato: 1
  nombre?: string
  huella?: string
  decisiones?: Record<string, unknown>
  modos: { claro: TokensDeModo; oscuro: TokensDeModo | null }
  tipografia: {
    cuerpo: string
    titulos: string
    pesos: { h3: number; h4: number; h5: number; h6: number; subtitle1: number; caption: number; boton: number }
    espaciado: { h3: string; h4: string; h5: string; boton: string }
  }
  forma: { radio: number; tarjeta: number; boton: number; dialogo: number; aviso: number; lista: number; barra: number }
  densidad: { boton: string; cabecera_celda: string; celda: string }
  componentes: {
    tarjeta_con_borde: boolean
    lateral_con_degradado: boolean
    aurora: { claro: string[]; oscuro: string[] }
  }
  graficos: string[]
  estados: {
    ok: string; warn: string; error: string; neutral: string; info: string
    reclamado: string; aceptado: string; rechazado: string; sin_respuesta: string
  }
  /** From worst to best: the bands of a score. */
  rangos: string[]
}

/** The tokens of the requested mode. A theme with no dark mode shows light in dark mode too. */
export function tokensDe(tema: TemaWai, mode: ColorModeName): TokensDeModo {
  return mode === 'dark' && tema.modos.oscuro ? tema.modos.oscuro : tema.modos.claro
}

/** The «presence» gradient (the persona's orb, the cover page's aura). */
export function aurora(tema: TemaWai, mode: ColorModeName): string {
  const a = mode === 'dark' && tema.modos.oscuro ? tema.componentes.aurora.oscuro : tema.componentes.aurora.claro
  return `linear-gradient(135deg, ${a[0]} 0%, ${a[1]} 50%, ${a[2]} 100%)`
}

function familia(nombre: string): string {
  return `"${nombre}", "Roboto", "Helvetica Neue", "Arial", sans-serif`
}

/** Builds the MUI theme for one mode. Both modes are first-class. */
export function construirTema(tema: TemaWai, mode: ColorModeName): Theme {
  const dark = mode === 'dark' && !!tema.modos.oscuro
  const c = tokensDe(tema, mode)
  const t = tema.tipografia
  const f = tema.forma
  const d = tema.densidad
  const titulos = t.titulos && t.titulos !== t.cuerpo ? { fontFamily: familia(t.titulos) } : {}
  return createTheme({
    palette: {
      mode: dark ? 'dark' : 'light',
      primary: { main: c.primario, dark: c.primario_oscuro, light: c.primario_claro, contrastText: c.sobre_primario },
      secondary: { main: c.secundario },
      warning: { main: c.aviso },
      error: { main: c.peligro },
      // Empty = the library's own (with its built-in light/dark variants).
      ...(c.exito ? { success: { main: c.exito } } : {}),
      ...(c.info ? { info: { main: c.info } } : {}),
      background: { default: c.fondo, paper: c.papel },
      text: { primary: c.texto, secondary: c.texto_suave },
      divider: c.borde,
      // In DARK, the "light greys" many screens use as a subtle background
      // (grey.50 / grey.100) are remapped to DARK tints: that way those backgrounds (tab
      // headers, detail boxes, tables…) stop showing white in dark mode.
      ...(dark && c.gris50 && c.gris100 ? { grey: { 50: c.gris50, 100: c.gris100 } } : {}),
    },
    typography: {
      fontFamily: familia(t.cuerpo),
      h3: { fontWeight: t.pesos.h3, letterSpacing: t.espaciado.h3, ...titulos },
      h4: { fontWeight: t.pesos.h4, letterSpacing: t.espaciado.h4, ...titulos },
      h5: { fontWeight: t.pesos.h5, letterSpacing: t.espaciado.h5, ...titulos },
      h6: { fontWeight: t.pesos.h6, ...titulos },
      subtitle1: { fontWeight: t.pesos.subtitle1, fontSize: '0.95rem' },
      body2: { fontSize: '0.875rem', lineHeight: 1.6 },
      caption: { fontSize: '0.75rem', fontWeight: t.pesos.caption },
      button: { fontWeight: t.pesos.boton, letterSpacing: t.espaciado.boton },
    },
    shape: { borderRadius: f.radio },
    components: {
      MuiCssBaseline: {
        styleOverrides: {
          body: {
            scrollbarWidth: 'thin',
            scrollbarColor: `${alpha(c.texto_tenue, 0.4)} transparent`,
            '&::-webkit-scrollbar': { width: 7, height: 7 },
            '&::-webkit-scrollbar-thumb': { borderRadius: 4, backgroundColor: alpha(c.texto_tenue, 0.4) },
          },
          '*': { scrollbarWidth: 'thin', scrollbarColor: `${alpha(c.texto_tenue, 0.4)} transparent` },
          // «Presence» motion of the persona's avatar (enabled via sx).
          '@keyframes wai-breathe': { '0%,100%': { transform: 'scale(1)' }, '50%': { transform: 'scale(1.05)' } },
          '@keyframes wai-aura': { '0%,100%': { opacity: 0.55, transform: 'scale(1)' }, '50%': { opacity: 0.15, transform: 'scale(1.16)' } },
          '@media (prefers-reduced-motion: reduce)': {
            '*': { animationDuration: '0.001ms !important', animationIterationCount: '1 !important' },
          },
        },
      },
      MuiCard: {
        styleOverrides: {
          root: {
            backgroundImage: 'none',
            ...(tema.componentes.tarjeta_con_borde ? { border: `1px solid ${c.borde}` } : {}),
            boxShadow: dark
              ? `0 0 0 1px ${alpha(c.sombra, 0.2)}, 0 18px 44px -14px ${alpha(c.sombra, 0.6)}`
              : `0 0 2px 0 ${alpha(c.sombra, 0.2)}, 0 12px 28px -6px ${alpha(c.sombra, 0.16)}`,
            borderRadius: f.tarjeta,
            transition: 'box-shadow 0.25s ease, transform 0.12s ease',
            '&:hover': {
              boxShadow: dark
                ? `0 0 0 1px ${alpha(c.primario, 0.18)}, 0 26px 60px -14px ${alpha(c.sombra, 0.66)}`
                : `0 0 2px 0 ${alpha(c.sombra, 0.2)}, 0 22px 44px -8px ${alpha(c.sombra, 0.22)}`,
            },
          },
        },
      },
      MuiButton: {
        styleOverrides: {
          root: { textTransform: 'none', fontWeight: t.pesos.boton, borderRadius: f.boton, padding: d.boton },
          contained: { boxShadow: 'none', '&:hover': { boxShadow: `0 10px 22px -6px ${alpha(c.primario, 0.42)}` } },
          outlined: { borderColor: c.borde },
        },
      },
      MuiChip: { styleOverrides: { root: { fontWeight: 620, fontSize: '0.75rem' }, sizeSmall: { height: 26 } } },
      MuiTableHead: {
        styleOverrides: {
          root: {
            '& .MuiTableCell-head': {
              backgroundColor: c.cabecera_tabla,
              color: c.texto_suave,
              fontWeight: 700, fontSize: '0.72rem', textTransform: 'uppercase', letterSpacing: '0.05em',
              borderBottom: `2px solid ${c.borde}`,
              borderRight: `1px solid ${alpha(c.texto_tenue, 0.12)}`,
              padding: d.cabecera_celda,
              '&:last-of-type': { borderRight: 'none' },
            },
          },
        },
      },
      MuiTableBody: {
        styleOverrides: {
          root: {
            '& .MuiTableRow-root': { transition: 'background-color 0.15s', '&:hover': { backgroundColor: c.fila_hover } },
            '& .MuiTableCell-body': {
              borderBottomColor: alpha(c.texto_tenue, 0.14),
              borderRight: `1px solid ${alpha(c.texto_tenue, 0.07)}`,
              padding: d.celda,
              '&:last-of-type': { borderRight: 'none' },
            },
          },
        },
      },
      MuiTablePagination: { styleOverrides: { root: { borderTop: `1px solid ${alpha(c.texto_tenue, 0.12)}` } } },
      MuiDialog: {
        styleOverrides: {
          paper: {
            backgroundImage: 'none',
            borderRadius: f.dialogo,
            boxShadow: dark ? `0 40px 90px -10px ${alpha(c.sombra, 0.7)}` : `0 40px 80px -8px ${alpha(c.sombra, 0.28)}`,
          },
        },
      },
      MuiDrawer: {
        styleOverrides: {
          paper: {
            backgroundColor: c.lateral,
            ...(tema.componentes.lateral_con_degradado
              ? { backgroundImage: `radial-gradient(120% 60% at 0% 0%, ${alpha(c.primario, 0.14)}, transparent 60%)` }
              : {}),
            color: c.lateral_texto,
            borderRight: 'none',
            '& .MuiToolbar-root': {
              backgroundColor: 'rgba(255,255,255,0.04)',
              '& .MuiTypography-root': { color: '#FFFFFF', fontWeight: 750 },
              '& .MuiSvgIcon-root': { color: c.primario_claro },
            },
            '& .MuiDivider-root': { borderColor: 'rgba(255,255,255,0.08)' },
            '& .MuiListSubheader-root': {
              backgroundColor: 'transparent', color: alpha('#FFFFFF', 0.46),
              fontSize: '0.68rem', fontWeight: 700, letterSpacing: '0.09em', textTransform: 'uppercase',
              paddingLeft: 16, paddingRight: 16,
            },
            '& .MuiListItemIcon-root': { color: 'rgba(255,255,255,0.8)', minWidth: 36 },
            '& .MuiListItemText-primary': { color: '#E1E5EA', fontSize: '0.85rem', fontWeight: 520 },
            '& .MuiListItemButton-root': {
              borderRadius: f.lista, margin: '2px 8px',
              '&:hover': { backgroundColor: 'rgba(255,255,255,0.06)' },
              '&.Mui-selected': {
                backgroundColor: alpha(c.primario, 0.26),
                '&:hover': { backgroundColor: alpha(c.primario, 0.32) },
                '& .MuiListItemIcon-root': { color: '#FFFFFF' },
                '& .MuiListItemText-primary': { color: '#FFFFFF', fontWeight: 620 },
              },
            },
            '& .MuiChip-outlined': { borderColor: 'rgba(255,255,255,0.2)', color: '#E1E5EA' },
          },
        },
      },
      MuiAppBar: {
        styleOverrides: {
          root: {
            backgroundColor: alpha(c.papel, dark ? 0.66 : 0.72),
            backgroundImage: 'none',
            backdropFilter: 'blur(8px)',
            color: c.texto,
            boxShadow: `inset 0 -1px 0 ${c.borde}`,
          },
        },
      },
      MuiLinearProgress: { styleOverrides: { root: { borderRadius: f.barra, height: 6 }, bar: { borderRadius: f.barra } } },
      MuiTooltip: { styleOverrides: { tooltip: { borderRadius: f.aviso, fontSize: '0.75rem' } } },
    },
  })
}
