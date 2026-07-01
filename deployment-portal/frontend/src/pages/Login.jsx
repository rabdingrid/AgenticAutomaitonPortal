import { useState } from 'react'
import { useNavigate, useLocation } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext.jsx'

function BrandMark() {
  return (
    <svg className="login-mark" width="44" height="44" viewBox="0 0 44 44" fill="none" aria-hidden="true">
      <rect x="2" y="2" width="40" height="40" rx="12" fill="rgba(13, 148, 136, 0.15)" stroke="rgba(94, 234, 212, 0.5)" strokeWidth="1.5" />
      <path d="M14 22h16M22 14v16" stroke="#5EEAD4" strokeWidth="2" strokeLinecap="round" />
      <circle cx="22" cy="22" r="4" fill="#0D9488" />
    </svg>
  )
}

export default function Login() {
  const { login } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const from = location.state?.from?.pathname || '/'

  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [showPass, setShowPass] = useState(false)

  async function handleSubmit(e) {
    e.preventDefault()
    setError('')
    if (!email.trim() || !password) {
      setError('Email and password are required.')
      return
    }
    setLoading(true)
    try {
      await login(email.trim(), password)
      navigate(from, { replace: true })
    } catch (err) {
      setError(err.message || 'Login failed. Check your credentials.')
    } finally {
      setLoading(false)
    }
  }

  function handleMicrosoftLogin() {
    alert('Microsoft SSO coming soon — not yet configured.')
  }

  return (
    <div className="login-root">
      <div className="login-bg-shape login-bg-shape-1" aria-hidden="true" />
      <div className="login-bg-shape login-bg-shape-2" aria-hidden="true" />

      <div className="login-wrap">
        <div className="login-card">
          <div className="login-brand">
            <BrandMark />
            <h1 className="login-brand-title">Internal Automation Portal</h1>
            <p className="login-brand-sub">Deployment automation</p>
          </div>

          <div className="login-body">
            <div className="login-card-header">
              <h2 className="login-heading">Sign in</h2>
              <p className="login-subheading">Use your work email to continue</p>
            </div>

            {error && (
              <div className="login-error alert alert-error" role="alert">
                {error}
              </div>
            )}

            <form onSubmit={handleSubmit} className="login-form" noValidate>
              <div className="field-group">
                <label htmlFor="email" className="field-label">Email</label>
                <input
                  id="email"
                  type="email"
                  autoComplete="email"
                  autoFocus
                  spellCheck={false}
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="you@company.com"
                  disabled={loading}
                />
              </div>

              <div className="field-group">
                <label htmlFor="password" className="field-label">Password</label>
                <div className="login-password-wrap">
                  <input
                    id="password"
                    type={showPass ? 'text' : 'password'}
                    autoComplete="current-password"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    placeholder="••••••••"
                    disabled={loading}
                  />
                  <button
                    type="button"
                    className="login-show-pass"
                    onClick={() => setShowPass((v) => !v)}
                    aria-label={showPass ? 'Hide password' : 'Show password'}
                  >
                    {showPass ? 'Hide' : 'Show'}
                  </button>
                </div>
              </div>

              <button type="submit" className="btn btn-block login-btn-primary" disabled={loading}>
                {loading ? <span className="login-spinner" /> : 'Sign in'}
              </button>

              <a
                href="/forgot-password"
                className="login-forgot"
                onClick={(e) => {
                  e.preventDefault()
                  alert('Contact your admin to reset your password.')
                }}
              >
                Forgot password?
              </a>
            </form>

            <div className="login-divider">
              <span>or</span>
            </div>

            <button className="btn login-btn-ms btn-block" onClick={handleMicrosoftLogin} type="button">
              <svg width="20" height="20" viewBox="0 0 21 21" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
                <rect x="1" y="1" width="9" height="9" fill="#f25022" />
                <rect x="11" y="1" width="9" height="9" fill="#7fba00" />
                <rect x="1" y="11" width="9" height="9" fill="#00a4ef" />
                <rect x="11" y="11" width="9" height="9" fill="#ffb900" />
              </svg>
              Sign in with Microsoft
            </button>

            <p className="login-ms-note">Microsoft SSO via Entra ID — coming soon</p>
          </div>
        </div>
      </div>
    </div>
  )
}
