import React, { useEffect, useState, useCallback } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../api.js'

const STATUS_BADGE = {
  pending_approval: { cls: 'badge-pending', label: 'Pending' },
  running: { cls: 'badge-running', label: 'Running' },
  queued: { cls: 'badge-queued', label: 'Queued' },
  done: { cls: 'badge-done', label: 'Resolved' },
  failed: { cls: 'badge-failed', label: 'Failed' },
  blocked: { cls: 'badge-blocked', label: 'Blocked' },
}

const SECTION_LABEL = { build: 'Build', yaml: 'YAML', db: 'DB' }

export default function Home() {
  const navigate = useNavigate()
  const [stats, setStats] = useState(null)
  const [activity, setActivity] = useState([])
  const [period, setPeriod] = useState('weekly')
  const [error, setError] = useState(null)

  const load = useCallback(async () => {
    try {
      const [s, a] = await Promise.all([
        api.getStats(period),
        api.getActivity(6),
      ])
      setStats(s)
      setActivity(a)
      setError(null)
    } catch (e) {
      setError(e.message)
    }
  }, [period])

  useEffect(() => { load() }, [load])

  async function handleSeed() {
    await api.seedDemo()
    load()
  }

  return (
    <div>
      <div className="hero">
        <p className="hero-eyebrow">Deployment automation</p>
        <h1 className="hero-title">Welcome to DeployPortal</h1>
        <p className="hero-sub">
          Submit structured deployment requests with Gitspace merge links, YAML config updates,
          and DB migrations — all tracked in one place.
        </p>
        <Link to="/request" className="hero-cta">
          ➕ New deployment request
        </Link>
      </div>

      <div className="period-toggle">
        {['daily', 'weekly', 'monthly'].map((p) => (
          <button
            key={p}
            type="button"
            className={`period-btn ${period === p ? 'active' : ''}`}
            onClick={() => setPeriod(p)}
          >
            {p[0].toUpperCase() + p.slice(1)}
          </button>
        ))}
      </div>

      {stats && (
        <div className="stats-grid">
          <div className="stat-box">
            <div className="stat-num">{stats.total}</div>
            <div className="stat-label">Total requests</div>
          </div>
          <div className="stat-box">
            <div className="stat-num" style={{ color: 'var(--green)' }}>{stats.resolved}</div>
            <div className="stat-label">Resolved</div>
          </div>
          <div className="stat-box">
            <div className="stat-num" style={{ color: 'var(--blue)' }}>{stats.in_progress}</div>
            <div className="stat-label">In progress</div>
          </div>
          <div className="stat-box">
            <div className="stat-num" style={{ color: 'var(--red)' }}>{stats.blocked}</div>
            <div className="stat-label">Failed / blocked</div>
          </div>
        </div>
      )}

      <div className="quick-actions">
        <Link to="/request" className="quick-tile">
          <span className="quick-tile-icon">📝</span>
          <p className="quick-tile-title">New request</p>
          <p className="quick-tile-sub">Paste Gitspace links and submit</p>
        </Link>
        <Link to="/history" className="quick-tile">
          <span className="quick-tile-icon">📋</span>
          <p className="quick-tile-title">View history</p>
          <p className="quick-tile-sub">All tasks and their status</p>
        </Link>
        <button type="button" className="quick-tile" onClick={handleSeed} style={{ border: '1px solid var(--border)', textAlign: 'left' }}>
          <span className="quick-tile-icon">🌱</span>
          <p className="quick-tile-title">Load demo data</p>
          <p className="quick-tile-sub">Seed sample tasks for testing</p>
        </button>
      </div>

      {error && <div className="alert alert-error">{error}</div>}

      <p className="section-heading">
        <span>Recent activity</span>
        <Link to="/history" style={{ fontSize: 12, color: 'var(--blue)', textDecoration: 'none', textTransform: 'none', letterSpacing: 0 }}>
          View all →
        </Link>
      </p>

      <div className="card" style={{ padding: '0.75rem 1rem' }}>
        {activity.length === 0 ? (
          <div className="empty-state" style={{ padding: '2rem 1rem' }}>
            No requests yet.{' '}
            <Link to="/request" style={{ color: 'var(--blue)' }}>Create your first request</Link>
            {' '}or load demo data above.
          </div>
        ) : (
          activity.map((item) => {
            const badge = STATUS_BADGE[item.status] || STATUS_BADGE.queued
            return (
              <div
                key={item.task_id}
                className="task-row"
                onClick={() => navigate(`/tasks/${item.task_id}`)}
              >
                <span className={`badge ${badge.cls}`} style={{ minWidth: 76, justifyContent: 'center' }}>
                  {badge.label}
                </span>
                <div className="task-row-main">
                  <p className="task-row-title">{item.task_id} · {item.jira_id}</p>
                  <p className="task-row-meta">
                    {item.environment} · {item.requested_by} · {new Date(item.created_at).toLocaleString()}
                  </p>
                </div>
                <div>
                  {item.sections.map((sec) => (
                    <span key={sec} className="pill" style={{ background: 'var(--blue-light)', color: 'var(--blue)' }}>
                      {SECTION_LABEL[sec] || sec}
                    </span>
                  ))}
                </div>
              </div>
            )
          })
        )}
      </div>
    </div>
  )
}
