/**
 * How much the employee thinks on THIS message.
 *
 * The model is chosen once and for everything (Operations → Model). This is something else: on
 * that same model, how much reasoning is asked for the question being written.
 * "What trips do I have?" does not need the same as "compare these three offers for me", and
 * until now both paid the same time.
 *
 * Two interface decisions:
 *
 * · **What you see is what will run.** The control shows the level of the NEXT message;
 *   changing it saves nothing, just as changing lanes does not change your car. To make it
 *   stick there is a button that says so in those words.
 * · **The names are product names.** Rápido / Normal / Profundo (Fast / Normal / Deep). That
 *   underneath they are the provider's effort levels is a detail of the current model and does not show.
 */
import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import {
  Box, Chip, Divider, ListItemText, Menu, MenuItem, Tooltip, Typography,
} from '@mui/material'
import PsychologyIcon from '@mui/icons-material/Psychology'
import CheckIcon from '@mui/icons-material/Check'
import { apiFetch } from '../../services/apiClient'

export interface NivelDeclarado {
  id: string
  nombre: string
  para: string
  esfuerzo?: string
}

interface Props {
  /** The level chosen for the next message ('' = whatever the employee has). */
  valor: string
  onChange: (nivel: string) => void
  disabled?: boolean
  compacto?: boolean
}

export default function NivelDeRazonamiento({ valor, onChange, disabled, compacto }: Props) {
  const { t } = useTranslation()
  const [niveles, setNiveles] = useState<NivelDeclarado[]>([])
  const [mio, setMio] = useState('')
  // What someone who does not choose runs with. Showing it is what stops «El del empleado»
  // (The employee's) from being a black box: it was picked blind and an operator change
  // showed up nowhere. Empty = not even the employee has chosen, and the model default rules.
  const [delEmpleado, setDelEmpleado] = useState('')
  // What «El del empleado» (The employee's) comes out to when the employee has not set
  // anything either. There is no "let the model decide": the effort is always set by the
  // caller, and if nobody sets it the layer below does. Saying so is what removes the question.
  const [sinFijarEquivale, setSinFijarEquivale] = useState('')
  const [ancla, setAncla] = useState<HTMLElement | null>(null)

  // The catalog comes from the employee, not the client: a level it does not declare cannot be
  // requested (its pod is the one that has them registered on the model). That way an old
  // screen against a new employee shows what really exists.
  useEffect(() => {
    let vivo = true
    void (async () => {
      try {
        const r = await apiFetch('/api/agent/razonamiento')
        if (!r.ok) return
        const d = await r.json()
        if (!vivo) return
        setNiveles(Array.isArray(d?.niveles) ? d.niveles : [])
        setDelEmpleado(String(d?.del_empleado?.nombre || ''))
        setSinFijarEquivale(String(d?.del_empleado?.equivale_a?.nombre || ''))
        const preferido = typeof d?.mio === 'string' ? d.mio : ''
        setMio(preferido)
        // With no previous choice on this screen, start with the pinned level.
        if (preferido && !valor) onChange(preferido)
      } catch {
        /* without a catalog the control is not shown: it is a convenience, not a requirement */
      }
    })()
    return () => { vivo = false }
    // Only on mount: reloading it on every change would overwrite the turn's choice.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const fijar = useCallback(async (nivel: string) => {
    setMio(nivel)
    try {
      await apiFetch('/api/agent/razonamiento', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ nivel }),
      })
    } catch {
      /* failing to save the preference must not prevent sending the message */
    }
  }, [])

  if (!niveles.length) return null

  const actual = niveles.find((n) => n.id === valor)
  const etiqueta = actual
    ? actual.nombre
    : (delEmpleado
      ? t('agent.chat.razonamiento.delEmpleadoCon', 'El del empleado ({{nivel}})', { nivel: delEmpleado })
      : t('agent.chat.razonamiento.delEmpleado', 'El del empleado'))

  return (
    <>
      <Tooltip title={t('agent.chat.razonamiento.tooltip', 'Cuánto piensa antes de contestar') as string}>
        <span>
          <Chip
            icon={<PsychologyIcon fontSize="small" />}
            label={compacto && !actual ? '' : etiqueta}
            size="small"
            variant={actual ? 'filled' : 'outlined'}
            color={actual ? 'primary' : 'default'}
            onClick={(e) => setAncla(e.currentTarget)}
            disabled={disabled}
            sx={{
              alignSelf: 'flex-start',
              mt: 0.5,
              maxWidth: 160,
              '& .MuiChip-label': compacto && !actual ? { px: 0.5 } : undefined,
            }}
          />
        </span>
      </Tooltip>
      <Menu
        anchorEl={ancla}
        open={!!ancla}
        onClose={() => setAncla(null)}
        anchorOrigin={{ vertical: 'top', horizontal: 'left' }}
        transformOrigin={{ vertical: 'bottom', horizontal: 'left' }}
        slotProps={{ paper: { sx: { maxWidth: 340 } } }}
      >
        <Typography
          variant="caption"
          color="text.secondary"
          sx={{ px: 2, pt: 1, pb: 0.5, display: 'block' }}
        >
          {t('agent.chat.razonamiento.titulo', 'Cuánto piensa en este mensaje')}
        </Typography>
        <MenuItem
          onClick={() => { onChange(''); setAncla(null) }}
          selected={!valor}
        >
          <Box sx={{ width: 24, flexShrink: 0 }}>
            {!valor && <CheckIcon fontSize="small" color="primary" />}
          </Box>
          <ListItemText
            primary={delEmpleado
              ? t('agent.chat.razonamiento.delEmpleadoCon', 'El del empleado ({{nivel}})', { nivel: delEmpleado })
              : t('agent.chat.razonamiento.delEmpleado', 'El del empleado')}
            secondary={delEmpleado
              ? t('agent.chat.razonamiento.delEmpleadoAyuda',
                  'Lo que tiene configurado. No eliges por él.')
              : (sinFijarEquivale
                ? t('agent.chat.razonamiento.delEmpleadoSinFijarCon',
                    'Él tampoco lo ha fijado, así que lo pone el modelo: hoy sale {{nivel}}.',
                    { nivel: sinFijarEquivale })
                : t('agent.chat.razonamiento.delEmpleadoSinFijar',
                    'Él tampoco lo ha fijado, así que lo pone el modelo.'))}
            secondaryTypographyProps={{ sx: { whiteSpace: 'normal' } }}
          />
        </MenuItem>
        <Divider />
        {niveles.map((n) => (
          <MenuItem
            key={n.id}
            onClick={() => { onChange(n.id); setAncla(null) }}
            selected={valor === n.id}
          >
            <Box sx={{ width: 24, flexShrink: 0 }}>
              {valor === n.id && <CheckIcon fontSize="small" color="primary" />}
            </Box>
            <ListItemText
              primary={n.nombre}
              secondary={n.para}
              secondaryTypographyProps={{ sx: { whiteSpace: 'normal' } }}
            />
          </MenuItem>
        ))}
        <Divider />
        {/* Pinning it is a separate act and is said in those words: choosing above changes this
            message, this changes what all the following ones start with —also on mobile,
            because it is saved in your profile and not in this browser—. */}
        <MenuItem
          onClick={() => { void fijar(valor); setAncla(null) }}
          disabled={mio === valor}
        >
          <ListItemText
            primary={t('agent.chat.razonamiento.fijar', 'Dejarlo como mi nivel por defecto')}
            secondary={mio === valor
              ? t('agent.chat.razonamiento.yaFijado', 'Ya es el tuyo por defecto.')
              : t('agent.chat.razonamiento.fijarAyuda', 'Se aplicará a tus próximos mensajes, aquí y en el móvil.')}
            secondaryTypographyProps={{ sx: { whiteSpace: 'normal' } }}
          />
        </MenuItem>
      </Menu>
    </>
  )
}
