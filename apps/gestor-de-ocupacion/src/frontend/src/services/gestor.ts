import { apiClient } from './apiClient'

export interface Pagina<T> {
  items: T[]
  total: number
  limit: number
  offset: number
}

export interface Hotel {
  id: number
  nombre: string
  tarifa_noche_eur: number
  distancia_centro_valencia_km: number
  habitaciones_totales: number
  fuera_servicio: number
  vendidas: number
  disponibles: number
  libres: number
  ocupacion_porcentaje: number
  motivo_fuera_servicio?: string
}

export interface Ocupacion {
  hoteles: number
  vendidas: number
  fuera_servicio: number
  sobreventa: number
}

export interface Habitacion {
  id: number
  numero: string
  estado: 'libre' | 'ocupada' | 'fuera_servicio'
  detalle?: string
}

export interface Reserva {
  id: number
  huesped: string
  habitacion: string
  entrada: string
  salida: string
  estado: string
}

export interface HuespedPorReubicar {
  reserva_id: number
  huesped: string
  origen_id: number
  origen: string
}

export interface Propuesta {
  id: number
  huesped: string
  origen: string
  destino: string
  tarifa_origen: number
  tarifa_destino: number
  diferencia_tarifa: number
  distancia_destino_km: number
  motivo: string
  estado: 'pendiente' | 'aprobada'
  propuesto_por: string
  propuesto_en: string
  aprobado_por?: string
  aprobado_en?: string
}

export interface DatosReubicaciones {
  propuestas: Pagina<Propuesta>
  historial: Pagina<Propuesta>
  huespedes_por_reubicar: Pagina<HuespedPorReubicar>
  destinos: Pagina<Hotel>
}

export const gestorApi = {
  ocupacion: () => apiClient.get<Ocupacion>('/api/ocupacion'),
  hoteles: () => apiClient.get<Pagina<Hotel>>('/api/hoteles?limit=50&offset=0'),
  hotel: (id: string) => apiClient.get<Hotel>(`/api/hoteles/${id}`),
  habitaciones: (id: string, offset = 0, limit = 20) => apiClient.get<Pagina<Habitacion>>(`/api/hoteles/${id}/habitaciones?limit=${limit}&offset=${offset}`),
  reservas: (id: string, offset = 0, limit = 20) => apiClient.get<Pagina<Reserva>>(`/api/hoteles/${id}/reservas?limit=${limit}&offset=${offset}`),
  reubicaciones: (propuestasOffset = 0, historialOffset = 0, limit = 20) =>
    apiClient.get<DatosReubicaciones>(`/api/reubicaciones?limit=${limit}&offset=${propuestasOffset}&historial_limit=${limit}&historial_offset=${historialOffset}`),
  proponer: (payload: { reserva_id: number; hotel_destino_id: number; motivo: string }) =>
    apiClient.post<{ id: number; estado: string }>('/api/reubicaciones', payload),
  aprobar: (id: number) => apiClient.post<{ estado: string }>(`/api/reubicaciones/${id}/aprobar`, {}),
  cargarDatosEjemplo: () => apiClient.post<{ cargados: boolean }>('/api/ocupacion/datos-de-ejemplo'),
}
