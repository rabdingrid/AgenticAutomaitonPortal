import React, { useEffect, useState, useCallback, useMemo } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../contexts/AuthContext.jsx'
import StatsFilterGrid from '../components/StatsFilterGrid.jsx'
import { STATUS_FILTERS, filterTasksByStatus } from '../utils/taskFilters.js'

const STATUS_BADGE = {
  pending_approval: { cls: 'badge-pending', label: 'Pending' },
  running: { cls: 'badge-running', label: 'Running' },
  queued: { cls: 'badge-queued', label: 'Queued' },
  done: { cls: 'badge-done', label: 'Resolved' },
  failed: { cls: 'badge-failed', label: 'Failed' },
  blocked: { cls: 'badge-blocked', label: 'Blocked' },
}

const SECTION_LABEL = { build: 'Build', yaml: 'YAML', db: 'DB', phrases: 'Phrases' }

export default function Home() {
  const navigate = useNavigate()
  const { user } = useAuth()
  const isDevops = user?.approval_stage === 'devops'
  const [stats, setStats] = useState(null)
  const [activity, setActivity] = useState([])
  const [tasks, setTasks] = useState([])
  const [statusFilter, setStatusFilter] = useState('all')
  const [period, setPeriod] = useState('weekly')
  const [codeFreeze, setCodeFreeze] = useState(null)
  const [freezeBusy, setFreezeBusy] = useState(false)
  const [error, setError] = useState(null)

  const load = useCallback(async () => {
    try {
      const [s, a, cf, t] = await Promise.all([
        api.getStats(period),
        api.getActivity(6, period),
        api.getCodeFreeze(),
        api.listTasks({ period }),
      ])
      setStats(s)
      setActivity(a)
      setCodeFreeze(cf)
      setTasks(t)
      setError(null)
    } catch (e) {
      setError(e.message)
    }
  }, [period])

  useEffect(() => { load() }, [load])

  const displayItems = useMemo(() => {
    if (statusFilter === 'all') return activity
    return filterTasksByStatus(tasks, statusFilter).map((task) => ({
      task_id: task.task_id,
      jira_id: task.jira_id,
      environment: task.environment,
      status: task.status,
      requested_by: task.requested_by,
      created_at: task.created_at,
      sections: (task.jobs || []).map((j) => j.section),
    }))
  }, [statusFilter, activity, tasks])

  const sectionHeading = STATUS_FILTERS[statusFilter]?.heading || 'Recent activity'

  async function handleSeed() {
    await api.seedDemo()
    load()
  }

  async function toggleCodeFreeze() {
    setFreezeBusy(true)
    try {
      const updated = await api.setCodeFreeze(!codeFreeze.enabled)
      setCodeFreeze(updated)
    } catch (e) {
      setError(e.message)
    } finally {
      setFreezeBusy(false)
    }
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

      {isDevops && codeFreeze && (
        <div className="card code-freeze-card" style={{ background: codeFreeze.enabled ? 'var(--red-light)' : 'var(--white)' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 16 }}>
            <div>
              <p className="card-title" style={{ marginBottom: 2 }}>
                {codeFreeze.enabled ? '🔒 Code Freeze: ENABLED' : '🔓 Code Freeze: Disabled'}
              </p>
              <p className="card-sub" style={{ marginBottom: 0 }}>
                {codeFreeze.enabled
                  ? 'New requests require Dev Lead → QA → DevOps approval'
                  : 'New requests require Dev Lead → DevOps approval'}
                <span style={{ color: 'var(--text-tertiary)' }}> · DevOps only</span>
              </p>
            </div>
            <button
              type="button"
              className={`toggle-switch ${codeFreeze.enabled ? 'on' : ''}`}
              onClick={toggleCodeFreeze}
              disabled={freezeBusy}
              aria-label="Toggle code freeze"
            >
              <span className="toggle-knob" />
            </button>
          </div>
        </div>
      )}

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

      <StatsFilterGrid
        stats={stats}
        activeFilter={statusFilter}
        onFilterChange={setStatusFilter}
      />

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
        <span>{sectionHeading}</span>
        <span className="section-heading-actions">
          {statusFilter !== 'all' && (
            <button
              type="button"
              className="filter-clear-link"
              onClick={() => setStatusFilter('all')}
            >
              Clear filter
            </button>
          )}
          <Link to="/history" className="section-heading-link">
            View all →
          </Link>
        </span>
      </p>

      <div className="card" style={{ padding: '0.75rem 1rem' }}>
        {displayItems.length === 0 ? (
          <div className="empty-state" style={{ padding: '2rem 1rem' }}>
            {statusFilter === 'all'
              ? (
                <>
                  No requests yet.{' '}
                  <Link to="/request" style={{ color: 'var(--blue)' }}>Create your first request</Link>
                  {' '}or load demo data above.
                </>
              )
              : `No ${STATUS_FILTERS[statusFilter]?.label.toLowerCase() || 'matching'} requests.`}
          </div>
        ) : (
          displayItems.map((item) => {
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
