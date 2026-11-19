import React, { ReactNode, useEffect, useMemo, useState } from 'react';
import {
  AppBar,
  Avatar,
  Box,
  Chip,
  CircularProgress,
  Collapse,
  Divider,
  Drawer,
  IconButton,
  List,
  ListItem,
  ListItemButton,
  ListItemIcon,
  ListItemText,
  ListSubheader,
  Menu,
  MenuItem,
  Stack,
  ToggleButton,
  ToggleButtonGroup,
  Toolbar,
  Tooltip,
  Typography,
} from '@mui/material';
import DashboardIcon from '@mui/icons-material/Dashboard';
import StorageIcon from '@mui/icons-material/Storage';
import EditNoteIcon from '@mui/icons-material/EditNote';
import ShieldOutlinedIcon from '@mui/icons-material/ShieldOutlined'
import GroupsIcon from '@mui/icons-material/Groups';
import OutboxIcon from '@mui/icons-material/Outbox';
import SmartToyIcon from '@mui/icons-material/SmartToy';
import PsychologyIcon from '@mui/icons-material/Psychology';
import HistoryIcon from '@mui/icons-material/History';
import EventIcon from '@mui/icons-material/Event';
import PaidIcon from '@mui/icons-material/PaidOutlined';
import ExpandMoreIcon from '@mui/icons-material/ExpandMore';
import GroupIcon from '@mui/icons-material/Group';
import LanguageIcon from '@mui/icons-material/Language';
import HealthBell from './HealthBell';
import ScheduleIcon from '@mui/icons-material/Schedule';
import LogoutIcon from '@mui/icons-material/Logout';
import AlternateEmailIcon from '@mui/icons-material/AlternateEmail';
import WavingHandIcon from '@mui/icons-material/WavingHand';
import ChatIcon from '@mui/icons-material/Chat';
import MenuBookIcon from '@mui/icons-material/MenuBook';
import CalendarMonthIcon from '@mui/icons-material/CalendarMonth';
import AssignmentTurnedInIcon from '@mui/icons-material/AssignmentTurnedIn';
import FactCheckIcon from '@mui/icons-material/FactCheck';
import AutoModeIcon from '@mui/icons-material/AutoMode';
import TimelineIcon from '@mui/icons-material/Timeline';
import BadgeIcon from '@mui/icons-material/Badge';
import AssignmentIndIcon from '@mui/icons-material/AssignmentInd';
import AdminPanelSettingsIcon from '@mui/icons-material/AdminPanelSettings';
import ChevronLeftIcon from '@mui/icons-material/ChevronLeft';
import ChevronRightIcon from '@mui/icons-material/ChevronRight';
import { Link as RouterLink, useLocation, useNavigate } from 'react-router-dom';
import { useEsMovil } from '../hooks/useEsMovil';
import SoloChatMovil from './SoloChatMovil';
import { useTranslation } from 'react-i18next';
import { useAuth } from '../auth/AuthContext';
import { ColorModeToggle, useColorMode } from '../theme/ColorMode';
import { useManifest } from '../hooks/useManifest';
import { apiFetch } from '../services/apiClient';
import { usePickLabel } from '../utils/dataLabels';
import {
  useTzMode,
  setTzMode,
  activeTimeZoneName,
  timeZoneAbbrev,
  businessTimeZone,
  browserTimeZone,
} from '../utils/datetime';

const DRAWER_WIDTH = 248;
const COLLAPSED_WIDTH = 72;

type Area = 'employee' | 'admin';

type NavItem = {
  to: string;
  labelKey: string;
  icon: ReactNode;
};

type NavGroup = {
  key: string;
  labelKey: string;
  items: NavItem[];
};

// Which top-level area a route belongs to. Lets a deep-link (e.g.
// /operations) auto-select the right menu, and the area toggle jump to
// each area's landing page.
// v7.38.0 — "Ejecuciones" (/runs) and "Eventos" (/events) moved to the employee
// "Seguimiento" group (they're the operator's monitoring tools), so they are no
// longer admin-area routes. Still admin-only in the nav (the endpoints stay
// require_admin), so a non-admin never sees them nor 403s.
const ADMIN_PREFIXES = ['/operations', '/capacidades', '/companeros', '/identity', '/users', '/channels', '/consumo'];
const AREA_HOME: Record<Area, string> = { employee: '/', admin: '/operations' };

/** 14.18.0 — the employee screens a MEMBER does not get: a member USES the employee (chat,
 *  manual, their own encargos), it does not operate it. The backend refuses them (403); here
 *  they leave the menu and a typed address goes back to the welcome. Approvals depend on
 *  «can approve», not on being admin (see `soloQuienAprueba`). */
const SOLO_ADMIN_EMPLEADO = ['/dashboard', '/trabajo-autonomo', '/calendar', '/actividad',
  '/runs', '/events', '/domain/', '/data/']
const soloAdmin = (path: string) =>
  SOLO_ADMIN_EMPLEADO.some((p) => path === p || (p.endsWith('/') ? path.startsWith(p) : path.startsWith(p + '/')))
const soloQuienAprueba = (path: string) => path === '/approvals' || path.startsWith('/approvals/')

function areaForPath(path: string, extraAdmin?: string | null): Area {
  // v7.67.0 — ``extraAdmin`` lets the caller classify an app-authored path (the
  // Comunicaciones domain page, whose slug is dynamic) as ADMIN too, so opening
  // it from the Auditoría y consumo group keeps the admin menu.
  if (extraAdmin && (path === extraAdmin || path.startsWith(extraAdmin + '/'))) {
    return 'admin';
  }
  return ADMIN_PREFIXES.some((p) => path === p || path.startsWith(p + '/'))
    ? 'admin'
    : 'employee';
}

export function Layout({ children }: { children: ReactNode }) {
  const { t, i18n } = useTranslation();
  const { manifest } = useManifest();
  const { applyDefault: applyDefaultColorMode } = useColorMode();
  // The employee's default theme (admin Identity): applied when the manifest loads ONLY
  // if the operator has not picked a theme by hand (the toggle persists their choice).
  useEffect(() => {
    const d = manifest?.default_color_mode;
    if (d === 'light' || d === 'dark') applyDefaultColorMode(d);
  }, [manifest, applyDefaultColorMode]);
  const pickLabel = usePickLabel();
  React.useEffect(() => {
    const name = manifest?.app_name || manifest?.agent_app?.app_name;
    if (name) document.title = name;
    // P4-6: surface the tenant's currency on a global so currency
    // formatters (DomainDetail.fmtCurrency, KPI cards, charts) can
    // pick it up without each component depending on the manifest
    // hook. Defaults to EUR if the manifest doesn't declare one.
    const ccy = manifest?.agent_app?.locale?.currency || manifest?.locale?.currency;
    if (ccy && typeof window !== 'undefined') {
      (window as unknown as { __waiCurrency?: string }).__waiCurrency = ccy;
    }
    // Business timezone for the centralised date formatters
    // (utils/datetime.ts). Falls back to the browser tz when unset.
    const tz = (manifest as any)?.timezone
      || (manifest as any)?.locale?.timezone
      || manifest?.agent_app?.locale?.timezone;
    if (tz && typeof window !== 'undefined') {
      (window as unknown as { __waiTimezone?: string }).__waiTimezone = tz;
    }
  }, [manifest]);
  const { user, logout } = useAuth();
  const loc = useLocation();
  const navigate = useNavigate();

  // ── MOBILE: chat only, and the decision lives HERE on purpose ──────────────
  //
  // This menu is a fixed-width `permanent` `Drawer`: on a narrow screen it either
  // eats the content or leaves the tables, details and approvals unreadable.
  // We were serving an app that could be RENDERED but not USED, and that does
  // not read as "this is desktop-only", it reads as "this is broken".
  //
  // It lives in `Layout` and not in `App.tsx` because `Layout` is INVARIANT and
  // `App.tsx` is scaffold: placed there, the feature travelled to NEW employees and
  // reached none of the existing ones —`--fix-invariants` does not touch the
  // scaffold—, so it was deployed and switched on nowhere. Measured: the bundle of
  // the travel employee, already updated, had no trace of it.
  const esMovil = useEsMovil();
  useEffect(() => {
    // Any other address leads to the chat instead of a broken screen: someone who
    // opens a saved link from their phone ends up where they can work.
    if (esMovil && loc.pathname !== '/agent') {
      navigate('/agent', { replace: true });
    }
  }, [esMovil, loc.pathname, navigate]);

  const [langAnchor, setLangAnchor] = useState<HTMLElement | null>(null);
  const [userAnchor, setUserAnchor] = useState<HTMLElement | null>(null);
  const [tzAnchor, setTzAnchor] = useState<HTMLElement | null>(null);

  // Global display-timezone toggle: every screen renders dates in this
  // tz, and the header chip below makes the active one explicit.
  const tzMode = useTzMode();
  const businessTz = businessTimeZone();
  const localTz = browserTimeZone();
  const activeTz = activeTimeZoneName();
  const activeAbbr = timeZoneAbbrev();

  // Collapsible sidebar (mini icon-rail). Persisted so the operator's
  // preference survives reloads.
  const [collapsed, setCollapsed] = useState<boolean>(() => {
    try {
      return localStorage.getItem('wai.nav.collapsed') === '1';
    } catch {
      return false;
    }
  });
  const toggleCollapsed = () => {
    setCollapsed((c) => {
      const next = !c;
      try {
        localStorage.setItem('wai.nav.collapsed', next ? '1' : '0');
      } catch {
        /* ignore */
      }
      return next;
    });
  };

  // Per-group fold. The lower-value groups ("seguimiento" tracking and the
  // agent-"generado" data) start FOLDED so the front door (Inicio/Chat) and
  // the daily work stay above the fold; the operator can pin any group open
  // and it persists. Keyed by group key (see the nav groups below).
  const GROUP_DEFAULT_FOLDED = ['tracking', 'generated'];
  const [foldedGroups, setFoldedGroups] = useState<Record<string, boolean>>(() => {
    try {
      const raw = localStorage.getItem('wai.nav.foldedGroups');
      if (raw) return JSON.parse(raw) as Record<string, boolean>;
    } catch {
      /* ignore */
    }
    return GROUP_DEFAULT_FOLDED.reduce((m, k) => ({ ...m, [k]: true }), {});
  });
  const isFolded = (key: string) =>
    foldedGroups[key] ?? GROUP_DEFAULT_FOLDED.includes(key);
  const toggleGroup = (key: string) => {
    setFoldedGroups((prev) => {
      const next = { ...prev, [key]: !isFolded(key) };
      try {
        localStorage.setItem('wai.nav.foldedGroups', JSON.stringify(next));
      } catch {
        /* ignore */
      }
      return next;
    });
  };

  // v7.67.0 — Comunicaciones (the OUTBOX) lives in Admin → Auditoría y consumo.
  // Its domain path must count as ADMIN so opening it keeps the admin menu; the
  // slug is app-authored, so resolve the path from the manifest.
  const commsPath = useMemo<string | null>(() => {
    const ents = (manifest as any)?.domain_entities ?? [];
    const e = ents.find((x: any) =>
      (x?.source ?? 'project') === 'local'
      && String(x?.table ?? x?.slug ?? '').toLowerCase() === 'communications');
    return e ? `/domain/${e.slug}` : null;
  }, [manifest]);

  // Top-level area switch: "Empleado WAI" workspace vs "Administración".
  // Initialised from the current route so a refresh on /operations keeps
  // the admin menu; kept in sync as the operator navigates.
  const [area, setArea] = useState<Area>(() => areaForPath(loc.pathname, commsPath));
  useEffect(() => {
    setArea(areaForPath(loc.pathname, commsPath));
  }, [loc.pathname, commsPath]);

  const switchArea = (next: Area | null) => {
    if (!next || next === area) return;
    setArea(next);
    navigate(AREA_HOME[next]);
  };

  // Admin gating (v7.23.0): only a user whose directory role is admin sees the
  // Administración area. Fail-CLOSED — the admin nav/area is shown ONLY once we
  // KNOW the role is admin (meAdmin === true); while unknown we show a permission
  // check on admin routes (no content flash) and redirect members away. The
  // backend also gates every admin endpoint with require_admin, so a member
  // never receives admin data even if the UI briefly renders a shell. `null` =
  // still loading, `true`/`false` = resolved. The deploy identity is always
  // admin, so an operator is never locked out.
  const [meAdmin, setMeAdmin] = useState<boolean | null>(null);
  const [meApprove, setMeApprove] = useState(false);
  useEffect(() => {
    let cancelled = false;
    apiFetch('/api/agent/users/me')
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (cancelled) return;
        setMeAdmin(d ? !!d.is_admin : false);
        setMeApprove(!!d?.can_approve);
      })
      .catch(() => { if (!cancelled) setMeAdmin(false); });
    return () => { cancelled = true; };
  }, []);
  const canAdmin = meAdmin === true;
  // Fail-closed like the admin area: until the role is KNOWN, a member screen set.
  const vetada = !canAdmin && (soloAdmin(loc.pathname) || (soloQuienAprueba(loc.pathname) && !meApprove));
  useEffect(() => {
    if (meAdmin === false && vetada) navigate('/', { replace: true });
  }, [meAdmin, vetada, navigate]);
  const onAdminRoute = areaForPath(loc.pathname, commsPath) === 'admin';
  useEffect(() => {
    if (meAdmin === false && area === 'admin') {
      setArea('employee');
      navigate(AREA_HOME.employee);
    }
  }, [meAdmin, area, navigate]);

  const { employeeGroups, adminGroups } = useMemo(() => {
    const entities = (manifest as any)?.domain_entities ?? [];
    // Defence in depth — the backend already strips runtime entities
    // (see manifest_routes.RUNTIME_BLACKLIST). Keep the business nav
    // clean even if a stale manifest leaks operational tables.
    const RUNTIME_SLUGS = new Set([
      'events', 'agent_runs', 'tool_call_log',
      'event_audit', 'watcher_cursor', 'watcher_schedules',
      'channels', 'channel_messages',
      // v7.30.0 — work-coordination substrate already shown in "Trabajo del
      // empleado" (calendar/agenda, encargos, approvals): keep them out of
      // "Lo que genero" so each surface appears exactly once.
      'scheduled_items', 'approvals',
    ]);
    const businessOnly = entities.filter((e: any) => {
      const slug = String(e?.slug ?? '').toLowerCase();
      const table = String(e?.table ?? '').toLowerCase();
      return !RUNTIME_SLUGS.has(slug) && !RUNTIME_SLUGS.has(table);
    });
    // "Datos que consulto" = PROJECT data the agent reads (warehouse).
    // "Lo que genero" = LOCAL business outputs the agent writes (alerts,
    // recommendations, decisions, scoring). Comunicaciones (the outbox) is NOT
    // here — it's an audit surface under Admin → Auditoría y consumo (v7.67.0).
    const projectNav: NavItem[] = businessOnly
      .filter((e: any) => (e.source ?? 'project') !== 'local')
      .map((e: any) => ({
        to: `/domain/${e.slug}`,
        labelKey: pickLabel(e, e.slug),
        icon: <StorageIcon />,
      }));
    // Canonical order for "Lo que genero" (What I generate): incidents → suggestions
    // → decisions → metrics; non-canonical entities keep their manifest order at the
    // end. (Comunicaciones is shown separately, under Auditoría y consumo.)
    const OUTPUT_ORDER = ['alerts', 'recommendations', 'decisions', 'scoring'];
    const outputRank = (e: any) => {
      const i = OUTPUT_ORDER.indexOf(String(e?.table ?? e?.slug ?? '').toLowerCase());
      return i === -1 ? OUTPUT_ORDER.length : i;
    };
    // v7.67.0 — Comunicaciones (the OUTBOX) is an operational/audit surface (what
    // the employee SENT), not a "what I generate" business output. It moves to
    // Admin → Auditoría y consumo, so pull it out of the employee "Lo que genero".
    const isComms = (e: any) =>
      String(e?.table ?? e?.slug ?? '').toLowerCase() === 'communications';
    const commsEntity = businessOnly.find(
      (e: any) => (e.source ?? 'project') === 'local' && isComms(e));
    const agentDataNav: NavItem[] = businessOnly
      .filter((e: any) => (e.source ?? 'project') === 'local' && !isComms(e))
      .slice()
      .sort((a: any, b: any) => outputRank(a) - outputRank(b))
      .map((e: any) => ({
        to: `/domain/${e.slug}`,
        labelKey: pickLabel(e, e.slug),
        icon: <EditNoteIcon />,
      }));

    const channelsEnabled = (manifest as any)?.channels_enabled ?? true;

    const employee: NavGroup[] = [
      {
        // The employee itself: its welcome/presentation (the KEY front door,
        // first) and the chat (how you talk to it). Leads the whole menu.
        key: 'employee',
        labelKey: 'nav.groups.employee',
        items: [
          { to: '/', labelKey: 'nav.welcome', icon: <WavingHandIcon /> },
          // v7.61.0 — the dashboard (business panel) lives right under Bienvenida:
          // it is the boss's at-a-glance view, part of "tu empleado", not audit.
          ...(canAdmin ? [{ to: '/dashboard', labelKey: 'nav.dashboard', icon: <DashboardIcon /> }] : []),
          { to: '/agent', labelKey: 'nav.agent', icon: <ChatIcon /> },
          // Employee manual: global guide (how a WAI employee works) + what is
          // specific to THIS employee (how to configure its connectors).
          { to: '/manual', labelKey: 'nav.manual', icon: <MenuBookIcon /> },
        ],
      },
      {
        // What the employee does for you.
        key: 'work',
        labelKey: 'nav.groups.work',
        items: [
          ...(canAdmin ? [
            { to: '/trabajo-autonomo', labelKey: 'nav.autonomousWork', icon: <AutoModeIcon /> },
            { to: '/calendar', labelKey: 'nav.calendar', icon: <CalendarMonthIcon /> },
          ] : []),
          // A member: only THEIR encargos (the backend filters them).
          { to: '/encargos', labelKey: 'nav.encargos', icon: <AssignmentTurnedInIcon /> },
          ...(canAdmin || meApprove
            ? [{ to: '/approvals', labelKey: 'nav.approvals', icon: <FactCheckIcon /> }] : []),
        ],
      },
      {
        // How it's going / what it has done. v7.38.0 — "Ejecuciones" + "Eventos"
        // moved here from the admin "Auditoría" group (they are the operator's
        // monitoring surface). Admin-only (the endpoints are require_admin), so a
        // non-admin member simply doesn't see them.
        key: 'tracking',
        labelKey: 'nav.groups.tracking',
        items: [
          { to: '/actividad', labelKey: 'nav.activity', icon: <TimelineIcon /> },
          ...(canAdmin ? [
            { to: '/runs', labelKey: 'nav.runs', icon: <HistoryIcon /> },
            { to: '/events', labelKey: 'nav.events', icon: <EventIcon /> },
          ] : []),
        ],
      },
      { key: 'generated', labelKey: 'nav.groups.generated', items: agentDataNav },
      { key: 'data', labelKey: 'nav.groups.data', items: projectNav },
    ].filter((g) => canAdmin || !['tracking', 'generated', 'data'].includes(g.key));

    const admin: NavGroup[] = [
      {
        key: 'agentAdmin',
        labelKey: 'nav.groups.agentAdmin',
        items: [
          { to: '/identity', labelKey: 'nav.identity', icon: <AssignmentIndIcon /> },
          { to: '/users', labelKey: 'nav.users', icon: <GroupIcon /> },
          { to: '/operations', labelKey: 'nav.operations', icon: <PsychologyIcon /> },
          { to: '/capacidades', labelKey: 'nav.capabilities', icon: <ShieldOutlinedIcon /> },
          { to: '/companeros', labelKey: 'nav.colleagues', icon: <GroupsIcon /> },
          ...(channelsEnabled
            ? [{ to: '/channels', labelKey: 'nav.channels', icon: <AlternateEmailIcon /> }]
            : []),
        ],
      },
      {
        key: 'audit',
        labelKey: 'nav.groups.audit',
        items: [
          // v7.67.0 — Comunicaciones (outbox) moved here from "Lo que genero".
          ...(commsEntity
            ? [{ to: `/domain/${commsEntity.slug}`, labelKey: pickLabel(commsEntity, commsEntity.slug), icon: <OutboxIcon /> }]
            : []),
          { to: '/consumo', labelKey: 'nav.consumo', icon: <PaidIcon /> },
        ],
      },
    ];

    return { employeeGroups: employee, adminGroups: admin };
  }, [manifest, i18n.language, pickLabel, canAdmin, meApprove]);

  const groups = area === 'admin' ? adminGroups : employeeGroups;
  const projectChip = manifest?.agent?.name ?? t('app.title');
  const drawerWidth = collapsed ? COLLAPSED_WIDTH : DRAWER_WIDTH;

  const setLang = (lang: 'es' | 'en') => {
    i18n.changeLanguage(lang);
    try {
      localStorage.setItem('wai.lang', lang);
    } catch {
      /* ignore */
    }
    setLangAnchor(null);
  };

  if (esMovil) {
    // No menu, no bar: the full-screen chat with its workspace panel, which is
    // the only thing truly usable on a phone — and the most used, because it is
    // what you do on your way somewhere.
    return <SoloChatMovil>{children}</SoloChatMovil>;
  }

  return (
    <Box sx={{ display: 'flex', minHeight: '100vh' }}>
      <AppBar
        position="fixed"
        color="default"
        elevation={0}
        sx={{
          width: `calc(100% - ${drawerWidth}px)`,
          ml: `${drawerWidth}px`,
          borderBottom: 1,
          borderColor: 'divider',
          transition: (th) =>
            th.transitions.create(['width', 'margin'], { duration: 200 }),
        }}
      >
        <Toolbar>
          {/* Long employee names must ellipsize instead of pushing/clipping the
              toolbar actions: minWidth:0 lets the flex child shrink, noWrap +
              textOverflow gives the ellipsis, and the Tooltip keeps the full
              name one hover away. */}
          <Tooltip title={projectChip}>
            <Typography
              variant="h6"
              noWrap
              sx={{ flexGrow: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis' }}
            >
              {projectChip}
            </Typography>
          </Tooltip>
          <Stack direction="row" spacing={1} alignItems="center">
            {/* Global display-timezone indicator + switch. Always shows
                which tz the screen's times are in; clicking lets the
                operator pick the company tz or their own browser tz. */}
            <Tooltip
              title={`${t('layout.tz.showing')}: ${activeTz ?? '—'}`}
            >
              <Chip
                icon={<ScheduleIcon />}
                size="small"
                variant="outlined"
                onClick={(e) => setTzAnchor(e.currentTarget)}
                label={activeAbbr || activeTz || t('layout.tz.title')}
                sx={{ fontWeight: 600 }}
              />
            </Tooltip>
            <Menu
              anchorEl={tzAnchor}
              open={Boolean(tzAnchor)}
              onClose={() => setTzAnchor(null)}
            >
              <ListSubheader sx={{ bgcolor: 'transparent', lineHeight: '2.2em' }}>
                {t('layout.tz.title')}
              </ListSubheader>
              <MenuItem
                selected={tzMode === 'business'}
                onClick={() => { setTzMode('business'); setTzAnchor(null); }}
              >
                <ListItemText
                  primary={t('layout.tz.business')}
                  secondary={businessTz ?? t('layout.tz.unknown')}
                />
              </MenuItem>
              <MenuItem
                selected={tzMode === 'local'}
                onClick={() => { setTzMode('local'); setTzAnchor(null); }}
              >
                <ListItemText
                  primary={t('layout.tz.local')}
                  secondary={localTz ?? t('layout.tz.unknown')}
                />
              </MenuItem>
            </Menu>

            {/* Employee warnings (v7.170.0). Here and not inside a screen:
                "the employee is not watching anything" must be seen wherever you
                are. Rendered BEFORE language and session because it is the only
                thing on this bar that may require an action. */}
            {canAdmin && <HealthBell />}

            <ColorModeToggle />

            <Tooltip title={t('layout.language') as string}>
              <IconButton
                onClick={(e) => setLangAnchor(e.currentTarget)}
                aria-label={t('layout.language') as string}
              >
                <LanguageIcon />
              </IconButton>
            </Tooltip>
            <Menu
              anchorEl={langAnchor}
              open={Boolean(langAnchor)}
              onClose={() => setLangAnchor(null)}
            >
              <MenuItem
                selected={i18n.language.startsWith('es')}
                onClick={() => setLang('es')}
              >
                Español
              </MenuItem>
              <MenuItem
                selected={i18n.language.startsWith('en')}
                onClick={() => setLang('en')}
              >
                English
              </MenuItem>
            </Menu>

            <Tooltip title={user?.email ?? ''}>
              <IconButton
                onClick={(e) => setUserAnchor(e.currentTarget)}
                aria-label={t('layout.userMenu') as string}
              >
                <Avatar sx={{ width: 32, height: 32 }}>
                  {(user?.email ?? '?').slice(0, 1).toUpperCase()}
                </Avatar>
              </IconButton>
            </Tooltip>
            <Menu
              anchorEl={userAnchor}
              open={Boolean(userAnchor)}
              onClose={() => setUserAnchor(null)}
            >
              <MenuItem disabled>
                <Stack>
                  <Typography variant="body2">{user?.email ?? ''}</Typography>
                </Stack>
              </MenuItem>
              <Divider />
              <MenuItem onClick={() => { setUserAnchor(null); logout(); }}>
                <ListItemIcon>
                  <LogoutIcon fontSize="small" />
                </ListItemIcon>
                <ListItemText>{t('layout.logout')}</ListItemText>
              </MenuItem>
            </Menu>
          </Stack>
        </Toolbar>
      </AppBar>

      <Drawer
        variant="permanent"
        sx={{
          width: drawerWidth,
          flexShrink: 0,
          whiteSpace: 'nowrap',
          '& .MuiDrawer-paper': {
            width: drawerWidth,
            boxSizing: 'border-box',
            overflowX: 'hidden',
            transition: (th) =>
              th.transitions.create('width', { duration: 200 }),
          },
        }}
      >
        <Toolbar sx={{ justifyContent: collapsed ? 'center' : 'space-between' }}>
          {!collapsed && (
            <Stack direction="row" alignItems="center" spacing={1}>
              <SmartToyIcon color="primary" />
              <Typography variant="subtitle1" noWrap>{t('app.title')}</Typography>
            </Stack>
          )}
          <Tooltip title={t(collapsed ? 'layout.expandMenu' : 'layout.collapseMenu') as string}>
            <IconButton onClick={toggleCollapsed} size="small" sx={{ color: 'inherit' }}>
              {collapsed ? <ChevronRightIcon /> : <ChevronLeftIcon />}
            </IconButton>
          </Tooltip>
        </Toolbar>
        <Divider />

        {/* Area switch: Empleado WAI vs Administración */}
        <Box sx={{ p: collapsed ? 0.5 : 1 }}>
          {collapsed ? (
            <Stack spacing={0.5} alignItems="center">
              {(['employee', 'admin'] as Area[]).filter((a) => a === 'employee' || canAdmin).map((a) => (
                <Tooltip
                  key={a}
                  placement="right"
                  title={t(a === 'employee' ? 'nav.areas.employee' : 'nav.areas.admin') as string}
                >
                  <IconButton
                    size="small"
                    onClick={() => switchArea(a)}
                    sx={{
                      color: area === a ? '#fff' : 'rgba(255,255,255,0.6)',
                      bgcolor: area === a ? 'rgba(255,255,255,0.16)' : 'transparent',
                      '&:hover': { bgcolor: 'rgba(255,255,255,0.1)' },
                    }}
                  >
                    {a === 'employee' ? <BadgeIcon fontSize="small" /> : <AdminPanelSettingsIcon fontSize="small" />}
                  </IconButton>
                </Tooltip>
              ))}
            </Stack>
          ) : (
            <ToggleButtonGroup
              value={area}
              exclusive
              fullWidth
              size="small"
              onChange={(_e, v) => switchArea(v as Area | null)}
              sx={{
                '& .MuiToggleButton-root': {
                  color: 'rgba(255,255,255,0.65)',
                  borderColor: 'rgba(255,255,255,0.16)',
                  textTransform: 'none',
                  fontWeight: 600,
                  gap: 0.5,
                  py: 0.5,
                  '&.Mui-selected': {
                    color: '#fff',
                    bgcolor: 'rgba(255,255,255,0.16)',
                    '&:hover': { bgcolor: 'rgba(255,255,255,0.22)' },
                  },
                },
              }}
            >
              <ToggleButton value="employee">
                <BadgeIcon fontSize="small" />
                {t('nav.areas.employee')}
              </ToggleButton>
              {canAdmin && (
                <ToggleButton value="admin">
                  <AdminPanelSettingsIcon fontSize="small" />
                  {t('nav.areas.admin')}
                </ToggleButton>
              )}
            </ToggleButtonGroup>
          )}
        </Box>
        <Divider />

        <List dense>
          {groups.map((g) => {
            if (g.items.length === 0) return null;
            // When the sidebar is a mini icon-rail (collapsed) there are no
            // subheaders, so per-group folding doesn't apply — show all icons.
            // A group that holds the CURRENT route is always shown so the
            // operator can see where they are even if that group folds by default.
            const hasActive = g.items.some((it) =>
              it.to === '/' ? loc.pathname === '/' : loc.pathname.startsWith(it.to));
            const folded = !collapsed && isFolded(g.key) && !hasActive;
            return (
              <Box key={g.key}>
                {!collapsed && (
                  <ListSubheader
                    disableSticky
                    onClick={() => toggleGroup(g.key)}
                    sx={{
                      // The sidebar is DARK: never override the text colour on
                      // hover (text.primary would turn it dark → invisible).
                      // Use the same translucent white overlay the rest of the
                      // rail uses for its hover affordance instead.
                      lineHeight: '32px', cursor: 'pointer', userSelect: 'none',
                      display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                      pr: 1, borderRadius: 1,
                      '&:hover': { bgcolor: 'rgba(255,255,255,0.08)' },
                    }}
                  >
                    {t(g.labelKey)}
                    <ExpandMoreIcon
                      fontSize="small"
                      sx={{ transform: folded ? 'rotate(-90deg)' : 'none', transition: '0.2s', opacity: 0.7 }}
                    />
                  </ListSubheader>
                )}
                <Collapse in={!folded} timeout="auto" unmountOnExit>
                  {g.items.map((it) => {
                    const selected =
                      it.to === '/'
                        ? loc.pathname === '/'
                        : loc.pathname.startsWith(it.to);
                    const label = t(it.labelKey, { defaultValue: it.labelKey });
                    return (
                      <ListItem key={it.to} disablePadding>
                        {/* A long name (a prospecting KPI history page) ran off the edge
                            of the sidebar: it is cut with an ellipsis and the full name shows on
                            hover, as when the sidebar is collapsed. */}
                        <Tooltip
                          title={collapsed || String(label).length > 24 ? (label as string) : ''}
                          placement="right"
                          disableHoverListener={!(collapsed || String(label).length > 24)}
                        >
                          <ListItemButton
                            component={RouterLink}
                            to={it.to}
                            selected={selected}
                            sx={{ justifyContent: collapsed ? 'center' : 'flex-start' }}
                          >
                            <ListItemIcon sx={{ minWidth: collapsed ? 0 : 36 }}>{it.icon}</ListItemIcon>
                            {!collapsed && <ListItemText primary={label} sx={{ minWidth: 0 }} primaryTypographyProps={{ noWrap: true }} />}
                          </ListItemButton>
                        </Tooltip>
                      </ListItem>
                    );
                  })}
                </Collapse>
              </Box>
            );
          })}
        </List>
        <Box sx={{ flexGrow: 1 }} />
        <Divider />
        <Box sx={{ p: 1.5 }}>
          {/* `version` can be null on older backends — rendering the
              chip then produced a literal "vnull". */}
          {!collapsed && manifest?.agent?.version != null && manifest.agent.version !== '' && (
            <Chip
              size="small"
              label={`v${manifest.agent.version}`}
              variant="outlined"
            />
          )}
        </Box>
      </Drawer>

      <Box
        component="main"
        // Remount the routed screen when the display timezone changes so
        // every date re-formats in the new tz (the date helpers are pure
        // functions read during render; a key bump is the cleanest way to
        // force a full re-read across all child components at once).
        key={`tz-${tzMode}`}
        sx={{
          flexGrow: 1,
          p: 3,
          mt: 8,
          minWidth: 0,
          // Pages like `Agent` need a defined height so their
          // internally-scrolling chat does not overflow the viewport. Without
          // this, `height: 100%` on the child resolves to 0. With
          // `display:flex` + `flexDirection: column` + `minHeight: 0`,
          // the child with `flex:1` or `height:100%` takes exactly the
          // available space and its inner area scrolls without touching the parent.
          display: 'flex',
          flexDirection: 'column',
          minHeight: 0,
          height: 'calc(100vh - 64px)',
        }}
      >
        {(onAdminRoute || vetada) && !canAdmin ? (
          // Fail-closed: never render admin-page content until the role is
          // confirmed admin. `null` (loading) shows the spinner; `false`
          // shows it until the redirect effect lands on the employee home.
          <Box sx={{ flexGrow: 1, display: 'flex', alignItems: 'center',
                     justifyContent: 'center' }}>
            <CircularProgress size={28} />
          </Box>
        ) : children}
      </Box>
    </Box>
  );
}

export default Layout;
