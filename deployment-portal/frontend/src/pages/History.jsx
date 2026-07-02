import React, { useEffect, useState, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../contexts/AuthContext.jsx'
import RequestDetails from '../components/RequestDetails.jsx'
import ApprovalPanel from '../components/ApprovalPanel.jsx'
import { SECTION_ICONS } from '../components/OrchestratorPlan.jsx'

function CompactPlanSummary({ plan, subTasks }) {
  if (!plan?.phases?.length) return null
  const stMap = Object.fromEntries((subTasks || []).map((st) => [st.sub_task_id, st]))
  return (
    <div className="history-plan-summary">
      {plan.phases.map((phase) => (
        <div key={phase.phase} className="hps-phase">
          <span className="hps-phase-label">Phase {phase.phase}</span>
          <div className="hps-sub-tasks">
            {phase.sub_task_ids.map((stId) => {
              const st = stMap[stId]
              return (
                <span key={stId} className={`hps-chip hps-chip-${st?.status || 'queued'}`}>
                  {SECTION_ICONS[st?.section] || '🔧'} {st?.label || stId}
                </span>
              )
            })}
          </div>
        </div>
      ))}
    </div>
  )
}

const STATUS_BADGE = {
  pending_approval: { cls: 'badge-pending', label: 'Pending approval' },
  rejected: { cls: 'badge-failed', label: 'Rejected' },
  running: { cls: 'badge-running', label: 'Running' },
  queued: { cls: 'badge-queued', label: 'Queued' },
  done: { cls: 'badge-done', label: 'Resolved' },
  failed: { cls: 'badge-failed', label: 'Failed' },
  blocked: { cls: 'badge-blocked', label: 'Blocked' },
}

const SECTION_ICON = { build: 'Build', yaml: 'YAML', db: 'DB', phrases: 'Phrases' }

export default function History() {
  const navigate = useNavigate()
  const { user } = useAuth()
  const role = user?.approval_stage || 'developer'
  const [period, setPeriod] = useState('weekly')
  const [stats, setStats] = useState(null)
  const [tasks, setTasks] = useState([])
  const [approvers, setApprovers] = useState([])
  const [expandedId, setExpandedId] = useState(null)
  const [fullTasks, setFullTasks] = useState({})
  const [error, setError] = useState(null)

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

  async function toggleExpand(taskId, e) {
    e.stopPropagation()
    setExpandedId((prev) => (prev === taskId ? null : taskId))
    // DevOps sees a compact execution-plan summary — fetch the expanded task.
    if (role === 'devops' && !fullTasks[taskId]) {
      try {
        const full = await api.getTaskFull(taskId)
        setFullTasks((prev) => ({ ...prev, [taskId]: full }))
      } catch {
        /* summary is best-effort */
      }
    }
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
            const showApproval = task.status === 'pending_approval' || task.status === 'rejected'

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
                    {showApproval && <ApprovalPanel task={task} onDecided={load} />}
                    {role === 'devops' && fullTasks[task.task_id]?.orchestrator_plan?.phases?.length > 0 && (
                      <>
                        <p className="card-sub" style={{ fontWeight: 600, marginTop: 14, marginBottom: 0 }}>
                          Execution plan
                        </p>
                        <CompactPlanSummary
                          plan={fullTasks[task.task_id].orchestrator_plan}
                          subTasks={fullTasks[task.task_id].sub_tasks}
                        />
                      </>
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
