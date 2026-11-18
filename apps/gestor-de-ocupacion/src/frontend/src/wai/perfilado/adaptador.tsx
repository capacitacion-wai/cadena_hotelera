/**
 * Profiling module — THIS app's adapter (SCAFFOLD: it belongs to the app).
 *
 * Created ONCE by `modulos.py instalar perfilado`; `modulos.py actualizar` never touches it. The
 * module's screens (invariant) only reach the app through this file: its page header, its table,
 * its buttons, its API client and its vocabulary. Change what is below so the screens look like
 * the rest of the app — never the module's own files, and never with colours of your own: the
 * theme (`src/theme/tema.ts`) already gives them (`checks/colores_del_tema.py`).
 *
 * The installer filled the two imports with what it found in this app; if it did not find them it
 * left the usual paths and said so: point them at the app's PageHeader and API client.
 */
import type { ReactNode } from 'react'
import {
  Box, Button, Paper, Table, TableBody, TableCell, TableContainer, TableHead, TableRow, Typography,
} from '@mui/material'
import type { ButtonProps } from '@mui/material'
import PageHeader from '../../components/PageHeader'
import { apiFetch } from '../../services/apiClient'
import { ErrorDeApi } from './tipos'
import type { Columna, TextosDelPerfilado } from './tipos'

/** A whole screen of the module: the app's page header and the content below. */
export function Pagina({ titulo, subtitulo, acciones, children }: {
  titulo: string
  subtitulo?: string
  acciones?: ReactNode
  children: ReactNode
}) {
  return (
    <Box>
      <PageHeader title={titulo} subtitle={subtitulo} actions={acciones} />
      {children}
    </Box>
  )
}

/** The app's table. Swap it for the app's own list component if it has one. */
export function Tabla<T>({ columnas, filas, clave, vacio }: {
  columnas: Columna<T>[]
  filas: T[]
  clave: (fila: T) => string | number
  vacio: string
}) {
  return (
    <TableContainer component={Paper} variant="outlined">
      <Table size="small">
        <TableHead>
          <TableRow>
            {columnas.map((c) => <TableCell key={c.clave} align={c.alinear}>{c.titulo}</TableCell>)}
          </TableRow>
        </TableHead>
        <TableBody>
          {filas.length === 0 ? (
            <TableRow>
              <TableCell colSpan={columnas.length}>
                <Typography variant="body2" color="text.secondary">{vacio}</Typography>
              </TableCell>
            </TableRow>
          ) : filas.map((f) => (
            <TableRow key={clave(f)} hover>
              {columnas.map((c) => <TableCell key={c.clave} align={c.alinear}>{c.celda(f)}</TableCell>)}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableContainer>
  )
}

/** The app's button. */
export function Boton(props: ButtonProps) {
  return <Button {...props} />
}

/** Every call of the module goes through the app's API client (token, mobile app, 401). */
export async function llamarApi<T>(metodo: 'GET' | 'POST' | 'PATCH' | 'DELETE', ruta: string, cuerpo?: unknown): Promise<T> {
  const res = await apiFetch(ruta, {
    method: metodo,
    headers: cuerpo === undefined ? {} : { 'Content-Type': 'application/json' },
    body: cuerpo === undefined ? undefined : JSON.stringify(cuerpo),
  })
  if (!res.ok) {
    let detalle = ''
    try {
      const j = await res.json()
      detalle = typeof j?.detail === 'string' ? j.detail : ''
    } catch {
      detalle = ''
    }
    throw new ErrorDeApi(res.status, res.headers.get('X-WAI-Codigo') || '', detalle)
  }
  return (await res.json()) as T
}

/**
 * The app's vocabulary: overrides of the module's texts, under each language of the app's screens
 * («Members» instead of «Users»…). Only the keys that change; the module ships es and en and shows
 * only the languages the app's i18n declares. Another language (fr, pt…) starts in English: its
 * texts go here, under its code.
 */
export const TEXTOS_PROPIOS: TextosDelPerfilado = {
  es: {
    menu_usuarios: 'Personas con acceso',
    menu_roles: 'Perfiles y permisos',
    menu_mi_acceso: 'Mi acceso',
  },
}
