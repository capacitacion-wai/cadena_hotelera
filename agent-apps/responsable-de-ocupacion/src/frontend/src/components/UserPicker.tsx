/**
 * UserPicker — picker of people from the DIRECTORY (Usuarios page).
 *
 * Product rule (v7.24.0): outside the directory record an email or a phone
 * is NEVER typed by hand. Every UI that needs "a person" uses this picker;
 * the stored value is the canonical identifier the field needs — email (the
 * directory key) or E.164 phone — taken from the chosen record. No freeSolo:
 * if the person is not in the directory, they are registered first in
 * Usuarios (it is the employee's only contacts catalogue).
 *
 * `kind` decides which handle it fills: 'email' → one option per user with
 * an email; 'whatsapp' → one option per E.164 phone of each user.
 * Legacy values typed by hand before 7.24.0 are still rendered (as their
 * raw handle) so as not to break existing configs.
 */
import { useMemo } from 'react'
import { Autocomplete, Chip, TextField } from '@mui/material'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { apiFetch } from '../services/apiClient'

export type DirectoryUser = {
  id: number
  email: string | null
  display_name: string | null
  alias: string | null
  phones: string[]
  role: 'admin' | 'member'
}

/** Shared directory query — same key as Usuarios/Consumo to reuse the
 * existing cache and invalidations. */
export function useUsersDirectory() {
  return useQuery({
    queryKey: ['users-directory'],
    queryFn: () => apiFetch('/api/agent/users').then((r) => (r.ok ? r.json() : { items: [] })),
    staleTime: 60_000,
  })
}

export type HandleKind = 'email' | 'whatsapp'

export type UserOption = { handle: string; name: string }

function optionsFor(users: DirectoryUser[], kind: HandleKind): UserOption[] {
  const out: UserOption[] = []
  for (const u of users) {
    const name = (u.display_name || u.alias || u.email || u.phones?.[0] || '').trim()
    if (kind === 'email') {
      if (u.email) out.push({ handle: u.email, name })
    } else {
      for (const p of u.phones || []) if (p) out.push({ handle: p, name })
    }
  }
  return out
}

function labelOf(o: UserOption): string {
  return o.name && o.name.toLowerCase() !== o.handle.toLowerCase() ? `${o.name} (${o.handle})` : o.handle
}

type BaseProps = {
  kind: HandleKind
  label: string
  helperText?: string
  placeholder?: string
  disabled?: boolean
  size?: 'small' | 'medium'
  fullWidth?: boolean
}

type SingleProps = BaseProps & {
  multiple?: false
  value: string | null
  onChange: (handle: string | null) => void
}

type MultiProps = BaseProps & {
  multiple: true
  value: string[]
  onChange: (handles: string[]) => void
}

export default function UserPicker(props: SingleProps | MultiProps) {
  const { t } = useTranslation()
  const { data } = useUsersDirectory()
  const users = (data?.items ?? []) as DirectoryUser[]
  const options = useMemo(() => optionsFor(users, props.kind), [users, props.kind])
  const byHandle = useMemo(
    () => new Map(options.map((o) => [o.handle.toLowerCase(), o])),
    [options],
  )
  // A stored handle that is no longer in the directory (or a legacy one typed by
  // hand) is shown as its own text — visible and deletable, never broken.
  const toOption = (h: string): UserOption => byHandle.get(h.trim().toLowerCase()) ?? { handle: h, name: h }

  const common = {
    size: props.size ?? ('small' as const),
    fullWidth: props.fullWidth ?? true,
    disabled: props.disabled,
    options,
    getOptionLabel: (o: UserOption | string) => (typeof o === 'string' ? o : labelOf(o)),
    isOptionEqualToValue: (o: UserOption, v: UserOption) =>
      o.handle.toLowerCase() === v.handle.toLowerCase(),
    noOptionsText: t(
      'userPicker.noOptions',
      'No hay personas en el directorio — dalas de alta en Usuarios',
    ),
    renderInput: (params: any) => (
      <TextField
        {...params}
        label={props.label}
        placeholder={props.placeholder}
        helperText={props.helperText}
      />
    ),
  }

  if (props.multiple) {
    return (
      <Autocomplete
        {...common}
        multiple
        value={(props.value || []).map(toOption)}
        onChange={(_e, v) => props.onChange((v as UserOption[]).map((o) => o.handle))}
        renderTags={(value, getTagProps) =>
          (value as UserOption[]).map((o, i) => {
            const { key, ...p } = getTagProps({ index: i })
            return <Chip size="small" label={o.name || o.handle} title={o.handle} key={key} {...p} />
          })
        }
      />
    )
  }
  return (
    <Autocomplete
      {...common}
      value={props.value ? toOption(props.value) : null}
      onChange={(_e, v) => props.onChange(v ? (v as UserOption).handle : null)}
    />
  )
}
