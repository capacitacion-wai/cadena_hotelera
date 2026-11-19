/**
 * PersonaEditorCard — operator surface to edit the "AI employee"
 * identity (name, role, bio, personality traits, greeting and the
 * assignable icon/avatar).
 *
 * This is the canonical place to edit the employee's identity: it is
 * rendered by the dedicated "Identidad" admin page (src/pages/Identity.tsx)
 * rather than on the public Welcome page, so a casual viewer can't rewrite
 * the persona but an operator can tune it without a redeploy.
 *
 * Persistence: PATCH /api/agent/persona upserts a row in the local
 * ``agent_persona`` table; the manifest endpoint merges it over the
 * manifest persona so the change shows up in the welcome hero, the chat
 * header and the chat reply avatar. ``resetManifestCache()`` is called
 * on save so already-loaded pages pick up the new identity without a
 * full reload.
 *
 * Localizable fields (role / greeting / bio / tone) are edited in the
 * UI's active language; the other language's value (if any) is
 * preserved so a bilingual persona isn't clobbered by a single-locale
 * edit.
 */
import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert, Box, Button, Card, CardContent, Chip, CircularProgress,
  Divider, Grid, Stack, TextField, Tooltip, Typography,
} from '@mui/material'
import BadgeOutlinedIcon from '@mui/icons-material/BadgeOutlined'
import RestartAltIcon from '@mui/icons-material/RestartAlt'
import AddIcon from '@mui/icons-material/Add'

import { apiFetch } from '../services/apiClient'
import { resetManifestCache } from '../hooks/useManifest'
import { PersonaAvatar } from './PersonaAvatar'
import type { AgentPersona, LocaleText } from '../hooks/useManifest'

// A small palette of friendly "icono" suggestions so an operator can
// assign a face in one click without hunting for an emoji.
const EMOJI_SUGGESTIONS = ['🤖', '🔧', '📦', '📊', '🛒', '💼', '🧾', '📈', '🛠️', '🦊', '🐱', '🦉']

interface PersonaResponse {
  resolved: AgentPersona | null
  manifest_default: AgentPersona | null
  override: AgentPersona | null
  overridden: boolean
  updated_by?: string | null
  updated_at?: string | null
}

function locOf(v: LocaleText | undefined | null, lang: string): string {
  if (!v) return ''
  if (typeof v === 'string') return v
  return v[lang] || v.es || v.en || (Object.values(v)[0] as string) || ''
}

function otherLocOf(v: LocaleText | undefined | null, lang: string, other: string): string {
  if (!v || typeof v === 'string') return ''
  return v[other] || ''
}

/** Build the payload value for a localizable field: a {lang} dict that
 * preserves the other language's value if it exists. Empty → null
 * (clears the override). */
function localePayload(
  draft: string,
  lang: string,
  other: string,
  existing: LocaleText | undefined | null,
): Record<string, string> | null {
  const out: Record<string, string> = {}
  const kept = otherLocOf(existing, lang, other)
  if (kept) out[other] = kept
  if (draft.trim()) out[lang] = draft.trim()
  return Object.keys(out).length ? out : null
}

export default function PersonaEditorCard() {
  const { t, i18n } = useTranslation()
  const qc = useQueryClient()
  const lang = (i18n.language?.startsWith('en') ? 'en' : 'es') as 'es' | 'en'
  const other = lang === 'es' ? 'en' : 'es'

  const { data, isLoading } = useQuery<PersonaResponse>({
    queryKey: ['agent-persona'],
    queryFn: () => apiFetch('/api/agent/persona').then(async (r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      return r.json()
    }),
  })

  const resolved = data?.resolved ?? null
  const override = data?.override ?? null

  // Draft state — seeded from the resolved persona (manifest + override).
  const [name, setName] = useState('')
  const [role, setRole] = useState('')
  const [greeting, setGreeting] = useState('')
  const [bio, setBio] = useState('')
  const [tone, setTone] = useState('')
  const [traits, setTraits] = useState<string[]>([])
  const [traitDraft, setTraitDraft] = useState('')
  const [emoji, setEmoji] = useState('')
  const [color, setColor] = useState('')
  const [imageUrl, setImageUrl] = useState('')
  const [saveError, setSaveError] = useState<string | null>(null)

  useEffect(() => {
    if (!data) return
    const p = data.resolved ?? {}
    setName(p.name ?? '')
    setRole(locOf(p.role, lang))
    setGreeting(locOf(p.greeting, lang))
    setBio(locOf(p.personality?.bio, lang))
    setTone(locOf(p.personality?.tone, lang))
    setTraits((p.personality?.traits ?? []).map((tr) => locOf(tr, lang)).filter(Boolean))
    setEmoji(p.avatar?.emoji ?? '')
    setColor(p.avatar?.color ?? '')
    setImageUrl(p.avatar?.image_url ?? '')
    setSaveError(null)
  }, [data, lang])

  // Live preview persona so the avatar reflects the draft as you type.
  const previewPersona: AgentPersona = useMemo(() => ({
    name: name || resolved?.name,
    role,
    avatar: { emoji, color: color || undefined, image_url: imageUrl || undefined },
    personality: { bio, tone, traits },
    greeting,
  }), [name, role, emoji, color, imageUrl, bio, tone, traits, greeting, resolved])

  const save = useMutation({
    mutationFn: () => {
      const payload: Record<string, unknown> = {
        name: name.trim() || null,
        role: localePayload(role, lang, other, override?.role),
        greeting: localePayload(greeting, lang, other, override?.greeting),
        personality: {
          bio: localePayload(bio, lang, other, override?.personality?.bio),
          tone: localePayload(tone, lang, other, override?.personality?.tone),
          traits: traits.map((tr) => tr.trim()).filter(Boolean),
        },
        avatar: {
          emoji: emoji.trim() || null,
          color: color.trim() || null,
          image_url: imageUrl.trim() || null,
        },
      }
      return apiFetch('/api/agent/persona', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      }).then(async (r) => {
        if (!r.ok) {
          const b = await r.json().catch(() => null)
          throw new Error(b?.detail || `HTTP ${r.status}`)
        }
        return r.json() as Promise<PersonaResponse>
      })
    },
    onSuccess: (d) => {
      qc.setQueryData(['agent-persona'], d)
      setSaveError(null)
      // Propagate to welcome hero / chat header / chat reply avatars.
      resetManifestCache()
    },
    onError: (e: any) => setSaveError(String(e?.message ?? e)),
  })

  const reset = useMutation({
    mutationFn: () =>
      apiFetch('/api/agent/persona', { method: 'DELETE' }).then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        return r.json() as Promise<PersonaResponse>
      }),
    onSuccess: (d) => {
      qc.setQueryData(['agent-persona'], d)
      setSaveError(null)
      resetManifestCache()
    },
    onError: (e: any) => setSaveError(String(e?.message ?? e)),
  })

  const addTrait = () => {
    const v = traitDraft.trim()
    if (!v || traits.length >= 6 || traits.includes(v)) { setTraitDraft(''); return }
    setTraits((prev) => [...prev, v])
    setTraitDraft('')
  }

  if (isLoading) {
    return (
      <Card variant="outlined" sx={{ mb: 3, borderRadius: 2 }}>
        <CardContent>
          <Stack alignItems="center" sx={{ py: 3 }}><CircularProgress size={22} /></Stack>
        </CardContent>
      </Card>
    )
  }

  return (
    <Card variant="outlined" sx={{ mb: 3, borderRadius: 2 }}>
      <CardContent>
        <Stack direction="row" spacing={1.5} alignItems="center" sx={{ mb: 0.5 }}>
          <BadgeOutlinedIcon color="primary" />
          <Box sx={{ flex: 1 }}>
            <Typography variant="h6" sx={{ fontWeight: 700, lineHeight: 1.2 }}>
              {t('operations.persona.title')}
            </Typography>
            <Typography variant="body2" color="text.secondary">
              {t('operations.persona.subtitle')}
            </Typography>
          </Box>
          <Tooltip title={t('operations.persona.resetHelp') as string}>
            <span>
              <Button
                size="small"
                color="inherit"
                startIcon={<RestartAltIcon sx={{ fontSize: 16 }} />}
                onClick={() => reset.mutate()}
                disabled={!data?.overridden || reset.isPending}
              >
                {t('operations.persona.reset')}
              </Button>
            </span>
          </Tooltip>
          <Button
            size="small"
            variant="contained"
            onClick={() => save.mutate()}
            disabled={save.isPending}
            startIcon={save.isPending ? <CircularProgress size={14} /> : undefined}
          >
            {t('common.save')}
          </Button>
        </Stack>

        <Divider sx={{ my: 2 }} />

        {saveError && <Alert severity="error" sx={{ mb: 2 }}>{saveError}</Alert>}
        {save.isSuccess && !save.isPending && (
          <Alert severity="success" sx={{ mb: 2 }}>{t('operations.persona.saved')}</Alert>
        )}

        <Grid container spacing={3}>
          {/* Live preview */}
          <Grid item xs={12} md={3}>
            <Stack alignItems="center" spacing={1.25} sx={{ py: 1 }}>
              <PersonaAvatar persona={previewPersona} size={88} status fallbackName={name} />
              <Typography variant="subtitle1" sx={{ fontWeight: 800, textAlign: 'center' }}>
                {name || t('operations.persona.namePlaceholder')}
              </Typography>
              {role && (
                <Typography variant="caption" color="text.secondary" sx={{ textAlign: 'center' }}>
                  {role}
                </Typography>
              )}
              <Typography variant="caption" color="text.secondary" sx={{ textAlign: 'center', mt: 0.5 }}>
                {t('operations.persona.previewHint')}
              </Typography>
            </Stack>
          </Grid>

          {/* Editable fields */}
          <Grid item xs={12} md={9}>
            <Grid container spacing={2}>
              <Grid item xs={12} sm={6}>
                <TextField
                  label={t('operations.persona.name')}
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  size="small"
                  fullWidth
                  inputProps={{ maxLength: 80 }}
                  helperText={t('operations.persona.nameHelp')}
                />
              </Grid>
              <Grid item xs={12} sm={6}>
                <TextField
                  label={`${t('operations.persona.role')} (${lang.toUpperCase()})`}
                  value={role}
                  onChange={(e) => setRole(e.target.value)}
                  size="small"
                  fullWidth
                  inputProps={{ maxLength: 200 }}
                  helperText={t('operations.persona.roleHelp')}
                />
              </Grid>
              <Grid item xs={12}>
                <TextField
                  label={`${t('operations.persona.bio')} (${lang.toUpperCase()})`}
                  value={bio}
                  onChange={(e) => setBio(e.target.value)}
                  size="small"
                  fullWidth
                  multiline
                  minRows={2}
                  inputProps={{ maxLength: 1000 }}
                  helperText={t('operations.persona.bioHelp')}
                />
              </Grid>
              <Grid item xs={12} sm={6}>
                <TextField
                  label={`${t('operations.persona.greeting')} (${lang.toUpperCase()})`}
                  value={greeting}
                  onChange={(e) => setGreeting(e.target.value)}
                  size="small"
                  fullWidth
                  inputProps={{ maxLength: 400 }}
                  helperText={t('operations.persona.greetingHelp')}
                />
              </Grid>
              <Grid item xs={12} sm={6}>
                <TextField
                  label={`${t('operations.persona.tone')} (${lang.toUpperCase()})`}
                  value={tone}
                  onChange={(e) => setTone(e.target.value)}
                  size="small"
                  fullWidth
                  inputProps={{ maxLength: 200 }}
                  helperText={t('operations.persona.toneHelp')}
                />
              </Grid>

              {/* Traits */}
              <Grid item xs={12}>
                <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 0.5 }}>
                  {t('operations.persona.traits')}
                </Typography>
                <Stack direction="row" spacing={0.5} flexWrap="wrap" useFlexGap sx={{ mb: 1 }}>
                  {traits.map((tr) => (
                    <Chip
                      key={tr}
                      label={tr}
                      size="small"
                      onDelete={() => setTraits((prev) => prev.filter((x) => x !== tr))}
                    />
                  ))}
                  {traits.length === 0 && (
                    <Typography variant="caption" color="text.secondary">
                      {t('operations.persona.traitsEmpty')}
                    </Typography>
                  )}
                </Stack>
                <Stack direction="row" spacing={1}>
                  <TextField
                    placeholder={t('operations.persona.traitPlaceholder') as string}
                    value={traitDraft}
                    onChange={(e) => setTraitDraft(e.target.value)}
                    onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addTrait() } }}
                    size="small"
                    disabled={traits.length >= 6}
                    inputProps={{ maxLength: 40 }}
                    sx={{ maxWidth: 280 }}
                  />
                  <Button
                    size="small"
                    onClick={addTrait}
                    startIcon={<AddIcon sx={{ fontSize: 16 }} />}
                    disabled={traits.length >= 6 || !traitDraft.trim()}
                  >
                    {t('operations.persona.addTrait')}
                  </Button>
                </Stack>
              </Grid>

              {/* Icon / avatar */}
              <Grid item xs={12}>
                <Divider sx={{ mb: 1.5 }} />
                <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 0.5 }}>
                  {t('operations.persona.icon')}
                </Typography>
                <Stack direction="row" spacing={0.5} flexWrap="wrap" useFlexGap sx={{ mb: 1.5 }}>
                  <Chip
                    label={t('operations.persona.iconInitial', { initial: (name || resolved?.name || '·').charAt(0).toUpperCase() })}
                    size="small"
                    variant={emoji ? 'outlined' : 'filled'}
                    color={emoji ? 'default' : 'primary'}
                    onClick={() => setEmoji('')}
                  />
                  {EMOJI_SUGGESTIONS.map((e) => (
                    <Chip
                      key={e}
                      label={e}
                      size="small"
                      variant={emoji === e ? 'filled' : 'outlined'}
                      color={emoji === e ? 'primary' : 'default'}
                      onClick={() => setEmoji(e)}
                    />
                  ))}
                </Stack>
                <Grid container spacing={2}>
                  <Grid item xs={6} sm={3}>
                    <TextField
                      label={t('operations.persona.emoji')}
                      value={emoji}
                      onChange={(e) => setEmoji(e.target.value)}
                      size="small"
                      fullWidth
                      inputProps={{ maxLength: 8 }}
                    />
                  </Grid>
                  <Grid item xs={6} sm={3}>
                    <TextField
                      label={t('operations.persona.color')}
                      value={color}
                      onChange={(e) => setColor(e.target.value)}
                      size="small"
                      fullWidth
                      placeholder="#2563eb" // color-fijo: format example in a text field
                      inputProps={{ maxLength: 32 }}
                    />
                  </Grid>
                  <Grid item xs={12} sm={6}>
                    <TextField
                      label={t('operations.persona.imageUrl')}
                      value={imageUrl}
                      onChange={(e) => setImageUrl(e.target.value)}
                      size="small"
                      fullWidth
                      placeholder="https://… / /persona-mascot.png"
                      helperText={t('operations.persona.imageUrlHelp')}
                    />
                  </Grid>
                </Grid>
              </Grid>
            </Grid>
          </Grid>
        </Grid>
      </CardContent>
    </Card>
  )
}
