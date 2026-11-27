// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { I18nextProvider } from 'react-i18next'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { ThemeProvider } from '@mui/material'
import i18n from '../i18n'
import theme from '../theme/theme'
import OcupacionPage from './OcupacionPage'
import HotelPage from './HotelPage'
import ReubicacionesPage from './ReubicacionesPage'
import { gestorApi } from '../services/gestor'

const mockPerfilado = vi.hoisted(() => ({ permisos: {} as Record<string, string[]>, roles: [] as string[] }))

vi.mock('../services/gestor', () => ({ gestorApi: {
  ocupacion: vi.fn(), hoteles: vi.fn(), hotel: vi.fn(), habitaciones: vi.fn(), reservas: vi.fn(), reubicaciones: vi.fn(),
  crearReserva: vi.fn(), editarReserva: vi.fn(), cancelarReserva: vi.fn(),
  proponer: vi.fn(), aprobar: vi.fn(), cargarDatosEjemplo: vi.fn(),
} }))

vi.mock('../wai/perfilado', () => ({
  useMiAcceso: () => ({ yo: { permisos: mockPerfilado.permisos, roles: mockPerfilado.roles }, error: null, cargando: false, recargar: vi.fn() }),
}))

const hoteles = [
  { id: 1, nombre: 'Gran Hotel Centro', tarifa_noche_eur: 180, distancia_centro_valencia_km: 0.5, habitaciones_totales: 30, fuera_servicio: 3, vendidas: 30, disponibles: 27, libres: 0, ocupacion_porcentaje: 111.1, motivo_fuera_servicio: 'Fuga de agua' },
  { id: 2, nombre: 'Hotel Playa Malvarrosa', tarifa_noche_eur: 210, distancia_centro_valencia_km: 4.5, habitaciones_totales: 24, fuera_servicio: 0, vendidas: 23, disponibles: 24, libres: 1, ocupacion_porcentaje: 95.8 },
]

function renderWithApp(node: ReactNode, start = '/') {
  return render(
    <I18nextProvider i18n={i18n}>
      <ThemeProvider theme={theme}>
        <MemoryRouter initialEntries={[start]}>{node}</MemoryRouter>
      </ThemeProvider>
    </I18nextProvider>,
  )
}

describe('pantallas de ocupación y reubicación', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    mockPerfilado.roles = []
    mockPerfilado.permisos = { 'CAP-3': ['ver', 'aprobar'], 'CAP-4': ['crear'] }
  })
  afterEach(cleanup)

  it('destaca el aviso cuando el centro supera su capacidad disponible', async () => {
    vi.mocked(gestorApi.ocupacion).mockResolvedValue({ hoteles: 5, vendidas: 101, fuera_servicio: 3, sobreventa: 3 })
    vi.mocked(gestorApi.hoteles).mockResolvedValue({ items: hoteles, total: 5, limit: 50, offset: 0 })

    renderWithApp(<OcupacionPage />)

    expect(await screen.findByRole('heading', { name: 'Ocupación de esta noche' })).toBeInTheDocument()
    expect(screen.getByText('Hay huéspedes por reubicar.')).toBeInTheDocument()
    expect(screen.getByText('3 por encima de capacidad')).toBeInTheDocument()
  })

  it('permite al administrador owner cargar datos de ejemplo con permiso general', async () => {
    mockPerfilado.permisos = { '*': ['ver', 'crear'] }
    vi.mocked(gestorApi.ocupacion).mockResolvedValue({ hoteles: 0, vendidas: 0, fuera_servicio: 0, sobreventa: 0 })
    vi.mocked(gestorApi.hoteles).mockResolvedValue({ items: [], total: 0, limit: 50, offset: 0 })

    renderWithApp(<OcupacionPage />)

    expect(await screen.findByRole('button', { name: 'Cargar datos de ejemplo' })).toBeInTheDocument()
  })

  it('muestra las habitaciones fuera de servicio en la ficha del hotel', async () => {
    vi.mocked(gestorApi.hotel).mockResolvedValue(hoteles[0])
    vi.mocked(gestorApi.habitaciones).mockResolvedValue({
      items: [{ id: 1, numero: '201', estado: 'fuera_servicio', detalle: 'Fuga de agua' }],
      total: 30,
      limit: 20,
      offset: 0,
    })
    vi.mocked(gestorApi.reservas).mockResolvedValue({ items: [], total: 30, limit: 20, offset: 0 })

    renderWithApp(
      <Routes><Route path="/hoteles/:hotelId" element={<HotelPage />} /></Routes>,
      '/hoteles/1',
    )

    expect(await screen.findByRole('heading', { name: 'Gran Hotel Centro' })).toBeInTheDocument()
    expect(screen.getAllByText('Fuga de agua').length).toBeGreaterThan(0)
    expect(screen.getByText('Fuera de servicio')).toBeInTheDocument()
  })

  it('permite a Consulta leer las cancelaciones con su auditoría, sin controles de escritura', async () => {
    mockPerfilado.roles = ['consulta']
    vi.mocked(gestorApi.hotel).mockResolvedValue(hoteles[0])
    vi.mocked(gestorApi.habitaciones).mockResolvedValue({
      items: [{ id: 1, numero: '201', estado: 'libre' }], total: 30, limit: 20, offset: 0,
    })
    vi.mocked(gestorApi.reservas).mockResolvedValue({
      items: [{
        id: 7, huesped: 'Amparo Vidal', habitacion_id: 201, habitacion: '201',
        entrada: '2026-12-01', salida: '2026-12-02', estado: 'cancelada',
        motivo_cancelacion: 'Cambio de planes', cancelada_por: 'direccion@example.test',
        cancelada_en: '2026-11-27T10:30:00+00:00',
      }], total: 1, limit: 20, offset: 0,
    })

    renderWithApp(
      <Routes><Route path="/hoteles/:hotelId" element={<HotelPage />} /></Routes>,
      '/hoteles/1',
    )
    await screen.findByRole('heading', { name: 'Gran Hotel Centro' })
    fireEvent.click(screen.getByRole('tab', { name: 'Reservas' }))
    fireEvent.click(await screen.findByRole('tab', { name: 'Canceladas' }))

    expect(await screen.findByText('Cambio de planes')).toBeInTheDocument()
    expect(screen.getByText('Cancelada por direccion@example.test')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Nueva reserva' })).not.toBeInTheDocument()
  })

  it.each(['admin', 'direccion', 'recepcion'])('%s puede abrir el formulario de reserva desde el hotel', async (role) => {
    mockPerfilado.roles = [role]
    vi.mocked(gestorApi.hotel).mockResolvedValue(hoteles[0])
    vi.mocked(gestorApi.habitaciones).mockResolvedValue({
      items: [{ id: 1, numero: '201', estado: 'libre' }], total: 30, limit: 20, offset: 0,
    })
    vi.mocked(gestorApi.reservas).mockResolvedValue({ items: [], total: 0, limit: 20, offset: 0 })

    renderWithApp(
      <Routes><Route path="/hoteles/:hotelId" element={<HotelPage />} /></Routes>,
      '/hoteles/1',
    )
    await screen.findByRole('heading', { name: 'Gran Hotel Centro' })
    fireEvent.click(screen.getByRole('tab', { name: 'Reservas' }))

    fireEvent.click(await screen.findByRole('button', { name: 'Nueva reserva' }))
    expect(await screen.findByRole('heading', { name: 'Crear una reserva' })).toBeInTheDocument()
  })

  it('presenta las propuestas pendientes para que Dirección las revise', async () => {
    vi.mocked(gestorApi.reubicaciones).mockResolvedValue({
      propuestas: { items: [{
        id: 1, huesped: 'Amparo Vidal', origen: 'Gran Hotel Centro', destino: 'Hotel Congresos',
        tarifa_origen: 180, tarifa_destino: 95, diferencia_tarifa: -85, distancia_destino_km: 18,
        motivo: 'Fuga de agua', estado: 'pendiente', propuesto_por: 'Recepción', propuesto_en: '2026-11-18T10:00:00Z',
      }], total: 1, limit: 20, offset: 0 },
      historial: { items: [], total: 0, limit: 20, offset: 0 },
      huespedes_por_reubicar: { items: [], total: 0, limit: 100, offset: 0 },
      destinos: { items: hoteles, total: 2, limit: 100, offset: 0 },
    })

    renderWithApp(<ReubicacionesPage />)

    await waitFor(() => expect(screen.getByText('Amparo Vidal')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: 'Aprobar y mover huésped' })).toBeInTheDocument()
  })
})
