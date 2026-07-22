import React, { useEffect, useState } from 'react'
import { api } from '../api.js'

const SUBTYPE_LABELS = {
  microservice: 'Microservice',
  portal: 'Portal',
  utility: 'Utilities',
  phrases: 'Phrases',
  schemaforms: 'SchemaForms',
  newschemaforms: 'NewSchemaForms',
}

function validationForLink(linkValidation, sectionKey, serviceKey, branchPair) {
  if (!linkValidation) return undefined
  if (sectionKey === 'build' && branchPair) {
    const from = (branchPair.from || '').trim()
    const to = (branchPair.to || '').trim()
    return linkValidation[`build:${serviceKey}:${from}:${to}`]
  }
  return linkValidation[serviceKey]
}

export default function LinkSectionEditor({
  sectionKey,
  title,
  subtitle,
  icon,
  iconBg,
  enabled,
  onToggle,
  allowedSubTypes,
  needsReleaseBranch,
  releaseBranch,
  onReleaseBranchChange,
  links,
  onChange,
  showToggle = true,
  onRemoveGroup,
  showBranches,
  branchPair,
  onBranchChange,
  onAddGroup,
  linkValidation,
  showBuildOnlyToggle = false,
  buildOnly = false,
  onBuildOnlyChange,
}) {
  const [activeTab, setActiveTab] = useState(allowedSubTypes[0])
  const [services, setServices] = useState({})

  useEffect(() => {
    if (!enabled) return
    Promise.all(
      allowedSubTypes.map((st) =>
        api.getServices(sectionKey, st).then((list) => [st, list]).catch(() => [st, []]),
      ),
    ).then((pairs) => setServices(Object.fromEntries(pairs)))
  }, [enabled, sectionKey, allowedSubTypes.join(',')])

  function addService(serviceKey) {
    if (!serviceKey) return
    if (links.some((l) => l.service_key === serviceKey)) return
    const svc = (services[activeTab] || []).find((s) => s.key === serviceKey)
    onChange([...links, { sub_type: activeTab, service_key: serviceKey, label: svc?.label || serviceKey }])
  }

  function removeService(serviceKey) {
    onChange(links.filter((l) => l.service_key !== serviceKey))
  }

  const available = (services[activeTab] || []).filter(
    (s) => !links.some((l) => l.service_key === s.key),
  )
  const activeLabel = SUBTYPE_LABELS[activeTab]?.toLowerCase() || 'service'

  return (
    <div className={`job-section-card ${enabled ? 'enabled' : ''}`}>
      <div className="job-section-header">
        <div className="job-section-icon" style={{ background: iconBg }}>{icon}</div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <p className="job-section-title">{title}</p>
          {subtitle && <p className="card-sub" style={{ margin: 0 }}>{subtitle}</p>}
        </div>

        {enabled && allowedSubTypes.length > 1 && (
          <div className="subtype-tabs">
            {allowedSubTypes.map((st) => (
              <button
                key={st}
                type="button"
                className={`subtype-tab subtype-tab-${st} ${activeTab === st ? 'active' : ''}`}
                onClick={() => setActiveTab(st)}
              >
                {SUBTYPE_LABELS[st]}
              </button>
            ))}
          </div>
        )}

        {showToggle ? (
          <button
            type="button"
            className={`toggle-switch ${enabled ? 'on' : ''}`}
            onClick={onToggle}
            aria-label={`Toggle ${title}`}
          >
            <span className="toggle-knob" />
          </button>
        ) : onRemoveGroup ? (
          <button
            type="button"
            className="card-remove-btn"
            onClick={onRemoveGroup}
            title="Remove this card"
          >
            ×
          </button>
        ) : null}
      </div>

      {enabled && (
        <>
          {showBuildOnlyToggle && (
            <div className="build-only-row">
              <div className="build-only-text">
                <span className="build-only-label">Only build (no merge)</span>
                <span className="build-only-hint">
                  Trigger the Jenkins build directly with a blank MergeID — no merge, no branch required.
                </span>
              </div>
              <button
                type="button"
                className={`toggle-switch ${buildOnly ? 'on' : ''}`}
                onClick={() => onBuildOnlyChange?.(!buildOnly)}
                aria-label="Toggle build-only mode"
              >
                <span className="toggle-knob" />
              </button>
              {buildOnly && onAddGroup && (
                <button
                  type="button"
                  className="branch-add-btn"
                  onClick={onAddGroup}
                  title="Add another Build card"
                >
                  +
                </button>
              )}
            </div>
          )}

          {needsReleaseBranch && (
            <div className="field-group">
              <label className="field-label">Release branch</label>
              <input
                type="text"
                value={releaseBranch || ''}
                onChange={(e) => onReleaseBranchChange(e.target.value)}
                placeholder="release/2026-07"
              />
            </div>
          )}

          <div className="select-chip-row">
            <div className="select-chip-left">
              <label className="field-label">Select {activeLabel}</label>
              <select value="" onChange={(e) => addService(e.target.value)}>
                <option value="">Select {activeLabel}...</option>
                {available.map((s) => (
                  <option key={s.key} value={s.key}>{s.label}</option>
                ))}
              </select>
            </div>

            <div className="select-chip-right">
              <label className="field-label">Selected ({links.length})</label>
              <div className="chip-list">
                {links.length === 0 && <span className="chip-empty">Nothing selected yet</span>}
                {links.map((l) => {
                  const v = validationForLink(linkValidation, sectionKey, l.service_key, branchPair)
                  const state = !v ? 'pending' : !v.valid ? 'invalid' : v.warn ? 'warn' : 'valid'
                  return (
                    <span key={l.service_key} className={`chip chip-${l.sub_type} chip-validation`}>
                      {linkValidation && (
                        <span className={`chip-badge ${state}`} title={state}>
                          {state === 'valid' ? '✓' : state === 'warn' ? '⚠' : state === 'invalid' ? '✕' : '–'}
                        </span>
                      )}
                      <span className="chip-type">{SUBTYPE_LABELS[l.sub_type]}</span>
                      <span className="chip-label">{l.label}</span>
                      <button
                        type="button"
                        className="chip-x"
                        onClick={() => removeService(l.service_key)}
                        title="Remove"
                      >
                        ×
                      </button>
                    </span>
                  )
                })}
              </div>
              {linkValidation &&
                links.map((l) => {
                  const v = validationForLink(linkValidation, sectionKey, l.service_key, branchPair)
                  if (!v) return null
                  return (
                    <React.Fragment key={l.service_key}>
                      {(v.errors || []).map((err, i) => (
                        <p className="validation-error-text" key={`e-${l.service_key}-${i}`}>
                          {l.label}: {err}
                        </p>
                      ))}
                      {(v.warnings || []).map((warn, i) => (
                        <p className="validation-warning-text" key={`w-${l.service_key}-${i}`}>
                          {l.label}: {warn}
                        </p>
                      ))}
                    </React.Fragment>
                  )
                })}
            </div>
          </div>

          {showBranches && branchPair && !buildOnly && (
            <div className="build-branches">
              <div className="branch-pair">
                <div className="branch-pair-fields">
                  <div>
                    <label className="field-label field-label-required">Gitspace branch — From</label>
                    <input
                      type="text"
                      value={branchPair.from}
                      onChange={(e) => onBranchChange({ from: e.target.value })}
                      placeholder="e.g. feature/my-change"
                      required
                    />
                  </div>
                  <div>
                    <label className="field-label field-label-required">Gitspace branch — To (auto from environment)</label>
                    <input
                      type="text"
                      value={branchPair.to}
                      onChange={(e) => onBranchChange({ to: e.target.value })}
                      placeholder="e.g. develop"
                      required
                    />
                  </div>
                </div>
                {onAddGroup && (
                  <div className="branch-pair-actions">
                    <button
                      type="button"
                      className="branch-add-btn"
                      onClick={onAddGroup}
                      title="Add another Build card"
                    >
                      +
                    </button>
                  </div>
                )}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  )
}
