import React, { useEffect, useState, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api.js'
import LinkSectionEditor, { emptyLink } from '../components/LinkSectionEditor.jsx'

const SECTION_DEFS = [
  {
    key: 'build',
    title: 'Gitspace merge — Build',
    subtitle: 'Microservice, Portal, or Utilities merge links',
    icon: '🔀',
    iconBg: 'var(--blue-light)',
    order: 3,
    subTypes: ['microservice', 'portal', 'utility'],
  },
  {
    key: 'yaml',
    title: 'YAML / Config',
    subtitle: 'Microservice or Portal config links (no utilities)',
    icon: '📄',
    iconBg: 'var(--purple-light)',
    order: 1,
    subTypes: ['microservice', 'portal'],
  },
  {
    key: 'db',
    title: 'DB / Liquibase',
    subtitle: 'Microservice migration links only',
    icon: '🗄️',
    iconBg: 'var(--teal-light)',
    order: 2,
    subTypes: ['microservice'],
  },
]

export default function NewRequest() {
  const navigate = useNavigate()
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(null)
  const [environments, setEnvironments] = useState([])
  const [approvers, setApprovers] = useState([])

  const [form, setForm] = useState({
    environment: 'INTEG',
    jira_id: '',
    description: '',
    branch_from: '',
    branch_to: '',
    approver_key: '',
  })

  const [enabledSections, setEnabledSections] = useState({
    build: true,
    yaml: false,
    db: false,
  })

  const [sectionLinks, setSectionLinks] = useState({
    build: [emptyLink(['microservice', 'portal', 'utility'], 'microservice')],
    yaml: [emptyLink(['microservice', 'portal'], 'microservice')],
    db: [emptyLink(['microservice'], 'microservice')],
  })

  useEffect(() => {
    Promise.all([api.getEnvironments(), api.getApprovers()])
      .then(([envs, appr]) => {
        setEnvironments(envs)
        setApprovers(appr)
        if (envs.length) setForm((f) => ({ ...f, environment: envs[0].key }))
        if (appr.length) setForm((f) => ({ ...f, approver_key: appr[0].key }))
      })
      .catch((e) => setError(e.message))
  }, [])

  const activeSections = useMemo(
    () => SECTION_DEFS.filter((s) => enabledSections[s.key]).sort((a, b) => a.order - b.order),
    [enabledSections],
  )

  function toggleSection(key) {
    setEnabledSections((prev) => ({ ...prev, [key]: !prev[key] }))
  }

  function setSectionLinksFor(key, links) {
    setSectionLinks((prev) => ({ ...prev, [key]: links }))
  }

  async function handleSubmit(e) {
    e.preventDefault()
    setError(null)

    if (!form.jira_id.trim()) {
      setError('Jira ID is required.')
      return
    }
    if (!form.approver_key) {
      setError('Please select an approver.')
      return
    }
    if (activeSections.length === 0) {
      setError('Enable at least one section (Build, YAML, or DB).')
      return
    }

    for (const sec of activeSections) {
      const links = sectionLinks[sec.key]
      const valid = links.filter((l) => l.url.trim())
      if (valid.length === 0) {
        setError(`${sec.title}: add at least one link with a URL.`)
        return
      }
    }

    setSubmitting(true)
    try {
      const payload = {
        environment: form.environment,
        jira_id: form.jira_id.trim(),
        description: form.description.trim(),
        branch_from: form.branch_from.trim(),
        branch_to: form.branch_to.trim(),
        approver_key: form.approver_key,
        sections: activeSections.map((sec) => ({
          section: sec.key,
          links: sectionLinks[sec.key]
            .filter((l) => l.url.trim())
            .map((l) => ({
              sub_type: l.sub_type,
              url: l.url.trim(),
              label: l.label.trim() || l.sub_type,
            })),
        })),
      }
      const task = await api.createTask(payload)
      navigate(`/tasks/${task.task_id}`)
    } catch (err) {
      setError(err.message)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <form onSubmit={handleSubmit}>
      <div className="page-header">
        <h1 className="page-title">New deployment request</h1>
        <p className="page-sub">
          Paste Gitspace merge links for each section. Toggle sections on/off and use + to add more links.
        </p>
      </div>

      <div className="card">
        <p className="card-title">Request details</p>

        <div className="field-row">
          <div>
            <label className="field-label">Environment to promote</label>
            <select
              value={form.environment}
              onChange={(e) => setForm({ ...form, environment: e.target.value })}
            >
              {environments.map((env) => (
                <option key={env.key} value={env.key}>{env.label}</option>
              ))}
            </select>
          </div>
          <div>
            <label className="field-label">Jira ID</label>
            <input
              type="text"
              value={form.jira_id}
              onChange={(e) => setForm({ ...form, jira_id: e.target.value })}
              placeholder="TRB-16996"
              required
            />
          </div>
        </div>

        <div className="field-group">
          <label className="field-label">Description of the request</label>
          <textarea
            rows={3}
            value={form.description}
            onChange={(e) => setForm({ ...form, description: e.target.value })}
            placeholder="Brief summary of what this deployment includes..."
          />
        </div>

        <div className="field-row">
          <div>
            <label className="field-label">Gitspace branch — From</label>
            <input
              type="text"
              value={form.branch_from}
              onChange={(e) => setForm({ ...form, branch_from: e.target.value })}
              placeholder="develop"
            />
          </div>
          <div>
            <label className="field-label">Gitspace branch — To</label>
            <input
              type="text"
              value={form.branch_to}
              onChange={(e) => setForm({ ...form, branch_to: e.target.value })}
              placeholder="release/2026-06"
            />
          </div>
        </div>
      </div>

      <p className="section-heading">Deployment sections</p>

      {SECTION_DEFS.map((sec) => (
        <LinkSectionEditor
          key={sec.key}
          sectionKey={sec.key}
          title={sec.title}
          subtitle={sec.subtitle}
          icon={sec.icon}
          iconBg={sec.iconBg}
          enabled={enabledSections[sec.key]}
          onToggle={() => toggleSection(sec.key)}
          allowedSubTypes={sec.subTypes}
          links={sectionLinks[sec.key]}
          onChange={(links) => setSectionLinksFor(sec.key, links)}
        />
      ))}

      <div className="card">
        <p className="card-title">Approval</p>
        <div className="field-group" style={{ marginBottom: 0 }}>
          <label className="field-label">Approver</label>
          <select
            value={form.approver_key}
            onChange={(e) => setForm({ ...form, approver_key: e.target.value })}
            required
          >
            <option value="">Select approver...</option>
            {approvers.map((a) => (
              <option key={a.key} value={a.key}>{a.name}</option>
            ))}
          </select>
        </div>
      </div>

      <div className="card orchestrator-preview">
        <p className="orchestrator-preview-title">Orchestrator preview</p>
        <p className="card-sub" style={{ marginBottom: 8 }}>
          Jobs run in this order when multiple sections are enabled:
        </p>
        <div style={{ fontSize: 12.5, color: 'var(--blue)' }}>
          {activeSections.length === 0 ? (
            <span style={{ color: 'var(--text-tertiary)' }}>Enable at least one section above</span>
          ) : (
            activeSections.map((s, i) => (
              <span key={s.key}>
                {i > 0 && <span style={{ color: 'var(--text-tertiary)' }}> → </span>}
                {s.icon} {s.title}
                <span style={{ color: 'var(--text-tertiary)', fontSize: 11 }}>
                  {' '}({sectionLinks[s.key].filter((l) => l.url.trim()).length || 0} link(s))
                </span>
              </span>
            ))
          )}
        </div>
      </div>

      {error && <div className="alert alert-error">{error}</div>}

      <button type="submit" className="btn btn-primary btn-block" disabled={submitting}>
        {submitting ? <><span className="spinner" /> Submitting...</> : 'Submit deployment request'}
      </button>
    </form>
  )
}
