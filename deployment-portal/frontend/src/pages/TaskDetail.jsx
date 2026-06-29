import React, { useEffect, useState, useCallback } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { api } from '../api.js'

const STATUS_BADGE = {
  running: { cls: 'badge-running', label: 'Running', icon: '●' },
  queued: { cls: 'badge-queued', label: 'Queued', icon: '○' },
  done: { cls: 'badge-done', label: 'Done', icon: '✓' },
  failed: { cls: 'badge-failed', label: 'Failed', icon: '✕' },
  blocked: { cls: 'badge-blocked', label: 'Blocked', icon: '⚠' },
}

const JOB_ICON = { microservice: '📦', yaml: '📄', db: '🗄️', portal: '🖥️', script: '⚙️' }

function Badge({ status }) {
  const s = STATUS_BADGE[status] || STATUS_BADGE.queued
  return <span className={`badge ${s.cls}`}>{s.icon} {s.label}</span>
}

export default function TaskDetail() {
  const { taskId } = useParams()
  const navigate = useNavigate()
  const [task, setTask] = useState(null)
  const [selectedJob, setSelectedJob] = useState(null)
  const [error, setError] = useState(null)

  const load = useCallback(async () => {
    try {
      const t = await api.getTask(taskId)
      setTask(t)
    } catch (e) {
      setError(e.message)
    }
  }, [taskId])

  useEffect(() => {
    load()
    const interval = setInterval(load, 3000) // poll — stands in for websockets/SSE later
    return () => clearInterval(interval)
  }, [load])

  if (error) return <div className="alert alert-error">{error}</div>
  if (!task) return <div className="empty-state">Loading task...</div>

  const stepperStates = task.jobs.map((j) => j.status)

  async function simulateAdvance(jobId) {
    await api.updateJobStatus(jobId, 'done', 'Manually marked done (demo control)')
    load()
  }

  async function simulateFail(jobId) {
    await api.updateJobStatus(jobId, 'failed', 'Manually marked failed (demo control)')
    load()
  }

  return (
    <div>
      <div className="page-header">
        <p className="page-sub" style={{ marginBottom: 4 }}>
          <span style={{ cursor: 'pointer', color: 'var(--blue)' }} onClick={() => navigate('/history')}>
            ← Back to history
          </span>
        </p>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
          <div>
            <h1 className="page-title" style={{ marginBottom: 2 }}>
              {task.task_id} · {task.jira_key}
            </h1>
            <p className="page-sub">{task.description}</p>
          </div>
          <Badge status={task.status} />
        </div>
        <div style={{ display: 'flex', gap: 16, fontSize: 12, color: 'var(--text-secondary)', marginTop: 10 }}>
          <span>👤 {task.requested_by}</span>
          <span>🕒 {new Date(task.created_at).toLocaleString()}</span>
          <span>🌐 {task.environment}</span>
          <span>⚡ {task.priority}</span>
        </div>
      </div>

      <div className="card">
        <p className="card-title">Master orchestrator — job sequence</p>

        <div className="stepper">
          {task.jobs.map((j, i) => {
            const isDone = j.status === 'done'
            const isRunning = j.status === 'running'
            const isFailed = j.status === 'failed'
            const bg = isDone ? 'var(--green-light)' : isRunning ? 'var(--blue-light)' : isFailed ? 'var(--red-light)' : 'var(--slate-light)'
            const color = isDone ? 'var(--green)' : isRunning ? 'var(--blue)' : isFailed ? 'var(--red)' : 'var(--text-tertiary)'
            return (
              <React.Fragment key={j.job_id}>
                <div className="step-dot" style={{ background: bg, color }}>
                  {isDone ? '✓' : isFailed ? '✕' : i + 1}
                </div>
                {i < task.jobs.length - 1 && (
                  <div className="step-line" style={{ background: isDone ? 'var(--green)' : 'var(--border)' }} />
                )}
              </React.Fragment>
            )
          })}
        </div>

        {task.jobs.map((job) => (
          <div
            key={job.job_id}
            className="task-row"
            onClick={() => setSelectedJob(selectedJob === job.job_id ? null : job.job_id)}
          >
            <span style={{ fontSize: 18 }}>{JOB_ICON[job.job_type] || '🔧'}</span>
            <div className="task-row-main">
              <p className="task-row-title">{job.job_id} · {job.fields.service || job.fields.portal_name || job.fields.utility_name || job.job_type}</p>
              <p className="task-row-meta">{job.jenkins_job} · agent: {job.agent}</p>
            </div>
            <Badge status={job.status} />
          </div>
        ))}

        {selectedJob && (
          <JobExpanded
            job={task.jobs.find((j) => j.job_id === selectedJob)}
            onAdvance={() => simulateAdvance(selectedJob)}
            onFail={() => simulateFail(selectedJob)}
          />
        )}
      </div>

      <div className="card" style={{ background: 'var(--slate-light)', border: 'none' }}>
        <p style={{ fontSize: 12, color: 'var(--text-secondary)', margin: 0 }}>
          ℹ️ This task counts as <strong>one entry</strong> in the queue. All {task.jobs.length} job(s) run sequentially per
          orchestrator rules, dispatched to their specialist agents.
        </p>
      </div>
    </div>
  )
}

function JobExpanded({ job, onAdvance, onFail }) {
  if (!job) return null

  return (
    <div className="card" style={{ marginTop: 12, background: 'var(--slate-light)' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 10 }}>
        <p className="card-title" style={{ marginBottom: 0 }}>{job.job_id} details</p>
        <Badge status={job.status} />
      </div>

      <table className="kv-table" style={{ marginBottom: 14 }}>
        <tbody>
          {Object.entries(job.fields).map(([k, v]) => (
            <tr key={k}><td>{k}</td><td>{v}</td></tr>
          ))}
          <tr><td>Jenkins job</td><td>{job.jenkins_job}</td></tr>
          <tr><td>Agent</td><td>{job.agent}</td></tr>
          {job.depends_on?.length > 0 && (
            <tr><td>Depends on</td><td>{job.depends_on.join(', ')}</td></tr>
          )}
        </tbody>
      </table>

      <p className="card-sub" style={{ marginBottom: 8, fontWeight: 600 }}>Steps</p>
      {job.steps.map((step, i) => (
        <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8, fontSize: 12.5 }}>
          <span>{step.status === 'done' ? '✅' : step.status === 'running' ? '🔄' : step.status === 'failed' ? '❌' : '⚪'}</span>
          <span style={{ opacity: step.status === 'queued' ? 0.5 : 1 }}>{step.label}</span>
        </div>
      ))}

      <p className="card-sub" style={{ marginBottom: 8, fontWeight: 600, marginTop: 14 }}>Jenkins parameters (sent payload)</p>
      <div className="log-box" style={{ maxHeight: 160 }}>
        {JSON.stringify(job.jenkins_params, null, 2)}
      </div>

      <p className="card-sub" style={{ marginBottom: 8, fontWeight: 600, marginTop: 14 }}>Logs</p>
      <div className="log-box">
        {job.logs.join('\n')}
      </div>

      {job.status === 'running' && (
        <div style={{ display: 'flex', gap: 8, marginTop: 14 }}>
          <button className="btn" style={{ flex: 1, color: 'var(--green)' }} onClick={onAdvance}>
            ✓ Simulate: mark done
          </button>
          <button className="btn" style={{ flex: 1, color: 'var(--red)' }} onClick={onFail}>
            ✕ Simulate: mark failed
          </button>
        </div>
      )}
    </div>
  )
}
