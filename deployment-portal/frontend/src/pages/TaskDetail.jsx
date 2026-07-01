import React, { useEffect, useState, useCallback } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { api } from '../api.js'
import RequestDetails from '../components/RequestDetails.jsx'
import ApprovalPanel from '../components/ApprovalPanel.jsx'
import ValidationReport from '../components/ValidationReport.jsx'

const STATUS_BADGE = {
  pending_approval: { cls: 'badge-pending', label: 'Pending approval', icon: '⏳' },
  rejected: { cls: 'badge-failed', label: 'Rejected', icon: '✕' },
  running: { cls: 'badge-running', label: 'Running', icon: '●' },
  queued: { cls: 'badge-queued', label: 'Queued', icon: '○' },
  done: { cls: 'badge-done', label: 'Done', icon: '✓' },
  failed: { cls: 'badge-failed', label: 'Failed', icon: '✕' },
  blocked: { cls: 'badge-blocked', label: 'Blocked', icon: '⚠' },
}

const SECTION_META = {
  build: { icon: '🔀', label: 'Build / Gitspace merge' },
  yaml: { icon: '📄', label: 'YAML / Config' },
  db: { icon: '🗄️', label: 'DB / Liquibase' },
  phrases: { icon: '💬', label: 'Phrases' },
}

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
  const [revalidating, setRevalidating] = useState(false)

  const load = useCallback(async () => {
    try {
      const t = await api.getTask(taskId)
      setTask(t)
      setError(null)
    } catch (e) {
      setError(e.message)
    }
  }, [taskId])

  useEffect(() => {
    load()
    const interval = setInterval(load, 3000)
    return () => clearInterval(interval)
  }, [load])

  if (error && !task) return <div className="alert alert-error">{error}</div>
  if (!task) return <div className="empty-state">Loading task...</div>

  const isPending = task.status === 'pending_approval'
  const isRejected = task.status === 'rejected'
  const orchestratorStarted = !isPending && !isRejected
  const approverName = task.approver_name || task.approver_key

  async function handleRevalidate() {
    setRevalidating(true)
    try {
      await api.revalidateTask(taskId)
      await load()
    } catch (e) {
      setError(e.message)
    } finally {
      setRevalidating(false)
    }
  }

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
              {task.task_id} · {task.jira_id}
            </h1>
            <p className="page-sub">{task.description || 'No description'}</p>
          </div>
          <Badge status={task.status} />
        </div>
      </div>

      <div className="card">
        <p className="card-title">Request details</p>
        <RequestDetails task={task} approverName={approverName} />
      </div>

      <ValidationReport task={task} onRevalidate={handleRevalidate} revalidating={revalidating} />

      {(isPending || isRejected) && <ApprovalPanel task={task} onDecided={load} />}

      {error && <div className="alert alert-error">{error}</div>}

      {orchestratorStarted && (
        <div className="card">
          <p className="card-title">Orchestrator — job sequence</p>

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

          {task.jobs.map((job) => {
            const meta = SECTION_META[job.section] || { icon: '🔧', label: job.section }
            return (
              <div
                key={job.job_id}
                className="task-row"
                onClick={() => setSelectedJob(selectedJob === job.job_id ? null : job.job_id)}
              >
                <span style={{ fontSize: 18 }}>{meta.icon}</span>
                <div className="task-row-main">
                  <p className="task-row-title">{job.job_id} · {meta.label}</p>
                  <p className="task-row-meta">
                    {job.links.length} link(s) · agent: {job.agent}
                  </p>
                </div>
                <Badge status={job.status} />
              </div>
            )
          })}

          {selectedJob && (
            <JobExpanded
              job={task.jobs.find((j) => j.job_id === selectedJob)}
              onAdvance={() => simulateAdvance(selectedJob)}
              onFail={() => simulateFail(selectedJob)}
            />
          )}
        </div>
      )}
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

      <p className="card-sub" style={{ marginBottom: 8, fontWeight: 600 }}>Selected services</p>
      <table className="kv-table" style={{ marginBottom: 14 }}>
        <tbody>
          {job.links.map((link, i) => (
            <tr key={i}>
              <td>{link.sub_type}</td>
              <td>
                <div style={{ fontWeight: 500 }}>{link.label || link.service_key || '—'}</div>
              </td>
            </tr>
          ))}
          {job.release_branch && <tr><td>Release branch</td><td>{job.release_branch}</td></tr>}
          <tr><td>Agent</td><td>{job.agent}</td></tr>
        </tbody>
      </table>

      <p className="card-sub" style={{ marginBottom: 8, fontWeight: 600 }}>Steps</p>
      {job.steps.map((step, i) => (
        <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8, fontSize: 12.5 }}>
          <span>{step.status === 'done' ? '✅' : step.status === 'running' ? '🔄' : step.status === 'failed' ? '❌' : '⚪'}</span>
          <span style={{ opacity: step.status === 'queued' ? 0.5 : 1 }}>{step.label}</span>
        </div>
      ))}

      <p className="card-sub" style={{ marginBottom: 8, fontWeight: 600, marginTop: 14 }}>Logs</p>
      <div className="log-box">
        {job.logs.join('\n')}
      </div>

      {job.status === 'running' && (
        <div style={{ display: 'flex', gap: 8, marginTop: 14 }}>
          <button type="button" className="btn" style={{ flex: 1, color: 'var(--green)' }} onClick={onAdvance}>
            ✓ Simulate: mark done
          </button>
          <button type="button" className="btn" style={{ flex: 1, color: 'var(--red)' }} onClick={onFail}>
            ✕ Simulate: mark failed
          </button>
        </div>
      )}
    </div>
  )
}
