/**
 * WAI Design System — Layout (sidebar + content shell).
 *
 * Copy this file verbatim into every new app at:
 *   src/frontend/src/components/Layout.tsx
 *
 * This component provides the canonical WAI shell:
 *   - Dark sidebar with gradient brand tile
 *   - Grouped navigation with active-state bullet
 *   - UI language selector (one button per language of the app's i18n, only with 2+): at the foot of the
 *     sidebar, inside the phone's menu and, in the WAI app, under the bottom bar's «Más»
 *   - Logged-in user tile with avatar + logout button (above the footer)
 *   - Mobile AppBar + temporary drawer below `sm`, with the same user menu
 *   - NATIVE MODE: inside the WAI mobile app, the container provides the header (with the
 *     app name and the back arrow), so the bar, the drawer and the user block are hidden
 *     here — two bars stacked on top of each other is exactly the look of "a website
 *     stuffed inside an app" — and the sections go in a BOTTOM tab BAR, like any phone
 *     app. Contract: `patterns/dentro-del-movil.md`.
 *
 * Customize ONLY through props (brand, navItems, navGroups, user). Do NOT
 * change colors, typography, radii or gradient unless the user explicitly
 * requests a visual change.
 */
import { ReactNode, useState } from 'react'
import { useNavigate, useLocation } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import {
  AppBar, Avatar, BottomNavigation, BottomNavigationAction, Box, Drawer, IconButton,
  ListItemButton, ListItemIcon, ListItemText, Menu, MenuItem, Paper, Toolbar, Tooltip,
  Typography, Divider, alpha,
} from '@mui/material'
import { lighten, type Theme } from '@mui/material/styles'
import { Logout as LogoutIcon, Menu as MenuIcon, MoreHoriz as MasIcon } from '@mui/icons-material'
import { DANGER, PRIMARY, PRIMARY_LIGHT, SECONDARY, SIDEBAR_DARK } from '../theme/theme'

// The sidebar colors come from the app's THEME (previously, fixed hex values from the old palette).
export { SIDEBAR_DARK }
const PRIMARY_SUAVE = lighten(PRIMARY_LIGHT, 0.45)
const DANGER_SUAVE = lighten(DANGER, 0.45)
const DRAWER_WIDTH = 264
/** Height of the native-mode bottom bar, WITHOUT the safe area (added separately). */
const BARRA_NATIVA = 60
/** Tabs that fit in a phone's bottom bar; with more, the last ones go under "More". */
const PESTANAS_MAX = 5
/** On a touch screen every control reaches 44 px: the screen walk warns `CONTROLES_PEQUENOS` below that, and this shell
 *  is in every app (quality round 4, 26/10/2026: its menu, user and logout buttons and sidebar entries were 28-40 px). */
const TACTIL = { '@media (pointer: coarse)': { minWidth: 44, minHeight: 44 } } as const

/** Are we inside the WAI mobile app (iOS or Android)?
 *
 *  It checks the object the container injects, and NOT the `userAgent` — it lies as soon as
 *  someone switches embedded browser — nor `window.ReactNativeWebView`, which is a library
 *  detail and not a contract of ours. See `patterns/dentro-del-movil.md`. */
export const enMovilDeWai = (): boolean =>
  typeof window !== 'undefined' && !!(window as { WAI_NATIVO?: unknown }).WAI_NATIVO

export interface LayoutNavItem {
  path: string
  icon: ReactNode
  label: string
  key: string
}

export interface LayoutNavGroup {
  label: string
  keys: string[]
}

export interface LayoutBrand {
  /** Short tag shown in the gradient tile, usually 2 letters. */
  short: string
  /** Full brand name shown on the right of the tile. */
  title: string
  /** Small uppercase tagline below the title. */
  tagline?: string
}

export interface LayoutUser {
  /** Logged-in user email (shown in the sidebar footer tile). */
  email: string
  /** Optional display name; falls back to the part before the `@`. */
  name?: string
  /** Called when the user clicks the logout icon / menu item. */
  onLogout: () => void
}

export interface LayoutProps {
  children: ReactNode
  brand: LayoutBrand
  navItems: LayoutNavItem[]
  navGroups: LayoutNavGroup[]
  /** Optional footer text below the nav; defaults to «Hecho con WAI» (`layout.poweredBy`). */
  footer?: string
  /** UI language state. Optional: without it the selector drives the app's i18n (`i18n.changeLanguage`). */
  uiLanguage?: {
    value: string
    onChange: (next: string) => void
  }
  /**
   * Logged-in user and logout handler. When present, the sidebar footer
   * shows an avatar + email + logout button, and the mobile AppBar shows
   * an avatar menu with the same logout action. Omit only for public /
   * pre-login screens.
   */
  user?: LayoutUser
}

function userInitial(u: LayoutUser): string {
  const source = (u.name || u.email || '?').trim()
  return source.charAt(0).toUpperCase()
}

function userDisplayName(u: LayoutUser): string {
  if (u.name && u.name.trim()) return u.name.trim()
  const at = u.email.indexOf('@')
  return at > 0 ? u.email.slice(0, at) : u.email
}

export default function Layout({
  children, brand, navItems, navGroups, footer, uiLanguage, user,
}: LayoutProps) {
  // Its texts through i18n too (6.34.0, `checks/textos_traducibles.py`): the template is the example the agent copies.
  const { t, i18n } = useTranslation()
  // The languages the app HAS (its i18n resources), not a fixed ES/EN: a Spanish-only app showed an English switch
  // and the agent had to remove it by hand (quality pilot, 25/10/2026). One language → no selector.
  const idiomasDeLaApp = Object.keys((i18n.options?.resources as Record<string, unknown>) || {})
  const conIdiomas = idiomasDeLaApp.length > 1
  // Without the prop the selector still works (7.9.0): an app with two languages that did not pass `uiLanguage` had none.
  const idioma = uiLanguage ?? {
    value: i18n.language || idiomasDeLaApp[0] || '',
    onChange: (next: string) => { void i18n.changeLanguage(next) },
  }
  const navigate = useNavigate()
  const location = useLocation()
  const [mobileOpen, setMobileOpen] = useState(false)
  const [mobileUserAnchor, setMobileUserAnchor] = useState<null | HTMLElement>(null)
  const [masAbierto, setMasAbierto] = useState(false)

  const isActive = (path: string) => {
    if (path === '/') return location.pathname === '/'
    return location.pathname.startsWith(path)
  }

  // Real buttons in a named group (7.6.0): a test finds them with get_by_role("button", name="EN"), a keyboard reaches
  // them, and the hidden copy of the menu on a phone does not count twice. `oscuro`: on the sidebar; otherwise on paper.
  const selectorDeIdioma = (oscuro: boolean) => (
    <Box role="group" aria-label={t('layout.language', 'Idioma')} sx={{
      display: 'flex', borderRadius: 1.5, overflow: 'hidden',
      border: 1, borderColor: oscuro ? alpha('#fff', 0.12) : 'divider',
    }}>
      {idiomasDeLaApp.map((x) => x.toUpperCase()).map((code) => {
        const active = idioma.value.toUpperCase().startsWith(code)
        return (
          <Box
            key={code}
            component="button"
            type="button"
            aria-pressed={active}
            onClick={() => idioma.onChange(code.toLowerCase())}
            sx={{
              border: 0, font: 'inherit', display: 'flex', alignItems: 'center', justifyContent: 'center',
              ...TACTIL,
              px: 1.5, py: 0.4, fontSize: '0.8rem', fontWeight: 700, cursor: 'pointer',
              transition: 'all 0.15s',
              ...(oscuro ? {
                bgcolor: active ? alpha(PRIMARY_LIGHT, 0.25) : 'transparent',
                color: active ? PRIMARY_SUAVE : alpha('#fff', 0.4),
                '&:hover': { bgcolor: active ? alpha(PRIMARY_LIGHT, 0.3) : alpha('#fff', 0.06) },
              } : {
                bgcolor: (th: Theme) => (active ? alpha(th.palette.primary.main, 0.12) : 'transparent'),
                color: (th: Theme) => (active
                  ? (th.palette.mode === 'dark' ? th.palette.primary.light : th.palette.primary.dark)
                  : th.palette.text.secondary),
              }),
            }}
          >
            {code}
          </Box>
        )
      })}
    </Box>
  )

  const drawer = (
    <Box sx={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      {/* Brand */}
      <Box sx={{ px: 2.5, py: 3 }}>
        <Box sx={{
          display: 'flex', alignItems: 'center', gap: 1.5,
          p: 1.5, borderRadius: 2,
          background: `linear-gradient(135deg, ${alpha(PRIMARY, 0.15)} 0%, ${alpha(SECONDARY, 0.1)} 100%)`,
        }}>
          <Box sx={{
            width: 40, height: 40, borderRadius: 1.5, display: 'flex',
            alignItems: 'center', justifyContent: 'center',
            background: `linear-gradient(135deg, ${PRIMARY} 0%, ${SECONDARY} 100%)`,
            color: '#fff', fontWeight: 800, fontSize: 16,
          }}>
            {brand.short}
          </Box>
          <Box sx={{ minWidth: 0 }}>
            <Typography sx={{
              fontWeight: 800, fontSize: '1.05rem', color: '#fff', lineHeight: 1.2,
              overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
            }}>
              {brand.title}
            </Typography>
            {brand.tagline && (
              <Typography sx={{
                fontSize: '0.72rem', color: alpha('#fff', 0.5),
                letterSpacing: '0.06em', textTransform: 'uppercase',
              }}>
                {brand.tagline}
              </Typography>
            )}
          </Box>
        </Box>
      </Box>

      {/* Navigation */}
      <Box sx={{ flex: 1, px: 2, overflow: 'auto' }}>
        {navGroups.map((group, gi) => (
          <Box key={gi} sx={{ mb: 1 }}>
            <Typography sx={{
              px: 1.5, py: 0.5, fontSize: '0.75rem', fontWeight: 700,
              color: alpha('#fff', 0.35), textTransform: 'uppercase',
              letterSpacing: '0.1em',
            }}>
              {group.label}
            </Typography>
            {navItems
              .filter((n) => group.keys.includes(n.key))
              .map((item) => {
                const active = isActive(item.path)
                return (
                  <ListItemButton
                    key={item.path}
                    selected={active}
                    onClick={() => { navigate(item.path); setMobileOpen(false) }}
                    sx={{
                      ...TACTIL, borderRadius: 1.5, mb: 0.25, py: 0.8,
                      color: active ? '#fff' : alpha('#fff', 0.6),
                      bgcolor: active ? alpha(PRIMARY, 0.24) : 'transparent',
                      '&:hover': {
                        bgcolor: active ? alpha(PRIMARY, 0.32) : alpha('#fff', 0.06),
                        color: '#fff',
                      },
                      '&.Mui-selected': {
                        bgcolor: alpha(PRIMARY, 0.24),
                        '&:hover': { bgcolor: alpha(PRIMARY, 0.32) },
                      },
                      transition: 'all 0.15s',
                    }}
                  >
                    <ListItemIcon sx={{
                      minWidth: 38, color: active ? PRIMARY_LIGHT : alpha('#fff', 0.4),
                      '& .MuiSvgIcon-root': { fontSize: 22 },
                    }}>
                      {item.icon}
                    </ListItemIcon>
                    <ListItemText
                      primary={item.label}
                      primaryTypographyProps={{
                        fontSize: '0.95rem',
                        fontWeight: active ? 600 : 450,
                      }}
                    />
                    {active && (
                      <Box sx={{
                        width: 4, height: 20, borderRadius: 2,
                        bgcolor: PRIMARY_LIGHT,
                        position: 'absolute', right: 8,
                      }} />
                    )}
                  </ListItemButton>
                )
              })}
          </Box>
        ))}
      </Box>

      {/* Language settings */}
      {conIdiomas && (
        <>
          <Divider sx={{ borderColor: alpha('#fff', 0.08) }} />
          <Box sx={{ px: 2, py: 1.5 }}>
            <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <Typography sx={{ fontSize: '0.8rem', color: alpha('#fff', 0.45), fontWeight: 600 }}>
                {t('layout.language', 'Idioma')}
              </Typography>
              {selectorDeIdioma(true)}
            </Box>
          </Box>
        </>
      )}

      {/* Logged-in user tile + logout */}
      {user && (
        <>
          <Divider sx={{ borderColor: alpha('#fff', 0.08) }} />
          <Box sx={{ px: 2, py: 1.5 }}>
            <Box sx={{
              display: 'flex', alignItems: 'center', gap: 1.25,
              p: 1, borderRadius: 1.5,
              bgcolor: alpha('#fff', 0.04),
              border: `1px solid ${alpha('#fff', 0.08)}`,
            }}>
              <Avatar
                sx={{
                  width: 34, height: 34, fontSize: '0.9rem', fontWeight: 700,
                  bgcolor: alpha(PRIMARY_LIGHT, 0.25), color: PRIMARY_SUAVE,
                }}
              >
                {userInitial(user)}
              </Avatar>
              <Box sx={{ minWidth: 0, flex: 1 }}>
                <Typography sx={{
                  fontSize: '0.85rem', fontWeight: 600, color: '#fff', lineHeight: 1.2,
                  overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                }}>
                  {userDisplayName(user)}
                </Typography>
                <Typography sx={{
                  fontSize: '0.7rem', color: alpha('#fff', 0.5), lineHeight: 1.2,
                  overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                }}>
                  {user.email}
                </Typography>
              </Box>
              <Tooltip title={t('layout.logout', 'Cerrar sesión')}>
                <IconButton
                  size="small"
                  aria-label={t('layout.logout', 'Cerrar sesión')}
                  onClick={user.onLogout}
                  sx={{
                    ...TACTIL, color: alpha('#fff', 0.55),
                    '&:hover': { color: DANGER_SUAVE, bgcolor: alpha(DANGER, 0.08) },
                  }}
                >
                  <LogoutIcon sx={{ fontSize: 18 }} />
                </IconButton>
              </Tooltip>
            </Box>
          </Box>
        </>
      )}

      {/* Footer */}
      <Divider sx={{ borderColor: alpha('#fff', 0.08) }} />
      <Box sx={{ py: 1.5, textAlign: 'center' }}>
        <Typography sx={{
          fontSize: '0.65rem', color: alpha('#fff', 0.25), letterSpacing: '0.05em',
        }}>
          {footer || t('layout.poweredBy', 'Hecho con WAI')}
        </Typography>
      </Box>
    </Box>
  )

  // INSIDE THE WAI MOBILE APP, the container provides the header and here goes the content
  // respecting the safe areas (the iOS home indicator, the Android gesture bar, the sides in
  // landscape) and the sections in a bottom bar. `100dvh` and not `100vh`: the embedded
  // browser's bar comes and goes, and with `vh` the screen jumps.
  //
  // The bar exists because without it a multi-section app is left WITHOUT NAVIGATION
  // inside the phone: the container has no tabs of its own for the web (seen on
  // 27/09/2026 in the travel app, which had to invent a menu). It changes section with
  // `navigate`, which PUSHES onto the history: so the Android back button and the iOS
  // gesture go back to the previous section instead of taking the person out of the app.
  // See `patterns/dentro-del-movil.md` §3.
  if (enMovilDeWai()) {
    const orden = navGroups.flatMap((g) => g.keys)
    const secciones = [
      ...orden.map((k) => navItems.find((i) => i.key === k)).filter((i): i is LayoutNavItem => !!i),
      ...navItems.filter((i) => !orden.includes(i.key)),
    ]
    // With 2+ languages «Más» always exists: its sheet carries the language selector (the container's header has none).
    const conMas = conIdiomas || secciones.length > PESTANAS_MAX
    const conBarra = secciones.length > 1 || conIdiomas
    const enBarra = conMas ? secciones.slice(0, PESTANAS_MAX - 1) : secciones
    const enMas = secciones.slice(enBarra.length)
    const activa = secciones.find((i) => isActive(i.path))
    const valor = activa ? (enBarra.includes(activa) ? activa.key : '__mas') : false
    return (
      <Box
        component="main"
        sx={{
          minHeight: '100dvh', minWidth: 0, maxWidth: '100%',
          bgcolor: 'background.default',
          pt: 'calc(env(safe-area-inset-top, 0px) + 8px)',
          pb: conBarra
            ? `calc(env(safe-area-inset-bottom, 0px) + ${BARRA_NATIVA + 16}px)`
            : 'calc(env(safe-area-inset-bottom, 0px) + 16px)',
          pl: 'calc(env(safe-area-inset-left, 0px) + 16px)',
          pr: 'calc(env(safe-area-inset-right, 0px) + 16px)',
        }}
      >
        {children}
        {conBarra && (
          <Paper
            component="nav"
            aria-label={t('layout.sections', 'Secciones')}
            elevation={0}
            sx={{
              position: 'fixed', left: 0, right: 0, bottom: 0, zIndex: (t) => t.zIndex.appBar,
              borderTop: 1, borderColor: 'divider', borderRadius: 0,
              pb: 'env(safe-area-inset-bottom, 0px)',
              pl: 'env(safe-area-inset-left, 0px)',
              pr: 'env(safe-area-inset-right, 0px)',
            }}
          >
            <BottomNavigation
              showLabels
              value={valor}
              sx={{ height: BARRA_NATIVA, bgcolor: 'background.paper' }}
            >
              {enBarra.map((i) => (
                <BottomNavigationAction
                  key={i.key}
                  value={i.key}
                  label={i.label}
                  icon={i.icon}
                  onClick={() => { if (!isActive(i.path)) navigate(i.path) }}
                  sx={{ minWidth: 0, px: 0.5, '& .MuiBottomNavigationAction-label': { fontSize: '0.72rem' } }}
                />
              ))}
              {conMas && (
                <BottomNavigationAction
                  value="__mas"
                  label={t('layout.more', 'Más')}
                  icon={<MasIcon />}
                  onClick={() => setMasAbierto(true)}
                  sx={{ minWidth: 0, px: 0.5, '& .MuiBottomNavigationAction-label': { fontSize: '0.72rem' } }}
                />
              )}
            </BottomNavigation>
          </Paper>
        )}
        <Drawer
          anchor="bottom"
          open={masAbierto}
          onClose={() => setMasAbierto(false)}
          PaperProps={{ sx: {
            borderTopLeftRadius: 16, borderTopRightRadius: 16,
            pb: 'calc(env(safe-area-inset-bottom, 0px) + 8px)',
            pl: 'env(safe-area-inset-left, 0px)', pr: 'env(safe-area-inset-right, 0px)',
          } }}
        >
          {enMas.map((i) => (
            <ListItemButton
              key={i.key}
              selected={isActive(i.path)}
              onClick={() => { setMasAbierto(false); if (!isActive(i.path)) navigate(i.path) }}
              sx={{ minHeight: 56 }}
            >
              <ListItemIcon>{i.icon}</ListItemIcon>
              <ListItemText primary={i.label} />
            </ListItemButton>
          ))}
          {conIdiomas && (
            <Box sx={{ px: 2, py: 1.5, display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <Typography sx={{ fontWeight: 600 }}>{t('layout.language', 'Idioma')}</Typography>
              {selectorDeIdioma(false)}
            </Box>
          )}
        </Drawer>
      </Box>
    )
  }

  return (
    <Box sx={{ display: 'flex', minHeight: '100dvh' }}>
      <AppBar
        position="fixed"
        elevation={0}
        sx={{
          display: { sm: 'none' },
          width: '100%',
        }}
      >
        <Toolbar sx={{ minHeight: '56px !important' }}>
          <IconButton
            edge="start"
            onClick={() => setMobileOpen(!mobileOpen)}
            // Its name for screen readers: without it, every mobile screen failed WCAG «button-name» (25/10/2026).
            aria-label={t('layout.menu', 'Abrir el menú')}
            sx={TACTIL}
          >
            <MenuIcon />
          </IconButton>
          <Box sx={{ flexGrow: 1 }} />
          <Typography sx={{ fontWeight: 700, fontSize: '0.95rem', mr: user ? 1 : 0 }}>
            {brand.title}
          </Typography>
          {user && (
            <>
              <Tooltip title={user.email}>
                <IconButton
                  size="small"
                  onClick={(e) => setMobileUserAnchor(e.currentTarget)}
                  sx={{ ml: 0.5, ...TACTIL }}
                >
                  <Avatar sx={{
                    width: 30, height: 30, fontSize: '0.85rem', fontWeight: 700,
                    bgcolor: alpha(PRIMARY_LIGHT, 0.25), color: PRIMARY_SUAVE,
                  }}>
                    {userInitial(user)}
                  </Avatar>
                </IconButton>
              </Tooltip>
              <Menu
                anchorEl={mobileUserAnchor}
                open={Boolean(mobileUserAnchor)}
                onClose={() => setMobileUserAnchor(null)}
                anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}
                transformOrigin={{ vertical: 'top', horizontal: 'right' }}
              >
                <MenuItem disabled sx={{ opacity: '1 !important' }}>
                  <Box>
                    <Typography sx={{ fontSize: '0.85rem', fontWeight: 600 }}>
                      {userDisplayName(user)}
                    </Typography>
                    <Typography sx={{ fontSize: '0.72rem', color: 'text.secondary' }}>
                      {user.email}
                    </Typography>
                  </Box>
                </MenuItem>
                <Divider />
                <MenuItem onClick={() => { setMobileUserAnchor(null); user.onLogout() }}>
                  <ListItemIcon><LogoutIcon fontSize="small" /></ListItemIcon>
                  <ListItemText primary={t('layout.logout', 'Cerrar sesión')} />
                </MenuItem>
              </Menu>
            </>
          )}
        </Toolbar>
      </AppBar>

      <Box component="nav" sx={{ width: { sm: DRAWER_WIDTH }, flexShrink: { sm: 0 } }}>
        <Drawer
          variant="temporary"
          open={mobileOpen}
          onClose={() => setMobileOpen(false)}
          sx={{
            display: { xs: 'block', sm: 'none' },
            '& .MuiDrawer-paper': { width: DRAWER_WIDTH, bgcolor: SIDEBAR_DARK },
          }}
        >
          {drawer}
        </Drawer>
        <Drawer
          variant="permanent"
          sx={{
            display: { xs: 'none', sm: 'block' },
            '& .MuiDrawer-paper': { width: DRAWER_WIDTH },
          }}
          open
        >
          {drawer}
        </Drawer>
      </Box>

      <Box
        component="main"
        sx={{
          // minWidth 0: without it a wide table stretches the flex child and the whole page scrolls sideways on a
          // phone (quality pilot, 25/10/2026: 760 px on a 390 px screen, the agent had to patch this file).
          flexGrow: 1, minWidth: 0, maxWidth: '100%', p: { xs: 2, sm: 3 }, mt: { xs: '56px', sm: 0 },
          bgcolor: 'background.default',
          minHeight: { xs: 'calc(100dvh - 56px)', sm: '100dvh' },
        }}
      >
        {children}
      </Box>
    </Box>
  )
}
