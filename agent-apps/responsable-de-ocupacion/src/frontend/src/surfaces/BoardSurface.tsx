/**
 * `board` archetype — the generic fallback for ANY custom entity that has a
 * real lifecycle but doesn't match a specialised surface. It's a kanban grouped
 * by the status field, with inline transitions — a business upgrade over the
 * flat grid, so a new entity the agent invents never lands worse than this.
 */
import { useMemo, useState } from 'react';
import { useParams } from 'react-router-dom';
import {
  Alert, Box, Button, Card, CardContent, Chip, CircularProgress, Menu, MenuItem,
  Snackbar, Stack, Typography,
} from '@mui/material';
import ArrowDropDownIcon from '@mui/icons-material/ArrowDropDown';
import { useTranslation } from 'react-i18next';
import { useManifest } from '../hooks/useManifest';
import { usePickLabel } from '../utils/dataLabels';
import { transition } from './common/api';
import { SurfaceShell, useEntityRows, AgingChip, DateText } from './common/primitives';
import { useActionConfirm } from './common/guide';
import { uiConfig } from './resolveArchetype';

export default function BoardSurface() {
  const { slug } = useParams<{ slug: string }>();
  const { manifest } = useManifest();
  const pickLabel = usePickLabel();
  const { t } = useTranslation();
  const { confirm, dialog } = useActionConfirm();
  const entity = useMemo(
    () => ((manifest as any)?.domain_entities ?? []).find((e: any) => e.slug === slug),
    [manifest, slug],
  );
  const cfg = uiConfig(entity);
  const pk = entity?.primary_key ?? 'id';
  const lifecycle = entity?.lifecycle;
  const statusField = lifecycle?.field ?? cfg.status_field ?? 'status';
  const states: string[] = cfg.lanes ?? lifecycle?.states ?? [];
  const listCols = (entity?.list_columns ?? []);
  const headline = cfg.headline
    ?? listCols.find((c: any) => /(title|name|subject)/i.test(c.id))?.id
    ?? listCols.find((c: any) => c.format === 'text')?.id
    ?? pk;
  const subFields = listCols
    .map((c: any) => c.id)
    .filter((id: string) => id !== headline && id !== statusField && id !== pk && !/_json$|idempotency|created_by_run/i.test(id))
    .slice(0, 2);

  const [menu, setMenu] = useState<{ el: HTMLElement; row: any } | null>(null);
  const [snack, setSnack] = useState<{ msg: string; sev: 'success' | 'error' } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const { rows, loading, error, refresh } = useEntityRows(
    slug, { sort: 'created_at', direction: 'desc', pageSize: 200 }, [],
  );

  const columns = useMemo(() => {
    const by: Record<string, any[]> = {};
    for (const r of rows) (by[String(r[statusField] ?? '—')] ??= []).push(r);
    const known = states.length ? states : Object.keys(by);
    const extras = Object.keys(by).filter((s) => !known.includes(s));
    return [...known, ...extras].map((s) => ({ state: s, items: by[s] ?? [] }));
  }, [rows, states, statusField]);

  const move = async (row: any, next: string) => {
    setMenu(null);
    if (!slug) return;
    setBusy(String(row[pk]));
    try { await transition(slug, row[pk], next); setSnack({ msg: t('surfaces.common.done', 'Hecho'), sev: 'success' }); refresh(); }
    catch (e: any) { setSnack({ msg: e?.message ?? 'error', sev: 'error' }); }
    finally { setBusy(null); }
  };

  if (!manifest) return <Box>{t('common.loading', 'Cargando…')}</Box>;
  if (!entity) return <Alert severity="warning">{t('domain.unknownEntity', { slug })}</Alert>;

  return (
    <SurfaceShell title={pickLabel(entity, entity.slug)} guide="board" guideLifecycle={lifecycle}>
      {loading && <Box sx={{ py: 4, textAlign: 'center' }}><CircularProgress size={26} /></Box>}
      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}

      <Box sx={{ display: 'flex', gap: 2, overflowX: 'auto', pb: 1, alignItems: 'flex-start' }}>
        {columns.map(({ state, items }) => (
          <Box key={state} sx={{ minWidth: 280, maxWidth: 320, flex: '0 0 auto' }}>
            <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
              <Chip size="small" label={`${t(`states.${state}`, state)} · ${items.length}`} />
            </Stack>
            <Stack spacing={1}>
              {items.map((row) => {
                const current = String(row[statusField] ?? '');
                const nexts: string[] = lifecycle?.transitions?.[current]
                  ?? states.filter((s) => s !== current);
                return (
                  <Card key={String(row[pk])} variant="outlined">
                    <CardContent sx={{ py: 1.25, '&:last-child': { pb: 1.25 } }}>
                      <Typography variant="body2" sx={{ fontWeight: 600 }}>
                        {String(row[headline] ?? `#${row[pk]}`)}
                      </Typography>
                      {subFields.map((f: string) => row[f] != null && row[f] !== '' && (
                        <Typography key={f} variant="caption" color="text.secondary" display="block" noWrap>
                          {String(row[f])}
                        </Typography>
                      ))}
                      <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ mt: 0.75 }}>
                        <Stack direction="row" spacing={0.75} alignItems="center">
                          <DateText value={row.created_at} />
                          <AgingChip createdAt={row.created_at} open />
                        </Stack>
                        {nexts.length > 0 && (
                          <Button size="small" endIcon={<ArrowDropDownIcon />} disabled={busy === String(row[pk])}
                            onClick={(e) => setMenu({ el: e.currentTarget, row })}>
                            {t('surfaces.board.move', 'Mover')}
                          </Button>
                        )}
                      </Stack>
                    </CardContent>
                  </Card>
                );
              })}
            </Stack>
          </Box>
        ))}
      </Box>

      <Menu open={!!menu} anchorEl={menu?.el} onClose={() => setMenu(null)}>
        {menu && (lifecycle?.transitions?.[String(menu.row[statusField] ?? '')]
          ?? states.filter((s) => s !== String(menu.row[statusField] ?? ''))
        ).map((s: string) => (
          <MenuItem key={s} onClick={() => { const row = menu.row; setMenu(null); confirm(s, () => move(row, s)); }}>
            {t(`states.${s}`, s)}
          </MenuItem>
        ))}
      </Menu>

      {dialog}
      <Snackbar open={!!snack} autoHideDuration={3000} onClose={() => setSnack(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}>
        {snack ? <Alert severity={snack.sev} onClose={() => setSnack(null)}>{snack.msg}</Alert> : undefined}
      </Snackbar>
    </SurfaceShell>
  );
}
