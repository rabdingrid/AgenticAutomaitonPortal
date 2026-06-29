import React, { useEffect, useState, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api.js'

const STATUS_BADGE = {
  running: { cls: 'badge-running', label: 'Running' },
  queued: { cls: 'badge-queued', label: 'Queued' },
  done: { cls: 'badge-done', label: 'Resolved' },
  failed: { cls: 'badge-failed', label: 'Failed' },
  blocked: { cls: 'badge-blocked', label: 'Blocked' },
}

const JOB_ICON = { microservice: 'MS', yaml: 'YAML', db: 'DB', portal: 'Portal', script: 'Script' }

export default function History() {
  const navigate = useNavigate()
  const [period, setPeriod] = useState('weekly')
  const [view, setView] = useState('all')
  const [stats, setStats] = useState(null)
  const [tasks, setTasks] = useState([])
  const [error, setError] = useState(null)

  const load = useCallback(async () => {
    try {
      const [s, t] = await Promise.all([
        api.getStats(period),
        api.listTasks(
          view === 'mine'
            ? { requested_by: 'A. Sharma', period }
            : { period },
        ),
      ])
      setStats(s)
      setTasks(t)
    } catch (e) {
      setError(e.message)
    }
  }, [period, view])

  useEffect(() => { load() }, [load])

  async function handleSeed() {
    await api.seedDemo()
    load()
  }

  async function handleReset() {
    await api.resetDemo()
    load()
  }

  return (
    <div>
      <div className="page-header">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
          <div>
            <h1 className="page-title">History</h1>
            <p className="page-sub">All deployment requests and their current status.</p>
          </div>
          <div style={{ display: 'flex', gap: 8 }}>
            <button className="btn" onClick={handleSeed}>🌱 Seed demo data</button>
            <button className="btn" onClick={handleReset}>🗑 Reset</button>
          </div>
        </div>
      </div>

      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 14 }}>
        <div className="period-toggle">
          {['daily', 'weekly', 'monthly'].map((p) => (
            <button
              key={p}
              className={`period-btn ${period === p ? 'active' : ''}`}
              onClick={() => setPeriod(p)}
            >
              {p[0].toUpperCase() + p.slice(1)}
            </button>
          ))}
        </div>
        <select value={view} onChange={(e) => setView(e.target.value)} style={{ width: 200 }}>
          <option value="mine">My tasks</option>
          <option value="all">All tasks (approver view)</option>
        </select>
      </div>

      {stats && (
        <div className="stats-grid">
          <div className="stat-box">
            <div className="stat-num">{stats.total}</div>
            <div className="stat-label">Total this {period.replace('ly', '')}</div>
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

      {error && <div className="alert alert-error">{error}</div>}

      <div className="card">
        <p className="card-title">Recent tasks</p>

        {tasks.length === 0 ? (
          <div className="empty-state">
            No tasks yet. Click "Seed demo data" above, or create a new request.
          </div>
        ) : (
          tasks.map((task) => {
            const badge = STATUS_BADGE[task.status] || STATUS_BADGE.queued
            return (
              <div key={task.task_id} className="task-row" onClick={() => navigate(`/tasks/${task.task_id}`)}>
                <span className={`badge ${badge.cls}`} style={{ minWidth: 76, justifyContent: 'center' }}>
                  {badge.label}
                </span>
                <div className="task-row-main">
                  <p className="task-row-title">{task.task_id} · {task.jira_key}</p>
                  <p className="task-row-meta">{task.requested_by} · {new Date(task.created_at).toLocaleString()}</p>
                </div>
                <div>
                  {task.jobs.map((j) => (
                    <span
                      key={j.job_id}
                      className="pill"
                      style={{
                        background: j.status === 'done' ? 'var(--green-light)' : j.status === 'failed' ? 'var(--red-light)' : j.status === 'running' ? 'var(--blue-light)' : 'var(--slate-light)',
                        color: j.status === 'done' ? 'var(--green)' : j.status === 'failed' ? 'var(--red)' : j.status === 'running' ? 'var(--blue)' : 'var(--text-tertiary)',
                      }}
                    >
                      {JOB_ICON[j.job_type] || j.job_type}
                      {j.status === 'done' ? ' ✓' : j.status === 'failed' ? ' ✕' : j.status === 'running' ? ' ●' : ''}
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
