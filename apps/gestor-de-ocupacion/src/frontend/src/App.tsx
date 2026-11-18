import { Navigate, Route, Routes } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { AssignmentTurnedInOutlined, HotelOutlined, SwapHorizOutlined } from '@mui/icons-material'
import Layout, { type LayoutNavItem } from './components/Layout'
import { useAuth } from './auth/AuthContext'
import LoginPage from './auth/LoginPage'
import { ProtectedRoute } from './auth/ProtectedRoute'
import OcupacionPage from './pages/OcupacionPage'
import HotelPage from './pages/HotelPage'
import ReubicacionesPage from './pages/ReubicacionesPage'
import { rutasDelPerfilado, useEntradasDelPerfilado } from './wai/perfilado'

function AplicacionProtegida() {
  const { t } = useTranslation()
  const { email, logout } = useAuth()
  const perfilado = useEntradasDelPerfilado()
  const operaciones: LayoutNavItem[] = [
    { key: 'ocupacion', path: '/', label: t('nav.occupancy'), icon: <HotelOutlined /> },
    { key: 'reubicaciones', path: '/reubicaciones', label: t('nav.relocations'), icon: <SwapHorizOutlined /> },
  ]
  const acceso: LayoutNavItem[] = perfilado.map((item) => ({
    ...item,
    icon: item.key === 'perfilado-usuarios' ? <AssignmentTurnedInOutlined /> : item.icon,
  }))

  return (
    <Layout
      brand={{ short: 'HV', title: 'Hoteles Valencia', tagline: 'Ocupación y estancia' }}
      navItems={[...operaciones, ...acceso]}
      navGroups={[
        { label: t('nav.operations'), keys: operaciones.map((item) => item.key) },
        { label: t('nav.access'), keys: acceso.map((item) => item.key) },
      ]}
      user={{ email: email || 'usuario@wai', onLogout: logout }}
    >
      <Routes>
        <Route path="/" element={<OcupacionPage />} />
        <Route path="/hoteles/:hotelId" element={<HotelPage />} />
        <Route path="/reubicaciones" element={<ReubicacionesPage />} />
        {rutasDelPerfilado().map((route) => (
          <Route key={route.path} path={route.path} element={route.element} />
        ))}
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Layout>
  )
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="*" element={<ProtectedRoute><AplicacionProtegida /></ProtectedRoute>} />
    </Routes>
  )
}
