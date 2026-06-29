import React, { useState, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api.js'

const JOB_DEFS = [
  {
    key: 'microservice',
    icon: '📦',
    label: 'Microservice build & merge',
    order: 4,
    fields: [
      { name: 'service', label: 'Service name', placeholder: 'AccountService' },
      { name: 'merge_url', label: 'Merge request URL', placeholder: 'gitspace.../!2814' },
      { name: 'branch', label: 'Branch', placeholder: 'feature/billing-rule' },
    ],
  },
  {
    key: 'yaml',
    icon: '📄',
    label: 'YAML / config update',
    order: 2,
    fields: [
      { name: 'config_files', label: 'Config files (comma separated)', placeholder: 'parameters.yml, billing-rules.yml' },
    ],
  },
  {
    key: 'db',
    icon: '🗄️',
    label: 'DB / Liquibase script',
    order: 3,
    fields: [
      { name: 'script_url', label: 'Script artifact URL', placeholder: 'gitspace.../db/changelog.sql' },
      { name: 'run_order', label: 'Run order', type: 'select', options: ['Before microservice', 'After microservice'] },
    ],
  },
  {
    key: 'portal',
    icon: '🖥️',
    label: 'Portal deploy',
    order: 5,
    fields: [
      { name: 'portal_name', label: 'Portal name', type: 'select', options: ['Admin Portal', 'Titan eCommerce', 'CMS'] },
    ],
  },
  {
    key: 'script',
    icon: '⚙️',
    label: 'Run script / utility',
    order: 1,
    fields: [
      { name: 'utility_name', label: 'Utility name', placeholder: 'e.g. cache-flush, reindex' },
    ],
  },
]

export default function NewRequest() {
  const navigate = useNavigate()
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(null)

  const [form, setForm] = useState({
    jira_key: 'TRB-19204',
    environment: 'INTEG',
    priority: 'Normal',
    description: 'Account service update + config change for new billing rule',
    requested_by: 'A. Sharma',
  })

  const [enabledJobs, setEnabledJobs] = useState({ microservice: true, yaml: true, db: false, portal: false, script: false })
  const [jobFields, setJobFields] = useState({
    microservice: { service: 'AccountService', merge_url: 'gitspace.../!2814', branch: 'feature/billing-rule' },
    yaml: { config_files: 'parameters.yml, billing-rules.yml' },
    db: { run_order: 'Before microservice' },
    portal: { portal_name: 'Admin Portal' },
    script: {},
  })

  const orderedActiveJobs = useMemo(() => {
    return JOB_DEFS
      .filter((j) => enabledJobs[j.key])
      .sort((a, b) => a.order - b.order)
  }, [enabledJobs])

  function toggleJob(key) {
    setEnabledJobs((prev) => ({ ...prev, [key]: !prev[key] }))
  }

  function setField(jobKey, fieldName, value) {
    setJobFields((prev) => ({
      ...prev,
      [jobKey]: { ...prev[jobKey], [fieldName]: value },
    }))
  }

  async function handleSubmit() {
    setError(null)
    if (orderedActiveJobs.length === 0) {
      setError('Select at least one job type before submitting.')
      return
    }

    setSubmitting(true)
    try {
      const payload = {
        ...form,
        jobs: orderedActiveJobs.map((j) => ({
          job_type: j.key,
          fields: jobFields[j.key] || {},
        })),
      }
      const task = await api.createTask(payload)
      navigate(`/tasks/${task.task_id}`)
    } catch (e) {
      setError(e.message)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div>
      <div className="page-header">
        <h1 className="page-title">New deployment request</h1>
        <p className="page-sub">Fill in the details below — this becomes one task with multiple jobs underneath it.</p>
      </div>

      <div className="card">
        <p className="card-title">Request details</p>
        <div className="field-row">
          <div>
            <label className="field-label">Environment</label>
            <select value={form.environment} onChange={(e) => setForm({ ...form, environment: e.target.value })}>
              <option>INTEG</option>
              <option>UAT</option>
              <option>PROD</option>
            </select>
          </div>
          <div>
            <label className="field-label">Jira ticket</label>
            <input
              type="text"
              value={form.jira_key}
              onChange={(e) => setForm({ ...form, jira_key: e.target.value })}
              placeholder="TRB-16996"
            />
          </div>
        </div>
        <div className="field-row">
          <div>
            <label className="field-label">Requested by</label>
            <input
              type="text"
              value={form.requested_by}
              onChange={(e) => setForm({ ...form, requested_by: e.target.value })}
            />
          </div>
          <div>
            <label className="field-label">Priority</label>
            <select value={form.priority} onChange={(e) => setForm({ ...form, priority: e.target.value })}>
              <option>Normal</option>
              <option>High</option>
              <option>Urgent</option>
            </select>
          </div>
        </div>
        <div className="field-group">
          <label className="field-label">Description</label>
          <textarea
            rows={2}
            value={form.description}
            onChange={(e) => setForm({ ...form, description: e.target.value })}
            placeholder="Short description of what this deployment does"
          />
        </div>
      </div>

      <div className="card">
        <p className="card-title">Jobs in this request</p>
        <p className="card-sub">Check every job type this task needs. Each becomes its own job under one task ID.</p>

        {JOB_DEFS.map((job) => (
          <div key={job.key}>
            <label className={`job-toggle ${enabledJobs[job.key] ? 'checked' : ''}`}>
              <input
                type="checkbox"
                checked={!!enabledJobs[job.key]}
                onChange={() => toggleJob(job.key)}
              />
              <span className="job-icon">{job.icon}</span>
              <span className="job-toggle-label">{job.label}</span>
            </label>

            <div className={`job-section ${enabledJobs[job.key] ? 'show' : ''}`}>
              <div className="field-row" style={{ gridTemplateColumns: `repeat(${job.fields.length}, 1fr)` }}>
                {job.fields.map((f) => (
                  <div key={f.name}>
                    <label className="field-label">{f.label}</label>
                    {f.type === 'select' ? (
                      <select
                        value={jobFields[job.key]?.[f.name] || f.options[0]}
                        onChange={(e) => setField(job.key, f.name, e.target.value)}
                      >
                        {f.options.map((opt) => <option key={opt}>{opt}</option>)}
                      </select>
                    ) : (
                      <input
                        type="text"
                        value={jobFields[job.key]?.[f.name] || ''}
                        onChange={(e) => setField(job.key, f.name, e.target.value)}
                        placeholder={f.placeholder}
                      />
                    )}
                  </div>
                ))}
              </div>
            </div>
          </div>
        ))}
      </div>

      <div className="card" style={{ background: 'var(--slate-light)', border: 'none' }}>
        <p className="orchestrator-preview-title">Orchestrator preview</p>
        <p className="card-sub" style={{ marginBottom: 8 }}>Based on jobs selected, execution follows this DAG order (parallel branches run together when deps allow):</p>
        <div style={{ fontSize: 12.5, color: 'var(--blue)' }}>
          {orderedActiveJobs.length === 0 ? (
            <span style={{ color: 'var(--text-tertiary)' }}>Select at least one job above</span>
          ) : (
            orderedActiveJobs.map((j, i) => (
              <span key={j.key}>
                {i > 0 && <span style={{ color: 'var(--text-tertiary)' }}> &nbsp;→&nbsp; </span>}
                {j.icon} {i + 1}. {j.label}
              </span>
            ))
          )}
        </div>
      </div>

      {error && <div className="alert alert-error">{error}</div>}

      <button
        className="btn btn-primary btn-block"
        onClick={handleSubmit}
        disabled={submitting}
      >
        {submitting ? <><span className="spinner" /> Submitting...</> : 'Submit request — creates 1 task →'}
      </button>
    </div>
  )
}
