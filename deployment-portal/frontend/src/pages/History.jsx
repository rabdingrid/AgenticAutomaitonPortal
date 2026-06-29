import React, { useEffect, useState, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api.js'
import RequestDetails from '../components/RequestDetails.jsx'

const STATUS_BADGE = {
  pending_approval: { cls: 'badge-pending', label: 'Pending approval' },
  running: { cls: 'badge-running', label: 'Running' },
  queued: { cls: 'badge-queued', label: 'Queued' },
  done: { cls: 'badge-done', label: 'Resolved' },
  failed: { cls: 'badge-failed', label: 'Failed' },
  blocked: { cls: 'badge-blocked', label: 'Blocked' },
}

const SECTION_ICON = { build: 'Build', yaml: 'YAML', db: 'DB' }

export default function History() {
  const navigate = useNavigate()
  const [period, setPeriod] = useState('weekly')
  const [stats, setStats] = useState(null)
  const [tasks, setTasks] = useState([])
  const [approvers, setApprovers] = useState([])
  const [expandedId, setExpandedId] = useState(null)
  const [error, setError] = useState(null)
  const [approving, setApproving] = useState(null)

  const approverMap = Object.fromEntries(approvers.map((a) => [a.key, a.name]))

  const load = useCallback(async () => {
    try {
      const [s, t, appr] = await Promise.all([
        api.getStats(period),
        api.listTasks(),
        api.getApprovers(),
      ])
      setStats(s)
      setTasks(t)
      setApprovers(appr)
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

  async function handleReset() {
    await api.resetDemo()
    setExpandedId(null)
    load()
  }

  async function handleApprove(taskId, role, e) {
    e.stopPropagation()
    setApproving(`${taskId}-${role}`)
    try {
      await api.approveTask(taskId, role)
      await load()
    } catch (err) {
      setError(err.message)
    } finally {
      setApproving(null)
    }
  }

  function toggleExpand(taskId, e) {
    e.stopPropagation()
    setExpandedId((prev) => (prev === taskId ? null : taskId))
  }

  return (
    <div>
      <div className="page-header">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
          <div>
            <h1 className="page-title">History</h1>
            <p className="page-sub">All deployment requests — expand any row to see full request details.</p>
          </div>
          <div style={{ display: 'flex', gap: 8 }}>
            <button type="button" className="btn" onClick={handleSeed}>🌱 Seed demo data</button>
            <button type="button" className="btn" onClick={handleReset}>🗑 Reset</button>
          </div>
        </div>
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
            <div className="stat-num" style={{ color: 'var(--amber)' }}>{stats.pending ?? 0}</div>
            <div className="stat-label">Pending approval</div>
          </div>
          <div className="stat-box">
            <div className="stat-num" style={{ color: 'var(--green)' }}>{stats.resolved}</div>
            <div className="stat-label">Resolved</div>
          </div>
          <div className="stat-box">
            <div className="stat-num" style={{ color: 'var(--blue)' }}>{stats.in_progress}</div>
            <div className="stat-label">In progress</div>
          </div>
        </div>
      )}

      {error && <div className="alert alert-error">{error}</div>}

      <div className="card">
        <p className="card-title">All tasks</p>

        {tasks.length === 0 ? (
          <div className="empty-state">
            No tasks yet. Click &quot;Seed demo data&quot; above, or create a new request.
          </div>
        ) : (
          tasks.map((task) => {
            const badge = STATUS_BADGE[task.status] || STATUS_BADGE.queued
            const isExpanded = expandedId === task.task_id
            const approverName = approverMap[task.approver_key] || task.approver_key
            const isPending = task.status === 'pending_approval'

            return (
              <div key={task.task_id} className="history-item">
                <div
                  className="task-row"
                  onClick={() => navigate(`/tasks/${task.task_id}`)}
                >
                  <span className={`badge ${badge.cls}`} style={{ minWidth: 110, justifyContent: 'center' }}>
                    {badge.label}
                  </span>
                  <div className="task-row-main">
                    <p className="task-row-title">{task.task_id} · {task.jira_id}</p>
                    <p className="task-row-meta">
                      {task.environment} · {task.branch_from || '—'} → {task.branch_to || '—'} · {new Date(task.created_at).toLocaleString()}
                    </p>
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
                        {SECTION_ICON[j.section] || j.section}
                      </span>
                    ))}
                  </div>
                  <button
                    type="button"
                    className="btn-expand"
                    onClick={(e) => toggleExpand(task.task_id, e)}
                    title={isExpanded ? 'Collapse' : 'Show full details'}
                  >
                    {isExpanded ? '▲' : '▼'}
                  </button>
                </div>

                {isExpanded && (
                  <div className="history-expanded" onClick={(e) => e.stopPropagation()}>
                    <RequestDetails task={task} approverName={approverName} />

                    {isPending && (
                      <div className="approval-panel">
                        <p className="card-sub" style={{ marginBottom: 10, fontWeight: 600 }}>
                          Pending approval — both signatures required before orchestrator starts
                        </p>
                        <div className="approval-buttons">
                          <button
                            type="button"
                            className="btn btn-approve-approver"
                            disabled={task.approver_approved || approving}
                            onClick={(e) => handleApprove(task.task_id, 'approver', e)}
                          >
                            {task.approver_approved ? '✓ ' : ''}
                            {approving === `${task.task_id}-approver` ? 'Approving...' : `Approve as ${approverName}`}
                          </button>
                          <button
                            type="button"
                            className="btn btn-approve-devops"
                            disabled={task.devops_approved || approving}
                            onClick={(e) => handleApprove(task.task_id, 'devops', e)}
                          >
                            {task.devops_approved ? '✓ ' : ''}
                            {approving === `${task.task_id}-devops` ? 'Approving...' : 'Approve as DevOps team'}
                          </button>
                        </div>
                        <div className="approval-status-row">
                          <span className={task.approver_approved ? 'approval-check done' : 'approval-check'}>
                            {task.approver_approved ? '✓' : '○'} Approver
                          </span>
                          <span className={task.devops_approved ? 'approval-check done' : 'approval-check'}>
                            {task.devops_approved ? '✓' : '○'} DevOps
                          </span>
                        </div>
                      </div>
                    )}
                  </div>
                )}
              </div>
            )
          })
        )}
      </div>
    </div>
  )
}
