/**
 * Profiling module — «Sign in with a code»: the PUBLIC page of the external accounts (level 4,
 * setting `alta: externas`). A person of an outside organisation has no WAI password: they type their
 * email, the platform emails them a code, and the code becomes a session of THIS app only (valid
 * nowhere else). Wire it OUTSIDE the protected shell (`rutasPublicasDelPerfilado()`), and link it from
 * the app's login page.
 *
 * The session is stored where the app's AuthContext reads it (`localStorage` `token` / `email`,
 * patterns/authentication.md) and the page reloads the app at `/`.
 *
 * INVARIANT FILE — installed by `wai-app-builder/assets/modulos.py`; do not edit it. Theme only: no
 * colours, fonts or spacing of its own.
 */
import { useState } from 'react'
import { Alert, Box, Card, CardContent, Stack, TextField, Typography } from '@mui/material'
import { Boton } from './adaptador'
import { entradaExterna } from './api'
import { mensajeDeError, useTextos } from './textos'

export default function PaginaEntrar() {
  const t = useTextos()
  const [email, setEmail] = useState('')
  const [codigo, setCodigo] = useState('')
  const [enviado, setEnviado] = useState(false)
  const [ocupado, setOcupado] = useState(false)
  const [error, setError] = useState('')

  const pedir = () => {
    setOcupado(true); setError('')
    entradaExterna.pedirCodigo(email.trim())
      .then(() => setEnviado(true), (e) => setError(mensajeDeError(t, e)))
      .finally(() => setOcupado(false))
  }
  const entrar = () => {
    setOcupado(true); setError('')
    entradaExterna.entrar(email.trim(), codigo.trim()).then((s) => {
      localStorage.setItem('token', s.access_token)
      localStorage.setItem('email', email.trim().toLowerCase())
      window.location.assign('/')
    }, (e) => { setError(mensajeDeError(t, e)); setOcupado(false) })
  }

  return (
    <Box sx={{ minHeight: '100dvh', display: 'flex', alignItems: 'center', justifyContent: 'center', p: 2 }}>
      <Card variant="outlined" sx={{ width: '100%', maxWidth: 420 }}>
        <CardContent>
          <Stack spacing={2}>
            <Typography variant="h5" component="h1">{t('entrar_titulo')}</Typography>
            <Typography variant="body2" color="text.secondary">{t(enviado ? 'entrar_enviado' : 'entrar_ayuda')}</Typography>
            {error ? <Alert severity="warning">{error}</Alert> : null}
            <TextField label={t('campo_correo')} type="email" autoComplete="email" value={email} disabled={enviado}
              onChange={(e) => setEmail(e.target.value)} autoFocus={!enviado} />
            {enviado ? (
              <TextField label={t('entrar_codigo')} value={codigo} autoComplete="one-time-code" autoFocus
                inputProps={{ inputMode: 'numeric' }} onChange={(e) => setCodigo(e.target.value)} />
            ) : null}
            {enviado ? (
              <Stack direction="row" spacing={1} justifyContent="space-between">
                <Boton disabled={ocupado} onClick={() => { setEnviado(false); setCodigo('') }}>{t('entrar_otro_correo')}</Boton>
                <Boton variant="contained" disabled={ocupado || !codigo.trim()} onClick={entrar}>{t('entrar_boton')}</Boton>
              </Stack>
            ) : (
              <Boton variant="contained" disabled={ocupado || !email.trim()} onClick={pedir}>{t('entrar_pedir_codigo')}</Boton>
            )}
          </Stack>
        </CardContent>
      </Card>
    </Box>
  )
}
