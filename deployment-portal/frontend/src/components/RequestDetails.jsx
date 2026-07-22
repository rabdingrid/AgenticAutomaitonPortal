import React from 'react'

const SECTION_META = {
  build: { icon: '🔀', label: 'Gitspace merge — Build' },
  yaml: { icon: '📄', label: 'YAML / Config' },
  db: { icon: '🗄️', label: 'DB / Liquibase' },
  phrases: { icon: '💬', label: 'Json & SchemaForms' },
}

const SUBTYPE_LABELS = {
  microservice: 'Microservice',
  portal: 'Portal',
  utility: 'Utilities',
}

export default function RequestDetails({ task, approverName }) {
  if (!task) return null

  return (
    <div className="request-details">
      <table className="kv-table">
        <tbody>
          <tr><td>Environment</td><td>{task.environment}</td></tr>
          <tr><td>Jira ID</td><td>{task.jira_id}</td></tr>
          <tr><td>Description</td><td>{task.description || '—'}</td></tr>
          <tr>
            <td>Branches</td>
            <td>
              {(task.branches && task.branches.length > 0)
                ? task.branches.map((b, i) => (
                    <div key={i}>{(b.from || '—')} → {(b.to || '—')}</div>
                  ))
                : <span>{(task.branch_from || '—')} → {(task.branch_to || '—')}</span>}
            </td>
          </tr>
          <tr><td>Approver (Dev Lead)</td><td>{approverName || task.approver_key}</td></tr>
          {task.cc_emails ? (
            <tr><td>CC</td><td>{task.cc_emails}</td></tr>
          ) : null}
          <tr><td>Code freeze at submit</td><td>{task.code_freeze_enabled ? 'Enabled' : 'Disabled'}</td></tr>
          <tr><td>Requested by</td><td>{task.requested_by}</td></tr>
          <tr><td>Submitted</td><td>{new Date(task.created_at).toLocaleString()}</td></tr>
        </tbody>
      </table>

      {task.jobs?.map((job) => {
        const meta = SECTION_META[job.section] || { icon: '🔧', label: job.section }
        return (
          <div key={job.job_id} className="request-section-block">
            <p className="request-section-title">
              {meta.icon} {meta.label}
              {job.release_branch && (
                <span className="pill" style={{ marginLeft: 8, background: 'var(--blue-light)', color: 'var(--blue)' }}>
                  release: {job.release_branch}
                </span>
              )}
              {(job.branch_from || job.branch_to) && (
                <span className="pill" style={{ marginLeft: 8, background: 'var(--blue-light)', color: 'var(--blue)' }}>
                  {(job.branch_from || '—')} → {(job.branch_to || '—')}
                </span>
              )}
            </p>
            {job.links.map((link, i) => (
              <div key={i} className="request-link-item">
                <span className="pill" style={{ background: 'var(--slate-light)', color: 'var(--text-secondary)' }}>
                  {SUBTYPE_LABELS[link.sub_type] || link.sub_type}
                </span>
                <span className="request-link-label">{link.label || link.service_key || '—'}</span>
              </div>
            ))}
          </div>
        )
      })}
    </div>
  )
}
