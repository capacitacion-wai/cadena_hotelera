import { useEffect, useMemo, useRef, useState } from "react";
import { Link as RouterLink, useNavigate } from "react-router-dom";
import {
  Alert, Autocomplete, Box, Button, Card, CardContent, Chip, Stack,
  TextField, Tooltip, IconButton, Switch, FormControlLabel,
} from "@mui/material";
import RefreshIcon from "@mui/icons-material/Refresh";
import { DataGrid, GridColDef, GridSortModel } from "@mui/x-data-grid";
import { useTranslation } from "react-i18next";

import PageHeader from "../components/PageHeader";
import StatusChip from "../components/StatusChip";
import EmptyState from "../components/EmptyState";
import { useAuth } from "../auth/AuthContext";
import { apiBase } from "../services/apiClient";
import { formatDateTime } from "../utils/datetime";
import { useEventTypeLabel, useSourceLabel } from "../utils/eventLabels";

const STATUSES = [
  "PENDING", "CLAIMED", "IN_PROGRESS",
  "RETRY_SCHEDULED", "FAILED", "COMPLETED",
];

export default function EventsPage() {
  const { t, i18n } = useTranslation();
  const { token, logout } = useAuth();
  const navigate = useNavigate();
  const eventTypeLabel = useEventTypeLabel();
  const sourceLabel = useSourceLabel();
  // Distinct type/source values for the filter SELECTORS (vs guessing in a text
  // box). Fetched once; new values are rare and still visible in the table.
  const [catalog, setCatalog] = useState<{ types: string[]; sources: string[] }>({ types: [], sources: [] });

  const [page, setPage]         = useState(0);
  const [pageSize, setPageSize] = useState(25);
  const [sortModel, setSortModel] = useState<GridSortModel>([
    { field: "detected_at", sort: "desc" },
  ]);
  const [statusSel, setStatusSel] = useState<string[]>([]);
  const [type, setType]   = useState("");
  const [source, setSource] = useState("");
  const [q, setQ]         = useState("");
  const [autorefresh, setAutorefresh] = useState(true);
  const [stats, setStats] = useState<Record<string, number>>({});
  // G-5: bulk-reprocess selection.
  const [selectedIds, setSelectedIds] = useState<number[]>([]);
  const [bulkBusy, setBulkBusy] = useState(false);

  const [rows, setRows]   = useState<any[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  // Drop responses that arrive after a newer load (filter / page
  // change, auto-refresh overlap) or after unmount.
  const loadSeq = useRef(0);

  // Business-timezone render (utils/datetime), not the raw browser locale.
  const fmt = (iso?: string) => formatDateTime(iso);

  const load = async () => {
    const seq = ++loadSeq.current;
    const params = new URLSearchParams({
      offset: String(page * pageSize),
      limit:  String(pageSize),
    });
    statusSel.forEach((s) => params.append("status", s));
    if (type)   params.set("type", type);
    if (source) params.set("source", source);
    if (q)      params.set("q", q);
    // Serialize the active sort so server-side sorting actually applies —
    // without this the sortable headers were a dead affordance (reload, no
    // reorder). The backend whitelists the column.
    if (sortModel[0]?.field) {
      params.set("sort", sortModel[0].field);
      params.set("order", sortModel[0].sort || "desc");
    }

    setLoading(true);
    try {
      const r = await fetch(`${apiBase}/events?${params.toString()}`, {
        headers: { Authorization: `Bearer ${token ?? ""}` },
      });
      if (seq !== loadSeq.current) return;
      if (r.status === 401) { logout(); return; }
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = await r.json();
      if (seq !== loadSeq.current) return;
      setRows(data.rows ?? []);
      setTotal(data.total ?? 0);
      setLoadError(null);
      const st = await fetch(`${apiBase}/events/stats`, {
        headers: { Authorization: `Bearer ${token ?? ""}` },
      });
      if (seq !== loadSeq.current) return;
      if (st.ok) setStats(await st.json());
    } catch (exc: unknown) {
      if (seq !== loadSeq.current) return;
      setLoadError(exc instanceof Error ? exc.message : 'unknown');
    } finally {
      if (seq === loadSeq.current) setLoading(false);
    }
  };

  useEffect(() => { load(); /* eslint-disable-next-line */ }, [page, pageSize, statusSel, type, source, q, sortModel]);
  useEffect(() => {
    if (!autorefresh) return;
    const id = setInterval(load, 10_000);
    return () => clearInterval(id);
  }, [autorefresh, page, pageSize, statusSel, type, source, q]);
  useEffect(() => () => { loadSeq.current += 1; }, []);
  useEffect(() => {
    fetch(`${apiBase}/events/catalog`, { headers: { Authorization: `Bearer ${token ?? ""}` } })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (d) setCatalog({ types: d.types ?? [], sources: d.sources ?? [] }); })
      .catch(() => { /* best-effort — selectors just stay empty */ });
  }, [token]);

  const cols: GridColDef[] = useMemo(() => [
    {
      field: "detected_at", headerName: t("events.cols.detectedAt"), flex: 1,
      valueFormatter: (value: any) => fmt(value),
    },
    {
      field: "type", headerName: t("events.cols.type"), flex: 1,
      valueFormatter: (value: any) => eventTypeLabel(value),
    },
    {
      field: "source", headerName: t("events.cols.source"), flex: 1,
      valueFormatter: (value: any) => sourceLabel(value),
    },
    {
      field: "status", headerName: t("events.cols.status"), width: 160,
      renderCell: (p) => <StatusChip status={p.value} />,
    },
    {
      field: "attempts", headerName: t("events.cols.attempts"), width: 130, sortable: false,
      valueGetter: (_v: any, row: any) => `${row.attempts}/${row.max_attempts}`,
    },
    {
      field: "next_attempt_at", headerName: t("events.cols.nextAttemptAt"), flex: 1,
      valueFormatter: (value: any) => fmt(value),
    },
    {
      field: "last_error", headerName: t("events.cols.lastError"), flex: 2, sortable: false,
      renderCell: (p: any) => (p.value ? <Chip size="small" color="error" label={String(p.value).slice(0, 80)} /> : null),
    },
  ], [t, i18n.language]);

  return (
    <Box>
      <PageHeader
        title={t("events.title")}
        subtitle={t("events.subtitle")}
        actions={
          <Stack direction="row" spacing={1} alignItems="center">
            <FormControlLabel
              control={<Switch checked={autorefresh} onChange={(e) => setAutorefresh(e.target.checked)} />}
              label={t("events.autorefresh")}
            />
            <Tooltip title={t("events.refresh") as string}>
              <IconButton onClick={load}><RefreshIcon /></IconButton>
            </Tooltip>
          </Stack>
        }
      />

      <Stack direction="row" spacing={1} flexWrap="wrap" sx={{ mb: 2 }}>
        {STATUSES.map((s) => (
          <Chip
            key={s}
            label={`${t(`events.statusLabel.${s}`, s)} ${stats[s] ?? 0}`}
            color={statusSel.includes(s) ? "primary" : "default"}
            onClick={() => {
              setPage(0);
              setStatusSel((cur) => cur.includes(s) ? cur.filter((x) => x !== s) : [...cur, s]);
            }}
          />
        ))}
      </Stack>

      <Stack direction="row" spacing={1} sx={{ mb: 2, flexWrap: "wrap" }}>
        <Autocomplete
          size="small" sx={{ minWidth: 200 }} options={catalog.types}
          value={type || null}
          onChange={(_e, v) => { setType(v || ""); setPage(0); }}
          getOptionLabel={(o) => eventTypeLabel(o)}
          renderInput={(p) => <TextField {...p} label={t("events.cols.type")} />}
        />
        <Autocomplete
          size="small" sx={{ minWidth: 200 }} options={catalog.sources}
          value={source || null}
          onChange={(_e, v) => { setSource(v || ""); setPage(0); }}
          getOptionLabel={(o) => sourceLabel(o)}
          renderInput={(p) => <TextField {...p} label={t("events.cols.source")} />}
        />
        <TextField size="small" label={t("events.search")}       value={q}      onChange={(e) => { setQ(e.target.value); setPage(0); }}      sx={{ minWidth: 240 }} />
      </Stack>

      {/* A8: persistent reminder when auto-refresh is off — silent
          toggling left operators staring at stale rows. */}
      {!autorefresh && (
        <Alert
          severity="warning"
          variant="outlined"
          sx={{ mb: 2, borderRadius: 2 }}
          action={
            <Chip
              size="small"
              color="warning"
              label={t('events.autorefreshResume')}
              onClick={() => setAutorefresh(true)}
              sx={{ cursor: 'pointer' }}
            />
          }
        >
          {t('events.autorefreshOff')}
        </Alert>
      )}
      {loadError && (
        <Alert severity="error" sx={{ mb: 2 }} onClose={() => setLoadError(null)}>
          {t('events.loadError')}: {loadError}
        </Alert>
      )}
      {!loading && rows.length === 0 && (
        <Box sx={{ mb: 2 }}>
          <EmptyState
            title={t('events.title')}
            body={t('events.emptyState')}
          />
        </Box>
      )}
      {/* G-5: bulk reprocess action when at least one event is
          selected. Operators with 50 stuck events used to click
          reprocess one at a time. */}
      {selectedIds.length > 0 && (
        <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
          <Chip
            label={`${selectedIds.length} ${t('events.selected')}`}
            color="primary"
            size="small"
          />
          <Button
            size="small"
            color="warning"
            variant="contained"
            disabled={bulkBusy}
            onClick={async () => {
              setBulkBusy(true);
              try {
                const results = await Promise.all(
                  selectedIds.map((id) =>
                    fetch(`${apiBase}/events/${id}/reprocess`, {
                      method: "POST",
                      headers: { Authorization: `Bearer ${token ?? ""}` },
                    }).then((r) => r.ok).catch(() => false),
                  ),
                );
                const failed = results.filter((ok) => !ok).length;
                if (failed > 0) {
                  setLoadError(
                    t('events.bulkReprocessFailed',
                      '{{count}} eventos no se pudieron reprocesar (¿estado no terminal o respuesta ya enviada?)',
                      { count: failed }) as string,
                  );
                }
                setSelectedIds([]);
                load();
              } finally {
                setBulkBusy(false);
              }
            }}
          >
            {t('events.bulkReprocess')}
          </Button>
          <Button size="small" onClick={() => setSelectedIds([])}>
            {t('events.bulkClear')}
          </Button>
        </Stack>
      )}
      {/* A3: DataGrid on tablet+, card list on phones. */}
      <Box sx={{ display: { xs: 'none', md: 'block' }, height: 600 }}>
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
          onRowClick={(r) => navigate(`/events/${r.id}`)}
          checkboxSelection
          rowSelectionModel={selectedIds}
          onRowSelectionModelChange={(m) => {
            // P4-7: DataGrid v7's selection model can be either an
            // array of ids or an `{ type, ids: Set }` object on
            // newer pre-releases. Normalise to number[] defensively.
            if (Array.isArray(m)) {
              setSelectedIds(m.map(Number).filter((n) => !Number.isNaN(n)));
            } else if (m && typeof m === 'object' && 'ids' in m) {
              const ids = (m as { ids: Iterable<unknown> }).ids;
              setSelectedIds(Array.from(ids, (v) => Number(v)).filter((n) => !Number.isNaN(n)));
            } else {
              setSelectedIds([]);
            }
          }}
          disableRowSelectionOnClick
        />
      </Box>
      <Stack spacing={1} sx={{ display: { xs: 'flex', md: 'none' } }}>
        {rows.map((r) => (
          <Card
            key={r.id}
            variant="outlined"
            sx={{ cursor: 'pointer' }}
            onClick={() => navigate(`/events/${r.id}`)}
          >
            <CardContent sx={{ p: 1.5, '&:last-child': { pb: 1.5 } }}>
              <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.5 }}>
                <StatusChip status={r.status} />
                <Box sx={{ flex: 1, fontSize: 13, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {eventTypeLabel(r.type)}
                </Box>
                <Chip size="small" variant="outlined" label={`${r.attempts}/${r.max_attempts}`} />
              </Stack>
              <Box sx={{ fontSize: 12, color: 'text.secondary' }}>
                {fmt(r.detected_at)} · {sourceLabel(r.source)}
              </Box>
              {r.last_error && (
                <Box sx={{ fontSize: 11, color: 'error.main', mt: 0.5, fontFamily: 'monospace', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {r.last_error}
                </Box>
              )}
            </CardContent>
          </Card>
        ))}
      </Stack>
    </Box>
  );
}
