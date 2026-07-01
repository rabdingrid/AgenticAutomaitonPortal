import React, { useState } from 'react'
import { api } from '../api.js'
import { useAuth, roleToStage } from '../contexts/AuthContext.jsx'

const ROLE_LABELS = {
  dev_lead: 'Development Lead',
  qa: 'QA',
  devops: 'DevOps Team',
}

function ApprovalStepper({ task, currentStage }) {
  return (
    <div className="approval-stepper">
      {task.approval_chain.map((role, i) => {
        const approval = task.approvals[role]
        const isCurrent = role === currentStage
        const approved = approval?.decision === 'approved'
        const bg = approved ? 'var(--green-light)' : isCurrent ? 'var(--blue-light)' : 'var(--slate-light)'
        const color = approved ? 'var(--green)' : isCurrent ? 'var(--blue)' : 'var(--text-tertiary)'
        return (
          <React.Fragment key={role}>
            <div className="approval-stage">
              <div className="step-dot" style={{ background: bg, color }}>
                {approved ? '✓' : i + 1}
              </div>
              <span
                className="approval-stage-label"
                style={{ color: isCurrent ? 'var(--blue)' : 'var(--text-secondary)', fontWeight: isCurrent ? 600 : 400 }}
              >
                {ROLE_LABELS[role]}
              </span>
            </div>
            {i < task.approval_chain.length - 1 && (
              <div className="step-line" style={{ background: approved ? 'var(--green)' : 'var(--border)', maxWidth: 40 }} />
            )}
          </React.Fragment>
        )
      })}
    </div>
  )
}

export default function ApprovalPanel({ task, onDecided }) {
  const { user } = useAuth()
  const [comment, setComment] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const currentStage = task.current_stage || task.approval_chain.find((role) => !task.approvals[role])
  const userStage = user?.approval_stage ?? roleToStage(user?.role)
  const canAct = !!userStage && userStage === currentStage && task.status === 'pending_approval'

  if (task.status === 'rejected') {
    const rejectedRole = task.approval_chain.find((r) => task.approvals[r]?.decision === 'rejected')
    return (
      <div className="card approval-card rejected">
        <p className="card-title">❌ Request rejected</p>
        <p className="card-sub" style={{ marginBottom: 8 }}>
          Rejected by {ROLE_LABELS[rejectedRole] || rejectedRole}
        </p>
        <div className="rejection-reason">
          <strong>Reason:</strong> {task.rejection_reason || '—'}
        </div>
      </div>
    )
  }

  if (!currentStage) return null

  async function decide(decision) {
    setError(null)
    if (decision === 'rejected' && !comment.trim()) {
      setError('A comment is required to reject.')
      return
    }
    setBusy(true)
    try {
      await api.approveTask(task.task_id, {
        role: currentStage,
        decision,
        comment: comment.trim() || null,
      })
      setComment('')
      onDecided()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  // Read-only view: developers, or any approver when it is not their turn.
  if (!canAct) {
    return (
      <div className="card approval-card">
        <p className="card-title">Awaiting approval: {ROLE_LABELS[currentStage]}</p>
        <p className="card-sub" style={{ marginBottom: 12 }}>
          {userStage
            ? `You are signed in as ${ROLE_LABELS[userStage]} — this request is waiting on ${ROLE_LABELS[currentStage]}.`
            : 'You can track the approval status here. Approvals are handled by the assigned reviewers.'}
        </p>
        {task.code_freeze_enabled && (
          <p className="card-sub" style={{ color: 'var(--red)' }}>🔒 Code freeze active — QA approval required in chain</p>
        )}
        <ApprovalStepper task={task} currentStage={currentStage} />
      </div>
    )
  }

  return (
    <div className="card approval-card">
      <p className="card-title">Awaiting your approval: {ROLE_LABELS[currentStage]}</p>
      {task.code_freeze_enabled && (
        <p className="card-sub" style={{ color: 'var(--red)' }}>🔒 Code freeze active — QA approval required in chain</p>
      )}

      <ApprovalStepper task={task} currentStage={currentStage} />

      <textarea
        placeholder="Comment (required only when rejecting)"
        value={comment}
        onChange={(e) => setComment(e.target.value)}
        rows={2}
        style={{ marginBottom: 10 }}
      />

      {error && <div className="alert alert-error" style={{ marginBottom: 10 }}>{error}</div>}

      <div style={{ display: 'flex', gap: 8 }}>
        <button type="button" className="btn btn-approve" style={{ flex: 1 }} disabled={busy} onClick={() => decide('approved')}>
          {busy ? 'Working...' : `Approve as ${ROLE_LABELS[currentStage]}`}
        </button>
        <button type="button" className="btn btn-reject" style={{ flex: 1 }} disabled={busy} onClick={() => decide('rejected')}>
          Reject
        </button>
      </div>
    </div>
  )
}

export { ROLE_LABELS }
