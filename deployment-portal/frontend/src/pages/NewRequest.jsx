import React, { useEffect, useState, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api.js'
import LinkSectionEditor from '../components/LinkSectionEditor.jsx'

// Display order — Build is shown first. `order` drives orchestrator preview.
const SECTION_DEFS = [
  {
    key: 'build',
    title: 'Gitspace merge — Build',
    subtitle: 'Microservice, Portal, or Utilities',
    icon: '🔀',
    iconBg: 'var(--blue-light)',
    order: 4,
    subTypes: ['microservice', 'portal', 'utility'],
    needsReleaseBranch: false,
  },
  {
    key: 'yaml',
    title: 'YAML / Config',
    subtitle: 'Release branch + Microservice / Portal',
    icon: '📄',
    iconBg: 'var(--purple-light)',
    order: 1,
    subTypes: ['microservice', 'portal'],
    needsReleaseBranch: true,
  },
  {
    key: 'db',
    title: 'DB / Liquibase',
    subtitle: 'Release branch + Microservice',
    icon: '🗄️',
    iconBg: 'var(--teal-light)',
    order: 2,
    subTypes: ['microservice'],
    needsReleaseBranch: true,
  },
  {
    key: 'phrases',
    title: 'Phrases',
    subtitle: 'Release branch + Portal',
    icon: '💬',
    iconBg: 'var(--amber-light)',
    order: 3,
    subTypes: ['portal'],
    needsReleaseBranch: true,
  },
]

const ENV_TO_BRANCH = {
  DEV: 'develop',
  SUPPORT: 'support',
  INTEG: 'integ',
  UAT: 'release/uat',
}

export default function NewRequest() {
  const navigate = useNavigate()
  const [submitting, setSubmitting] = useState(false)
  const [validating, setValidating] = useState(false)
  const [validation, setValidation] = useState(null)
  const [error, setError] = useState(null)
  const [environments, setEnvironments] = useState([])
  const [approvers, setApprovers] = useState([])

  const [form, setForm] = useState({
    environment: 'INTEG',
    jira_id: '',
    description: '',
    approver_key: '',
  })

  // Build is a list of independent groups — each its own services + a single
  // source → destination branch pair. The "+" spawns a whole new Build card.
  const [buildGroups, setBuildGroups] = useState([
    { links: [], branch: { from: '', to: ENV_TO_BRANCH.INTEG || '' } },
  ])

  const [enabledSections, setEnabledSections] = useState({
    build: true, yaml: false, db: false, phrases: false,
  })

  const [sectionLinks, setSectionLinks] = useState({
    yaml: [], db: [], phrases: [],
  })

  const [releaseBranches, setReleaseBranches] = useState({ yaml: '', db: '', phrases: '' })

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

  // "To" branch auto-populates from the selected environment (still editable).
  useEffect(() => {
    const def = ENV_TO_BRANCH[form.environment] || ''
    setBuildGroups((prev) => prev.map((g) => ({ ...g, branch: { ...g.branch, to: def } })))
    setValidation(null)
  }, [form.environment])

  function addBuildGroup() {
    setBuildGroups((prev) => [
      ...prev,
      { links: [], branch: { from: '', to: ENV_TO_BRANCH[form.environment] || '' } },
    ])
    setValidation(null)
  }

  function removeBuildGroup(idx) {
    setBuildGroups((prev) => (prev.length <= 1 ? prev : prev.filter((_, i) => i !== idx)))
    setValidation(null)
  }

  function updateBuildGroupLinks(idx, links) {
    setBuildGroups((prev) => prev.map((g, i) => (i === idx ? { ...g, links } : g)))
    setValidation(null)
  }

  function updateBuildGroupBranch(idx, patch) {
    setBuildGroups((prev) =>
      prev.map((g, i) => (i === idx ? { ...g, branch: { ...g.branch, ...patch } } : g)),
    )
    setValidation(null)
  }

  const activeSections = useMemo(
    () => SECTION_DEFS.filter((s) => enabledSections[s.key]).sort((a, b) => a.order - b.order),
    [enabledSections],
  )

  function updateForm(patch) {
    setForm((prev) => ({ ...prev, ...patch }))
    setValidation(null)
  }

  function toggleSection(key) {
    setEnabledSections((prev) => ({ ...prev, [key]: !prev[key] }))
    setValidation(null)
  }

  function setSectionLinksFor(key, links) {
    setSectionLinks((prev) => ({ ...prev, [key]: links }))
    setValidation(null)
  }

  function setReleaseBranchFor(key, val) {
    setReleaseBranches((prev) => ({ ...prev, [key]: val }))
    setValidation(null)
  }

  function mapLinks(links) {
    return links
      .filter((l) => l.service_key)
      .map((l) => ({ sub_type: l.sub_type, service_key: l.service_key, label: l.label || '' }))
  }

  function buildSectionsPayload() {
    const out = []
    // Non-build sections (single each).
    activeSections
      .filter((sec) => sec.key !== 'build')
      .forEach((sec) => {
        out.push({
          section: sec.key,
          release_branch: sec.needsReleaseBranch ? (releaseBranches[sec.key] || '').trim() : '',
          links: mapLinks(sectionLinks[sec.key]),
        })
      })
    // Build groups (one section entry per card).
    if (enabledSections.build) {
      buildGroups.forEach((g) => {
        out.push({
          section: 'build',
          release_branch: '',
          branch_from: (g.branch.from || '').trim(),
          branch_to: (g.branch.to || '').trim(),
          links: mapLinks(g.links),
        })
      })
    }
    return out
  }

  async function handleValidate() {
    setError(null)
    setValidating(true)
    try {
      const result = await api.validateRequest({
        environment: form.environment,
        jira_id: form.jira_id,
        sections: buildSectionsPayload(),
      })
      // Layer in client-only checks not covered server-side.
      const extra = []
      if (!form.approver_key) extra.push('Please select an approver')
      if (activeSections.length === 0) extra.push('Enable at least one section')
      const merged = { valid: result.valid && extra.length === 0, errors: [...result.errors, ...extra] }
      setValidation(merged)
    } catch (e) {
      setError(e.message)
    } finally {
      setValidating(false)
    }
  }

  async function handleSubmit(e) {
    e.preventDefault()
    setError(null)
    if (!validation || !validation.valid) {
      setError('Please run validation and fix any issues before submitting.')
      return
    }
    setSubmitting(true)
    try {
      const payload = {
        environment: form.environment,
        jira_id: form.jira_id.trim(),
        description: form.description.trim(),
        approver_key: form.approver_key,
        sections: buildSectionsPayload(),
      }
      const task = await api.createTask(payload)
      navigate(`/tasks/${task.task_id}`)
    } catch (err) {
      setError(err.message)
    } finally {
      setSubmitting(false)
    }
  }

  const canSubmit = validation && validation.valid && !submitting

  return (
    <form onSubmit={handleSubmit}>
      <div className="page-header">
        <h1 className="page-title">New deployment request</h1>
        <p className="page-sub">
          Select services from dropdowns, set branches, then validate before submitting.
        </p>
      </div>

      <div className="card">
        <p className="card-title">Request details</p>

        <div className="field-row">
          <div>
            <label className="field-label">Environment to promote</label>
            <select value={form.environment} onChange={(e) => updateForm({ environment: e.target.value })}>
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
              onChange={(e) => updateForm({ jira_id: e.target.value })}
              placeholder="TRB-16996"
            />
          </div>
        </div>

        <div className="field-group" style={{ marginBottom: 0 }}>
          <label className="field-label">Description of the request</label>
          <textarea
            rows={3}
            value={form.description}
            onChange={(e) => updateForm({ description: e.target.value })}
            placeholder="Brief summary of what this deployment includes..."
          />
        </div>
      </div>

      <p className="section-heading">Deployment sections</p>

      {SECTION_DEFS.map((sec) => {
        if (sec.key === 'build') {
          // When disabled, show a single collapsed card with the enable toggle.
          if (!enabledSections.build) {
            return (
              <LinkSectionEditor
                key="build"
                sectionKey="build"
                title={sec.title}
                subtitle={sec.subtitle}
                icon={sec.icon}
                iconBg={sec.iconBg}
                enabled={false}
                showToggle
                onToggle={() => toggleSection('build')}
                allowedSubTypes={sec.subTypes}
                links={buildGroups[0]?.links || []}
                onChange={() => {}}
              />
            )
          }
          // When enabled, one full card per build group; "+" adds another.
          return buildGroups.map((g, gi) => (
            <LinkSectionEditor
              key={`build-${gi}`}
              sectionKey="build"
              title={buildGroups.length > 1 ? `${sec.title} #${gi + 1}` : sec.title}
              subtitle={sec.subtitle}
              icon={sec.icon}
              iconBg={sec.iconBg}
              enabled
              showToggle={gi === 0}
              onToggle={() => toggleSection('build')}
              onRemoveGroup={gi > 0 ? () => removeBuildGroup(gi) : undefined}
              allowedSubTypes={sec.subTypes}
              links={g.links}
              onChange={(links) => updateBuildGroupLinks(gi, links)}
              showBranches
              branchPair={g.branch}
              onBranchChange={(patch) => updateBuildGroupBranch(gi, patch)}
              onAddGroup={addBuildGroup}
            />
          ))
        }
        return (
          <LinkSectionEditor
            key={sec.key}
            sectionKey={sec.key}
            title={sec.title}
            subtitle={sec.subtitle}
            icon={sec.icon}
            iconBg={sec.iconBg}
            enabled={enabledSections[sec.key]}
            showToggle
            onToggle={() => toggleSection(sec.key)}
            allowedSubTypes={sec.subTypes}
            needsReleaseBranch={sec.needsReleaseBranch}
            releaseBranch={releaseBranches[sec.key]}
            onReleaseBranchChange={(val) => setReleaseBranchFor(sec.key, val)}
            links={sectionLinks[sec.key]}
            onChange={(links) => setSectionLinksFor(sec.key, links)}
          />
        )
      })}

      <div className="card">
        <p className="card-title">Approval</p>
        <div className="field-group" style={{ marginBottom: 0 }}>
          <label className="field-label">Approver (Development Lead)</label>
          <select value={form.approver_key} onChange={(e) => updateForm({ approver_key: e.target.value })}>
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
            activeSections.map((s, i) => {
              const count = s.key === 'build'
                ? buildGroups.reduce((n, g) => n + g.links.filter((l) => l.service_key).length, 0)
                : sectionLinks[s.key].filter((l) => l.service_key).length
              const suffix = s.key === 'build' && buildGroups.length > 1 ? `, ${buildGroups.length} cards` : ''
              return (
                <span key={s.key}>
                  {i > 0 && <span style={{ color: 'var(--text-tertiary)' }}> → </span>}
                  {s.icon} {s.title}
                  <span style={{ color: 'var(--text-tertiary)', fontSize: 11 }}>
                    {' '}({count} item(s){suffix})
                  </span>
                </span>
              )
            })
          )}
        </div>
      </div>

      {error && <div className="alert alert-error">{error}</div>}

      {validation && !validation.valid && (
        <div className="alert alert-error">
          <strong>Fix the following before submitting:</strong>
          <ul style={{ margin: '8px 0 0', paddingLeft: 18 }}>
            {validation.errors.map((e, i) => <li key={i}>{e}</li>)}
          </ul>
        </div>
      )}

      {validation && validation.valid && (
        <div className="alert alert-success">✓ All validations passed — you can submit now.</div>
      )}

      <div style={{ display: 'flex', gap: 10 }}>
        <button type="button" className="btn" style={{ flex: 1 }} onClick={handleValidate} disabled={validating}>
          {validating ? <><span className="spinner" /> Validating...</> : '✓ Validate'}
        </button>
        <button type="submit" className="btn btn-primary" style={{ flex: 1 }} disabled={!canSubmit}>
          {submitting ? <><span className="spinner" /> Submitting...</> : 'Submit deployment request'}
        </button>
      </div>
    </form>
  )
}
