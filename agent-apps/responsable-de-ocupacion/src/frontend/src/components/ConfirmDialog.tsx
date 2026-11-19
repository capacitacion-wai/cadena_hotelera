/**
 * ConfirmDialog — a single, in-app confirmation modal used across the SPA
 * instead of the native ``window.confirm`` popup (which looks out of place
 * and can't be styled or themed).
 *
 * Usage:
 *   const { confirm, dialog } = useConfirm()
 *   ...
 *   <IconButton onClick={() => confirm(
 *      { message: t('x.deleteConfirm'), danger: true },
 *      () => del.mutate(id),
 *   )} />
 *   ...
 *   {dialog}   // render once, anywhere in the component tree
 */
import { useCallback, useState, type ReactNode } from 'react'
import {
  Button, Dialog, DialogActions, DialogContent, DialogContentText, DialogTitle,
  TextField,
} from '@mui/material'
import { useTranslation } from 'react-i18next'

export interface ConfirmOptions {
  title?: string
  message: ReactNode
  confirmLabel?: string
  cancelLabel?: string
  danger?: boolean
}

interface ConfirmState extends ConfirmOptions {
  onConfirm: () => void
}

export function useConfirm() {
  const { t } = useTranslation()
  const [state, setState] = useState<ConfirmState | null>(null)

  const confirm = useCallback((opts: ConfirmOptions, onConfirm: () => void) => {
    setState({ ...opts, onConfirm })
  }, [])

  const close = useCallback(() => setState(null), [])

  const dialog = (
    <Dialog open={!!state} onClose={close} maxWidth="xs" fullWidth>
      <DialogTitle sx={{ fontWeight: 700 }}>
        {state?.title ?? t('common.confirmTitle')}
      </DialogTitle>
      <DialogContent>
        <DialogContentText sx={{ whiteSpace: 'pre-line' }}>
          {state?.message}
        </DialogContentText>
      </DialogContent>
      <DialogActions>
        <Button onClick={close} sx={{ textTransform: 'none' }}>
          {state?.cancelLabel ?? t('common.cancel')}
        </Button>
        <Button
          variant="contained"
          color={state?.danger ? 'error' : 'primary'}
          sx={{ textTransform: 'none' }}
          onClick={() => { const act = state?.onConfirm; setState(null); act?.() }}
        >
          {state?.confirmLabel ?? t('common.confirm')}
        </Button>
      </DialogActions>
    </Dialog>
  )

  return { confirm, dialog }
}

export interface PromptOptions {
  title?: string
  message?: ReactNode
  label?: string
  defaultValue?: string
  confirmLabel?: string
  placeholder?: string
}

interface PromptState extends PromptOptions {
  onSubmit: (value: string) => void
}

/**
 * usePrompt — an in-app text-input modal, replacing the native
 * ``window.prompt`` popup. ``onSubmit`` fires with the entered value on
 * confirm; cancelling closes without calling it (blank IS a valid submit so
 * callers that treat "" as "keep current" still work).
 *
 *   const { prompt, dialog } = usePrompt()
 *   prompt({ message: t('x.changeAccountPrompt'), defaultValue: cur }, (val) => { ... })
 *   {dialog}
 */
export function usePrompt() {
  const { t } = useTranslation()
  const [state, setState] = useState<PromptState | null>(null)
  const [value, setValue] = useState('')

  const prompt = useCallback((opts: PromptOptions, onSubmit: (value: string) => void) => {
    setValue(opts.defaultValue ?? '')
    setState({ ...opts, onSubmit })
  }, [])

  const close = useCallback(() => setState(null), [])
  const submit = useCallback(() => {
    const act = state?.onSubmit
    setState(null)
    act?.(value)
  }, [state, value])

  const dialog = (
    <Dialog open={!!state} onClose={close} maxWidth="xs" fullWidth>
      <DialogTitle sx={{ fontWeight: 700 }}>
        {state?.title ?? t('common.confirmTitle')}
      </DialogTitle>
      <DialogContent>
        {state?.message && (
          <DialogContentText sx={{ whiteSpace: 'pre-line', mb: 2 }}>
            {state.message}
          </DialogContentText>
        )}
        {/* The label goes ABOVE, not floating over the border. The floating one is drawn over
            the outline's stroke and needs a gap opened in it; in this dialog the gap did not
            open and the border crossed the text. A margin fix was tried (10.1.4) and that was
            not it. A normal label cannot break that way. */}
        {!!state?.label && (
          <DialogContentText sx={{ fontSize: 13, mb: 0.75 }}>
            {state.label}
          </DialogContentText>
        )}
        <TextField
          autoFocus
          fullWidth
          size="small"
          placeholder={state?.placeholder}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') submit() }}
        />
      </DialogContent>
      <DialogActions>
        <Button onClick={close} sx={{ textTransform: 'none' }}>
          {t('common.cancel')}
        </Button>
        <Button variant="contained" sx={{ textTransform: 'none' }} onClick={submit}>
          {state?.confirmLabel ?? t('common.confirm')}
        </Button>
      </DialogActions>
    </Dialog>
  )

  return { prompt, dialog }
}

export default useConfirm
