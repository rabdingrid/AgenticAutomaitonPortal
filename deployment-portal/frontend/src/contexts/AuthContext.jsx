import { createContext, useContext, useState, useCallback } from 'react'

const AuthContext = createContext(null)

const TOKEN_KEY = 'aap_token'
const USER_KEY = 'aap_user'

// Maps the role stored on a user to the approval-chain stage they may act on.
// Keep this in sync with ROLE_TO_APPROVAL_STAGE in backend/auth.py.
const ROLE_TO_STAGE = {
  developer: null,
  'dev lead': 'dev_lead',
  dev_lead: 'dev_lead',
  qa: 'qa',
  devops: 'devops',
}

export function roleToStage(role) {
  if (!role) return null
  return ROLE_TO_STAGE[role.trim().toLowerCase()] ?? null
}

export function AuthProvider({ children }) {
  const [token, setToken] = useState(() => localStorage.getItem(TOKEN_KEY))
  const [user, setUser] = useState(() => {
    try {
      return JSON.parse(localStorage.getItem(USER_KEY))
    } catch {
      return null
    }
  })
  const [loading, setLoading] = useState(false)

  const login = useCallback(async (email, password) => {
    setLoading(true)
    try {
      const form = new URLSearchParams({ username: email, password })
      const res = await fetch('/api/auth/token', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: form,
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || 'Login failed')
      setToken(data.access_token)
      const userObj = {
        email: data.email || email.trim().toLowerCase(),
        display_name: data.display_name,
        role: data.role,
        approval_stage: data.approval_stage ?? roleToStage(data.role),
      }
      setUser(userObj)
      localStorage.setItem(TOKEN_KEY, data.access_token)
      localStorage.setItem(USER_KEY, JSON.stringify(userObj))
      return userObj
    } finally {
      setLoading(false)
    }
  }, [])

  const logout = useCallback(() => {
    setToken(null)
    setUser(null)
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(USER_KEY)
  }, [])

  return (
    <AuthContext.Provider
      value={{
        token,
        user,
        loading,
        login,
        logout,
        isAuthenticated: !!token,
      }}
    >
      {children}
    </AuthContext.Provider>
  )
}

export const useAuth = () => useContext(AuthContext)
