/**
 * RuleProposalCard — approve, right in the conversation, a rule the employee
 * just asked for (v7.140.0).
 *
 * The problem this solves: you agree something in the chat ("ok, don't warn me
 * about that again"), and until now the agreement died there — the operator had
 * to remember to go to another screen and re-say it in a form. So the natural
 * moment of agreement produced nothing, and the employee kept asking.
 *
 * Now the employee PROPOSES (it may never grant itself a rule), and the
 * proposal appears here, under the answer, with the two buttons that matter.
 * Approving stamps ``approved_where='chat'`` so the audit trail can point at the
 * conversation where it was agreed — the same effect the Aprendizaje queue
 * produces, just without leaving the thread.
 *
 * Implementation note: this deliberately does NOT extend the SSE contract. It
 * polls the proposals endpoint once the turn settles and shows what is pending
 * and RECENT (a proposal from last week belongs in the queue, not in the middle
 * of today's conversation).
 */

import { useTranslation } from 'react-i18next'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Box, Button, Chip, Stack, Typography } from '@mui/material'
import RuleIcon from '@mui/icons-material/Rule'
import AutoAwesomeIcon from '@mui/icons-material/AutoAwesome'
import CheckIcon from '@mui/icons-material/Check'
import { useState } from 'react'
import { apiFetch } from '../../services/apiClient'

type Proposal = {
  effect_id: number
  target: string
  label: string
  detail?: string | null
  proposed_at: string | null
  learning_id: number | null
  learning_subject?: string | null
  learning_content?: string | null
}

// Only a proposal born around this conversation belongs inline; anything older
// is queue material. Ten minutes covers a slow turn without dragging in
// yesterday's pending decisions.
const RECENT_MS = 10 * 60 * 1000

export default function RuleProposalCard({ active }: { active: boolean }) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const [busy, setBusy] = useState<number | null>(null)
  const [done, setDone] = useState<Record<number, 'accepted' | 'rejected'>>({})

  const { data } = useQuery({
    queryKey: ['self-proposals'],
    enabled: active,
    queryFn: async () => {
      const r = await apiFetch('/api/agent/self-memory/proposals')
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      return r.json() as Promise<{ items: Proposal[] }>
    },
    refetchInterval: active ? 20_000 : false,
    retry: false,
  })

  const now = Date.now()
  const pending = (data?.items ?? []).filter((p) => {
    if (done[p.effect_id]) return true   // keep it visible with its outcome
    if (!p.proposed_at) return false
    const ts = Date.parse(p.proposed_at)
    return Number.isFinite(ts) && now - ts < RECENT_MS
  })
  if (!active || pending.length === 0) return null

  const targetLabel = (target: string) => ({
    suppression: t('learning.target.suppression', 'Silencio'),
    parameter: t('learning.target.parameter', 'Dato de negocio'),
    instruction: t('learning.target.instruction', 'Instrucción'),
    automation: t('learning.target.automation', 'Automatización'),
  }[target] || target)

  const act = async (p: Proposal, what: 'accept' | 'reject') => {
    setBusy(p.effect_id)
    try {
      const r = await apiFetch(`/api/agent/self-memory/proposals/${p.effect_id}/${what}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        // ``where: chat`` is the whole point: the trail must be able to say
        // "you approved this in a conversation", not just "someone approved it".
        body: JSON.stringify(what === 'accept' ? { where: 'chat' } : {}),
      })
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      setDone((s) => ({ ...s, [p.effect_id]: what === 'accept' ? 'accepted' : 'rejected' }))
      for (const k of ['self-proposals', 'self-effects', 'self-entries',
                       'suppressions', 'knowledge-requests', 'automations']) {
        qc.invalidateQueries({ queryKey: [k] })
      }
    } catch {
      // Leave the buttons enabled: a failed approval must be retryable, and the
      // proposal is still pending server-side.
    } finally { setBusy(null) }
  }

  return (
    <Stack spacing={1} sx={{ mt: 1 }}>
      {pending.map((p) => {
        const outcome = done[p.effect_id]
        return (
          <Box
            key={p.effect_id}
            sx={{
              border: '1px solid', borderColor: outcome ? 'divider' : 'primary.main',
              borderLeft: '3px solid',
              borderLeftColor: outcome === 'accepted' ? 'success.main'
                : outcome === 'rejected' ? 'text.disabled' : 'primary.main',
              borderRadius: 1.5, p: 1.25,
              bgcolor: (th) => (th.palette.mode === 'dark' ? 'background.default' : 'grey.50'),
            }}
          >
            <Stack spacing={0.75}>
              <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
                <RuleIcon fontSize="small" color={outcome ? 'disabled' : 'primary'} />
                <Typography variant="caption" sx={{ fontWeight: 700 }}>
                  {t('learning.chat.title', 'Te pide permiso para cambiar algo')}
                </Typography>
                <Chip size="small" variant="outlined" label={targetLabel(p.target)}
                  sx={{ height: 18, fontSize: '0.7rem' }} />
              </Stack>
              <Typography variant="body2" sx={{ fontWeight: 600 }}>{p.label}</Typography>
              {p.detail && (
                <Typography variant="caption" color="text.secondary">{p.detail}</Typography>
              )}
              {p.learning_content && (
                <Typography variant="caption" color="text.secondary">
                  <AutoAwesomeIcon sx={{ fontSize: 12, verticalAlign: 'text-bottom' }} />{' '}
                  {p.learning_subject ? `${p.learning_subject}: ` : ''}“{p.learning_content}”
                </Typography>
              )}
              {outcome === 'accepted' ? (
                <Alert severity="success" icon={<CheckIcon fontSize="small" />} sx={{ py: 0 }}>
                  <Typography variant="caption">
                    {t('learning.chat.accepted', 'Hecho. Ya está en marcha y puedes deshacerlo en Operaciones → Aprendizaje.')}
                  </Typography>
                </Alert>
              ) : outcome === 'rejected' ? (
                <Typography variant="caption" color="text.secondary">
                  {t('learning.chat.rejected', 'Descartado. No volverá a pedírtelo.')}
                </Typography>
              ) : (
                <Stack direction="row" spacing={1}>
                  <Button
                    size="small" variant="contained" disabled={busy === p.effect_id}
                    onClick={() => act(p, 'accept')} sx={{ textTransform: 'none' }}
                  >
                    {t('learning.chat.accept', 'Sí, hazlo')}
                  </Button>
                  <Button
                    size="small" variant="outlined" color="inherit" disabled={busy === p.effect_id}
                    onClick={() => act(p, 'reject')} sx={{ textTransform: 'none' }}
                  >
                    {t('learning.chat.reject', 'No')}
                  </Button>
                </Stack>
              )}
            </Stack>
          </Box>
        )
      })}
    </Stack>
  )
}
