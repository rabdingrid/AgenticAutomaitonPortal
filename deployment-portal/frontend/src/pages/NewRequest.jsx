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
  DEV: 'dev',
  SUPPORT: 'support',
  INTEG: 'integ',
  UAT: 'master',
}

const SECTION_SHORT = {
  build: 'Build',
  yaml: 'YAML',
  db: 'DB',
  phrases: 'Phrases',
}

function formatPreviewIssues(sections) {
  const blocking = []
  const warnings = []
  for (const sec of sections || []) {
    const title = sec.title || SECTION_SHORT[sec.section] || sec.section
    for (const item of sec.items || []) {
      if (item.status !== 'fail' && item.status !== 'warn') continue
      const check = (item.checks || []).find((c) => c.status === item.status)
        || (item.checks || []).find((c) => c.status === 'fail' || c.status === 'warn')
      const detail = check?.detail || 'Issue detected'
      const branchHint = item.merge?.source_branch
        ? ` (${item.merge.source_branch} → ${item.merge.target_branch})`
        : ''
      const prefix = `${title} · ${item.label}${branchHint}`
      const line = detail.includes('\n') ? `${prefix}:\n${detail}` : `${prefix}: ${detail}`
      if (item.status === 'fail') blocking.push(`✕ ${line}`)
      else warnings.push(`⚠ ${line}`)
    }
  }
  return { blocking, warnings }
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
    cc_emails: '',
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

  // Per-link validation badges. Build uses service_key + branch pair (same service
  // can appear on multiple Build cards with different branches).
  const linkValidation = useMemo(() => {
    if (!validation?.sections) return null
    const map = {}
    for (const sec of validation.sections) {
      const rows = sec.items || sec.results || []
      for (const r of rows) {
        if (!r.service_key) continue
        const from = r.merge?.source_branch ?? ''
        const to = r.merge?.target_branch ?? ''
        const key = sec.section === 'build' && (from || to)
          ? `build:${r.service_key}:${from}:${to}`
          : r.service_key
        if (sec.items) {
          map[key] = {
            valid: r.status === 'pass' || r.status === 'warn',
            warn: r.status === 'warn',
            errors: (r.checks || [])
              .filter((c) => c.status === 'fail')
              .map((c) => `${c.name}: ${c.detail}`),
            warnings: (r.checks || [])
              .filter((c) => c.status === 'warn')
              .map((c) => c.detail || c.name),
          }
        } else {
          map[key] = { valid: r.valid, errors: r.errors || [] }
        }
      }
    }
    return map
  }, [validation])

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

  function getMandatoryFieldErrors() {
    const errors = []
    if (!form.jira_id.trim()) {
      errors.push('Jira ID is required')
    }
    if (enabledSections.build) {
      buildGroups.forEach((g, i) => {
        const prefix = buildGroups.length > 1 ? `Build #${i + 1}` : 'Build'
        if (!(g.branch.from || '').trim()) {
          errors.push(`${prefix}: Gitspace branch — From is required`)
        }
        if (!(g.branch.to || '').trim()) {
          errors.push(`${prefix}: Gitspace branch — To is required`)
        }
      })
    }
    return errors
  }

  async function handleValidate() {
    setError(null)
    const mandatoryErrors = getMandatoryFieldErrors()
    if (mandatoryErrors.length > 0) {
      setValidation({ valid: false, errors: mandatoryErrors, sections: [] })
      return
    }
    setValidating(true)
    try {
      const report = await api.validatePreview({
        environment: form.environment,
        jira_id: form.jira_id,
        sections: buildSectionsPayload(),
      })

      const extra = []
      if (!form.approver_key) extra.push('Please select an approver')
      if (activeSections.length === 0) extra.push('Enable at least one section')
      for (const sec of activeSections) {
        if (sec.needsReleaseBranch && !(releaseBranches[sec.key] || '').trim()) {
          extra.push(`Release branch is required for ${sec.title}`)
        }
      }

      const { blocking, warnings } = formatPreviewIssues(report.sections)
      const canSubmit = report.overall_status !== 'failed' && extra.length === 0
      const merged = {
        valid: canSubmit,
        hasWarnings: report.overall_status === 'warnings' || warnings.length > 0,
        errors: [...blocking, ...extra],
        warnings,
        sections: report.sections || [],
      }
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
    const mandatoryErrors = getMandatoryFieldErrors()
    if (mandatoryErrors.length > 0) {
      setValidation({ valid: false, errors: mandatoryErrors, sections: [] })
      setError('Please fill in all required fields before submitting.')
      return
    }
    if (!validation) {
      setError('Please run validation before submitting.')
      return
    }
    if (!validation.valid) {
      setError('Please run validation and fix blocking issues before submitting.')
      return
    }
    setSubmitting(true)
    try {
      const payload = {
        environment: form.environment,
        jira_id: form.jira_id.trim(),
        description: form.description.trim(),
        approver_key: form.approver_key,
        cc_emails: form.cc_emails.trim(),
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
            <label className="field-label field-label-required">Jira ID</label>
            <input
              type="text"
              value={form.jira_id}
              onChange={(e) => updateForm({ jira_id: e.target.value })}
              placeholder="TRB-16996"
              required
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
              linkValidation={linkValidation}
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
            linkValidation={linkValidation}
          />
        )
      })}

      <div className="card">
        <p className="card-title">Approval</p>
        <div className="field-group" style={{ marginBottom: 12 }}>
          <label className="field-label">Approver (Development Lead)</label>
          <select value={form.approver_key} onChange={(e) => updateForm({ approver_key: e.target.value })}>
            <option value="">Select approver...</option>
            {approvers.map((a) => (
              <option key={a.key} value={a.key}>{a.name}</option>
            ))}
          </select>
        </div>
        <div className="field-group" style={{ marginBottom: 0 }}>
          <label className="field-label">CC</label>
          <input
            type="text"
            value={form.cc_emails}
            onChange={(e) => updateForm({ cc_emails: e.target.value })}
            placeholder="e.g. teammate@company.com, manager@company.com"
          />
          <p className="field-hint">Optional. Comma-separated email addresses to notify along with the approver.</p>
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
          <ul className="validation-error-list">
            {validation.errors.map((e, i) => <li key={i}>{e}</li>)}
          </ul>
          <p className="validation-error-hint">
            Blocking issues are marked on each selected service above. Fix them and click Validate again.
          </p>
        </div>
      )}

      {validation && validation.valid && validation.warnings?.length > 0 && (
        <>
          <div className="alert alert-warning">
            <strong>Advisory warnings (submit allowed):</strong>
            <ul className="validation-warning-list">
              {validation.warnings.map((w, i) => <li key={i}>{w}</li>)}
            </ul>
          </div>
          <div className="alert alert-success">✓ No blocking issues — you can submit.</div>
        </>
      )}

      {validation && validation.valid && !validation.warnings?.length && (
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
