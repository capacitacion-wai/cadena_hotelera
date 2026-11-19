/**
 * Ejecuciones page — dashboard + audit trail.
 *
 * The user-visible layout is:
 *
 *   1. Page header (title + refresh button).
 *   2. Filter bar: time range (presets + custom), mode toggle,
 *      status chips, model multi-select, event-type multi-select,
 *      full-text search. The filter bar drives BOTH the KPI panel
 *      and the table below — no more "summary shows last 7d while
 *      list shows last 50" mismatch.
 *   3. Dashboard panel: KPI cards (totals + cost + tokens +
 *      success rate + p50/p95 duration + currently-running
 *      count + avg cost per run + errors) and a 2×2 grid of
 *      charts (timeline, by model, by event-type, by status).
 *   4. List of runs (DataGrid on desktop, cards on mobile),
 *      respecting the same filters.
 *
 * The backend exposes everything we need through
 * ``/api/runs/summary`` (KPIs + breakdowns + catalogs for the
 * dropdowns) and ``/api/runs`` (paginated list). Both endpoints
 * accept the same filter family.
 */
import { localeSeguro } from '../utils/datetime'
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Alert, Autocomplete, Box, Button, Card, CardContent, Chip, CircularProgress,
  FormControlLabel, Grid, IconButton, Stack, Switch, TextField,
  ToggleButton, ToggleButtonGroup, Tooltip, Typography,
} from "@mui/material";
import RefreshIcon from "@mui/icons-material/Refresh";
import FilterAltOffIcon from "@mui/icons-material/FilterAltOff";
import StopCircleIcon from "@mui/icons-material/StopCircle";
import { DataGrid, GridColDef, GridSortModel } from "@mui/x-data-grid";
import {
  CartesianGrid, Bar, BarChart, Cell, Legend, Line, LineChart,
  Pie, PieChart, ResponsiveContainer, Tooltip as RTooltip,
  XAxis, YAxis,
} from "recharts";
import { useTranslation } from "react-i18next";

import PageHeader from "../components/PageHeader";
import SectionTitle from "../components/SectionTitle";
import StatusChip from "../components/StatusChip";
import KpiCard from "../components/KpiCard";
import EmptyState from "../components/EmptyState";
import { useConfirm } from "../components/ConfirmDialog";
import { useAuth } from "../auth/AuthContext";
import { apiBase } from "../services/apiClient";
import { CHART_COLORS, STATUS_COLORS } from "../theme/chart-colors";
import { formatDate, formatDateTime, formatTime } from "../utils/datetime";
import { useEventTypeLabel, truncateLabel } from "../utils/eventLabels";

const STATUSES = ["RUNNING", "COMPLETED", "FAILED"];

// Fixed status -> colour for the "Por estado" donut so the meaning is
// unambiguous regardless of which statuses are present in the window:
// FAILED is ALWAYS the error colour, COMPLETED ok, RUNNING the running one — from the
// app's THEME (chart-colors), not fixed hex from another palette.
const STATUS_FILL: Record<string, string> = {
  COMPLETED: STATUS_COLORS.COMPLETED,
  FAILED: STATUS_COLORS.FAILED,
  RUNNING: STATUS_COLORS.RUNNING,
};

type Preset = "24h" | "7d" | "30d" | "90d" | "custom";
const PRESETS: Preset[] = ["24h", "7d", "30d", "90d", "custom"];

// Map a preset key to a (from, to) window. ``custom`` returns
// nulls so callers can read from the explicit date pickers.
function presetWindow(p: Preset): { from?: string; to?: string } {
  if (p === "custom") return {};
  const map: Record<Exclude<Preset, "custom">, number> = {
    "24h": 1,
    "7d": 7,
    "30d": 30,
    "90d": 90,
  };
  const days = map[p];
  const now = new Date();
  const from = new Date(now.getTime() - days * 24 * 3600 * 1000);
  return { from: from.toISOString(), to: now.toISOString() };
}

// Business-timezone render (utils/datetime), not the raw browser locale,
// so operators in any country see the company's wall clock.
function fmtRowDateTime(iso?: string): string {
  return formatDateTime(iso);
}

export default function RunsPage() {
  const { t, i18n } = useTranslation();
  const { token, logout } = useAuth();
  const navigate = useNavigate();
  const { confirm, dialog: confirmDialog } = useConfirm();
  const eventTypeLabel = useEventTypeLabel();

  // ── Filter state ────────────────────────────────────────────────
  const [preset, setPreset] = useState<Preset>("7d");
  const [customFrom, setCustomFrom] = useState<string>("");
  const [customTo, setCustomTo] = useState<string>("");
  // ORIGIN of the run. The default is the employee's WORK: what it did on its own and
  // what a colleague asked of it. Neither "autonomous only" nor "everything":
  //   · starting at "autonomous" HID real work —a turn requested by a colleague is
  //     stored with `mode='colleague'`—, and the screen showed a single run for the
  //     day while the collaboration card counted seven requests;
  //   · starting at "" (all) drowns the audit in chat rows, which are
  //     conversation, not work.
  // That is why the filter accepts several modes at once (the backend splits on commas).
  const [mode, setMode] = useState<string>("autonomous,colleague");
  const [statusSel, setStatusSel] = useState<string[]>([]);
  const [modelSel, setModelSel] = useState<string[]>([]);
  const [eventTypeSel, setEventTypeSel] = useState<string[]>([]);
  const [q, setQ] = useState("");
  const [autoRefresh, setAutoRefresh] = useState(false);

  // ── List state ──────────────────────────────────────────────────
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(25);
  const [sortModel, setSortModel] = useState<GridSortModel>([
    { field: "started_at", sort: "desc" },
  ]);
  const [rows, setRows] = useState<any[]>([]);
  const [total, setTotal] = useState(0);
  const [summary, setSummary] = useState<any | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState<Record<number, boolean>>({});

  // Cost is shown in CREDITS, not money. The backend already converts
  // USD → credits (tenant agent_cost_per_credit). ``unit`` appends the
  // localised "créditos/credits" suffix for headline figures.
  const credits = (n?: number | null, unit = false) => {
    if (n == null) return "—";
    const v = Number(n);
    const txt = v.toLocaleString(localeSeguro(i18n.language), {
      maximumFractionDigits: v >= 100 ? 0 : v >= 1 ? 1 : 2,
    });
    return unit ? `${txt} ${t("runs.summary.creditsUnit")}` : txt;
  };
  const tokensFmt = (n?: number | null) => {
    if (n == null) return "—";
    if (n < 1000) return String(n);
    if (n < 1_000_000) return `${(n / 1000).toFixed(1)}k`;
    return `${(n / 1_000_000).toFixed(2)}M`;
  };

  // Build the shared filter querystring used by both endpoints.
  const buildParams = (extra?: Record<string, string>) => {
    const params = new URLSearchParams();
    const win = preset === "custom"
      ? { from: customFrom || undefined, to: customTo || undefined }
      : presetWindow(preset);
    if (win.from) params.set("from", win.from);
    if (win.to)   params.set("to", win.to);
    if (mode)     params.set("mode", mode);
    statusSel.forEach((s)    => params.append("status", s));
    modelSel.forEach((m)     => params.append("model", m));
    eventTypeSel.forEach((e) => params.append("event_type", e));
    if (q) params.set("q", q);
    // Serialize the active sort so server-side sorting actually applies (the
    // headers were a dead affordance before). Backend whitelists the column.
    if (sortModel[0]?.field) {
      params.set("sort", sortModel[0].field);
      params.set("order", sortModel[0].sort || "desc");
    }
    Object.entries(extra ?? {}).forEach(([k, v]) => params.set(k, v));
    return params;
  };

  // Drop responses arriving after a newer load (filter change or
  // auto-refresh overlap) or after unmount.
  const loadSeq = useRef(0);

  const load = async () => {
    const seq = ++loadSeq.current;
    setLoading(true);
    setError(null);
    try {
      const listParams = buildParams({
        offset: String(page * pageSize),
        limit:  String(pageSize),
      });
      const sumParams = buildParams();
      const [listResp, sumResp] = await Promise.all([
        fetch(`${apiBase}/runs?${listParams.toString()}`, {
          headers: { Authorization: `Bearer ${token ?? ""}` },
        }),
        fetch(`${apiBase}/runs/summary?${sumParams.toString()}`, {
          headers: { Authorization: `Bearer ${token ?? ""}` },
        }),
      ]);
      if (seq !== loadSeq.current) return;
      if (listResp.status === 401) { logout(); return; }
      if (!listResp.ok) throw new Error(`HTTP ${listResp.status}`);
      const data = await listResp.json();
      if (seq !== loadSeq.current) return;
      setRows(data.rows ?? []);
      setTotal(data.total ?? 0);
      if (sumResp.ok) setSummary(await sumResp.json());
    } catch (exc: any) {
      if (seq !== loadSeq.current) return;
      setError(exc?.message ?? "error");
    } finally {
      if (seq === loadSeq.current) setLoading(false);
    }
  };

  useEffect(() => { load(); /* eslint-disable-next-line */ },
    [page, pageSize, sortModel, preset, customFrom, customTo, mode, statusSel, modelSel, eventTypeSel, q]);
  useEffect(() => () => { loadSeq.current += 1; }, []);

  // Optional 30s auto-refresh — handy when watching a busy demo
  // tenant where new runs land every few seconds.
  useEffect(() => {
    if (!autoRefresh) return;
    const timer = window.setInterval(() => { load(); }, 30_000);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoRefresh, page, pageSize, preset, customFrom, customTo, mode, statusSel, modelSel, eventTypeSel, q]);

  // Operator kill switch — stop an in-flight run (chat or autonomous).
  const cancelRun = (id: number) => confirm(
    { message: t("runs.stopConfirm"), danger: true, confirmLabel: t("runs.stop") },
    () => { void doCancelRun(id); },
  );
  const doCancelRun = async (id: number) => {
    setCancelling((c) => ({ ...c, [id]: true }));
    try {
      const r = await fetch(`${apiBase}/runs/${id}/cancel`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token ?? ""}` },
      });
      if (r.status === 401) { logout(); return; }
    } catch {
      /* best-effort — the next poll/refresh reflects the real state */
    } finally {
      setCancelling((c) => { const n = { ...c }; delete n[id]; return n; });
      load();
    }
  };

  // Live polling while any run is in-flight, so the in-progress
  // indicator and the moment a run finishes (or is stopped) show up
  // without the operator hitting refresh.
  const hasRunning = rows.some((r) => r.status === "RUNNING");
  useEffect(() => {
    if (!hasRunning) return;
    const timer = window.setInterval(() => { load(); }, 7_000);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hasRunning, page, pageSize, preset, customFrom, customTo, mode, statusSel, modelSel, eventTypeSel, q]);

  const cols: GridColDef[] = useMemo(() => [
    {
      field: "started_at", headerName: t("runs.cols.startedAt"), flex: 1,
      valueFormatter: (value: any) => fmtRowDateTime(value),
    },
    {
      // Not server-sortable (it lives on the joined events row), so don't
      // offer a header click that the backend can't honour.
      field: "event_type", headerName: t("runs.cols.eventType"), flex: 1, sortable: false,
      valueGetter: (_v: any, r: any) => r.event_type ?? "—",
      valueFormatter: (value: any) => eventTypeLabel(value),
    },
    {
      field: "status", headerName: t("runs.cols.status"), width: 140,
      renderCell: (p) => <StatusChip status={p.value} />,
    },
    {
      field: "attempts", headerName: t("runs.attemptsCol", "Intentos"), width: 100,
      valueGetter: (_v: any, r: any) => r.attempts ?? 1,
    },
    // ORIGIN: at a glance, without having to filter. Mixing autonomous work, a
    // conversation and a colleague's request in one list —without saying which is
    // which— turns the audit into guesswork.
    {
      field: "mode",
      headerName: t("runs.cols.origin", "Origen"),
      width: 130,
      renderCell: (p) => {
        const etiquetas: Record<string, string> = {
          autonomous: t("runs.mode.autonomous"),
          chat: t("runs.mode.chat"),
          colleague: t("runs.mode.colleague", "De un compañero"),
          manual_reset: t("runs.mode.manualReset", "Reinicio manual"),
        };
        const colores: Record<string, "default" | "primary" | "secondary" | "info"> = {
          autonomous: "primary", chat: "info", colleague: "secondary",
        };
        return (
          <Chip size="small" variant="outlined"
            color={colores[String(p.value)] ?? "default"}
            label={etiquetas[String(p.value)] ?? String(p.value ?? "")} />
        );
      },
    },
    // WHO asked for it. The column next to it says what kind of run it is; this one says
    // the person. Empty when the employee acted on its own —that is the answer, not a
    // gap— and also empty before 12.7.0, because that data did not exist and cannot be
    // rebuilt: the session it would have to be derived from rotates and is collected.
    {
      field: "solicitante",
      headerName: t("runs.cols.requester", "Quién"),
      width: 190,
      renderCell: (p) => {
        if (p.row?.origen == null && !p.value) return <>—</>;
        if (!p.value) {
          return (
            <Typography variant="body2" color="text.secondary" fontStyle="italic">
              {t("runs.requester.agent", "El empleado")}
            </Typography>
          );
        }
        return <Typography variant="body2" noWrap title={String(p.value)}>{String(p.value)}</Typography>;
      },
    },
    // WHERE it came in. With the channel type in front, because email and WhatsApp are
    // not handled the same way and an auditor first wants to know which door it came through.
    {
      field: "origen",
      headerName: t("runs.cols.surface", "Por dónde"),
      width: 160,
      renderCell: (p) => {
        const v = String(p.value ?? "");
        if (!v) return <>—</>;
        const canal = String(p.row?.canal_kind ?? "");
        const etiquetas: Record<string, string> = {
          chat: t("runs.surface.chat", "Chat"),
          canal: canal
            ? t(`runs.channelKind.${canal}`, canal === "email" ? "Correo" : canal)
            : t("runs.surface.channel", "Canal"),
          automatizacion: t("runs.surface.automation", "Automatización"),
          vigia: t("runs.surface.watcher", "Vigilancia"),
          programado: t("runs.surface.scheduled", "Agenda"),
          companero: t("runs.surface.colleague", "Compañero"),
        };
        return <Typography variant="body2" noWrap>{etiquetas[v] ?? v}</Typography>;
      },
    },
    { field: "model", headerName: t("runs.cols.model"), flex: 1 },
    {
      field: "duration_sec", headerName: t("runs.cols.duration"), width: 120,
      valueFormatter: (value: any) => value == null ? "—" : `${Number(value).toFixed(1)}s`,
    },
    { field: "tokens_in", headerName: t("runs.cols.tokensIn"), width: 110 },
    { field: "tokens_out", headerName: t("runs.cols.tokensOut"), width: 110 },
    {
      // ``credits`` is derived from the string cost_usd column — not safely
      // server-sortable — so it's display-only.
      field: "credits", headerName: t("runs.cols.cost"), width: 110, sortable: false,
      valueFormatter: (value: any) => credits(value == null ? null : Number(value)),
    },
    {
      field: "actions", headerName: t("runs.cols.actions"), width: 120,
      sortable: false, filterable: false,
      renderCell: (p) => p.row.status !== "RUNNING" ? null : (
        <Tooltip title={t("runs.stop")}>
          <span>
            <IconButton
              size="small"
              color="error"
              disabled={!!cancelling[p.row.id]}
              onClick={(e) => { e.stopPropagation(); cancelRun(p.row.id); }}
            >
              {cancelling[p.row.id]
                ? <CircularProgress size={16} />
                : <StopCircleIcon fontSize="small" />}
            </IconButton>
          </span>
        </Tooltip>
      ),
    },
  ], [t, i18n.language, cancelling]);

  const catalogs = summary?.catalogs ?? { models: [], event_types: [] };
  const activeFilterCount =
    (mode !== "autonomous,colleague" ? 1 : 0) +
    (preset !== "7d" ? 1 : 0) +
    statusSel.length +
    modelSel.length +
    eventTypeSel.length +
    (q ? 1 : 0);

  const clearFilters = () => {
    setPreset("7d");
    setCustomFrom("");
    setCustomTo("");
    setMode("autonomous");
    setStatusSel([]);
    setModelSel([]);
    setEventTypeSel([]);
    setQ("");
    setPage(0);
  };

  return (
    <Box>
      <PageHeader
        title={t("runs.title")}
        subtitle={t("runs.subtitle")}
        actions={
          <Stack direction="row" spacing={1} alignItems="center">
            <FormControlLabel
              control={
                <Switch
                  size="small"
                  checked={autoRefresh}
                  onChange={(_e, c) => setAutoRefresh(c)}
                />
              }
              label={t("runs.autoRefresh")}
              sx={{ mr: 0.5 }}
            />
            <Tooltip title={t("runs.refresh") as string}>
              <IconButton onClick={load}><RefreshIcon /></IconButton>
            </Tooltip>
          </Stack>
        }
      />

      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}

      {/* ── Filter bar ──────────────────────────────────────────── */}
      <Card variant="outlined" sx={{ mb: 2 }}>
        <CardContent sx={{ pb: 1.5 }}>
          <Stack spacing={1.5}>
            <Stack
              direction={{ xs: "column", md: "row" }}
              spacing={1.5}
              alignItems={{ md: "center" }}
              flexWrap="wrap"
            >
              <ToggleButtonGroup
                size="small"
                value={preset}
                exclusive
                onChange={(_e, v) => { if (v) { setPreset(v); setPage(0); } }}
              >
                {PRESETS.map((p) => (
                  <ToggleButton key={p} value={p} sx={{ textTransform: "none" }}>
                    {t(`runs.preset.${p}`)}
                  </ToggleButton>
                ))}
              </ToggleButtonGroup>
              {preset === "custom" && (
                <Stack direction="row" spacing={1} alignItems="center">
                  <TextField
                    size="small"
                    type="datetime-local"
                    label={t("runs.from")}
                    InputLabelProps={{ shrink: true }}
                    value={customFrom ? customFrom.slice(0, 16) : ""}
                    onChange={(e) => {
                      const v = e.target.value;
                      setCustomFrom(v ? new Date(v).toISOString() : "");
                      setPage(0);
                    }}
                    sx={{ minWidth: 200 }}
                  />
                  <TextField
                    size="small"
                    type="datetime-local"
                    label={t("runs.to")}
                    InputLabelProps={{ shrink: true }}
                    value={customTo ? customTo.slice(0, 16) : ""}
                    onChange={(e) => {
                      const v = e.target.value;
                      setCustomTo(v ? new Date(v).toISOString() : "");
                      setPage(0);
                    }}
                    sx={{ minWidth: 200 }}
                  />
                </Stack>
              )}
              <ToggleButtonGroup
                size="small"
                value={mode}
                exclusive
                onChange={(_e, v) => { if (v !== null) { setMode(v); setPage(0); } }}
              >
                <ToggleButton value="autonomous,colleague" sx={{ textTransform: "none" }}>
                  {t("runs.mode.work", "Trabajo")}
                </ToggleButton>
                <ToggleButton value="autonomous" sx={{ textTransform: "none" }}>
                  {t("runs.mode.autonomous")}
                </ToggleButton>
                <ToggleButton value="chat" sx={{ textTransform: "none" }}>
                  {t("runs.mode.chat")}
                </ToggleButton>
                <ToggleButton value="colleague" sx={{ textTransform: "none" }}>
                  {t("runs.mode.colleague", "De un compañero")}
                </ToggleButton>
                <ToggleButton value="" sx={{ textTransform: "none" }}>
                  {t("runs.mode.all")}
                </ToggleButton>
              </ToggleButtonGroup>
              <Box sx={{ flex: 1 }} />
              {activeFilterCount > 0 && (
                <Button
                  size="small"
                  variant="text"
                  startIcon={<FilterAltOffIcon />}
                  onClick={clearFilters}
                >
                  {t("runs.clearFilters", { count: activeFilterCount })}
                </Button>
              )}
            </Stack>
            <Stack
              direction={{ xs: "column", md: "row" }}
              spacing={1.5}
              alignItems={{ md: "center" }}
              flexWrap="wrap"
            >
              <Stack direction="row" spacing={1}>
                {STATUSES.map((s) => (
                  <Chip
                    key={s}
                    label={t(`runs.statusLabel.${s}`, s)}
                    size="small"
                    color={statusSel.includes(s) ? "primary" : "default"}
                    onClick={() => {
                      setPage(0);
                      setStatusSel((cur) => cur.includes(s)
                        ? cur.filter((x) => x !== s)
                        : [...cur, s]);
                    }}
                  />
                ))}
              </Stack>
              <Autocomplete
                size="small"
                multiple
                limitTags={2}
                options={catalogs.models ?? []}
                value={modelSel}
                onChange={(_e, v) => { setModelSel(v); setPage(0); }}
                renderInput={(p) => (
                  <TextField {...p} label={t("runs.filterModel")} />
                )}
                sx={{ minWidth: 220 }}
              />
              <Autocomplete
                size="small"
                multiple
                limitTags={2}
                options={catalogs.event_types ?? []}
                value={eventTypeSel}
                onChange={(_e, v) => { setEventTypeSel(v); setPage(0); }}
                getOptionLabel={(o) => eventTypeLabel(o)}
                renderInput={(p) => (
                  <TextField {...p} label={t("runs.filterEventType")} />
                )}
                sx={{ minWidth: 240 }}
              />
              <TextField
                size="small"
                label={t("runs.search")}
                value={q}
                onChange={(e) => { setQ(e.target.value); setPage(0); }}
                sx={{ minWidth: 240, flex: 1 }}
              />
            </Stack>
          </Stack>
        </CardContent>
      </Card>

      {/* ── Dashboard ──────────────────────────────────────────── */}
      {summary && (
        <>
          <SectionTitle>{t("runs.summary.title")}</SectionTitle>
          <Grid container spacing={2} sx={{ mb: 2 }}>
            <Grid item xs={6} sm={4} md={2}>
              <KpiCard label={t("runs.summary.total")} value={summary.total} />
            </Grid>
            <Grid item xs={6} sm={4} md={2}>
              <KpiCard
                label={t("runs.summary.cost")}
                value={credits(summary.credits, true)}
                subtitle={t("runs.summary.avgCostPerRun", {
                  v: summary.avg_credits_per_run == null
                    ? "—"
                    : credits(summary.avg_credits_per_run),
                }) as string}
              />
            </Grid>
            <Grid item xs={6} sm={4} md={2}>
              <KpiCard
                label={t("runs.summary.tokens")}
                value={tokensFmt(summary.tokens_total)}
                subtitle={`${tokensFmt(summary.tokens_in)} ↑ · ${tokensFmt(summary.tokens_out)} ↓`}
              />
            </Grid>
            <Grid item xs={6} sm={4} md={2}>
              <KpiCard
                label={t("runs.summary.successRate")}
                value={summary.success_rate == null
                  ? "—"
                  : `${(summary.success_rate * 100).toFixed(1)}%`}
                subtitle={t("runs.summary.failedHint", { v: summary.failed }) as string}
              />
            </Grid>
            <Grid item xs={6} sm={4} md={2}>
              <KpiCard
                label={t("runs.summary.avgDuration")}
                value={summary.duration_avg == null
                  ? "—"
                  : `${Number(summary.duration_avg).toFixed(1)}s`}
                subtitle={summary.duration_p50 == null
                  ? undefined
                  : `p50 ${Number(summary.duration_p50).toFixed(1)}s · p95 ${
                      summary.duration_p95 == null
                        ? "—"
                        : `${Number(summary.duration_p95).toFixed(1)}s`
                    }`}
              />
            </Grid>
            <Grid item xs={6} sm={4} md={2}>
              <KpiCard
                label={t("runs.summary.running")}
                value={summary.running}
                subtitle={t("runs.summary.failedHint", { v: summary.failed }) as string}
              />
            </Grid>

            {/* Timeline — switches granularity at ≤ 48h. */}
            <Grid item xs={12} md={8}>
              <Card variant="outlined" sx={{ height: "100%" }}>
                <CardContent>
                  <SectionTitle>
                    {summary.by_hour
                      ? t("runs.summary.byHour")
                      : t("runs.summary.byDay")}
                  </SectionTitle>
                  <Box sx={{ width: "100%", height: 260 }}>
                    <ResponsiveContainer>
                      <LineChart
                        data={summary.by_hour ?? summary.by_day ?? []}
                      >
                        <CartesianGrid strokeDasharray="3 3" />
                        <XAxis
                          dataKey={summary.by_hour ? "hour" : "day"}
                          tickFormatter={(v) => {
                            if (!v) return "";
                            const d = new Date(v);
                            if (Number.isNaN(d.getTime())) return v;
                            // Business-timezone axis labels (utils/datetime).
                            return summary.by_hour
                              ? formatTime(v)
                              : formatDate(v, { year: undefined });
                          }}
                        />
                        <YAxis yAxisId="runs" orientation="left" />
                        <YAxis yAxisId="cost" orientation="right" />
                        <RTooltip
                          formatter={(value: any, name: string) => {
                            if (name === "credits") return [credits(value, true), t("runs.summary.cost")];
                            if (name === "runs")     return [value, t("runs.summary.runs")];
                            return [value, name];
                          }}
                        />
                        <Legend />
                        <Line
                          yAxisId="runs"
                          type="monotone"
                          dataKey="runs"
                          name={t("runs.summary.runs") as string}
                          stroke={CHART_COLORS[0]}
                          strokeWidth={2}
                          dot={false}
                        />
                        <Line
                          yAxisId="cost"
                          type="monotone"
                          dataKey="credits"
                          name={t("runs.summary.cost") as string}
                          stroke={CHART_COLORS[2]}
                          strokeWidth={2}
                          dot={false}
                        />
                      </LineChart>
                    </ResponsiveContainer>
                  </Box>
                </CardContent>
              </Card>
            </Grid>

            {/* Status donut */}
            <Grid item xs={12} md={4}>
              <Card variant="outlined" sx={{ height: "100%" }}>
                <CardContent>
                  <SectionTitle>{t("runs.summary.byStatus")}</SectionTitle>
                  <Box sx={{ width: "100%", height: 260 }}>
                    <ResponsiveContainer>
                      <PieChart>
                        {(() => {
                          // Color the slices by STATUS, not by array index.
                          // The Pie data and the <Cell> list MUST come from
                          // the SAME filtered array: recharts paints cells by
                          // position, so mapping cells over the unfiltered
                          // list made FAILED inherit COMPLETED's green
                          // whenever RUNNING had 0 runs. Keying the colour on
                          // s.status guarantees FAILED is always red.
                          const statusData = (summary.by_status ?? [])
                            .filter((s: any) => s.runs > 0);
                          return (
                            <Pie
                              data={statusData}
                              dataKey="runs"
                              nameKey="status"
                              innerRadius={55}
                              outerRadius={85}
                              paddingAngle={2}
                            >
                              {statusData.map((s: any, i: number) => (
                                <Cell
                                  key={s.status}
                                  fill={
                                    STATUS_FILL[s.status]
                                    ?? CHART_COLORS[i % CHART_COLORS.length]
                                  }
                                />
                              ))}
                            </Pie>
                          );
                        })()}
                        <RTooltip />
                        <Legend />
                      </PieChart>
                    </ResponsiveContainer>
                  </Box>
                </CardContent>
              </Card>
            </Grid>

            {/* By model bar */}
            <Grid item xs={12} md={6}>
              <Card variant="outlined" sx={{ height: "100%" }}>
                <CardContent>
                  <SectionTitle>{t("runs.summary.byModel")}</SectionTitle>
                  <Box sx={{ width: "100%", height: 240 }}>
                    <ResponsiveContainer>
                      <BarChart
                        data={(summary.by_model ?? []).slice(0, 6)}
                        layout="vertical"
                      >
                        <CartesianGrid strokeDasharray="3 3" />
                        <XAxis type="number" />
                        <YAxis dataKey="model" type="category" width={140} />
                        <RTooltip
                          formatter={(value: any, name: string) => {
                            if (name === "credits") return [credits(value, true), t("runs.summary.cost")];
                            return [value, t("runs.summary.runs")];
                          }}
                        />
                        <Bar
                          dataKey="runs"
                          name={t("runs.summary.runs") as string}
                          fill={CHART_COLORS[0]}
                          onClick={(d: any) => {
                            if (!d || !d.model) return;
                            setModelSel((cur) =>
                              cur.includes(d.model) ? cur : [...cur, d.model]);
                            setPage(0);
                          }}
                          style={{ cursor: "pointer" }}
                        />
                      </BarChart>
                    </ResponsiveContainer>
                  </Box>
                </CardContent>
              </Card>
            </Grid>

            {/* By event type bar */}
            <Grid item xs={12} md={6}>
              <Card variant="outlined" sx={{ height: "100%" }}>
                <CardContent>
                  <SectionTitle>{t("runs.summary.byEventType")}</SectionTitle>
                  <Box sx={{ width: "100%", height: 240 }}>
                    <ResponsiveContainer>
                      <BarChart
                        data={(summary.by_event_type ?? []).slice(0, 6)}
                        layout="vertical"
                        margin={{ left: 8, right: 16 }}
                      >
                        <CartesianGrid strokeDasharray="3 3" />
                        <XAxis type="number" allowDecimals={false} />
                        <YAxis
                          dataKey="event_type"
                          type="category"
                          width={170}
                          interval={0}
                          tick={{ fontSize: 11 }}
                          tickFormatter={(v: any) => truncateLabel(eventTypeLabel(String(v)), 22)}
                        />
                        <RTooltip
                          labelFormatter={(label: any) => eventTypeLabel(String(label))}
                          formatter={(value: any, name: string) => {
                            if (name === "credits") return [credits(value, true), t("runs.summary.cost")];
                            if (name === "failed")   return [value, t("runs.summary.failed")];
                            return [value, t("runs.summary.runs")];
                          }}
                        />
                        <Legend />
                        <Bar
                          dataKey="runs"
                          name={t("runs.summary.runs") as string}
                          fill={CHART_COLORS[1]}
                          onClick={(d: any) => {
                            if (!d || !d.event_type) return;
                            if (d.event_type.startsWith("(")) return;
                            setEventTypeSel((cur) =>
                              cur.includes(d.event_type)
                                ? cur
                                : [...cur, d.event_type]);
                            setPage(0);
                          }}
                          style={{ cursor: "pointer" }}
                        />
                        <Bar
                          dataKey="failed"
                          name={t("runs.summary.failed") as string}
                          fill={STATUS_COLORS.FAILED}
                          stackId="errors"
                        />
                      </BarChart>
                    </ResponsiveContainer>
                  </Box>
                </CardContent>
              </Card>
            </Grid>
          </Grid>
        </>
      )}

      {/* ── List ──────────────────────────────────────────────── */}
      <SectionTitle>
        {t("runs.list.title")}
        {summary && total > 0 && (
          <Chip
            size="small"
            label={total}
            sx={{ ml: 1, height: 20, fontSize: "0.7rem" }}
          />
        )}
      </SectionTitle>
      {!loading && rows.length === 0 && (
        <Box sx={{ mb: 2 }}>
          <EmptyState
            title={t("runs.title")}
            body={t("runs.emptyState")}
          />
        </Box>
      )}
      <Box sx={{ display: { xs: "none", md: "block" }, height: 600 }}>
        <DataGrid
          columns={cols}
          rows={rows.map((r) => ({ id: r.id, ...r }))}
          rowCount={total}
          loading={loading}
          paginationMode="server"
          paginationModel={{ page, pageSize }}
          onPaginationModelChange={(m) => { setPage(m.page); setPageSize(m.pageSize); }}
          sortingMode="server"
          sortModel={sortModel}
          onSortModelChange={setSortModel}
          pageSizeOptions={[10, 25, 50, 100]}
          onRowClick={(r) => navigate(`/runs/${r.id}`)}
          disableRowSelectionOnClick
        />
      </Box>
      <Stack spacing={1} sx={{ display: { xs: "flex", md: "none" } }}>
        {rows.map((r) => (
          <Card
            key={r.id}
            variant="outlined"
            sx={{ cursor: "pointer" }}
            onClick={() => navigate(`/runs/${r.id}`)}
          >
            <CardContent sx={{ p: 1.5, "&:last-child": { pb: 1.5 } }}>
              <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.5 }}>
                <StatusChip status={r.status} />
                <Box sx={{ flex: 1, fontSize: 13, fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {r.event_type || "—"}
                </Box>
                {r.status === "RUNNING" && (
                  <Tooltip title={t("runs.stop")}>
                    <span>
                      <IconButton
                        size="small"
                        color="error"
                        disabled={!!cancelling[r.id]}
                        onClick={(e) => { e.stopPropagation(); cancelRun(r.id); }}
                      >
                        {cancelling[r.id]
                          ? <CircularProgress size={16} />
                          : <StopCircleIcon fontSize="small" />}
                      </IconButton>
                    </span>
                  </Tooltip>
                )}
              </Stack>
              <Box sx={{ fontSize: 12, color: "text.secondary" }}>
                {fmtRowDateTime(r.started_at)} · {credits(r.credits, true)} · {r.model || "—"}
              </Box>
            </CardContent>
          </Card>
        ))}
      </Stack>

      {confirmDialog}
    </Box>
  );
}
