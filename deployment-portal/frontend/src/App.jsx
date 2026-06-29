import React from 'react'
import { BrowserRouter, Routes, Route, NavLink, useNavigate } from 'react-router-dom'
import { AuthProvider, useAuth } from './contexts/AuthContext.jsx'
import ProtectedRoute from './components/ProtectedRoute.jsx'
import Login from './pages/Login.jsx'
import Home from './pages/Home.jsx'
import NewRequest from './pages/NewRequest.jsx'
import TaskDetail from './pages/TaskDetail.jsx'
import History from './pages/History.jsx'
import AppHeader from './components/AppHeader.jsx'

function AppShell() {
  const { user, logout } = useAuth()
  const navigate = useNavigate()

  function handleLogout() {
    logout()
    navigate('/login')
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="sidebar-logo">🚀 DeployPortal</div>
        <div className="sidebar-sub">Automation request system</div>

        <nav className="sidebar-nav">
          <NavLink to="/" end className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}>
            🏠 Home
          </NavLink>
          <NavLink to="/request" className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}>
            ➕ New request
          </NavLink>
          <NavLink to="/history" className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}>
            📋 History
          </NavLink>
        </nav>

        <div className="sidebar-footer">
          <div className="nav-user">
            <div className="nav-user-info">
              <span className="nav-user-name">{user?.display_name}</span>
              <span className="nav-user-role">{user?.role}</span>
            </div>
            <button type="button" className="nav-logout-btn" onClick={handleLogout}>
              <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                <path d="M6 2H3a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h3M11 11l3-3-3-3M14 8H6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
              Sign out
            </button>
          </div>
        </div>
      </aside>

      <div className="main-column">
        <AppHeader />
        <main className="main-content">
          <Routes>
            <Route path="/" element={<Home />} />
            <Route path="/request" element={<NewRequest />} />
            <Route path="/tasks/:taskId" element={<TaskDetail />} />
            <Route path="/history" element={<History />} />
          </Routes>
        </main>
      </div>
    </div>
  )
}

export default function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route
            path="/*"
            element={
              <ProtectedRoute>
                <AppShell />
              </ProtectedRoute>
            }
          />
        </Routes>
      </BrowserRouter>
    </AuthProvider>
  )
}
