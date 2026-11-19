/**
 * PersonaAvatar — the face of the "AI employee".
 *
 * Renders, in priority order:
 *   1. the persona's mascot image (`avatar.image_url`: path / URL / data URI),
 *   2. an emoji badge (`avatar.emoji`),
 *   3. a built-in friendly mascot illustration (inline SVG, on-brand).
 *
 * It is intentionally asset-free in the fallback path: the default
 * mascot is an inline SVG so a freshly generated app already has a
 * personable face without shipping image files. Per-agent mascots are
 * supplied through the manifest (`agent.persona.avatar.image_url`).
 */
import { Avatar, Box } from '@mui/material'
import type { AgentPersona } from '../hooks/useManifest'
import { PRIMARY, PRIMARY_LIGHT, SECONDARY, ACCENT_GOLD, aurora } from '../theme/theme'

export interface PersonaAvatarProps {
  persona?: AgentPersona | null
  /** Pixel size of the avatar (width === height). */
  size?: number
  /** Show a light ring + soft shadow (for use on dark hero banners). */
  ring?: boolean
  /** Show the green "at work" status dot. */
  status?: boolean
  /** "Presence" mode: a breathing avatar wrapped in a soft aurora aura (for heroes). */
  alive?: boolean
  /** Fallback initial when rendering the emoji/monogram badge. */
  fallbackName?: string
}

/** Built-in mascot: a friendly, rounded robot-helper character drawn
 * with the WAI palette. Scales to any size via the parent box. */
function DefaultMascot({ size = 84 }: { size?: number }) {
  return (
    <Box
      component="svg"
      viewBox="0 0 100 100"
      sx={{ width: size, height: size, display: 'block' }}
      aria-hidden
    >
      <defs>
        <linearGradient id="waiMascotBody" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor={PRIMARY_LIGHT} />
          <stop offset="100%" stopColor={PRIMARY} />
        </linearGradient>
      </defs>
      {/* soft halo */}
      <circle cx="50" cy="50" r="48" fill={PRIMARY} opacity="0.12" />
      {/* antenna */}
      <line x1="50" y1="20" x2="50" y2="11" stroke={SECONDARY} strokeWidth="3" strokeLinecap="round" />
      <circle cx="50" cy="9" r="4" fill={ACCENT_GOLD} />
      {/* head */}
      <rect x="22" y="22" width="56" height="48" rx="16" fill="url(#waiMascotBody)" />
      {/* face plate */}
      <rect x="29" y="31" width="42" height="30" rx="12" fill="#0B1929" opacity="0.92" /> {/* color-fijo: the avatar's visor is part of the drawing */}
      {/* eyes */}
      <circle cx="42" cy="45" r="5" fill="#FFFFFF" />
      <circle cx="58" cy="45" r="5" fill="#FFFFFF" />
      <circle cx="43" cy="46" r="2.2" fill={PRIMARY} />
      <circle cx="59" cy="46" r="2.2" fill={PRIMARY} />
      {/* smile */}
      <path d="M42 54 Q50 59 58 54" stroke={SECONDARY} strokeWidth="2.6" fill="none" strokeLinecap="round" />
      {/* ears */}
      <rect x="17" y="40" width="6" height="14" rx="3" fill={SECONDARY} />
      <rect x="77" y="40" width="6" height="14" rx="3" fill={SECONDARY} />
      {/* little collar / shoulders */}
      <path d="M30 70 Q50 80 70 70 L70 78 Q50 86 30 78 Z" fill={PRIMARY} opacity="0.85" />
    </Box>
  )
}

export function PersonaAvatar({
  persona,
  size = 84,
  ring = false,
  status = false,
  alive = false,
  fallbackName,
}: PersonaAvatarProps) {
  const image = persona?.avatar?.image_url?.trim()
  const emoji = persona?.avatar?.emoji?.trim()
  const seed = persona?.avatar?.color || PRIMARY
  const initial =
    (persona?.name || fallbackName || '').trim().charAt(0).toUpperCase() || '·'

  const ringSx = ring
    ? {
        border: '2px solid rgba(255,255,255,0.35)',
        boxShadow: '0 8px 24px rgba(0,0,0,0.25)',
      }
    : { border: `1px solid ${SECONDARY}22` }

  let inner
  if (image) {
    inner = (
      <Avatar
        src={image}
        alt={persona?.name || fallbackName || ''}
        sx={{ width: size, height: size, bgcolor: '#fff', ...ringSx }}
      />
    )
  } else if (emoji) {
    inner = (
      <Avatar
        sx={{
          width: size,
          height: size,
          fontSize: size * 0.5,
          background: `linear-gradient(135deg, ${seed} 0%, ${PRIMARY} 100%)`,
          color: '#fff',
          ...ringSx,
        }}
      >
        {emoji}
      </Avatar>
    )
  } else {
    // Built-in mascot illustration. We still wrap it in a sized box so
    // the ring/shadow line up with the image/emoji variants.
    inner = (
      <Box
        sx={{
          width: size,
          height: size,
          borderRadius: '50%',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          bgcolor: 'rgba(255,255,255,0.12)',
          overflow: 'hidden',
          ...ringSx,
        }}
        aria-label={persona?.name || fallbackName || 'AI mascot'}
      >
        {initial && size < 40 ? (
          // Tiny sizes: the detailed mascot reads as noise, so show the
          // initial on a brand gradient instead.
          <Avatar
            sx={{
              width: size,
              height: size,
              fontSize: size * 0.5,
              fontWeight: 800,
              background: `linear-gradient(135deg, ${PRIMARY_LIGHT} 0%, ${PRIMARY} 100%)`,
              color: '#fff',
            }}
          >
            {initial}
          </Avatar>
        ) : (
          <DefaultMascot size={size} />
        )}
      </Box>
    )
  }

  // "Presence" mode: a soft aurora aura behind + a gentle breathing motion.
  const framed = alive ? (
    <Box sx={{ position: 'relative', width: size, height: size, flexShrink: 0 }}>
      <Box
        aria-hidden
        sx={{
          position: 'absolute',
          inset: -Math.round(size * 0.15),
          borderRadius: '50%',
          background: (th) => aurora(th.palette.mode === 'dark' ? 'dark' : 'light'),
          filter: 'blur(11px)',
          zIndex: 0,
          animation: 'wai-aura 4.6s ease-in-out infinite',
        }}
      />
      <Box sx={{ position: 'relative', zIndex: 1, animation: 'wai-breathe 4.6s ease-in-out infinite' }}>
        {inner}
      </Box>
    </Box>
  ) : inner

  if (!status) return framed

  return (
    <Box sx={{ position: 'relative', flexShrink: 0, width: size, height: size }}>
      {framed}
      <Box
        sx={{
          position: 'absolute',
          right: Math.max(2, size * 0.05),
          bottom: Math.max(2, size * 0.05),
          width: Math.max(10, size * 0.19),
          height: Math.max(10, size * 0.19),
          borderRadius: '50%',
          bgcolor: 'success.main',
          border: '2px solid rgba(0,0,0,0.25)',
          boxShadow: '0 0 0 3px rgba(34,197,94,0.35)',
          zIndex: 2,
        }}
      />
    </Box>
  )
}

export default PersonaAvatar
