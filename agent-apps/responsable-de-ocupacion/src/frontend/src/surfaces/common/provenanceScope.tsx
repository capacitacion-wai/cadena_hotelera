/**
 * Autonomous-task provenance scope — shared by every business surface.
 * Links from «Lo que ha generado» carry `?created_by_automation=<id>[&only_open=1]`;
 * DomainList already honoured them; inbox/advisory/ledger/outbox did not.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { Alert, Button, Chip, MenuItem, TextField, Tooltip } from '@mui/material';
import SmartToyIcon from '@mui/icons-material/SmartToy';
import { useTranslation } from 'react-i18next';
import { apiBase } from '../../services/apiClient';
import { useAuth } from '../../auth/AuthContext';

export interface ProvenanceContext {
  id: number;
  title: string;
  only_open?: boolean;
}

export function useAutomationProvenance(
  slug: string | undefined,
  entitySource?: string,
  opts?: { listPath?: string; producersUrl?: string },
) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { token } = useAuth();
  const [searchParams, setSearchParams] = useSearchParams();
  const provAutomation = searchParams.get('created_by_automation') ?? '';
  const provOnlyOpen = searchParams.get('only_open') ?? '';
  const [producers, setProducers] = useState<{ id: number; title: string; count: number }[]>([]);
  const listPath = opts?.listPath ?? (slug ? `/domain/${slug}` : undefined);

  const queryExtras = useMemo(() => ({
    created_by_automation: provAutomation || undefined,
    only_open: provOnlyOpen ? true : undefined,
  }), [provAutomation, provOnlyOpen]);

  const setProvenance = useCallback((id: string) => {
    const next = new URLSearchParams(searchParams);
    if (id) next.set('created_by_automation', id);
    else next.delete('created_by_automation');
    next.delete('only_open');
    setSearchParams(next, { replace: true });
  }, [searchParams, setSearchParams]);

  const clearProvenance = useCallback(() => {
    if (listPath) navigate(listPath);
    else setSearchParams({}, { replace: true });
  }, [navigate, listPath, setSearchParams]);

  useEffect(() => {
    const producersUrl = opts?.producersUrl
      ?? (slug && (entitySource ?? 'local') === 'local' ? `${apiBase}/domain/${slug}/producers` : null);
    if (!producersUrl) {
      setProducers([]);
      return;
    }
    let alive = true;
    fetch(producersUrl, { headers: { Authorization: `Bearer ${token ?? ''}` } })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (alive && d) setProducers(d.items ?? []); })
      .catch(() => undefined);
    return () => { alive = false; };
  }, [slug, token, entitySource, opts?.producersUrl]);

  return {
    provAutomation,
    provOnlyOpen,
    queryExtras,
    producers,
    setProvenance,
    clearProvenance,
    t,
  };
}

export function ProvenanceScopeAlert({
  ctx,
  onClear,
}: {
  ctx: ProvenanceContext | null | undefined;
  onClear: () => void;
}) {
  const { t } = useTranslation();
  if (!ctx?.title) return null;
  return (
    <Alert severity="info" sx={{ mb: 2 }}
      action={
        <Button size="small" onClick={onClear}>
          {t('domain.origin.clearFilter', 'Ver todo')}
        </Button>
      }>
      {ctx.only_open
        ? t('domain.origin.scopedOpen', 'Solo lo que dejó sin cerrar la automatización «{{title}}».', { title: ctx.title })
        : t('domain.origin.scoped', 'Solo lo que ha creado la automatización «{{title}}».', { title: ctx.title })}
    </Alert>
  );
}

export function AutomationOriginChip({ origin }: { origin?: { title?: string; via?: string } | null }) {
  const { t } = useTranslation();
  if (!origin?.title) return null;
  const tip = origin.via === 'event'
    ? t('domain.origin.viaEvent', 'Creado por la automatización «{{title}}» (reacción determinista)', { title: origin.title })
    : t('domain.origin.viaRun', 'Creado por la automatización «{{title}}»', { title: origin.title });
  return (
    <Tooltip title={tip}>
      <Chip size="small" variant="outlined" icon={<SmartToyIcon sx={{ fontSize: 14 }} />}
        label={origin.title} sx={{ maxWidth: 220 }} />
    </Tooltip>
  );
}

export function AutomationProvenanceFilter({
  value,
  producers,
  onChange,
  disabled,
}: {
  value: string;
  producers: { id: number; title: string; count: number }[];
  onChange: (id: string) => void;
  disabled?: boolean;
}) {
  const { t } = useTranslation();
  if (!producers.length) return null;
  return (
    <TextField
      size="small"
      select
      disabled={disabled}
      label={t('domain.origin.filterLabel', 'Trabajo autónomo')}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      sx={{ minWidth: 230 }}
    >
      <MenuItem value="">{t('domain.origin.filterAny', 'Cualquier origen')}</MenuItem>
      {producers.map((p) => (
        <MenuItem key={p.id} value={String(p.id)}>
          {p.title} ({p.count})
        </MenuItem>
      ))}
    </TextField>
  );
}
