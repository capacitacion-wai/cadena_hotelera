import { useState } from "react";
import { useParams, Link as RouterLink } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Alert, Box, Button, Card, CardContent, Chip, Divider, Grid,
  IconButton, List, ListItem, ListItemText, Snackbar, Stack, Tooltip, Typography,
} from "@mui/material";
import {
  Replay as ReplayIcon, Cancel as CancelIcon,
  ContentCopy as CopyIcon, OpenInNew as OpenIcon,
  Schedule as ScheduleIcon, Storage as StorageIcon,
  TrendingUp as TrendingUpIcon, AccessTime as AccessTimeIcon,
  CheckCircle as CheckCircleIcon, ErrorOutline as ErrorOutlineIcon,
  Pending as PendingIcon, ChangeCircle as ChangeCircleIcon,
} from "@mui/icons-material";
import { useTranslation } from "react-i18next";

import PageHeader from "../components/PageHeader";
import SectionTitle from "../components/SectionTitle";
import StatusChip from "../components/StatusChip";
import { useConfirm } from "../components/ConfirmDialog";
import { apiFetch } from "../services/apiClient";
import { useManifest } from "../hooks/useManifest";
import { formatDateTime } from "../utils/datetime";

const STATUS_TONE: Record<string, { color: "success" | "info" | "warning" | "error" | "default"; icon: React.ReactNode }> = {
  PENDING:         { color: "warning", icon: <PendingIcon /> },
  CLAIMED:         { color: "info",    icon: <ChangeCircleIcon /> },
  IN_PROGRESS:     { color: "info",    icon: <ChangeCircleIcon /> },
  RETRY_SCHEDULED: { color: "warning", icon: <ScheduleIcon /> },
  COMPLETED:       { color: "success", icon: <CheckCircleIcon /> },
  FAILED:          { color: "error",   icon: <ErrorOutlineIcon /> },
  CANCELLED:       { color: "default", icon: <CancelIcon /> },
};

function FieldTile({ icon, label, value, mono = false }: {
  icon?: React.ReactNode; label: string; value: React.ReactNode; mono?: boolean;
}) {
  return (
    <Card variant="outlined" sx={{ borderRadius: 2, height: "100%" }}>
      <CardContent sx={{ py: 1.25, px: 2, "&:last-child": { pb: 1.25 } }}>
        <Stack direction="row" spacing={1} alignItems="center">
          {icon && <Box sx={{ color: "text.secondary", display: "flex" }}>{icon}</Box>}
          <Box sx={{ minWidth: 0, flex: 1 }}>
            <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
              {label}
            </Typography>
            <Typography
              variant="body2"
              sx={{
                fontWeight: 600,
                fontFamily: mono ? "monospace" : undefined,
                fontSize: mono ? "0.78rem" : "0.92rem",
                wordBreak: "break-word",
                overflowWrap: "anywhere",
              }}
            >
              {value ?? "—"}
            </Typography>
          </Box>
        </Stack>
      </CardContent>
    </Card>
  );
}

export default function EventDetailPage() {
  const { t, i18n } = useTranslation();
  const { id } = useParams<{ id: string }>();
  const qc = useQueryClient();
  const { manifest } = useManifest();
  const { confirm, dialog: confirmDialog } = useConfirm();
  // Business-timezone render (utils/datetime), not the raw browser locale.
  const fmt = (iso?: string) => formatDateTime(iso);

  const { data, isLoading, error } = useQuery({
    queryKey: ["event", id],
    queryFn:  () => apiFetch(`/api/events/${id}`).then((r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    }),
    refetchInterval: (q) => {
      const s = (q.state.data as any)?.status;
      return ["CLAIMED", "IN_PROGRESS", "RETRY_SCHEDULED"].includes(s)
        ? 5_000 : 30_000;
    },
    retry: false,
  });

  // apiFetch only THROWS on 401 — a 4xx/5xx returns a non-ok Response, so
  // without this guard a rejected reprocess/cancel ("not in terminal state",
  // "already replied") resolved as success and looked like it worked. Throw on
  // non-ok and surface the reason in a snackbar.
  const [snack, setSnack] = useState<string | null>(null);
  const reprocess = useMutation({
    mutationFn: () => apiFetch(`/api/events/${id}/reprocess`, { method: "POST" }).then(async (r) => {
      if (!r.ok) throw new Error((await r.text().catch(() => "")) || `HTTP ${r.status}`);
      return r;
    }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["event", id] }),
    onError:   (e: any) => setSnack(t("events.detail.reprocessFailed", "No se pudo reprocesar: {{m}}", { m: e?.message ?? "" })),
  });
  const cancel = useMutation({
    mutationFn: () => apiFetch(`/api/events/${id}/cancel`, { method: "POST" }).then(async (r) => {
      if (!r.ok) throw new Error((await r.text().catch(() => "")) || `HTTP ${r.status}`);
      return r;
    }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["event", id] }),
    onError:   (e: any) => setSnack(t("events.detail.cancelFailed", "No se pudo cancelar: {{m}}", { m: e?.message ?? "" })),
  });

  if (isLoading) {
    return <Box><Typography>{t("common.loading")}</Typography></Box>;
  }
  if (error || !data) {
    return (
      <Box>
        <Alert severity="error">
          {t("events.detail.loadError", { message: (error as any)?.message ?? "" })}
        </Alert>
      </Box>
    );
  }

  // Defensive defaults — the page must NEVER blank because a field is missing.
  const timeline    = Array.isArray(data.timeline)  ? data.timeline  : [];
  const runs        = Array.isArray(data.runs)      ? data.runs      : [];
  const auditLog    = Array.isArray(data.audit)     ? data.audit     : [];
  const keyFields   = data.key_fields && typeof data.key_fields === "object"
    ? data.key_fields : {};
  const isTerminal   = ["COMPLETED", "FAILED"].includes(data.status);
  const isReprocess  = isTerminal;
  // IN_PROGRESS / CLAIMED are cancellable too — the operator kill switch
  // for a stuck run (e.g. one looping past the gateway timeout). v6.36.6.
  const isCancelable = ["PENDING", "RETRY_SCHEDULED", "IN_PROGRESS", "CLAIMED"].includes(data.status);
  const isRunning    = ["IN_PROGRESS", "CLAIMED"].includes(data.status);

  // Resolve manifest event-type spec (`agent_app.event_type_specs[]`) so
  // the narrative can describe what the agent is supposed to DO with this
  // event, not just regurgitate type/status.
  const eventSpec = (manifest as any)?.actions?.find?.((a: any) => a.type === data.type)
                 ?? (manifest as any)?.event_type_specs?.find?.((s: any) => s.type === data.type)
                 ?? null;

  const narrative = buildNarrative(data, eventSpec, t, i18n.language);
  const tone = STATUS_TONE[data.status] ?? STATUS_TONE.PENDING;

  return (
    <Box>
      <PageHeader
        title={t("events.detail.title", { id: data.id })}
        subtitle={`${data.type} · ${data.source}`}
        breadcrumbs={[
          { label: t("nav.events"), to: "/events" },
          { label: `#${data.id}` },
        ]}
        actions={
          <Stack direction="row" spacing={1}>
            <Tooltip title={t("events.detail.copyFingerprint") as string}>
              <IconButton
                size="small"
                onClick={() => navigator.clipboard.writeText(data.fingerprint || "")}
              >
                <CopyIcon fontSize="small" />
              </IconButton>
            </Tooltip>
            <Button
              size="small" startIcon={<ReplayIcon />} disabled={!isReprocess}
              onClick={() => reprocess.mutate()}
            >
              {t("events.detail.reprocess")}
            </Button>
            <Tooltip
              title={(isRunning ? t("events.detail.cancelRunningHint") : "") as string}
            >
              <span>
                <Button
                  size="small" startIcon={<CancelIcon />} color="warning" disabled={!isCancelable}
                  onClick={() => {
                    if (isRunning) {
                      confirm(
                        { message: t("events.detail.cancelRunningConfirm"), danger: true, confirmLabel: t("events.detail.cancelRunning") },
                        () => cancel.mutate(),
                      );
                    } else {
                      cancel.mutate();
                    }
                  }}
                >
                  {isRunning ? t("events.detail.cancelRunning") : t("events.detail.cancel")}
                </Button>
              </span>
            </Tooltip>
          </Stack>
        }
      />
      {confirmDialog}

      {/* Functional narrative — the operator's headline answer */}
      <Card
        variant="outlined"
        sx={{
          mb: 3, borderRadius: 2, borderLeft: "4px solid",
          borderLeftColor: `${tone.color}.main`,
        }}
      >
        <CardContent>
          <Stack direction="row" spacing={2} alignItems="flex-start">
            <Box
              sx={{
                width: 44, height: 44, borderRadius: 2, flexShrink: 0,
                display: "flex", alignItems: "center", justifyContent: "center",
                backgroundColor: (theme) => (theme.palette as any)[tone.color]?.[50] ?? theme.palette.grey[100],
                color: `${tone.color}.main`,
              }}
            >
              {tone.icon}
            </Box>
            <Box sx={{ flex: 1, minWidth: 0 }}>
              <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }} flexWrap="wrap" useFlexGap>
                <StatusChip status={data.status} />
                <Chip size="small" variant="outlined"
                      label={`${data.attempts ?? 0}/${data.max_attempts ?? 0} ${t("events.detail.attempts")}`} />
                {data.processed_at && (
                  <Chip size="small" variant="outlined"
                        icon={<AccessTimeIcon fontSize="small" />}
                        label={fmt(data.processed_at)} />
                )}
                {data.next_attempt_at && data.status === "RETRY_SCHEDULED" && (
                  <Chip size="small" color="warning"
                        icon={<ScheduleIcon fontSize="small" />}
                        label={t("events.detail.nextAttempt", { time: fmt(data.next_attempt_at) })} />
                )}
              </Stack>
              <Typography variant="h6" sx={{ fontWeight: 700, mb: 0.5 }}>
                {narrative.headline}
              </Typography>
              {narrative.detail && (
                <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
                  {narrative.detail}
                </Typography>
              )}
              {narrative.bullets.length > 0 && (
                <List dense disablePadding>
                  {narrative.bullets.map((b, i) => (
                    <ListItem key={i} disableGutters sx={{ py: 0 }}>
                      <ListItemText
                        primary={
                          <Typography variant="body2">• {b}</Typography>
                        }
                      />
                    </ListItem>
                  ))}
                </List>
              )}
              {narrative.expectedAction && (
                <Alert severity="info" variant="outlined" sx={{ mt: 1.5 }}>
                  <Typography variant="caption" sx={{ fontWeight: 700, display: "block" }}>
                    {t("events.detail.expectedAction")}
                  </Typography>
                  <Typography variant="body2">{narrative.expectedAction}</Typography>
                </Alert>
              )}
            </Box>
          </Stack>
        </CardContent>
      </Card>

      {data.last_error && (
        <Alert severity="error" sx={{ mb: 3 }}>
          <Typography variant="subtitle2">{t("events.detail.lastError")}</Typography>
          <Typography
            variant="body2"
            sx={{
              fontFamily: "monospace", fontSize: "0.8rem",
              whiteSpace: "pre-wrap", wordBreak: "break-word",
              mt: 0.5,
            }}
          >
            {data.last_error}
          </Typography>
        </Alert>
      )}

      {/* Identity / Origin */}
      <SectionTitle>{t("events.detail.origin")}</SectionTitle>
      <Grid container spacing={1.5} sx={{ mb: 3 }}>
        <Grid item xs={12} md={4}>
          <FieldTile
            icon={<TrendingUpIcon fontSize="small" />}
            label={t("events.detail.type")}
            value={data.type}
          />
        </Grid>
        <Grid item xs={12} md={4}>
          <FieldTile
            icon={<StorageIcon fontSize="small" />}
            label={t("events.detail.source")}
            value={data.source}
          />
        </Grid>
        <Grid item xs={12} md={4}>
          <FieldTile
            icon={<AccessTimeIcon fontSize="small" />}
            label={t("events.detail.detectedAt")}
            value={fmt(data.detected_at)}
          />
        </Grid>
        <Grid item xs={12}>
          <FieldTile
            label={t("events.detail.fingerprint")}
            value={data.fingerprint}
            mono
          />
        </Grid>
      </Grid>

      {/* Key payload fields — render as a structured grid (not chips) */}
      {Object.keys(keyFields).length > 0 && (
        <>
          <SectionTitle>{t("events.detail.keyFields")}</SectionTitle>
          <Grid container spacing={1.5} sx={{ mb: 3 }}>
            {Object.entries(keyFields).map(([k, v]) => (
              <Grid item xs={12} sm={6} md={4} key={k}>
                <FieldTile label={humanise(k)} value={formatValue(v)} mono={k === "fingerprint"} />
              </Grid>
            ))}
          </Grid>
        </>
      )}

      {/* Timeline */}
      <SectionTitle>{t("events.detail.timeline")}</SectionTitle>
      <Card variant="outlined" sx={{ mb: 3, borderRadius: 2 }}>
        <CardContent>
          {timeline.length === 0 && (
            <Typography color="text.secondary" variant="body2">
              {t("events.detail.timelineEmpty")}
            </Typography>
          )}
          <List dense disablePadding>
            {timeline.map((tl: any, i: number) => (
              <ListItem
                key={i}
                disableGutters
                sx={{
                  py: 1, position: "relative", pl: 3,
                  "&::before": {
                    content: '""',
                    position: "absolute", left: 8, top: 16, bottom: 0,
                    width: 1.5,
                    backgroundColor: "divider",
                  },
                  "&:last-child::before": { display: "none" },
                  "&::after": {
                    content: '""',
                    position: "absolute", left: 4, top: 14,
                    width: 10, height: 10, borderRadius: "50%",
                    backgroundColor: tl.error ? "error.main" : "primary.main",
                    border: "2px solid",
                    borderColor: "background.paper",
                    zIndex: 1,
                  },
                  // A5: drop the timeline pseudo-elements at very narrow
                  // widths so the dot doesn't overlap the text on a
                  // 320-px screen. The list still reads bottom-up.
                  "@media (max-width: 360px)": {
                    pl: 1,
                    "&::before": { display: "none" },
                    "&::after": { display: "none" },
                  },
                }}
              >
                <ListItemText
                  primary={
                    <Typography variant="body2" sx={{ fontWeight: 600 }}>
                      {tl.label}
                    </Typography>
                  }
                  secondary={
                    <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
                      <Typography variant="caption" color="text.secondary">{fmt(tl.at)}</Typography>
                      {tl.by   && <Typography variant="caption" color="text.secondary">· {tl.by}</Typography>}
                      {tl.error && <Typography variant="caption" color="error">· {tl.error}</Typography>}
                    </Stack>
                  }
                />
              </ListItem>
            ))}
          </List>
        </CardContent>
      </Card>

      {/* Runs / tentativas */}
      <SectionTitle>{t("events.detail.runs")}</SectionTitle>
      <Card variant="outlined" sx={{ mb: 3, borderRadius: 2 }}>
        <CardContent>
          {runs.length === 0 && (
            <Typography color="text.secondary" variant="body2">
              {t("events.detail.runsEmpty")}
            </Typography>
          )}
          <List dense disablePadding>
            {runs.map((r: any, i: number) => (
              <ListItem
                key={r.id ?? i}
                disableGutters
                sx={{
                  py: 1, borderBottom: "1px solid",
                  borderColor: "divider",
                  "&:last-child": { borderBottom: 0 },
                }}
              >
                <Stack direction="row" spacing={1} alignItems="center"
                       sx={{ flex: 1, minWidth: 0 }} flexWrap="wrap" useFlexGap>
                  <Chip size="small" variant="outlined" label={`#${i + 1}`} />
                  <StatusChip status={r.status} />
                  <Typography variant="caption" color="text.secondary"
                              sx={{ fontFamily: "monospace", maxWidth: 220,
                                    overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {r.idempotency_key}
                  </Typography>
                  <Typography variant="caption" color="text.secondary">
                    {fmt(r.started_at)} · {r.duration_sec ?? "—"}s · {r.model || "—"}
                  </Typography>
                  {r.error && (
                    <Chip size="small" color="error" variant="outlined" label={String(r.error).slice(0, 60)} />
                  )}
                  <Box sx={{ flex: 1 }} />
                  <Button
                    component={RouterLink}
                    to={`/runs/${r.id}`}
                    endIcon={<OpenIcon />}
                    size="small"
                  >
                    #{r.id}
                  </Button>
                </Stack>
              </ListItem>
            ))}
          </List>
        </CardContent>
      </Card>

      {/* Payload (raw, collapsible) */}
      <SectionTitle>{t("events.detail.payload")}</SectionTitle>
      <Card variant="outlined" sx={{ mb: 3, borderRadius: 2 }}>
        <Box
          component="pre"
          sx={{
            m: 0, p: 2,
            fontFamily: "monospace", fontSize: "0.78rem",
            whiteSpace: "pre-wrap", wordBreak: "break-word",
            maxHeight: 320, overflow: "auto",
            backgroundColor: (theme) => theme.palette.grey[50],
          }}
        >
          {data.payload === undefined || data.payload === null
            ? "—"
            : JSON.stringify(data.payload, null, 2)}
        </Box>
      </Card>

      {/* Reprocess history */}
      {auditLog.length > 0 && (
        <>
          <SectionTitle>{t("events.detail.audit")}</SectionTitle>
          <Card variant="outlined" sx={{ borderRadius: 2 }}>
            <CardContent>
              <List dense disablePadding>
                {auditLog.map((a: any, i: number) => (
                  <ListItem key={i} disableGutters sx={{
                    py: 0.75, borderBottom: "1px solid", borderColor: "divider",
                    "&:last-child": { borderBottom: 0 },
                  }}>
                    <ListItemText
                      primary={
                        <Typography variant="body2">
                          {a.action} · <strong>{a.from_status}</strong> → <strong>{a.to_status}</strong>
                        </Typography>
                      }
                      secondary={`${fmt(a.at)} · ${a.actor ?? "—"}${a.note ? ` · ${a.note}` : ""}`}
                    />
                  </ListItem>
                ))}
              </List>
            </CardContent>
          </Card>
        </>
      )}
      <Snackbar
        open={!!snack}
        autoHideDuration={6000}
        onClose={() => setSnack(null)}
        anchorOrigin={{ vertical: "bottom", horizontal: "center" }}
      >
        <Alert severity="error" variant="filled" onClose={() => setSnack(null)} sx={{ width: "100%" }}>
          {snack}
        </Alert>
      </Snackbar>
    </Box>
  );
}

function humanise(key: string): string {
  if (!key) return "";
  return key
    .replace(/[_\-]+/g, " ")
    .replace(/\b([a-z])/g, (m) => m.toUpperCase());
}

function formatValue(v: unknown): string {
  if (v === null || v === undefined) return "—";
  // Business-timezone render (utils/datetime), not the raw browser locale.
  if (v instanceof Date) return formatDateTime(v);
  if (typeof v === "number") return v.toLocaleString();
  if (typeof v === "boolean") return v ? "✓" : "✕";
  if (typeof v === "object") {
    try { return JSON.stringify(v, null, 2); } catch { return String(v); }
  }
  // Auto-detect ISO timestamps and humanise them
  if (typeof v === "string" && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(v)) {
    return formatDateTime(v);
  }
  return String(v);
}

function buildNarrative(
  data: any,
  spec: any,
  t: (k: string, opts?: any) => string,
  _locale: string,
): { headline: string; detail: string; bullets: string[]; expectedAction?: string } {
  // Business-timezone render (utils/datetime), not the raw browser locale.
  const fmt = (iso?: string) => formatDateTime(iso);
  const status = data.status ?? "—";
  const attempts = data.attempts ?? 0;
  const maxAttempts = data.max_attempts ?? 0;
  const kf = data.key_fields ?? {};

  // Headline: prefer the manifest's human label for the event type
  // (event_type_specs[].label) — that's the operator-friendly name the
  // skill writes into wai-app.json. Fall back to the raw type code.
  const typeLabel = spec?.label ?? data.type ?? "—";
  const source = data.source ?? "—";
  const headline = t("events.detail.narrativeHeadline", {
    type: typeLabel, source, status,
  });

  // Detail paragraph — status-aware copy. Lets translators provide a
  // proper sentence per state instead of a mechanical pattern.
  const detail = t(`events.detail.narrativeBy.${status}`, {
    detected: fmt(data.detected_at),
    processed: fmt(data.processed_at),
    next: fmt(data.next_attempt_at),
    attempts, maxAttempts,
    defaultValue: t("events.detail.narrativeFallback", {
      detected: fmt(data.detected_at),
    }),
  });

  // Bullets — pick the most informative payload fields. The contract
  // with the watcher payload is loose by design (different watcher
  // types emit different shapes), so we look for the well-known keys.
  const bullets: string[] = [];
  if (kf.table) {
    bullets.push(t("events.detail.bulletTable", { table: kf.table }));
  }
  if (kf.previous != null && kf.current != null) {
    const prev = Number(kf.previous), cur = Number(kf.current);
    bullets.push(t("events.detail.bulletDelta", {
      previous: prev.toLocaleString(),
      current:  cur.toLocaleString(),
      delta:    (cur - prev).toLocaleString(),
    }));
  }
  if (kf.tick) {
    bullets.push(t("events.detail.bulletTick", { tick: fmt(kf.tick) }));
  }
  if (kf.column && kf.value !== undefined) {
    bullets.push(t("events.detail.bulletThreshold", {
      column: kf.column, value: kf.value,
    }));
  }
  if (data.next_attempt_at && status === "RETRY_SCHEDULED") {
    bullets.push(t("events.detail.bulletRetry", { next: fmt(data.next_attempt_at) }));
  }

  // Expected action — surfaced separately so the operator immediately
  // understands what the agent is *supposed* to do with this event.
  // Pulled from the manifest event_type_specs[type].summary.
  const expectedAction = spec?.summary ?? undefined;

  return { headline, detail, bullets, expectedAction };
}
