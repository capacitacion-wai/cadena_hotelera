/**
 * Welcome — the "AI employee" cover letter.
 *
 * A zero-jargon presentation page that introduces the agent as a WAI
 * employee: what it optimises, what it does automatically, what wakes
 * it up, where it listens, and what data it shows vs. creates. It is
 * 100% manifest-driven so every generated app gets a tailored intro
 * without hardcoding domain copy, and every visible string is i18n
 * (section labels from the bundle, per-item copy resolved per-locale
 * from the manifest's {es,en} fields).
 */
import { ReactNode, useEffect, useState } from 'react';
import {
  alpha,
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  CircularProgress,
  Divider,
  Grid,
  Stack,
  Tooltip,
  Typography,
} from '@mui/material';
import NotificationsActiveIcon from '@mui/icons-material/NotificationsActive';
import AutoAwesomeIcon from '@mui/icons-material/AutoAwesome';
import InsightsIcon from '@mui/icons-material/Insights';
import StorageIcon from '@mui/icons-material/Storage';
import EditNoteIcon from '@mui/icons-material/EditNote';
import HubIcon from '@mui/icons-material/Hub';
import AlternateEmailIcon from '@mui/icons-material/AlternateEmail';
import ChatIcon from '@mui/icons-material/Chat';
import ArrowForwardIcon from '@mui/icons-material/ArrowForward';
import AutoModeIcon from '@mui/icons-material/AutoMode';
import TrendingUpIcon from '@mui/icons-material/TrendingUp';
import TrendingDownIcon from '@mui/icons-material/TrendingDown';
import AssignmentTurnedInIcon from '@mui/icons-material/AssignmentTurnedIn';
import CalendarMonthIcon from '@mui/icons-material/CalendarMonth';
import FactCheckIcon from '@mui/icons-material/FactCheck';
import HandshakeIcon from '@mui/icons-material/Handshake';
import SmartToyIcon from '@mui/icons-material/SmartToy';
import ScheduleIcon from '@mui/icons-material/Schedule';
import TuneIcon from '@mui/icons-material/Tune';
import { Link as RouterLink } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { useManifest } from '../hooks/useManifest';
import type { AgentPersona } from '../hooks/useManifest';
import { usePickLabel } from '../utils/dataLabels';
import { useQuery } from '@tanstack/react-query';
import { apiFetch } from '../services/apiClient';
import { SIDEBAR_DARK, PRIMARY_DARK } from '../theme/theme';
import { PersonaAvatar } from '../components/PersonaAvatar';
import { BienvenidaDeMiembro } from '../components/BienvenidaDeMiembro';
import { useKnowledgeRequests } from '../components/BusinessParameters';

type Kpi = {
  id: string;
  label?: string;
  label_en?: string;
  labels?: Record<string, string>;
  description?: string;
  description_en?: string;
  descriptions?: Record<string, string>;
  format?: 'number' | 'integer' | 'percent' | 'currency';
  good_when?: 'higher' | 'lower';
  target?: number | null;
  value?: number | null;
  status?: string;
};

// Inbound email limits — kept in sync with backend channels/base.py
// (MAX_INBOUND_ATTACHMENTS / *_BYTES). Surfaced as concrete "house
// rules" so the operator knows exactly what they can send.
const MAIL_MAX_ATTACHMENTS = 10;
const MAIL_PER_FILE = '15 MB';
const MAIL_TOTAL = '40 MB';

const tenantCurrency = () =>
  (typeof window !== 'undefined' &&
    (window as unknown as { __waiCurrency?: string }).__waiCurrency) ||
  'EUR';

function fmt(
  v: number | null | undefined,
  format: 'number' | 'integer' | 'percent' | 'currency' = 'number',
) {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  switch (format) {
    case 'percent':
      return `${(v * 100).toFixed(1)}%`;
    case 'currency':
      return v.toLocaleString(undefined, { style: 'currency', currency: tenantCurrency() });
    case 'integer':
      return Math.round(v).toLocaleString();
    default:
      return v.toLocaleString(undefined, { maximumFractionDigits: 2 });
  }
}

/** 14.18.0 — the welcome depends on who opens it: the admin gets the full view (indicators,
 *  channels, routines), a MEMBER a very simple one (`BienvenidaDeMiembro`). Until the role is
 *  known, nothing: the full view would fire admin-only requests for a member. */
export function WelcomePage() {
  const [admin, setAdmin] = useState<boolean | null>(null);
  useEffect(() => {
    let vivo = true;
    apiFetch('/api/agent/users/me')
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (vivo) setAdmin(!!d?.is_admin); })
      .catch(() => { if (vivo) setAdmin(false); });
    return () => { vivo = false; };
  }, []);
  if (admin === null) return null;
  return admin ? <BienvenidaCompleta /> : <BienvenidaDeMiembro />;
}

function BienvenidaCompleta() {
  const { t, i18n } = useTranslation();
  const pickLabel = usePickLabel();
  const { manifest, loading } = useManifest();
  const lang = (i18n.language?.startsWith('en') ? 'en' : 'es') as 'es' | 'en';

  const [kpis, setKpis] = useState<Kpi[] | null>(null);
  const [channels, setChannels] = useState<any[] | null>(null);
  const [autonomousCount, setAutonomousCount] = useState<number>(0);

  // Resolve a manifest copy node that may be a plain string or a
  // {es,en} locale dict to the active language (with graceful fallback).
  const loc = (v: any): string => {
    if (!v) return '';
    if (typeof v === 'string') return v;
    if (typeof v === 'object') return v[lang] || v.es || v.en || '';
    return '';
  };

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await apiFetch('/api/dashboard/summary');
        if (r.ok) {
          const d = await r.json();
          if (!cancelled) setKpis(d.kpis ?? []);
        } else if (!cancelled) {
          setKpis([]);
        }
      } catch {
        if (!cancelled) setKpis([]);
      }
    })();
    (async () => {
      try {
        const r = await apiFetch('/api/channels');
        if (r.ok) {
          const d = await r.json();
          const list = Array.isArray(d) ? d : d?.channels ?? d?.items ?? [];
          if (!cancelled) setChannels(list);
        } else if (!cancelled) {
          setChannels([]);
        }
      } catch {
        if (!cancelled) setChannels([]);
      }
    })();
    (async () => {
      try {
        const r = await apiFetch('/api/agent/automations');
        if (r.ok) {
          const d = await r.json();
          if (!cancelled) setAutonomousCount((d?.items ?? []).length);
        }
      } catch { /* count stays 0 */ }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const agent = manifest?.agent ?? {};
  const persona = (agent.persona ?? null) as AgentPersona | null;
  const functionalName =
    (agent.display_name as string) ||
    (agent.name as string) ||
    (manifest?.app_name as string) ||
    t('app.title');
  // The hero headline is the persona's first name when present (e.g.
  // "Fina"), otherwise the functional agent name. The functional name
  // is kept as a sub-label so operators still know which agent this is.
  const personaName = (persona?.name || '').trim();
  const displayName = personaName || functionalName;
  // Role line: persona role first (e.g. "your stock-control teammate"),
  // then the marketing tagline, then the description.
  const role = loc(persona?.role);
  const tagline =
    role ||
    loc(agent.tagline) ||
    (agent.description as string) ||
    t('welcome.heroSubtitleFallback');
  const objective = manifest?.presentation?.objective || '';
  // First-person intro: prefer the persona's authored bio; otherwise
  // synthesise "Soy tu empleado IA para <goal>." from the discovery
  // objective so the hero always states what this employee is for.
  const personaBio = loc(persona?.personality?.bio);
  const iAmFor = personaBio
    ? personaBio
    : objective
    ? t('welcome.iAmFor', { goal: objective.charAt(0).toLowerCase() + objective.slice(1) })
    : '';
  // A few personality adjectives rendered as chips under the intro.
  const traits = (persona?.personality?.traits ?? [])
    .map((tr) => loc(tr))
    .filter(Boolean)
    .slice(0, 6);

  const entities = (manifest?.domain_entities ?? []) as any[];

  const readEntities = entities.filter((e: any) => (e?.source ?? 'project') !== 'local');
  const writeEntities = entities.filter((e: any) => (e?.source ?? 'project') === 'local');
  // Connectors (MCP servers) the employee can reach — part of what it consults
  // and acts on (Kommo, Gmail, WhatsApp, …). Shown in the "what I show and
  // what I create" section alongside the data it reads/writes.
  const connectors: any[] = Array.isArray((agent as any)?.mcps) ? (agent as any).mcps : []
  // Which of the canonical "employee outputs" this agent produces — so the
  // welcome page can explain, in the employee's own voice, WHEN it raises an
  // alert, makes a recommendation, or logs a decision.
  const hasOutput = (tbl: string) =>
    writeEntities.some((e: any) => String(e?.table ?? e?.slug ?? '').toLowerCase() === tbl);
  // v7.156.0 — the explanation ADAPTS to this employee. Before, the chips showed the
  // name the employee gives them (e.g. "detected incidents", "mitigation plans") and right
  // below the text talked about "alerts" and "recommendations": two vocabularies for the
  // same thing on the same card. Now the title is ITS name and the body mentions it. And
  // `scoring`, which had no explanation at all, stops showing up as a mute chip.
  const outputExplainers = writeEntities
    .map((e: any) => {
      const table = String(e?.table ?? e?.slug ?? '').toLowerCase();
      const label = String(e?.label ?? e?.name ?? '').trim();
      const key = ['alerts', 'recommendations', 'decisions', 'scoring'].includes(table)
        ? table : null;
      if (!key || !label) return null;
      return {
        k: table,
        t: label,
        b: t(`welcome.outputs.${key}.b`, { label }),
      };
    })
    .filter(Boolean) as { k: string; t: string; b: string }[];

  const liveKpis = (kpis ?? []).filter((k) => k.status !== 'error').slice(0, 4);
  const channelsEnabled = manifest?.channels_enabled ?? true;
  const activeChannels = channels ?? [];

  // At-a-glance numbers for the hero — only show the ones that have data
  // so the strip never looks empty on a freshly generated app.
  const heroStats = [
    // v7.66.0 — the "automations" stat counts ONLY real standing orders from
    // /api/agent/automations (autonomousCount), NEVER manifest.actions
    // (event_type_specs are direct playbooks, not the autonomous contract).
    autonomousCount > 0 ? { n: autonomousCount, label: t('welcome.stats.automations') } : null,
    channelsEnabled && activeChannels.length > 0
      ? { n: activeChannels.length, label: t('welcome.stats.channels') }
      : null,
    entities.length > 0 ? { n: entities.length, label: t('welcome.stats.data') } : null,
  ].filter(Boolean) as { n: number; label: string }[];

  // Business parameters the employee is waiting on → a prominent banner so the
  // operator notices the employee needs data to work well (v7.43.0).
  const knowledgePending = useKnowledgeRequests().pending;

  // v7.140.0 — rules the employee is ASKING permission to apply (things it
  // learned about the environment). Same reasoning as the banner above: a
  // proposal nobody sees is never answered, and meanwhile the employee keeps
  // producing the same false positive it already knows is noise.
  const learningPending = useQuery({
    queryKey: ['self-proposals-banner'],
    queryFn: async () => {
      const r = await apiFetch('/api/agent/self-memory/proposals');
      if (!r.ok) return { total: 0 };
      return r.json() as Promise<{ total: number }>;
    },
    refetchInterval: 120_000,
    refetchIntervalInBackground: false,
    retry: false,
  }).data?.total ?? 0;

  if (loading && !manifest) {
    return (
      <Stack alignItems="center" justifyContent="center" sx={{ height: '100%' }}>
        <CircularProgress />
      </Stack>
    );
  }

  return (
    <Box sx={{ height: '100%', overflowY: 'auto', pr: 0.5 }}>
      {/* Business parameters the employee is waiting on — the operator fills them
          in Operations → Conocimiento. */}
      {knowledgePending > 0 && (
        <Alert severity="warning" sx={{ mb: 2 }}
          action={
            <Button color="inherit" size="small" component={RouterLink} to="/operations">
              {t('welcome.knowledgeBanner.cta', 'Rellenar')}
            </Button>
          }>
          {t('welcome.knowledgeBanner.text', {
            count: knowledgePending,
            defaultValue: `El empleado necesita ${knowledgePending} dato(s) para funcionar mejor.`,
          })}
        </Alert>
      )}
      {/* Learned rules waiting for a human OK — they change BEHAVIOUR, so they
          never apply themselves; the operator decides in Operaciones →
          Aprendizaje (v7.140.0). */}
      {learningPending > 0 && (
        <Alert severity="info" sx={{ mb: 2 }}
          action={
            <Button color="inherit" size="small" component={RouterLink}
              to="/operations#aprendizaje">
              {t('welcome.learningBanner.cta', 'Revisar')}
            </Button>
          }>
          {t('welcome.learningBanner.text', {
            count: learningPending,
            defaultValue: `El empleado ha aprendido algo y te pide aplicar ${learningPending} regla(s).`,
          })}
        </Alert>
      )}
      {/* ---- Hero (aurora "presencia") ------------------------------- */}
      <Card
        sx={{
          mb: 3,
          position: 'relative',
          overflow: 'hidden',
          color: '#fff',
          border: 'none',
          // Deep-ink base + two glows (indigo top-left, mint bottom-right)
          // = the redesign's presence "aurora". Text stays legible over the base.
          background: `
            radial-gradient(120% 120% at 12% -12%, rgba(110,139,255,0.50), transparent 56%),
            radial-gradient(90% 100% at 108% 120%, rgba(79,227,192,0.30), transparent 60%),
            linear-gradient(135deg, ${SIDEBAR_DARK} 0%, #0E1626 100%)`, // color-fijo: the end of the cover gradient
          // Soft decorative glows so the hero reads as a polished banner
          // rather than a flat colored box.
          '&::before': {
            content: '""',
            position: 'absolute',
            top: -120,
            right: -80,
            width: 360,
            height: 360,
            borderRadius: '50%',
            background: 'radial-gradient(circle, rgba(255,255,255,0.16) 0%, transparent 70%)',
            pointerEvents: 'none',
          },
          '&::after': {
            content: '""',
            position: 'absolute',
            bottom: -140,
            left: -60,
            width: 300,
            height: 300,
            borderRadius: '50%',
            background: 'radial-gradient(circle, rgba(255,255,255,0.08) 0%, transparent 70%)',
            pointerEvents: 'none',
          },
        }}
      >
        <CardContent sx={{ p: { xs: 3, md: 4 }, position: 'relative' }}>
          <Stack
            direction={{ xs: 'column', sm: 'row' }}
            spacing={3}
            alignItems={{ xs: 'flex-start', sm: 'center' }}
          >
            {/* Persona avatar: breathing aurora presence + ring + "at work" dot */}
            <PersonaAvatar
              persona={persona}
              size={104}
              ring
              status
              alive
              fallbackName={functionalName}
            />
            <Box sx={{ flexGrow: 1, minWidth: 0 }}>
              <Stack direction="row" spacing={1} sx={{ mb: 1 }} flexWrap="wrap" useFlexGap>
                <Chip
                  icon={<SmartToyIcon sx={{ fontSize: 16, color: '#fff !important' }} />}
                  label={t('welcome.aiEmployeeBadge')}
                  size="small"
                  sx={{ bgcolor: 'rgba(255,255,255,0.16)', color: '#fff', fontWeight: 700 }}
                />
                <Chip
                  label={t('welcome.status')}
                  size="small"
                  sx={{
                    bgcolor: 'rgba(34,197,94,0.18)',
                    color: '#bbf7d0', // color-fijo: light green, legible over the dark cover
                    fontWeight: 700,
                    '& .MuiChip-label': { px: 1 },
                  }}
                />
                <Chip
                  icon={<ScheduleIcon sx={{ fontSize: 16, color: 'rgba(255,255,255,0.85) !important' }} />}
                  label={t('welcome.availability')}
                  size="small"
                  sx={{ bgcolor: 'rgba(255,255,255,0.10)', color: 'rgba(255,255,255,0.9)', fontWeight: 600 }}
                />
              </Stack>
              <Typography variant="h3" sx={{ color: '#fff', fontWeight: 800, lineHeight: 1.1, mb: 0.25 }}>
                {displayName}
              </Typography>
              {/* When the headline is the persona's first name, keep the
                  functional agent name visible so operators still know
                  which system they're talking to. */}
              {personaName && (
                <Typography
                  variant="caption"
                  sx={{
                    color: 'rgba(255,255,255,0.6)',
                    textTransform: 'uppercase',
                    letterSpacing: '0.06em',
                    fontWeight: 700,
                    display: 'block',
                    mb: 0.75,
                  }}
                >
                  {functionalName}
                </Typography>
              )}
              <Typography variant="h6" sx={{ color: 'rgba(255,255,255,0.9)', fontWeight: 400 }}>
                {tagline}
              </Typography>
              <Typography
                variant="body2"
                sx={{ color: 'rgba(255,255,255,0.7)', mt: 0.75, fontStyle: 'italic' }}
              >
                {t('welcome.aiEmployeeNote')}
              </Typography>
              {iAmFor && (
                <Typography
                  variant="body2"
                  sx={{ color: 'rgba(255,255,255,0.75)', mt: 1.5, maxWidth: 760 }}
                >
                  {iAmFor}
                </Typography>
              )}
              {traits.length > 0 && (
                <Stack direction="row" spacing={1} sx={{ mt: 1.75 }} flexWrap="wrap" useFlexGap>
                  {traits.map((tr) => (
                    <Chip
                      key={tr}
                      label={tr}
                      size="small"
                      sx={{
                        bgcolor: 'rgba(255,255,255,0.14)',
                        color: '#fff',
                        fontWeight: 600,
                        backdropFilter: 'blur(2px)',
                      }}
                    />
                  ))}
                </Stack>
              )}
              <Stack direction="row" spacing={1.5} sx={{ mt: 3 }} flexWrap="wrap" useFlexGap>
                <Button
                  component={RouterLink}
                  to="/agent"
                  variant="contained"
                  color="primary"
                  size="large"
                  startIcon={<ChatIcon />}
                  sx={{ fontWeight: 700, boxShadow: '0 8px 24px rgba(0,0,0,0.25)' }}
                >
                  {t('welcome.talkToMe')}
                </Button>
                <Button
                  component={RouterLink}
                  to="/dashboard"
                  variant="outlined"
                  size="large"
                  startIcon={<InsightsIcon />}
                  sx={{
                    color: '#fff',
                    borderColor: 'rgba(255,255,255,0.4)',
                    '&:hover': { borderColor: '#fff', bgcolor: 'rgba(255,255,255,0.08)' },
                  }}
                >
                  {t('welcome.viewDashboard')}
                </Button>
                <Tooltip title={t('welcome.editIdentityHint') as string} arrow>
                  <Button
                    component={RouterLink}
                    to="/identity"
                    size="large"
                    startIcon={<TuneIcon />}
                    sx={{
                      color: 'rgba(255,255,255,0.85)',
                      '&:hover': { color: '#fff', bgcolor: 'rgba(255,255,255,0.08)' },
                    }}
                  >
                    {t('welcome.editIdentity')}
                  </Button>
                </Tooltip>
              </Stack>
            </Box>
          </Stack>

          {/* At-a-glance stats */}
          {heroStats.length > 0 && (
            <Stack
              direction="row"
              spacing={{ xs: 2, sm: 4 }}
              sx={{ mt: 3.5, pt: 2.5, borderTop: '1px solid rgba(255,255,255,0.15)' }}
              flexWrap="wrap"
              useFlexGap
            >
              {heroStats.map((s) => (
                <Box key={s.label} sx={{ minWidth: 92 }}>
                  <Typography variant="h4" sx={{ color: '#fff', fontWeight: 800, lineHeight: 1 }}>
                    {s.n}
                  </Typography>
                  <Typography
                    variant="caption"
                    sx={{ color: 'rgba(255,255,255,0.7)', textTransform: 'uppercase', letterSpacing: '0.05em' }}
                  >
                    {s.label}
                  </Typography>
                </Box>
              ))}
            </Stack>
          )}
        </CardContent>
      </Card>

      {/* ---- Value props strip --------------------------------------- */}
      <Grid container spacing={2} sx={{ mb: 3 }}>
        {[
          { icon: <AutoAwesomeIcon />, k: 'autonomy' },
          { icon: <NotificationsActiveIcon />, k: 'proactive' },
          { icon: <AlternateEmailIcon />, k: 'channels' },
        ].map((h) => (
          <Grid item xs={12} md={4} key={h.k}>
            <Card
              variant="outlined"
              sx={{
                height: '100%',
                transition: 'transform 150ms ease, box-shadow 150ms ease, border-color 150ms ease',
                '&:hover': {
                  transform: 'translateY(-3px)',
                  boxShadow: 4,
                  borderColor: 'primary.main',
                },
              }}
            >
              <CardContent>
                <Stack direction="row" spacing={1.5} alignItems="flex-start">
                  <Box
                    sx={{
                      flexShrink: 0,
                      width: 44,
                      height: 44,
                      borderRadius: 2,
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      color: 'primary.main',
                      bgcolor: (theme) => alpha(theme.palette.primary.main, 0.12),
                    }}
                  >
                    {h.icon}
                  </Box>
                  <Box>
                    <Typography variant="subtitle1" sx={{ fontWeight: 700 }}>
                      {t(`welcome.highlights.${h.k}.title`)}
                    </Typography>
                    <Typography variant="body2" color="text.secondary">
                      {t(`welcome.highlights.${h.k}.body`)}
                    </Typography>
                  </Box>
                </Stack>
              </CardContent>
            </Card>
          </Grid>
        ))}
      </Grid>

      {/* ---- Ways to work with me ------------------------------------ */}
      <Section
        icon={<HandshakeIcon color="primary" />}
        title={t('welcome.collaborate.title')}
        subtitle={t('welcome.collaborate.subtitle')}
      >
        <Grid container spacing={2}>
          {[
            { k: 'autonomous', to: '/trabajo-autonomo', icon: <AutoModeIcon />, count: autonomousCount },
            { k: 'encargos', to: '/encargos', icon: <AssignmentTurnedInIcon /> },
            { k: 'calendar', to: '/calendar', icon: <CalendarMonthIcon /> },
            { k: 'approvals', to: '/approvals', icon: <FactCheckIcon /> },
          ].map((c: any) => (
            <Grid item xs={12} md={6} key={c.k}>
              <Card
                variant="outlined"
                component={RouterLink}
                to={c.to}
                sx={{
                  height: '100%',
                  display: 'block',
                  textDecoration: 'none',
                  color: 'inherit',
                  transition: 'transform 150ms ease, box-shadow 150ms ease, border-color 150ms ease',
                  '&:hover': {
                    transform: 'translateY(-3px)',
                    boxShadow: 4,
                    borderColor: 'primary.main',
                  },
                }}
              >
                <CardContent>
                  <Stack direction="row" spacing={1.5} alignItems="flex-start">
                    <Box
                      sx={{
                        flexShrink: 0,
                        width: 44,
                        height: 44,
                        borderRadius: 2,
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                        color: 'primary.main',
                        bgcolor: (theme) => alpha(theme.palette.primary.main, 0.12),
                      }}
                    >
                      {c.icon}
                    </Box>
                    <Box sx={{ minWidth: 0 }}>
                      <Typography variant="subtitle1" sx={{ fontWeight: 700 }}>
                        {t(`welcome.collaborate.${c.k}.title`)}
                        {typeof c.count === 'number' ? ` (${c.count})` : ''}
                      </Typography>
                      <Typography variant="body2" color="text.secondary">
                        {t(`welcome.collaborate.${c.k}.body`)}
                      </Typography>
                      <Stack direction="row" spacing={0.5} alignItems="center" sx={{ mt: 1.25, color: 'primary.main' }}>
                        <Typography variant="caption" sx={{ fontWeight: 700 }}>
                          {t(`welcome.collaborate.${c.k}.cta`)}
                        </Typography>
                        <ArrowForwardIcon sx={{ fontSize: 15 }} />
                      </Stack>
                    </Box>
                  </Stack>
                </CardContent>
              </Card>
            </Grid>
          ))}
        </Grid>
      </Section>

      {/* ---- What I optimise ----------------------------------------- */}
      {liveKpis.length > 0 && (
        <Section
          icon={<InsightsIcon color="primary" />}
          title={t('welcome.optimize.title')}
          subtitle={t('welcome.optimize.subtitle')}
        >
          <Grid container spacing={2}>
            {liveKpis.map((k) => {
              const good =
                k.target == null || k.value == null
                  ? null
                  : k.good_when === 'lower'
                  ? (k.value as number) <= (k.target as number)
                  : (k.value as number) >= (k.target as number);
              const why =
                loc(k.descriptions) ||
                (lang === 'en' ? k.description_en || k.description : k.description) ||
                '';
              return (
                <Grid item xs={12} md={6} key={k.id}>
                  <Card variant="outlined" sx={{ height: '100%' }}>
                    <CardContent>
                      <Stack direction="row" alignItems="flex-start" spacing={1}>
                        <Box sx={{ flexGrow: 1, minWidth: 0 }}>
                          <Typography variant="overline" color="text.secondary">
                            {pickLabel(k, k.id)}
                          </Typography>
                          <Typography variant="h4" sx={{ mt: 0.25, fontWeight: 800, lineHeight: 1.1 }}>
                            {fmt(k.value, k.format)}
                          </Typography>
                        </Box>
                        {k.target != null && (
                          <Chip
                            size="small"
                            color={good == null ? 'default' : good ? 'success' : 'warning'}
                            icon={k.good_when === 'lower' ? <TrendingDownIcon /> : <TrendingUpIcon />}
                            label={`${t('welcome.optimize.target')}: ${fmt(k.target, k.format)}`}
                          />
                        )}
                      </Stack>
                      {why && (
                        <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
                          {why}
                        </Typography>
                      )}
                    </CardContent>
                  </Card>
                </Grid>
              );
            })}
          </Grid>
          <Button
            component={RouterLink}
            to="/dashboard"
            endIcon={<ArrowForwardIcon />}
            sx={{ mt: 1.5 }}
          >
            {t('welcome.optimize.seeDashboard')}
          </Button>
        </Section>
      )}

      {/* v7.66.0 — the old "what I do automatically" section rendered
          manifest.actions (event_type_specs = DIRECT PLAYBOOKS) as if they were
          the employee's active automations. That conflated build-time playbooks
          with the real autonomy contract, which lives in Trabajo autónomo
          (/api/agent/automations). Removed: what the employee does on its own is
          the "Trabajo autónomo" section below, sourced from the real standing
          orders — never from the manifest. */}

      {/* ---- Trabajo autónomo (v7.27.0: replaces the design-time watcher
              list) — the employee runs standing jobs the boss OR the employee
              itself creates; they live in /trabajo-autonomo. */}
      <Section
        icon={<NotificationsActiveIcon color="primary" />}
        title={t('welcome.autonomousWork.title', 'Trabajo autónomo')}
        subtitle={t('welcome.autonomousWork.subtitle', 'Trabajo recurrente que hago sin que nadie me lo pida cada vez: vigilancias, informes y avisos.')}
      >
        <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
          {t('welcome.autonomousWork.body', 'En Trabajo autónomo puedes ver, pausar o crear mis trabajos recurrentes («cuando pase X, haz Y», «cada lunes a las 9, prepara Z»). También puedes pedírmelo por chat y los creo yo mismo — según mi nivel de autonomía, te los dejaré en Aprobaciones antes de activarlos.')}
        </Typography>
        <Button
          component={RouterLink}
          to="/trabajo-autonomo"
          variant="outlined"
          size="small"
          sx={{ textTransform: 'none' }}
        >
          {t('welcome.autonomousWork.cta', 'Ver mi trabajo autónomo')}
        </Button>
      </Section>

      {/* ---- Where I listen ------------------------------------------ */}
      {channelsEnabled && (
        <Section
          icon={<AlternateEmailIcon color="primary" />}
          title={t('welcome.channels.title')}
          subtitle={t('welcome.channels.subtitle')}
        >
          {activeChannels.length > 0 ? (
            <Grid container spacing={2}>
              {activeChannels.map((c: any, i: number) => {
                const address =
                  c?.mailbox_address || c?.address || c?.from_number || c?.identity || '—';
                const name = c?.display_name || address;
                const kindLabel = c?.kind
                  ? t(`channels.kind.${c.kind}`, { defaultValue: c.kind })
                  : null;
                const status = c?.status
                  ? t(`channels.status.${c.status}`, { defaultValue: c.status })
                  : null;
                const fc = c?.filter_config ?? {};
                // Sender whitelist is REQUIRED + fail-closed in the poller
                // (from_domain_allow for email, from_numbers_allow for
                // whatsapp). Read those exact keys; legacy aliases kept as
                // a fallback only.
                const senders: string[] =
                  fc.from_domain_allow ??
                  fc.from_numbers_allow ??
                  fc.from_domains ??
                  fc.allowed_senders ??
                  fc.from_numbers ??
                  [];
                const subject: string = fc.subject_regex ?? '';
                const replyAuto = (c?.reply_mode ?? 'auto') === 'auto';
                const isEmail = (c?.kind ?? 'email') === 'email';
                return (
                  <Grid item xs={12} md={6} key={c?.id ?? i}>
                    <Card variant="outlined" sx={{ height: '100%' }}>
                      <CardContent>
                        <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
                          <AlternateEmailIcon fontSize="small" color="primary" />
                          <Typography variant="subtitle1" noWrap sx={{ flexGrow: 1, minWidth: 0 }}>
                            {name}
                          </Typography>
                          {kindLabel && <Chip size="small" label={kindLabel} />}
                          {status && <Chip size="small" variant="outlined" label={status} />}
                        </Stack>

                        {/* Concrete address to reach the employee */}
                        <Typography variant="caption" color="text.secondary">
                          {isEmail ? t('welcome.channels.writeTo') : t('welcome.channels.reachAt')}
                        </Typography>
                        <Typography
                          variant="body2"
                          sx={{ fontWeight: 700, fontFamily: 'monospace', mb: 1.5, wordBreak: 'break-all' }}
                        >
                          {address}
                        </Typography>

                        {/* House rules — concrete, not vague */}
                        <Typography variant="caption" color="text.secondary">
                          {t('welcome.channels.rules')}
                        </Typography>
                        <Stack component="ul" sx={{ m: 0, mt: 0.5, pl: 2.5 }} spacing={0.5}>
                          {isEmail && (
                            <Typography component="li" variant="body2" color="text.secondary">
                              {t('welcome.channels.ruleAttachments', {
                                max: MAIL_MAX_ATTACHMENTS,
                                perFile: MAIL_PER_FILE,
                                total: MAIL_TOTAL,
                              })}
                            </Typography>
                          )}
                          {isEmail && (
                            <Typography component="li" variant="body2" color="text.secondary">
                              {t('welcome.channels.ruleThread')}
                            </Typography>
                          )}
                          <Typography component="li" variant="body2" color="text.secondary">
                            {/* v7.155.0 — the text was the EMAIL one for every channel:
                                on Telegram and WhatsApp it read "I only handle emails", which
                                makes no sense and confuses whoever is about to write there. */}
                            {senders.length > 0
                              ? `${t(isEmail ? 'welcome.channels.allowedSenders'
                                             : 'welcome.channels.allowedSendersMsg')}: ${senders.join(', ')}`
                              : t(isEmail ? 'welcome.channels.restrictedSenders'
                                          : 'welcome.channels.restrictedSendersMsg')}
                          </Typography>
                          {subject && (
                            <Typography component="li" variant="body2" color="text.secondary">
                              {t('welcome.channels.subjectFilter')}: <code>{subject}</code>
                            </Typography>
                          )}
                          <Typography component="li" variant="body2" color="text.secondary">
                            {replyAuto
                              ? t(isEmail ? 'welcome.channels.replyAuto'
                                          : 'welcome.channels.replyAutoMsg')
                              : t('welcome.channels.replyLog')}
                          </Typography>
                        </Stack>
                      </CardContent>
                    </Card>
                  </Grid>
                );
              })}
            </Grid>
          ) : (
            <Typography variant="body2" color="text.secondary">
              {t('welcome.channels.empty')}
            </Typography>
          )}
        </Section>
      )}

      {/* ---- What I show vs. create ---------------------------------- */}
      {(readEntities.length > 0 || writeEntities.length > 0 || connectors.length > 0) && (
        <Section
          icon={<StorageIcon color="primary" />}
          title={t('welcome.data.title')}
          subtitle={t('welcome.data.subtitle')}
        >
          <Grid container spacing={2}>
            <Grid item xs={12} md={6}>
              <Card variant="outlined" sx={{ height: '100%' }}>
                <CardContent>
                  <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
                    <StorageIcon fontSize="small" color="action" />
                    <Typography variant="subtitle1">{t('welcome.data.reads')}</Typography>
                  </Stack>
                  {readEntities.length > 0 ? (
                    <Stack direction="row" spacing={0.5} flexWrap="wrap" useFlexGap>
                      {readEntities.map((e: any) => (
                        <Chip
                          key={e.slug}
                          size="small"
                          variant="outlined"
                          component={RouterLink}
                          clickable
                          to={`/domain/${e.slug}`}
                          label={pickLabel(e, e.slug)}
                        />
                      ))}
                    </Stack>
                  ) : (
                    <Typography variant="body2" color="text.secondary">
                      {t('welcome.data.readsEmpty')}
                    </Typography>
                  )}
                </CardContent>
              </Card>
            </Grid>
            <Grid item xs={12} md={6}>
              <Card variant="outlined" sx={{ height: '100%' }}>
                <CardContent>
                  <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
                    <EditNoteIcon fontSize="small" color="action" />
                    <Typography variant="subtitle1">{t('welcome.data.writes')}</Typography>
                  </Stack>
                  {writeEntities.length > 0 ? (
                    <Stack direction="row" spacing={0.5} flexWrap="wrap" useFlexGap>
                      {writeEntities.map((e: any) => (
                        <Chip
                          key={e.slug}
                          size="small"
                          color="primary"
                          variant="outlined"
                          component={RouterLink}
                          clickable
                          to={`/domain/${e.slug}`}
                          label={pickLabel(e, e.slug)}
                        />
                      ))}
                    </Stack>
                  ) : (
                    <Typography variant="body2" color="text.secondary">
                      {t('welcome.data.writesEmpty')}
                    </Typography>
                  )}

                  {/* The employee explains WHEN it produces each output — how an
                      employee leaves a trace of its work in a company. */}
                  {outputExplainers.length > 0 && (
                    <Box sx={{ mt: 1.5 }}>
                      <Divider sx={{ mb: 1 }} />
                      <Typography variant="caption" color="text.secondary" sx={{ fontWeight: 700, display: 'block', mb: 0.75 }}>
                        {t('welcome.outputs.title', 'Cómo dejo constancia de mi trabajo')}
                      </Typography>
                      <Stack spacing={0.75}>
                        {outputExplainers.map((o) => (
                          <Typography key={o.k} variant="body2" color="text.secondary">
                            <b>{o.t}:</b> {o.b}
                          </Typography>
                        ))}
                      </Stack>
                    </Box>
                  )}
                </CardContent>
              </Card>
            </Grid>

            {/* Connectors (MCP) the employee can reach — part of what it
                consults and acts on (Kommo, Gmail, WhatsApp, …). */}
            {connectors.length > 0 && (
              <Grid item xs={12}>
                <Card variant="outlined">
                  <CardContent>
                    <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
                      <HubIcon fontSize="small" color="action" />
                      <Typography variant="subtitle1">{t('welcome.data.connectors', 'Conectores')}</Typography>
                    </Stack>
                    <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
                      {t('welcome.data.connectorsHint', 'Herramientas externas a las que me conecto para consultar y actuar.')}
                    </Typography>
                    <Stack direction="row" spacing={0.5} flexWrap="wrap" useFlexGap>
                      {connectors.map((c: any, i: number) => (
                        <Chip
                          key={c?.name ?? i}
                          size="small"
                          color={c?.external ? 'primary' : 'default'}
                          variant="outlined"
                          label={c?.display_name || c?.composio_toolkit || c?.name || '—'}
                          sx={{ textTransform: 'capitalize' }}
                        />
                      ))}
                    </Stack>
                  </CardContent>
                </Card>
              </Grid>
            )}
          </Grid>
        </Section>
      )}

      {/* ---- Final CTA ----------------------------------------------- */}
      <Card
        sx={{
          mt: 1,
          color: '#fff',
          position: 'relative',
          overflow: 'hidden',
          background: `linear-gradient(135deg, ${PRIMARY_DARK} 0%, ${SIDEBAR_DARK} 100%)`,
          '&::before': {
            content: '""',
            position: 'absolute',
            top: -100,
            left: '40%',
            width: 320,
            height: 320,
            borderRadius: '50%',
            background: 'radial-gradient(circle, rgba(255,255,255,0.12) 0%, transparent 70%)',
            pointerEvents: 'none',
          },
        }}
      >
        <CardContent sx={{ textAlign: 'center', py: 5, position: 'relative' }}>
          <Box sx={{ display: 'flex', justifyContent: 'center', mb: 1.5 }}>
            <PersonaAvatar persona={persona} size={56} ring fallbackName={functionalName} />
          </Box>
          <Typography variant="h5" sx={{ color: '#fff', fontWeight: 800 }}>
            {t('welcome.cta.title')}
          </Typography>
          <Typography variant="body1" sx={{ color: 'rgba(255,255,255,0.8)', mb: 2.5, mt: 0.5 }}>
            {t('welcome.cta.subtitle')}
          </Typography>
          <Button
            component={RouterLink}
            to="/agent"
            variant="contained"
            color="primary"
            size="large"
            startIcon={<ChatIcon />}
            sx={{ fontWeight: 700, boxShadow: '0 8px 24px rgba(0,0,0,0.25)' }}
          >
            {t('welcome.cta.button')}
          </Button>
        </CardContent>
      </Card>
    </Box>
  );
}

function Section({
  icon,
  title,
  subtitle,
  children,
}: {
  icon: ReactNode;
  title: string;
  subtitle?: string;
  children: ReactNode;
}) {
  return (
    <Box sx={{ mb: 4 }}>
      <Stack direction="row" spacing={1.5} alignItems="center">
        <Box
          sx={{
            width: 38,
            height: 38,
            borderRadius: 2,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            bgcolor: (theme) => alpha(theme.palette.primary.main, 0.12),
          }}
        >
          {icon}
        </Box>
        <Box>
          <Typography variant="h6" sx={{ fontWeight: 700, lineHeight: 1.2 }}>{title}</Typography>
          {subtitle && (
            <Typography variant="body2" color="text.secondary">
              {subtitle}
            </Typography>
          )}
        </Box>
      </Stack>
      <Divider sx={{ my: 2 }} />
      {children}
    </Box>
  );
}

export default WelcomePage;
