import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { localeSeguro } from "../utils/datetime";
import { useParams, useSearchParams, Link } from "react-router-dom";
import { useState } from "react";
import {
  Accordion, AccordionDetails, AccordionSummary, Alert, Box, Button,
  Card, CardContent, Chip, CircularProgress, Dialog, DialogContent, DialogTitle,
  Grid, IconButton, Stack, Tab, Tabs, ToggleButton, ToggleButtonGroup, Tooltip, Typography,
} from "@mui/material";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import ContentCopyIcon from "@mui/icons-material/ContentCopy";
import StopCircleIcon from "@mui/icons-material/StopCircle";
import FullscreenIcon from "@mui/icons-material/Fullscreen";
import FullscreenExitIcon from "@mui/icons-material/FullscreenExit";
import CloseIcon from "@mui/icons-material/Close";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkBreaks from "remark-breaks";
import rehypeSanitize from "rehype-sanitize";
import { useTranslation } from "react-i18next";

import PageHeader from "../components/PageHeader";
import SectionTitle from "../components/SectionTitle";
import KpiCard from "../components/KpiCard";
import StatusChip from "../components/StatusChip";
import ArtifactList from "../components/ArtifactList";
import { useConfirm } from "../components/ConfirmDialog";
import { apiFetch } from "../services/apiClient";
import { formatDateTime } from "../utils/datetime";

type Tab = "request" | "response" | "structured" | "tools" | "metrics";

/**
 * Compact row that displays a single piece of request/response metadata
 * (provider, model, mode, started_at, idempotency_key, …). The previous
 * KpiCard-based layout rendered these in display-size font, which made
 * the hash overflow horizontally and dwarfed the actual content. This
 * component keeps the label and the value on the same baseline at a
 * sensible size, with truncation + copy-on-click for long opaque
 * values.
 */
function FieldTile({ label, value, mono = false }: {
  label: string; value: React.ReactNode; mono?: boolean;
}) {
  return (
    <Card variant="outlined" sx={{ borderRadius: 2, height: "100%" }}>
      <CardContent sx={{ py: 1.25, px: 2, "&:last-child": { pb: 1.25 } }}>
        <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 0.25 }}>
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
      </CardContent>
    </Card>
  );
}

function CopyableHash({ value }: { value: string }) {
  if (!value) return <span>—</span>;
  return (
    <Stack direction="row" spacing={0.5} alignItems="center">
      <Tooltip title={value} placement="bottom-start">
        <Box
          component="code"
          sx={{
            fontFamily: "monospace", fontSize: "0.78rem",
            maxWidth: 220, overflow: "hidden", textOverflow: "ellipsis",
            whiteSpace: "nowrap", display: "inline-block",
          }}
        >
          {value}
        </Box>
      </Tooltip>
      <Tooltip title="Copy">
        <Button
          size="small" sx={{ minWidth: 0, px: 0.5 }}
          onClick={() => navigator.clipboard.writeText(value)}
        >
          <ContentCopyIcon fontSize="inherit" />
        </Button>
      </Tooltip>
    </Stack>
  );
}

/** Strip the most common Markdown noise so a response can be previewed as
 *  plain prose in the outcome band (headings/links/code/list bullets → text). */
function plainPreview(md: string, max = 320): string {
  const text = (md || "")
    .replace(/```[\s\S]*?```/g, " ")          // fenced code blocks
    .replace(/`[^`]*`/g, " ")                 // inline code
    .replace(/!\[[^\]]*\]\([^)]*\)/g, " ")    // images
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")  // links → their text
    .replace(/^[ \t]*[#>*\-+]+[ \t]+/gm, "")  // heading / quote / list markers
    .replace(/[*_~`]+/g, "")                  // residual emphasis tokens
    .replace(/\s+/g, " ")
    .trim();
  return text.length > max ? `${text.slice(0, max).trimEnd()}…` : text;
}

/** A compact label-over-value fact for the outcome band (lighter than a
 *  FieldTile — no card chrome, meant to sit inline in a row). */
function InlineFact({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <Box>
      <Typography variant="caption" color="text.secondary" sx={{ display: "block", lineHeight: 1.3 }}>
        {label}
      </Typography>
      <Typography variant="body2" sx={{ fontWeight: 600 }}>
        {value ?? "—"}
      </Typography>
    </Box>
  );
}

/**
 * Outcome-first hero for the execution detail. An operator almost always
 * lands here from "Ver qué hizo →" wanting one answer — *what did the
 * employee do, did it work, and what did it cost* — so this band surfaces
 * the status, when it ran, the credit cost, and a plain-language preview of
 * the result before any tab is touched. The tabs below stay for forensics.
 */
function OutcomeBand({ data, onOpenResponse, onOpenTools }: {
  data: any; onOpenResponse: () => void; onOpenTools: () => void;
}) {
  const { t, i18n } = useTranslation();
  const status = String(data.status || "").toUpperCase();
  const finished = !["RUNNING", "PENDING", "CLAIMED", "RETRYING"].includes(status);
  const failed = status === "FAILED" || status === "ERROR";
  const summary: string | undefined = data.response?.structured?.summary;
  const md: string | undefined = data.response?.markdown;
  const outcome = summary || (md ? plainPreview(md) : "");
  const toolCount = data.tool_calls?.length ?? 0;
  const credits =
    data.credits == null
      ? null
      : `${Number(data.credits).toLocaleString(localeSeguro(i18n.language), {
          maximumFractionDigits: Number(data.credits) >= 1 ? 1 : 2,
        })} ${t("runs.summary.creditsUnit")}`;

  return (
    <Card variant="outlined" sx={{ borderRadius: 2, mb: 3 }}>
      <CardContent sx={{ py: 2 }}>
        <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap alignItems="center">
          <StatusChip status={data.status} />
          <Chip
            size="small"
            variant="outlined"
            label={data.mode === "chat" ? t("runs.modeChat") : t("runs.modeAutonomous")}
          />
          {data.model && <Chip size="small" variant="outlined" label={data.model} />}
        </Stack>

        <Stack direction="row" spacing={4} flexWrap="wrap" useFlexGap sx={{ mt: 1.75 }}>
          <InlineFact label={t("runs.overview.ran")} value={formatDateTime(data.started_at)} />
          {data.duration_sec != null && (
            <InlineFact label={t("runs.metrics.duration")} value={`${data.duration_sec}s`} />
          )}
          {credits && <InlineFact label={t("runs.metrics.cost")} value={credits} />}
        </Stack>

        <Box sx={{ mt: 2 }}>
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 0.5 }}>
            {t("runs.overview.outcome")}
          </Typography>
          {outcome ? (
            <Typography variant="body2" sx={{ whiteSpace: "pre-line" }}>{outcome}</Typography>
          ) : (
            <Typography variant="body2" color="text.secondary">
              {!finished ? t("runs.overview.running") : failed ? t("runs.overview.failed") : t("runs.response.empty")}
            </Typography>
          )}
          <Stack direction="row" spacing={2} sx={{ mt: 0.5 }} flexWrap="wrap" useFlexGap>
            {md && (
              <Button size="small" sx={{ px: 0, minWidth: 0 }} onClick={onOpenResponse}>
                {t("runs.overview.viewFull")}
              </Button>
            )}
            {toolCount > 0 && (
              <Button size="small" sx={{ px: 0, minWidth: 0 }} onClick={onOpenTools}>
                {t("runs.overview.viewTools", { count: toolCount })}
              </Button>
            )}
          </Stack>
        </Box>
      </CardContent>
    </Card>
  );
}

export default function RunDetailPage() {
  const { t } = useTranslation();
  const { id } = useParams<{ id: string }>();
  const [params, setParams] = useSearchParams();
  const qc = useQueryClient();
  const { confirm, dialog: confirmDialog } = useConfirm();
  // An operator arriving from "Ver qué hizo →" wants the OUTCOME first, not
  // the raw prompt. So the default tab is resolved from the data below (it
  // lands on the response when there is one); an explicit ?tab= deep-link
  // always wins so shared links are stable.
  const explicitTab = params.get("tab") as Tab | null;
  const setTab = (next: Tab) => setParams({ tab: next });

  const { data, isLoading, error } = useQuery({
    queryKey: ["run", id],
    queryFn: () => apiFetch(`/api/runs/${id}`).then((r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    }),
    retry: false,
    // While the run is in-flight, poll so the operator sees tool calls
    // land and the status flip to COMPLETED/FAILED live.
    refetchInterval: (q) =>
      String((q.state.data as any)?.status || "").toUpperCase() === "RUNNING"
        ? 5_000
        : false,
  });
  const cancelRun = useMutation({
    mutationFn: () => apiFetch(`/api/runs/${id}/cancel`, { method: "POST" }).then((r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["run", id] }),
  });

  if (isLoading) return <Box sx={{ p: 3 }}>{t("common.loading")}</Box>;
  if (error)     return <Box sx={{ p: 3 }}><Alert severity="error">{t("runs.error")}: {(error as any)?.message ?? ""}</Alert></Box>;
  if (!data)     return <Box sx={{ p: 3 }}><Alert severity="warning">{t("runs.error")}</Alert></Box>;

  return (
    // pb: the request tab's prompt is long; without bottom air the last lines
    // sit under the viewport edge and feel "cut off" when you scroll to the end.
    <Box sx={{ pb: 6 }}>
      <PageHeader
        title={t("runs.detailTitle", { id: data.id })}
        subtitle={data.event_type ?? t("runs.modeChat")}
        breadcrumbs={[
          { label: t("nav.runs"), to: "/runs" },
          { label: `#${data.id}` },
        ]}
        actions={
          <Stack direction="row" spacing={1}>
            {String(data.status || "").toUpperCase() === "RUNNING" && (
              <Button
                size="small"
                color="error"
                variant="outlined"
                startIcon={cancelRun.isPending ? <CircularProgress size={14} /> : <StopCircleIcon />}
                disabled={cancelRun.isPending}
                onClick={() => confirm(
                  { message: t("runs.stopConfirm"), danger: true, confirmLabel: t("runs.stop") },
                  () => cancelRun.mutate(),
                )}
              >
                {t("runs.stop")}
              </Button>
            )}
            {data.event_id && (
              <Button component={Link} to={`/events/${data.event_id}`} size="small">
                {t("runs.openEvent")}
              </Button>
            )}
          </Stack>
        }
      />
      {confirmDialog}

      {/* Outcome-first band: status + when + cost + a plain-language preview
          of what the employee actually did, so the operator gets the answer
          without opening a single tab. */}
      <OutcomeBand data={data} onOpenResponse={() => setTab("response")} onOpenTools={() => setTab("tools")} />

      {data.error && (
        <Alert severity="error" sx={{ mb: 2 }}>
          <Typography
            variant="body2"
            sx={{
              fontFamily: "monospace", fontSize: "0.8rem",
              whiteSpace: "pre-wrap", wordBreak: "break-word",
            }}
          >
            {data.error}
          </Typography>
        </Alert>
      )}

      {Array.isArray(data.artifacts) && data.artifacts.length > 0 && (
        <Card variant="outlined" sx={{ borderRadius: 2, p: 2, mb: 2 }}>
          <Typography variant="subtitle2" sx={{ mb: 1 }}>
            {t("artifacts.title")}
          </Typography>
          <ArtifactList artifacts={data.artifacts} />
        </Card>
      )}

      {/* While the run hasn't finished there is no response /
          structured / metrics content yet — showing those tabs only
          confuses the operator. They unlock when data exists. */}
      {(() => {
        const finished = !["RUNNING", "PENDING", "CLAIMED", "RETRYING"].includes(
          String(data.status || "").toUpperCase(),
        );
        const hasResponse = finished && Boolean(data.response?.markdown || data.response?.structured);
        const hasStructured = Boolean(data.response?.structured && typeof data.response.structured === "object");
        const hasTools = (data.tool_calls?.length ?? 0) > 0;
        // Default landing tab favours the business outcome (response), then
        // the steps it took, then the raw request — never the prompt first.
        const tab: Tab = explicitTab ?? (hasResponse ? "response" : hasTools ? "tools" : "request");
        const effectiveTab = (
          (tab === "response" && !hasResponse)
          || (tab === "structured" && !hasStructured)
          || (tab === "metrics" && !finished)
          || (tab === "tools" && !hasTools && !finished)
        ) ? "request" : tab;
        return (
          <>
            <Tabs
              value={effectiveTab}
              onChange={(_, v) => setTab(v)}
              sx={{
                mb: 2,
                borderBottom: "1px solid",
                borderColor: "divider",
                "& .MuiTab-root": { textTransform: "none", fontWeight: 600 },
              }}
              variant="scrollable"
              scrollButtons="auto"
            >
              {/* Business-first order: what it produced → how → cost →
                  the technical request last. */}
              <Tab value="response"   label={t("runs.tabs.response")}   disabled={!hasResponse} />
              <Tab value="structured" label={t("runs.tabs.structured")} disabled={!hasStructured} />
              <Tab value="tools"      label={t("runs.tabs.tools")}      disabled={!hasTools && !finished} />
              <Tab value="metrics"    label={t("runs.tabs.metrics")}    disabled={!finished} />
              <Tab value="request"    label={t("runs.tabs.request")} />
            </Tabs>

            {effectiveTab === "request"    && <RequestTab data={data} />}
            {effectiveTab === "response"   && <ResponseTab data={data} />}
            {effectiveTab === "structured" && <StructuredTab data={data} />}
            {effectiveTab === "tools"      && <ToolsTab data={data} />}
            {effectiveTab === "metrics"    && <MetricsTab data={data} />}
          </>
        );
      })()}
    </Box>
  );
}

/**
 * The prompt the employee actually received. It mixes real Markdown
 * (``##`` sections, lists, bold) with ALL-CAPS internal banners, so the
 * operator needs BOTH a raw view (to copy / search) and a formatted
 * one (to read headings and lists). The inline box is short by design;
 * "Maximizar" opens a dialog that can go full-screen — prompts from
 * autonomous runs routinely exceed a few thousand characters.
 */
function PromptViewer({ title, text, emptyLabel }: {
  title: string; text: string; emptyLabel: string;
}) {
  const { t } = useTranslation();
  const [view, setView] = useState<"raw" | "formatted">("formatted");
  const [open, setOpen] = useState(false);
  const [maximized, setMaximized] = useState(false);
  const body = (text || "").trim();
  const has = Boolean(body);

  const renderBody = (tall: boolean) => {
    if (!has) {
      return (
        <Typography variant="body2" color="text.secondary" sx={{ p: 2 }}>
          {emptyLabel}
        </Typography>
      );
    }
    if (view === "raw") {
      return (
        <Box
          component="pre"
          sx={{
            m: 0, p: 2,
            whiteSpace: "pre-wrap", wordBreak: "break-word",
            fontFamily: "monospace", fontSize: "0.85rem",
            maxHeight: tall ? "none" : 520, overflow: "auto",
          }}
        >
          {body}
        </Box>
      );
    }
    return (
      <Box
        className="markdown-body"
        sx={{
          p: 2.5, wordBreak: "break-word",
          maxHeight: tall ? "none" : 520, overflow: "auto",
          "& h1, & h2, & h3": { mt: 2, mb: 1 },
          "& p": { mb: 1.25 },
          "& ul, & ol": { pl: 3, mb: 1.25 },
        }}
      >
        <ReactMarkdown
          remarkPlugins={[remarkGfm, remarkBreaks]}
          rehypePlugins={[rehypeSanitize]}
        >
          {body}
        </ReactMarkdown>
      </Box>
    );
  };

  const toolbar = (
    <Stack direction="row" spacing={0.5} alignItems="center" flexWrap="wrap" useFlexGap>
      <ToggleButtonGroup
        size="small"
        exclusive
        value={view}
        onChange={(_, v) => v && setView(v)}
        sx={{ "& .MuiToggleButton-root": { textTransform: "none", px: 1.25 } }}
      >
        <ToggleButton value="formatted">
          {t("runs.request.viewFormatted", "Con formato")}
        </ToggleButton>
        <ToggleButton value="raw">
          {t("runs.request.viewRaw", "Texto")}
        </ToggleButton>
      </ToggleButtonGroup>
      <Tooltip title={t("runs.response.copy", "Copiar")}>
        <span>
          <IconButton
            size="small"
            disabled={!has}
            onClick={() => navigator.clipboard.writeText(body)}
            aria-label={t("runs.response.copy", "Copiar")}
          >
            <ContentCopyIcon fontSize="inherit" />
          </IconButton>
        </span>
      </Tooltip>
      <Tooltip title={t("common.maximize", "Maximizar")}>
        <span>
          <IconButton
            size="small"
            disabled={!has}
            onClick={() => { setMaximized(false); setOpen(true); }}
            aria-label={t("common.maximize", "Maximizar")}
          >
            <FullscreenIcon fontSize="inherit" />
          </IconButton>
        </span>
      </Tooltip>
    </Stack>
  );

  return (
    <>
      <Stack direction="row" justifyContent="space-between" alignItems="center" spacing={1} sx={{ mb: 1 }}>
        <SectionTitle>{title}</SectionTitle>
        {toolbar}
      </Stack>
      <Card variant="outlined" sx={{ borderRadius: 2, mb: 2 }}>
        {renderBody(false)}
      </Card>

      <Dialog
        open={open}
        onClose={() => setOpen(false)}
        fullScreen={maximized}
        maxWidth="lg"
        fullWidth
        PaperProps={{ sx: maximized ? undefined : { height: "90vh" } }}
      >
        <DialogTitle sx={{ display: "flex", alignItems: "center", gap: 1, pr: 1 }}>
          <Box sx={{ flex: 1, minWidth: 0 }}>{title}</Box>
          <ToggleButtonGroup
            size="small"
            exclusive
            value={view}
            onChange={(_, v) => v && setView(v)}
            sx={{ "& .MuiToggleButton-root": { textTransform: "none", px: 1.25 } }}
          >
            <ToggleButton value="formatted">
              {t("runs.request.viewFormatted", "Con formato")}
            </ToggleButton>
            <ToggleButton value="raw">
              {t("runs.request.viewRaw", "Texto")}
            </ToggleButton>
          </ToggleButtonGroup>
          <Tooltip title={t("runs.response.copy", "Copiar")}>
            <IconButton size="small" disabled={!has} onClick={() => navigator.clipboard.writeText(body)}>
              <ContentCopyIcon fontSize="small" />
            </IconButton>
          </Tooltip>
          <Tooltip title={maximized
            ? t("common.restoreSize", "Restaurar tamaño")
            : t("common.maximize", "Maximizar")}>
            <IconButton
              size="small"
              onClick={() => setMaximized((v) => !v)}
              aria-label={maximized
                ? t("common.restoreSize", "Restaurar tamaño")
                : t("common.maximize", "Maximizar")}
            >
              {maximized ? <FullscreenExitIcon /> : <FullscreenIcon />}
            </IconButton>
          </Tooltip>
          <IconButton size="small" onClick={() => setOpen(false)} aria-label={t("common.close", "Cerrar")}>
            <CloseIcon />
          </IconButton>
        </DialogTitle>
        <DialogContent dividers sx={{ p: 0, display: "flex", flexDirection: "column" }}>
          <Box sx={{ flex: 1, overflow: "auto", minHeight: 0 }}>
            {renderBody(true)}
          </Box>
        </DialogContent>
      </Dialog>
    </>
  );
}

function RequestTab({ data }: { data: any }) {
  const { t } = useTranslation();
  const meta = data.request?.meta ?? {};
  const prompt = String(data.request?.prompt ?? "");
  return (
    <Stack spacing={2} sx={{ pb: 2 }}>
      <SectionTitle>{t("runs.request.summary")}</SectionTitle>
      <Grid container spacing={1.5}>
        <Grid item xs={6} sm={4} md={3}>
          <FieldTile label={t("runs.request.provider")} value={meta.provider_id} />
        </Grid>
        <Grid item xs={6} sm={4} md={3}>
          <FieldTile label={t("runs.request.model")}    value={meta.model_id ?? data.model} />
        </Grid>
        <Grid item xs={6} sm={4} md={3}>
          <FieldTile label={t("runs.request.mode")}     value={meta.mode ?? data.mode} />
        </Grid>
        {/* v12.7.0 — who asked for the run and where it came in. Empty for runs before
            that version: the data was not stored and cannot be rebuilt, so a dash is
            shown instead of inventing an attribution. */}
        <Grid item xs={6} sm={4} md={3}>
          <FieldTile
            label={t("runs.request.requester", "Quién lo pidió")}
            value={data.solicitante || (data.origen ? t("runs.requester.agent", "El empleado") : "—")}
          />
        </Grid>
        <Grid item xs={6} sm={4} md={3}>
          <FieldTile
            label={t("runs.request.surface", "Por dónde")}
            value={
              !data.origen ? "—"
              : data.origen === "canal"
                ? (data.canal_kind
                    ? String(t(`runs.channelKind.${data.canal_kind}`,
                        data.canal_kind === "email" ? "Correo" : data.canal_kind))
                    : t("runs.surface.channel", "Canal"))
                : String(t(`runs.surface.${
                    data.origen === "automatizacion" ? "automation"
                    : data.origen === "vigia" ? "watcher"
                    : data.origen === "programado" ? "scheduled"
                    : data.origen === "companero" ? "colleague"
                    : "chat"}`, data.origen))
            }
          />
        </Grid>
        <Grid item xs={6} sm={4} md={3}>
          <FieldTile
            label={t("runs.request.startedAt")}
            value={formatDateTime(data.started_at)}
          />
        </Grid>
        <Grid item xs={12} md={6}>
          <FieldTile
            label={t("runs.request.idempotency")}
            value={<CopyableHash value={data.idempotency_key} />}
          />
        </Grid>
        {data.trace_id && (
          <Grid item xs={12} md={6}>
            {/* IS6: trace_id chip so an operator can grep one
                incident across event → run → tool-call → log. */}
            <FieldTile
              label={t("runs.traceIdCol", "Trace ID")}
              value={<CopyableHash value={data.trace_id} />}
            />
          </Grid>
        )}
        {data.attempts != null && (
          <Grid item xs={6} sm={4} md={3}>
            <FieldTile
              label={t("runs.attemptsCol", "Intentos")}
              value={String(data.attempts)}
            />
          </Grid>
        )}
        {meta.system_prompt_hash && (
          <Grid item xs={12} md={6}>
            <FieldTile
              label="system_prompt sha256"
              value={<CopyableHash value={meta.system_prompt_hash} />}
            />
          </Grid>
        )}
      </Grid>
      {meta.system_prompt_preview && (
        <>
          <SectionTitle>{t("runs.request.systemPrompt")}</SectionTitle>
          <Alert severity="info" icon={false} sx={{ "& .MuiAlert-message": { width: "100%" } }}>
            <Typography variant="body2" sx={{ whiteSpace: "pre-wrap" }}>
              {meta.system_prompt_preview}
            </Typography>
          </Alert>
        </>
      )}
      <PromptViewer
        title={t("runs.request.userPrompt")}
        text={prompt}
        emptyLabel={t("runs.request.systemPromptMissing")}
      />
    </Stack>
  );
}

function ResponseTab({ data }: { data: any }) {
  const { t } = useTranslation();
  const md = data.response?.markdown ?? "";
  return (
    <Stack spacing={2}>
      <Stack direction="row" justifyContent="space-between" alignItems="center">
        <SectionTitle>{t("runs.response.markdown")}</SectionTitle>
        <Button
          size="small"
          startIcon={<ContentCopyIcon />}
          onClick={() => navigator.clipboard.writeText(md)}
          disabled={!md}
        >
          {t("runs.response.copy")}
        </Button>
      </Stack>
      {md
        ? (
          <Card variant="outlined" sx={{ borderRadius: 2, p: 2 }}>
            <Box className="markdown-body" sx={{ wordBreak: "break-word" }}>
              {/* G-7 (P3): explicit rehype-sanitize. react-markdown
                  escapes raw HTML by default, but a future change
                  to add rehype-raw would silently open an XSS
                  surface — this plugin is the belt-and-braces. */}
              <ReactMarkdown rehypePlugins={[rehypeSanitize]}>{md}</ReactMarkdown>
            </Box>
          </Card>
        )
        : <Alert severity="warning">{t("runs.response.empty")}</Alert>}
      {data.finished_at && (
        <Typography variant="caption" color="text.secondary">
          {t("runs.response.finishedAt", { time: formatDateTime(data.finished_at) })}
          {data.duration_sec != null && ` · ${data.duration_sec}s`}
        </Typography>
      )}
    </Stack>
  );
}

function StructuredTab({ data }: { data: any }) {
  const { t } = useTranslation();
  const s = data.response?.structured;
  if (!s || typeof s !== "object") {
    return <Alert severity="warning">{t("runs.structured.missing")}</Alert>;
  }
  return (
    <Stack spacing={2}>
      {s.summary && (
        <Card variant="outlined" sx={{ borderRadius: 2, p: 2 }}>
          <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
            {t("runs.structured.summary")}
          </Typography>
          <Typography sx={{ mt: 0.5 }}>{s.summary}</Typography>
        </Card>
      )}
      {s.metrics && Object.keys(s.metrics).length > 0 && (
        <>
          <SectionTitle>{t("runs.structured.metrics")}</SectionTitle>
          <Grid container spacing={1.5}>
            {Object.entries(s.metrics).map(([k, v]) => (
              <Grid item xs={6} sm={4} md={3} key={k}>
                <FieldTile label={k} value={String(v)} mono />
              </Grid>
            ))}
          </Grid>
        </>
      )}
      {s.next_step && (
        <Alert severity="info">
          <Typography variant="subtitle2">{t("runs.structured.nextStep")}</Typography>
          <Typography variant="body2">{s.next_step}</Typography>
        </Alert>
      )}
      {s.verification && typeof s.verification === "object" && (
        <Alert severity={s.verification.feasible === false ? "warning" : "success"}>
          <Typography variant="subtitle2">
            {t("runs.structured.verification")}
            {": "}
            {s.verification.feasible === false
              ? t("runs.structured.notFeasible")
              : t("runs.structured.feasible")}
          </Typography>
          {Array.isArray(s.verification.blockers)
            && s.verification.blockers.length > 0 && (
            <Box component="ul" sx={{ m: 0, mt: 0.5, pl: 2.5 }}>
              {s.verification.blockers.map((b: any, i: number) => (
                <li key={i}>
                  <Typography variant="body2">{String(b)}</Typography>
                </li>
              ))}
            </Box>
          )}
        </Alert>
      )}
      <SectionTitle>{t("runs.structured.raw")}</SectionTitle>
      <Card variant="outlined" sx={{ borderRadius: 2 }}>
        <Box
          component="pre"
          sx={{
            m: 0, p: 2,
            fontFamily: "monospace", fontSize: "0.78rem",
            whiteSpace: "pre-wrap", wordBreak: "break-word",
            maxHeight: 360, overflow: "auto",
          }}
        >
          {JSON.stringify(s, null, 2)}
        </Box>
      </Card>
    </Stack>
  );
}

function ToolsTab({ data }: { data: any }) {
  const { t } = useTranslation();
  if (!data.tool_calls?.length) {
    return <Alert severity="info">{t("runs.tools.empty")}</Alert>;
  }
  return (
    <Stack spacing={1}>
      {/* The key is the call, not the step: a step makes SEVERAL calls (the employee
          queries the PMS and a colleague in the same turn) and with `step_idx` React
          collapsed the repeats — one per group was shown, which is exactly the bug
          this tab was meant to fix. */}
      {data.tool_calls.map((tc: any, i: number) => (
        <Accordion
          key={tc.call_id || `${tc.step_idx}-${tc.tool_name}-${i}`}
          variant="outlined"
          sx={{ borderRadius: 2, "&:before": { display: "none" } }}
        >
          <AccordionSummary expandIcon={<ExpandMoreIcon />}>
            <Stack direction="row" spacing={1} alignItems="center" sx={{ width: "100%" }}>
              <Chip size="small" label={`#${tc.step_idx}`} />
              {/* Which connector it comes from. Without this, a bare "query" does not say
                  whether it asked a colleague, the PMS or the app itself. */}
              {tc.server && <Chip size="small" variant="outlined" label={tc.server} />}
              <Typography sx={{ fontWeight: 600 }}>{tc.tool_name}</Typography>
              {/* A failed call is NOT «pending»: it was painted so until 15.3.1 and the
                  person read a failure as work still to do. `interrupted` = the run ended
                  before the call answered (the backend decides it when reading). */}
              {(() => {
                const estado = tc.status || (tc.result_hash ? "ok" : "running");
                if (estado === "ok") return <Chip size="small" color="success" label={t("runs.tools.ok")} />;
                if (estado === "error") return <Chip size="small" color="error" label={t("runs.tools.failed")} />;
                if (estado === "interrupted") return <Chip size="small" label={t("runs.tools.interrupted")} />;
                return <Chip size="small" color="warning" label={t("runs.tools.running")} />;
              })()}
              {tc.duration_ms != null && (
                <Typography variant="caption" color="text.secondary">
                  {tc.duration_ms >= 1000
                    ? `${(tc.duration_ms / 1000).toFixed(1)} s`
                    : `${tc.duration_ms} ms`}
                </Typography>
              )}
              <Box sx={{ flex: 1 }} />
              <Typography variant="caption" color="text.secondary">
                {formatDateTime(tc.executed_at)}
              </Typography>
            </Stack>
          </AccordionSummary>
          <AccordionDetails>
            {tc.arguments_hash && (
              <Typography variant="caption" sx={{ display: "block", mb: 1 }}>
                {t("runs.tools.argsHash")}: <code>{tc.arguments_hash}</code>
              </Typography>
            )}
            {/* What it was called with. The argument hash served idempotency and nothing
                else: nobody diagnoses "why did it answer me that" by looking at a hash. */}
            {tc.arguments && (
              <Box
                component="pre"
                sx={{
                  m: 0, mb: 1, p: 1.5, borderRadius: 1,
                  bgcolor: "grey.100",
                  fontFamily: "monospace", fontSize: "0.75rem",
                  whiteSpace: "pre-wrap", wordBreak: "break-word",
                  maxHeight: 200, overflow: "auto",
                }}
              >
                {tc.arguments}
              </Box>
            )}
            <Box
              component="pre"
              sx={{
                m: 0, p: 2, borderRadius: 1,
                bgcolor: "grey.50",
                fontFamily: "monospace", fontSize: "0.78rem",
                whiteSpace: "pre-wrap", wordBreak: "break-word",
                maxHeight: 360, overflow: "auto",
              }}
            >
              {typeof tc.result_json === "string"
                ? tc.result_json
                : JSON.stringify(tc.result_json, null, 2)}
            </Box>
          </AccordionDetails>
        </Accordion>
      ))}
    </Stack>
  );
}

function MetricsTab({ data }: { data: any }) {
  const { t, i18n } = useTranslation();
  // Per-execution cost shown in CREDITS (backend already converted the
  // run's USD via the tenant agent_cost_per_credit rate).
  const creditsLabel =
    data.credits == null
      ? "—"
      : `${Number(data.credits).toLocaleString(localeSeguro(i18n.language), {
          maximumFractionDigits: Number(data.credits) >= 1 ? 1 : 2,
        })} ${t("runs.summary.creditsUnit")}`;
  return (
    <Grid container spacing={1.5}>
      <Grid item xs={6} sm={4} md={3}>
        <KpiCard label={t("runs.metrics.tokensIn")}    value={data.tokens_in ?? "—"} />
      </Grid>
      <Grid item xs={6} sm={4} md={3}>
        <KpiCard label={t("runs.metrics.tokensOut")}   value={data.tokens_out ?? "—"} />
      </Grid>
      <Grid item xs={6} sm={4} md={3}>
        <KpiCard label={t("runs.metrics.duration")}    value={data.duration_sec != null ? `${data.duration_sec}s` : "—"} />
      </Grid>
      <Grid item xs={6} sm={4} md={3}>
        <KpiCard label={t("runs.metrics.cost")}        value={creditsLabel} />
      </Grid>
      <Grid item xs={12} sm={8} md={6}>
        <FieldTile label={t("runs.metrics.model")} value={data.model} />
      </Grid>
    </Grid>
  );
}
