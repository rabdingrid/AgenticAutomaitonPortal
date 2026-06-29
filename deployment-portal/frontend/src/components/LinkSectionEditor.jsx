import React from 'react'

const SUBTYPE_LABELS = {
  microservice: 'Microservice',
  portal: 'Portal',
  utility: 'Utilities',
}

function emptyLink(subTypes, defaultSubType) {
  return { sub_type: defaultSubType || subTypes[0], url: '', label: '' }
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
  links,
  onChange,
}) {
  function setLink(index, field, value) {
    const next = links.map((l, i) => (i === index ? { ...l, [field]: value } : l))
    onChange(next)
  }

  function addLink() {
    onChange([...links, emptyLink(allowedSubTypes)])
  }

  function removeLink(index) {
    if (links.length <= 1) return
    onChange(links.filter((_, i) => i !== index))
  }

  return (
    <div className={`job-section-card ${enabled ? 'enabled' : ''}`}>
      <div className="job-section-header">
        <div className="job-section-icon" style={{ background: iconBg }}>{icon}</div>
        <div style={{ flex: 1 }}>
          <p className="job-section-title">{title}</p>
          {subtitle && <p className="card-sub" style={{ margin: 0 }}>{subtitle}</p>}
        </div>
        <button
          type="button"
          className={`toggle-switch ${enabled ? 'on' : ''}`}
          onClick={onToggle}
          aria-label={`Toggle ${title}`}
        >
          <span className="toggle-knob" />
        </button>
      </div>

      {enabled && (
        <>
          <div className="subtype-pills">
            {allowedSubTypes.map((st) => (
              <span key={st} className="subtype-pill-hint">{SUBTYPE_LABELS[st]}</span>
            ))}
          </div>

          {links.map((link, index) => (
            <div key={index} className="link-row">
              <select
                value={link.sub_type}
                onChange={(e) => setLink(index, 'sub_type', e.target.value)}
              >
                {allowedSubTypes.map((st) => (
                  <option key={st} value={st}>{SUBTYPE_LABELS[st]}</option>
                ))}
              </select>
              <div className="link-fields">
                <input
                  type="text"
                  value={link.label}
                  onChange={(e) => setLink(index, 'label', e.target.value)}
                  placeholder="Label / name (e.g. AccountService)"
                />
                <input
                  type="url"
                  value={link.url}
                  onChange={(e) => setLink(index, 'url', e.target.value)}
                  placeholder="Paste Gitspace merge URL here..."
                />
              </div>
              <button
                type="button"
                className="link-remove-btn"
                onClick={() => removeLink(index)}
                disabled={links.length <= 1}
                title="Remove link"
              >
                ×
              </button>
            </div>
          ))}

          <button type="button" className="add-link-btn" onClick={addLink}>
            + Add another {SUBTYPE_LABELS[allowedSubTypes[0]]?.toLowerCase() || 'link'}
          </button>
        </>
      )}
    </div>
  )
}

export { emptyLink }
