import { useAuth } from '../contexts/AuthContext.jsx'
import { useNavigate } from 'react-router-dom'

export default function AppHeader() {
  const { user, logout } = useAuth()
  const navigate = useNavigate()

  function handleLogout() {
    logout()
    navigate('/login')
  }

  return (
    <header className="app-header">
      <div className="app-header-spacer" />
      <div className="app-header-user">
        <div className="app-header-user-text">
          <span className="app-header-name">{user?.display_name}</span>
          <span className="app-header-role">{user?.role}</span>
        </div>
        <button type="button" className="app-header-logout" onClick={handleLogout}>
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
            <path d="M6 2H3a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h3M11 11l3-3-3-3M14 8H6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          Sign out
        </button>
      </div>
    </header>
  )
}
