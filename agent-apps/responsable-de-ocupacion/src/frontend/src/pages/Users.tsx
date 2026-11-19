/**
 * Usuarios — the employee's CONTACT DIRECTORY (admin, v7.20.0).
 *
 * A single directory of the people the employee knows/serves. Each entry has
 * contact points (email + phones) and recognition aliases (alias + nicknames) so
 * the agent can resolve "manda esto a Juan" → Juan's email/number, a role
 * (admin|member) that — for anyone who logs in — gates the Administración area,
 * and whether they may DECIDE the actions the employee parks.
 *
 * The directory is composed BY HAND (v7.145.0): the employee is born with just
 * its administrator and nothing is mirrored from the project any more. Two ways
 * in, and the distinction matters: people OF THE PROJECT (added in bulk, they can
 * log in) and EXTERNAL contacts (cannot log in — channels only).
 */
import { Fragment, useMemo, useState } from 'react'
import {
  Alert, Autocomplete, Box, Button, Checkbox, Chip, Dialog, DialogActions, DialogContent,
  DialogTitle, FormControlLabel, IconButton, InputAdornment, MenuItem, Snackbar, Stack, Switch,
  Table, TableBody, TableCell, TableHead, TableRow, TextField, Tooltip, Typography,
} from '@mui/material'
import GroupIcon from '@mui/icons-material/Group'
import SearchIcon from '@mui/icons-material/Search'
import GroupAddIcon from '@mui/icons-material/GroupAdd'
import PersonAddAltIcon from '@mui/icons-material/PersonAddAlt'
import EditIcon from '@mui/icons-material/Edit'
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline'
import RestoreIcon from '@mui/icons-material/Restore'
import { useTranslation } from 'react-i18next'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import PageHeader from '../components/PageHeader'
import EmptyState from '../components/EmptyState'
import LoadingRegion from '../components/LoadingRegion'
import { useConfirm } from '../components/ConfirmDialog'
import { apiClient, apiFetch } from '../services/apiClient'
import { formatFetchError } from '../utils/apiError'

interface AppUser {
  id: number
  email: string | null
  phones: string[]
  telegram: string | null
  display_name: string | null
  alias: string | null
  nicknames: string[]
  role: 'admin' | 'member'
  source: string
  active: boolean
  preferred_channel?: string | null
  credit_cap?: number | null
  channel_only?: boolean | null
  // Effective (an admin always can) vs explicitly designated.
  can_approve?: boolean
  designated_approver?: boolean
}

export default function Users() {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const { confirm, dialog: confirmDialog } = useConfirm()
  const [editing, setEditing] = useState<AppUser | null>(null)
  const [creating, setCreating] = useState(false)
  const [fromProject, setFromProject] = useState(false)
  const [snack, setSnack] = useState<string | null>(null)
  const [q, setQ] = useState('')
  const [showInactive, setShowInactive] = useState(false)

  const { data: me } = useQuery({
    queryKey: ['users-me'],
    queryFn: () => apiClient.get<{ is_admin: boolean }>('/api/agent/users/me'),
    staleTime: 60_000,
  })
  const isAdmin = !!me?.is_admin

  // Refresh BOTH the page list and the shared ['users-directory'] cache that
  // Operations' recipient picker and Consumo's name map read (staleTime 60s), so
  // an edit here shows up there immediately instead of up to a minute later.
  const refreshDir = () => {
    qc.invalidateQueries({ queryKey: ['users'] })
    qc.invalidateQueries({ queryKey: ['users-directory'] })
  }

  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['users', showInactive],
    queryFn: () => apiClient.get<{ items: AppUser[] }>(
      `/api/agent/users${showInactive ? '?include_inactive=true' : ''}`,
    ),
  })
  const reactivate = useMutation({
    mutationFn: (id: number) => apiFetch(`/api/agent/users/${id}`, {
      method: 'PATCH', body: JSON.stringify({ active: true }),
    }).then((r) => r.json()),
    onSuccess: () => refreshDir(),
  })
  const items = data?.items ?? []
  const filtered = items.filter((u) => {
    const term = q.trim().toLowerCase()
    if (!term) return true
    const hay = `${u.display_name || ''} ${u.alias || ''} ${u.email || ''} ${u.nicknames.join(' ')} ${u.phones.join(' ')} ${u.telegram || ''}`.toLowerCase()
    return hay.includes(term)
  })
  const sourceLabel: Record<string, string> = {
    project: t('users.source.project', 'proyecto'),
    manual: t('users.source.manual', 'manual'),
    agent: t('users.source.agent', 'agente'),
    login: t('users.source.login', 'acceso'),
  }

  const del = useMutation({
    mutationFn: (id: number) => apiFetch(`/api/agent/users/${id}`, { method: 'DELETE' }).then((r) => r.json()),
    onSuccess: () => refreshDir(),
  })

  return (
    <Box>
      <PageHeader
        icon={<GroupIcon />}
        title={t('users.title', 'Usuarios')}
        subtitle={t('users.subtitle', 'Directorio de personas que el empleado conoce y a las que puede escribir: email, teléfono, alias y apodos. El rol decide quién administra el empleado.')}
        actions={isAdmin ? (
          <Stack direction="row" spacing={1}>
            <Button startIcon={<PersonAddAltIcon />} onClick={() => setCreating(true)}
                    sx={{ textTransform: 'none' }}>
              {t('users.newExternal', 'Contacto externo')}
            </Button>
            <Button variant="contained" startIcon={<GroupAddIcon />} onClick={() => setFromProject(true)}
                    sx={{ textTransform: 'none' }}>
              {t('users.addFromProjectBtn', 'Añadir del proyecto')}
            </Button>
          </Stack>
        ) : undefined}
      />

      {isLoading ? (
        <LoadingRegion />
      ) : isError ? (
        <EmptyState variant="error" title={t('common.error', 'Error')}
                    actionLabel={t('common.retry', 'Reintentar')} onAction={() => refetch()} />
      ) : (
        <>
          {/* Search + "Ver inactivos" render even when the ACTIVE list is empty,
              so a directory whose people were all deactivated is never a dead
              end — the admin can always flip the toggle to recover them. */}
          {(items.length > 0 || isAdmin) && (
            <Stack direction="row" spacing={1.5} alignItems="center" sx={{ mb: 2, flexWrap: 'wrap', gap: 1 }}>
              {items.length > 0 && (
                <TextField
                  size="small"
                  value={q}
                  onChange={(e) => setQ(e.target.value)}
                  placeholder={t('users.searchPh', 'Buscar por nombre, email, teléfono o alias…') as string}
                  sx={{ maxWidth: 420, flex: 1, minWidth: 220 }}
                  InputProps={{ startAdornment: (
                    <InputAdornment position="start"><SearchIcon fontSize="small" /></InputAdornment>
                  ) }}
                />
              )}
              {isAdmin && (
                <Chip
                  label={t('users.showInactive', 'Ver inactivos')}
                  variant={showInactive ? 'filled' : 'outlined'}
                  color={showInactive ? 'primary' : 'default'}
                  onClick={() => setShowInactive((v) => !v)}
                />
              )}
            </Stack>
          )}
          {items.length === 0 ? (
            <EmptyState title={showInactive ? t('users.empty', 'Sin usuarios todavía') : t('users.emptyActive', 'Sin usuarios activos')}
                        body={showInactive
                          ? t('users.emptyBody', 'El empleado nace solo con su administrador. Añade en lote a las personas del proyecto, o da de alta un contacto externo (solo canales).')
                          : t('users.emptyActiveBody', 'No hay usuarios activos. Prueba «Ver inactivos» para recuperar a alguien, o añade personas del proyecto.')}
                        actionLabel={isAdmin ? t('users.addFromProjectBtn', 'Añadir del proyecto') : undefined}
                        onAction={isAdmin ? () => setFromProject(true) : undefined} />
          ) : filtered.length === 0 ? (
            <EmptyState title={t('users.noMatches', 'Ningún usuario coincide con la búsqueda.')} />
          ) : (
          <Box sx={{ border: '1px solid', borderColor: 'divider', borderRadius: 2, overflow: 'hidden' }}>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>{t('users.cols.name', 'Nombre / alias')}</TableCell>
                <TableCell>{t('users.cols.email', 'Email')}</TableCell>
                <TableCell>{t('users.cols.phones', 'Teléfonos')}</TableCell>
                <TableCell>{t('users.cols.telegram', 'Telegram')}</TableCell>
                <TableCell>{t('users.cols.role', 'Rol')}</TableCell>
                <TableCell align="right" />
              </TableRow>
            </TableHead>
            <TableBody>
              {filtered.map((u) => (
                <Fragment key={u.id}>
                  <TableRow hover sx={{ opacity: u.active ? 1 : 0.55 }}>
                    <TableCell>
                      <Stack direction="row" spacing={0.75} alignItems="center">
                        <Typography variant="body2" sx={{ fontWeight: 600 }}>
                          {u.display_name || u.alias || u.email || u.phones[0] || `#${u.id}`}
                        </Typography>
                        <Chip size="small" variant="outlined" label={sourceLabel[u.source] || u.source}
                              sx={{ height: 18, '& .MuiChip-label': { px: 0.75, fontSize: 10 } }} />
                        {u.channel_only === true && (
                          <Chip size="small" color="warning" variant="outlined"
                                label={t('users.channelOnly', 'Solo canal')}
                                title={t('users.channelOnlyHelp', 'No está en el proyecto WAI: no puede autenticarse; solo se comunica por los canales de escucha.') as string}
                                sx={{ height: 18, '& .MuiChip-label': { px: 0.75, fontSize: 10 } }} />
                        )}
                        {!u.active && (
                          <Chip size="small" color="warning" variant="outlined"
                                label={t('users.inactive', 'inactivo')}
                                sx={{ height: 18, '& .MuiChip-label': { px: 0.75, fontSize: 10 } }} />
                        )}
                      </Stack>
                      {u.nicknames.length > 0 && (
                        <Typography variant="caption" color="text.secondary">
                          {u.nicknames.join(', ')}
                        </Typography>
                      )}
                    </TableCell>
                    <TableCell>{u.email || '—'}</TableCell>
                    <TableCell>{u.phones.join(', ') || '—'}</TableCell>
                    {/* The Telegram handle is a CONTACT POINT like the phone: it is what
                        makes it possible to recognise the person when they write to the bot,
                        so it is shown in the table and not only when opening the record. */}
                    <TableCell>{u.telegram ? `@${u.telegram}` : '—'}</TableCell>
                    <TableCell>
                      <Stack direction="row" spacing={0.5} alignItems="center" sx={{ flexWrap: 'wrap', gap: 0.5 }}>
                        <Chip size="small" variant="outlined"
                              color={u.role === 'admin' ? 'primary' : 'default'}
                              label={u.role === 'admin' ? t('users.role.admin', 'Admin') : t('users.role.member', 'Miembro')} />
                        {/* Only the designated one: on an admin it would be noise (they always can). */}
                        {u.designated_approver && u.role !== 'admin' && (
                          <Chip size="small" color="success" variant="outlined"
                                label={t('users.approver', 'Puede aprobar')}
                                title={t('users.approverHelp', 'Puede aprobar o rechazar las acciones que el empleado deja pendientes, sin administrar el empleado.') as string}
                                sx={{ height: 18, '& .MuiChip-label': { px: 0.75, fontSize: 10 } }} />
                        )}
                      </Stack>
                    </TableCell>
                    <TableCell align="right">
                      {isAdmin && (
                        <Stack direction="row" spacing={0.5} justifyContent="flex-end">
                          {!u.active && (
                            <Tooltip title={t('users.reactivate', 'Reactivar') as string}>
                              <IconButton size="small" color="primary" disabled={reactivate.isPending}
                                          onClick={() => reactivate.mutate(u.id)}>
                                <RestoreIcon fontSize="small" />
                              </IconButton>
                            </Tooltip>
                          )}
                          <Tooltip title={t('common.edit', 'Editar') as string}>
                            <IconButton size="small" onClick={() => setEditing(u)}><EditIcon fontSize="small" /></IconButton>
                          </Tooltip>
                          <Tooltip title={t('common.delete', 'Eliminar') as string}>
                            <IconButton size="small" color="error"
                                        onClick={() => confirm(
                                          { message: t('users.deleteConfirm', '¿Eliminar este usuario del directorio?'), danger: true, confirmLabel: t('common.delete', 'Eliminar') },
                                          () => del.mutate(u.id),
                                        )}>
                              <DeleteOutlineIcon fontSize="small" />
                            </IconButton>
                          </Tooltip>
                        </Stack>
                      )}
                    </TableCell>
                  </TableRow>
                </Fragment>
              ))}
            </TableBody>
          </Table>
          </Box>
          )}
        </>
      )}

      {(creating || editing) && isAdmin && (
        <UserDialog
          user={editing}
          onClose={() => { setCreating(false); setEditing(null) }}
          onSaved={(info) => {
            setCreating(false); setEditing(null)
            refreshDir()
            setSnack(info?.notice === 'is_project_member'
              ? t('users.wasProjectMember', 'Esta persona SÍ está en el proyecto: se ha dado de alta como usuario normal, podrá entrar al panel')
              : info?.merged
                ? t('users.mergedSaved', 'Se fusionó con un contacto que ya existía con ese email')
                : t('users.saved', 'Usuario guardado'))
          }}
        />
      )}

      {fromProject && isAdmin && (
        <ProjectUsersDialog
          onClose={() => setFromProject(false)}
          onSaved={(n) => {
            setFromProject(false)
            refreshDir()
            setSnack(n === 1
              ? t('users.bulkAdded1', 'Se ha añadido 1 persona del proyecto')
              : t('users.bulkAdded', 'Se han añadido {{count}} personas del proyecto', { count: n }))
          }}
        />
      )}

      <Snackbar open={!!snack} autoHideDuration={4000} onClose={() => setSnack(null)}
                anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}>
        <Alert severity="success" variant="filled" onClose={() => setSnack(null)}>{snack}</Alert>
      </Snackbar>
      {confirmDialog}
    </Box>
  )
}

/**
 * A person's record. On CREATE it is always an EXTERNAL CONTACT (someone who is not
 * in the WAI project): the project's people come in through the bulk registration,
 * which guarantees they really exist. That is why the "will not be able to log in"
 * notice here is fixed and not a deduction — and the backend checks it: if the email
 * turns out to belong to the project, it registers them as a normal user and says so.
 */
function UserDialog({ user, onClose, onSaved }: {
  user: AppUser | null
  onClose: () => void
  onSaved: (info?: { merged?: boolean; notice?: string | null }) => void
}) {
  const { t } = useTranslation()
  const [email, setEmail] = useState(user?.email || '')
  const [displayName, setDisplayName] = useState(user?.display_name || '')
  const [alias, setAlias] = useState(user?.alias || '')
  const [phones, setPhones] = useState<string[]>(user?.phones || [])
  const [telegram, setTelegram] = useState(user?.telegram || '')
  const [nicknames, setNicknames] = useState<string[]>(user?.nicknames || [])
  const [role, setRole] = useState<'admin' | 'member'>(user?.role || 'member')
  const [canApprove, setCanApprove] = useState(!!user?.designated_approver)
  const [preferredChannel, setPreferredChannel] = useState<string>(user?.preferred_channel || '')
  const [creditCap, setCreditCap] = useState<string>(
    user?.credit_cap != null ? String(user.credit_cap) : '')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  // Create = external contact (will not be able to log in). Edit = whatever its record says.
  const channelOnly = user ? user.channel_only === true : true
  const reachable = phones.length > 0 || telegram.trim().length > 0

  const submit = async () => {
    setBusy(true); setErr(null)
    const body: Record<string, unknown> = {
      email: email.trim() || null, display_name: displayName.trim() || null,
      alias: alias.trim() || null, phones, telegram: telegram.trim() || null, nicknames, role,
      can_approve: channelOnly ? false : canApprove,
      preferred_channel: preferredChannel || '',
      credit_cap: creditCap.trim() === '' ? null : Number(creditCap),
    }
    // Explicit intent on creation: it is an external contact. The backend checks it
    // against the project's real list and sends back its verdict.
    if (!user) body.channel_only = true
    try {
      const resp = user
        ? await apiFetch(`/api/agent/users/${user.id}`, { method: 'PATCH', body: JSON.stringify(body) })
        : await apiFetch('/api/agent/users', { method: 'POST', body: JSON.stringify(body) })
      if (!resp.ok) {
        // formatFetchError normalises EVERY FastAPI detail shape (string,
        // {code,message}, pydantic array) to a string — a raw detail object
        // rendered in the Alert crashes React (#31).
        setErr(await formatFetchError(resp, t))
        return
      }
      const data = await resp.json().catch(() => null)
      onSaved({ merged: !!data?.merged, notice: data?.notice ?? null })
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle>{user ? t('users.edit', 'Editar usuario') : t('users.newExternal', 'Contacto externo')}</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 1 }}>
          {err && <Alert severity="error">{err}</Alert>}
          {!user && (
            <Alert severity="warning">
              {t('users.externalWarn', 'Un contacto externo NO podrá entrar al panel: no es usuario del proyecto WAI, así que no tiene con qué autenticarse. Solo hablará con el empleado por los canales (WhatsApp, Telegram o correo), y por eso necesita al menos un teléfono o un @usuario de Telegram. Tampoco puede aprobar acciones. Si la persona SÍ está en el proyecto, añádela con «Añadir del proyecto».')}
            </Alert>
          )}
          <TextField label={t('users.fields.displayName', 'Nombre')} value={displayName} size="small" fullWidth
                     onChange={(e) => setDisplayName(e.target.value)} />
          <TextField label={t('users.fields.email', 'Email')} value={email} size="small" fullWidth type="email"
                     onChange={(e) => setEmail(e.target.value)} />
          {user && channelOnly && (
            <Alert severity="warning">
              {t('users.channelOnlyWarn', 'Esta persona no está en el proyecto WAI: NO podrá autenticarse (entrar al panel). Solo podrá comunicarse por los canales de escucha (email / WhatsApp / Telegram).')}
            </Alert>
          )}
          <Autocomplete multiple freeSolo options={[]} value={phones}
            onChange={(_, v) => setPhones(v as string[])}
            renderTags={(value, getTagProps) => value.map((opt, i) => {
              const { key, ...p } = getTagProps({ index: i }); return <Chip size="small" label={opt} key={key} {...p} />
            })}
            renderInput={(params) => (
              <TextField {...params} size="small" label={t('users.fields.phones', 'Teléfonos (E.164)')}
                         placeholder="+34600111222" helperText={t('users.fields.phonesHelp', 'Enter para añadir; formato +34600111222')} />
            )} />
          <TextField label={t('users.fields.telegram', 'Telegram (@usuario)')} value={telegram} size="small" fullWidth
                     placeholder="@usuario"
                     helperText={t('users.fields.telegramHelp', 'Para que el bot de Telegram reconozca a esta persona y le responda.')}
                     onChange={(e) => setTelegram(e.target.value)} />
          <TextField label={t('users.fields.alias', 'Alias')} value={alias} size="small" fullWidth
                     onChange={(e) => setAlias(e.target.value)} />
          <Autocomplete multiple freeSolo options={[]} value={nicknames}
            onChange={(_, v) => setNicknames(v as string[])}
            renderTags={(value, getTagProps) => value.map((opt, i) => {
              const { key, ...p } = getTagProps({ index: i }); return <Chip size="small" label={opt} key={key} {...p} />
            })}
            renderInput={(params) => (
              <TextField {...params} size="small" label={t('users.fields.nicknames', 'Apodos / cómo le llaman')}
                         helperText={t('users.fields.nicknamesHelp', 'Nombres por los que el empleado debe reconocerle')} />
            )} />
          {/* The role only applies to someone who can authenticate; for a channel-only
              contact it makes no sense, so it is hidden. */}
          {!channelOnly && (
            <Stack spacing={1}>
              <TextField select label={t('users.fields.role', 'Rol')} value={role} size="small" sx={{ maxWidth: 220 }}
                         onChange={(e) => setRole(e.target.value as 'admin' | 'member')}
                         helperText={t('users.fields.roleHelp', 'Admin = puede administrar el empleado')}>
                <MenuItem value="member">{t('users.role.member', 'Miembro')}</MenuItem>
                <MenuItem value="admin">{t('users.role.admin', 'Admin')}</MenuItem>
              </TextField>
              {/* Approving is a business decision, not an administrative one: it can be
                  granted without handing over the governance panel. An admin already has it. */}
              <FormControlLabel
                control={<Switch size="small" checked={role === 'admin' || canApprove} disabled={role === 'admin'}
                                 onChange={(e) => setCanApprove(e.target.checked)} />}
                label={(
                  <Typography variant="body2">
                    {t('users.fields.canApprove', 'Puede aprobar acciones pendientes')}
                  </Typography>
                )} />
              <Typography variant="caption" color="text.secondary" sx={{ mt: -0.5 }}>
                {role === 'admin'
                  ? t('users.fields.canApproveAdmin', 'Los administradores pueden aprobar siempre.')
                  : t('users.fields.canApproveHelp', 'Podrá aprobar o rechazar lo que el empleado deje pendiente, sin acceso al resto de la administración.')}
              </Typography>
            </Stack>
          )}
          <Stack direction="row" spacing={2} sx={{ flexWrap: 'wrap', gap: 2 }}>
            <TextField select label={t('users.fields.preferredChannel', 'Canal preferido')} value={preferredChannel}
                       size="small" sx={{ minWidth: 200 }}
                       onChange={(e) => setPreferredChannel(e.target.value)}
                       helperText={t('users.fields.preferredChannelHelp', 'Por dónde contactarle por defecto')}>
              {/* The channels are sent by the backend (`REACHABLE_CHANNELS`): they are the
                  ones for which the record has a datum to write to. Slack is not there
                  because there is no Slack handle in the record — offering it would let
                  people pick a preference that could never be delivered. */}
              <MenuItem value="">{t('users.channel.auto', 'Automático')}</MenuItem>
              <MenuItem value="email">{t('users.channel.email', 'Correo')}</MenuItem>
              <MenuItem value="whatsapp">{t('users.channel.whatsapp', 'WhatsApp')}</MenuItem>
              <MenuItem value="telegram">{t('users.channel.telegram', 'Telegram')}</MenuItem>
            </TextField>
            <TextField label={t('users.fields.creditCap', 'Límite de créditos (30 días)')} value={creditCap}
                       size="small" sx={{ minWidth: 200 }} type="number" inputProps={{ min: 0, step: 1 }}
                       onChange={(e) => setCreditCap(e.target.value)}
                       placeholder={t('users.fields.creditCapPh', 'Sin límite') as string}
                       helperText={t('users.fields.creditCapHelp', 'Créditos que esta persona puede gastar; vacío = sin límite')} />
          </Stack>
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} sx={{ textTransform: 'none' }}>{t('common.cancel', 'Cancelar')}</Button>
        <Tooltip title={(!user && !reachable)
          ? t('users.needChannel', 'Añade un teléfono o un @usuario de Telegram: sin eso el empleado no tiene por dónde hablarle.') as string
          : ''}>
          <span>
            <Button variant="contained" onClick={submit} disabled={busy || (!user && !reachable)}
                    sx={{ textTransform: 'none' }}>
              {busy ? t('common.saving', 'Guardando…') : t('common.save', 'Guardar')}
            </Button>
          </span>
        </Tooltip>
      </DialogActions>
    </Dialog>
  )
}

interface ProjectPerson { email: string; name: string; in_directory: boolean }

/**
 * Bulk registration from the project — the DEFAULT way to populate the directory.
 * It shows ALL the people of the project (those already added appear marked and
 * locked, to see the whole project at a glance) and registers the selected ones in
 * one go. It is idempotent, so adding someone again duplicates nothing.
 */
function ProjectUsersDialog({ onClose, onSaved }: {
  onClose: () => void
  onSaved: (added: number) => void
}) {
  const { t } = useTranslation()
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [q, setQ] = useState('')
  const [role, setRole] = useState<'admin' | 'member'>('member')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['project-available'],
    queryFn: () => apiClient.get<{ users: ProjectPerson[]; source: string }>(
      '/api/agent/users/project-available'),
  })
  const people = data?.users ?? []
  const unavailable = data?.source === 'unavailable'
  const filtered = useMemo(() => {
    const term = q.trim().toLowerCase()
    if (!term) return people
    return people.filter((p) => `${p.name} ${p.email}`.toLowerCase().includes(term))
  }, [people, q])
  const selectable = filtered.filter((p) => !p.in_directory)
  const allPicked = selectable.length > 0 && selectable.every((p) => picked.has(p.email))

  const toggle = (email: string) => setPicked((prev) => {
    const next = new Set(prev)
    if (next.has(email)) next.delete(email); else next.add(email)
    return next
  })
  const toggleAll = () => setPicked((prev) => {
    const next = new Set(prev)
    if (allPicked) selectable.forEach((p) => next.delete(p.email))
    else selectable.forEach((p) => next.add(p.email))
    return next
  })

  const submit = async () => {
    setBusy(true); setErr(null)
    try {
      const resp = await apiFetch('/api/agent/users/bulk', {
        method: 'POST',
        body: JSON.stringify({ emails: Array.from(picked), role }),
      })
      if (!resp.ok) { setErr(await formatFetchError(resp, t)); return }
      const body = await resp.json().catch(() => null)
      onSaved((body?.created?.length ?? 0) + (body?.merged?.length ?? 0))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open onClose={onClose} maxWidth="md" fullWidth>
      <DialogTitle>{t('users.bulkTitle', 'Añadir personas del proyecto')}</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 1 }}>
          {err && <Alert severity="error">{err}</Alert>}
          <Typography variant="body2" color="text.secondary">
            {t('users.bulkHelp', 'Estas son las personas del proyecto WAI. Las que marques entrarán en el directorio del empleado: podrán entrar al panel y el empleado sabrá quiénes son. Nadie se añade automáticamente, así que el directorio es exactamente el que decidas.')}
          </Typography>
          {isLoading ? (
            <LoadingRegion />
          ) : isError || unavailable ? (
            <EmptyState variant="error"
                        title={t('users.bulkUnavailable', 'No se ha podido consultar los usuarios del proyecto')}
                        body={t('users.bulkUnavailableBody', 'La plataforma WAI no responde ahora mismo. Reinténtalo, o da de alta un contacto externo mientras tanto.')}
                        actionLabel={t('common.retry', 'Reintentar')} onAction={() => refetch()} />
          ) : people.length === 0 ? (
            <EmptyState title={t('users.bulkEmpty', 'El proyecto no tiene más usuarios')} />
          ) : (
            <>
              <Stack direction="row" spacing={1.5} alignItems="center" sx={{ flexWrap: 'wrap', gap: 1 }}>
                <TextField size="small" value={q} onChange={(e) => setQ(e.target.value)}
                           placeholder={t('users.bulkSearchPh', 'Buscar por nombre o email…') as string}
                           sx={{ maxWidth: 320, flex: 1, minWidth: 200 }}
                           InputProps={{ startAdornment: (
                             <InputAdornment position="start"><SearchIcon fontSize="small" /></InputAdornment>
                           ) }} />
                <TextField select size="small" label={t('users.bulkRole', 'Rol de los nuevos')} value={role}
                           sx={{ minWidth: 180 }}
                           onChange={(e) => setRole(e.target.value as 'admin' | 'member')}>
                  <MenuItem value="member">{t('users.role.member', 'Miembro')}</MenuItem>
                  <MenuItem value="admin">{t('users.role.admin', 'Admin')}</MenuItem>
                </TextField>
              </Stack>
              <Box sx={{ border: '1px solid', borderColor: 'divider', borderRadius: 2, overflow: 'auto', maxHeight: 420 }}>
                <Table size="small" stickyHeader>
                  <TableHead>
                    <TableRow>
                      <TableCell padding="checkbox">
                        <Checkbox size="small" checked={allPicked} disabled={selectable.length === 0}
                                  indeterminate={!allPicked && selectable.some((p) => picked.has(p.email))}
                                  onChange={toggleAll} />
                      </TableCell>
                      <TableCell>{t('users.cols.name', 'Nombre / alias')}</TableCell>
                      <TableCell>{t('users.cols.email', 'Email')}</TableCell>
                      <TableCell />
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {filtered.map((p) => (
                      <TableRow key={p.email} hover
                                sx={{ cursor: p.in_directory ? 'default' : 'pointer' }}
                                onClick={() => { if (!p.in_directory) toggle(p.email) }}>
                        <TableCell padding="checkbox">
                          <Checkbox size="small" checked={p.in_directory || picked.has(p.email)}
                                    disabled={p.in_directory} />
                        </TableCell>
                        <TableCell>{p.name || '—'}</TableCell>
                        <TableCell>{p.email}</TableCell>
                        <TableCell>
                          {p.in_directory && (
                            <Chip size="small" variant="outlined" label={t('users.alreadyAdded', 'ya está')}
                                  sx={{ height: 18, '& .MuiChip-label': { px: 0.75, fontSize: 10 } }} />
                          )}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </Box>
            </>
          )}
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} sx={{ textTransform: 'none' }}>{t('common.cancel', 'Cancelar')}</Button>
        <Button variant="contained" onClick={submit} disabled={busy || picked.size === 0}
                sx={{ textTransform: 'none' }}>
          {busy
            ? t('common.saving', 'Guardando…')
            : t('users.bulkAdd', 'Añadir {{count}}', { count: picked.size })}
        </Button>
      </DialogActions>
    </Dialog>
  )
}
