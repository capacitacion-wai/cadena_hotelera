// The notices bell in the top bar (v7.170.0).
//
// Before, these notices only existed inside the Operaciones screen: if you did not
// go in there, you never found out that the employee was watching nothing. Now they
// are seen from anywhere, next to the language and the session, as in any
// product.
//
// The two questions that decided the design, and their answers:
//
// **«Don't they stay there forever?»** No, and nothing needs to be marked as read:
// they are STATES, not events. The server recomputes them on every query from
// reality, so the moment the condition stops being true the notice disappears by
// itself. There is no history to purge nor a counter that gets stuck. The only thing
// stored is the explicit dismissal —«I know, it is on purpose»— and even that
// expires after 30 days.
//
// **«A member must not see administration messages.»** The filter is on the
// SERVER, not here: an admin notice does not even travel to a member (see
// `_visibles_para` in activity_routes.py). This screen decides nothing about
// permissions — if it did, opening the browser tools would be enough to read what
// is not yours to read.
//
// An ERROR cannot be dismissed: it means «you need to do something», and hiding it
// strips away its only purpose. The server marks it (`dismissible`) and here the
// button is simply not offered.
import React from 'react'
import {
  Badge, Box, Button, Divider, IconButton, List, ListItem, ListItemText,
  Menu, Tooltip, Typography,
} from '@mui/material'
import NotificationsNoneIcon from '@mui/icons-material/NotificationsNone'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { apiFetch } from '../services/apiClient'
import { healthNoticeText } from './healthNotices'

export default function HealthBell() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [anchor, setAnchor] = React.useState<null | HTMLElement>(null)

  const q = useQuery({
    queryKey: ['agent-health'],
    queryFn: () => apiFetch('/api/agent/health').then(async (r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      return r.json()
    }),
    staleTime: 30_000,
    refetchInterval: 60_000,
    // `retry: false` on purpose: if the query fails, the bell stays empty and
    // silent. A permanent error icon in the top bar because of a passing 500
    // scares more than it informs.
    retry: false,
  })

  const avisos: any[] = q.data?.blockers ?? []
  // The counter counts the ones that REQUIRE something. If it also counted the
  // informational ones it would live permanently in amber —«no active routines» is a
  // legitimate and stable state for many employees— and a number that never goes down
  // stops being looked at within a week.
  const urgentes = avisos.filter((b) => b.severity === 'error').length

  const descartar = useMutation({
    mutationFn: (fingerprint: string) =>
      apiFetch('/api/agent/health/dismiss', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ fingerprint }),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['agent-health'] }),
  })

  // With no notices NOTHING is painted. A bell that is always there and always empty is
  // permanent visual noise; its appearing is the signal in itself.
  if (!avisos.length) return null

  return (
    <>
      <Tooltip title={t('layout.notices.tooltip', 'Avisos del empleado') as string}>
        <IconButton
          onClick={(e) => setAnchor(e.currentTarget)}
          aria-label={t('layout.notices.tooltip', 'Avisos del empleado') as string}
        >
          <Badge
            badgeContent={urgentes || undefined}
            color="error"
            variant={urgentes ? 'standard' : 'dot'}
          >
            <NotificationsNoneIcon />
          </Badge>
        </IconButton>
      </Tooltip>

      <Menu
        anchorEl={anchor}
        open={Boolean(anchor)}
        onClose={() => setAnchor(null)}
        slotProps={{ paper: { sx: { maxWidth: 420 } } }}
      >
        <Box sx={{ px: 2, pt: 1.5, pb: 0.5 }}>
          <Typography variant="subtitle2">
            {t('layout.notices.title', 'Avisos del empleado')}
          </Typography>
          <Typography variant="caption" color="text.secondary">
            {t('layout.notices.subtitle',
              'Desaparecen solos en cuanto dejan de ser ciertos.')}
          </Typography>
        </Box>
        <Divider sx={{ mt: 1 }} />
        <List dense sx={{ py: 0 }}>
          {avisos.map((b, i) => (
            <ListItem
              key={`${b.key}-${i}`}
              alignItems="flex-start"
              sx={{ display: 'block', py: 1.25 }}
            >
              <ListItemText
                primary={healthNoticeText(b, t)}
                primaryTypographyProps={{
                  variant: 'body2',
                  color: b.severity === 'error' ? 'error.main' : 'text.primary',
                }}
              />
              <Box sx={{ display: 'flex', gap: 1, mt: 0.5 }}>
                {b.link && (
                  <Button
                    size="small"
                    onClick={() => { setAnchor(null); navigate(b.link) }}
                  >
                    {t('layout.notices.go', 'Ir')}
                  </Button>
                )}
                {/* Only the ones that require no action. The text says HOW LONG it lasts so
                    that dismissing is not read as «never see it again». */}
                {b.dismissible && b.fingerprint && (
                  <Button
                    size="small"
                    color="inherit"
                    disabled={descartar.isPending}
                    onClick={() => descartar.mutate(b.fingerprint)}
                  >
                    {t('layout.notices.dismiss', 'No avisarme en 30 días')}
                  </Button>
                )}
              </Box>
            </ListItem>
          ))}
        </List>
      </Menu>
    </>
  )
}
